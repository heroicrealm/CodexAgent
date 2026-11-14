"""Single coordinator with a background worker and child-process exclusion."""
from concurrent.futures import ThreadPoolExecutor
import signal
import socket
import time
import sys
import threading
from pathlib import Path
from .error_diagnostics import original, private_directory, private_file
from .cleanup import Cleanup
from .diagnostics import DiagnosticLogger
from .execution import Executor, ProcessRunner, Publisher
from .git import Git
from .github import GitHubAdapter
from .locking import InstanceLock
from .services import GroupService, PollService, Scheduler, StateService
from .storage import Storage


class Runtime:
    def __init__(self, config, *, github=None, git=None, runner=None):
        from .db_guard import check
        check(config, execution=True)
        self.config = config
        self.storage = Storage(config.database)
        self.logger = DiagnosticLogger(self.storage, config.debug_mode, config.retention_days)
        self.github = github or GitHubAdapter(config, self.logger)
        self.git = git or Git(config)
        self.groups = GroupService(self.storage, self.logger)
        self.state = StateService(self.storage, self.groups, config, self.logger)
        self.publisher = Publisher(config, self.storage, self.github, self.git, self.state)
        self.poll = PollService(config, self.storage, self.github, self.state, self.publisher, self.logger)
        self.scheduler = Scheduler(self.storage, self.groups, self.state, self.logger)
        self.runner = runner or ProcessRunner(config.codex_command)
        self.executor = Executor(config, self.storage, self.git, self.state, self.publisher, self.runner)
        self.cleanup = Cleanup(config, self.storage, self.git)
        self.stopping = False
        self._gate = threading.Lock()

    def stop(self, force=False):
        with self._gate:
            self.stopping = True
        if force:
            self.runner.stop()

    def _control(self, server, done):
        while not done.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    command = connection.recv(32)
                    if command in (b'stop', b'force'):
                        self.stop(force=command == b'force')
                        connection.sendall(b'stopping')
                except (OSError, TimeoutError):
                    pass

    def run(self, *, once=False):
        with InstanceLock(self.config) as lock:
            from .db_guard import check
            check(self.config, execution=True)
            self.runner.pass_fds = lock.fds
            self.executor.recover()
            self.cleanup.known()
            address = str(self.config.workspace.resolve() / '.control.sock')
            server = socket.socket(socket.AF_UNIX)
            Path(address).unlink(missing_ok=True)
            server.bind(address)
            server.listen(2)
            server.settimeout(0.1)
            done = threading.Event()
            control = threading.Thread(target=self._control, args=(server, done))
            control.start()
            previous = {}
            try:
                for sig in ((signal.SIGINT, signal.SIGTERM) if threading.current_thread() is threading.main_thread() else ()):
                    previous[sig] = signal.signal(sig, lambda *_: self.stop())
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = None
                    next_poll = 0
                    last_poll_at = next_poll_at = None
                    published = 0
                    while not self.stopping or (future and not future.done()):
                        if time.monotonic() - published >= 1:
                            from .monitoring import publish_metadata
                            try:
                                publish_metadata(self.config, last_poll_at, next_poll_at)
                            except OSError:
                                # Monitoring is advisory and must not stop execution.
                                pass
                            published = time.monotonic()
                        if future and future.done():
                            future.result()
                            future = None
                            self.cleanup.known()
                        if not self.stopping and time.monotonic() >= next_poll:
                            poll_ok = True
                            for project in self.config.projects:
                                if not project.enabled:
                                    continue
                                if self.stopping:
                                    break
                                try:
                                    self.poll.poll_project(project)
                                except Exception as exc:
                                    # Schema v1 requires an Issue FK. Unbound poll failures
                                    # stay local without inventing a registered Issue.
                                    area = private_directory(self.config.workspace / 'diagnostics')
                                    diagnostic = area / 'poll-errors.jsonl'
                                    private_file(diagnostic)
                                    with diagnostic.open('a') as stream:
                                        stream.write(original(exc, 'runtime.poll', {
                                            'project_id': project.id, 'repository': project.repository}) + '\n')
                                    poll_ok = False
                                    print(f'Project {project.id}: poll failed; no new execution scheduled', file=sys.stderr)
                                    self.logger.record('verbose', 'Runtime', 'poll', {'repository': project.repository}, 'failed')
                            self.logger.cleanup()
                            if not future:
                                self.cleanup.known()
                            from datetime import datetime, timezone, timedelta
                            last_poll_at = datetime.now(timezone.utc).isoformat()
                            next_poll_at = (datetime.now(timezone.utc) + timedelta(seconds=self.config.poll_interval)).isoformat()
                            next_poll = time.monotonic() + self.config.poll_interval
                            with self._gate:
                                if poll_ok and not future and not self.stopping:
                                    execution = self.scheduler.next(repositories={p.repository.lower() for p in self.config.projects if p.enabled})
                                    if execution:
                                        future = pool.submit(self.executor.execute, execution.execution_id)
                            if once:
                                self.stop()
                        done.wait(0.05)
                    self.cleanup.known()
            finally:
                done.set()
                server.close()
                control.join(timeout=2)
                Path(address).unlink(missing_ok=True)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
