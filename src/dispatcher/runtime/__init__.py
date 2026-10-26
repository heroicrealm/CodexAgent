"""Linux lifecycle and process ownership primitives."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from threading import RLock
from functools import wraps

from dispatcher.logging import event


def identity(pid):
    try:
        # comm can contain spaces and parentheses; fields after its last ')' are stable.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def group_members(pgid):
    result = []
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = stat.read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                result.append(int(stat.parent.name))
        except FileNotFoundError:
            pass
    return result


def serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self.mutex:
            return method(self, *args, **kwargs)
    return invoke


def prepare(config):
    for path in (config.workspace, config.logs, config.workspace / '.runtime'):
        if path.is_symlink():
            raise RuntimeError('Unsafe runtime directory')
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    for project in config.projects:
        from dispatcher.workspace import private_dir
        private_dir(config.workspace, config.workspace / project.id / 'tasks')
    return config.workspace / '.runtime'


def lock(root, blocking=False):
    fd = os.open(root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def running(root):
    try:
        state = json.loads((root / 'state.json').read_text())
        pid = state['pid']
        if type(pid) is not int or pid <= 1 or identity(pid) != state['identity']:
            return None
        fd = lock(root)
        if fd is not None:
            os.close(fd)
            return None
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


class Lifecycle:
    """Future scheduler contract: stop admission, drain, or cancel process groups."""
    def __init__(self, max_parallel_tasks=1, force_grace_timeout_seconds=5, registry=None):
        self.mutex = RLock()
        self.orphans = []
        self.max_parallel_tasks = max_parallel_tasks
        self.force_grace_timeout_seconds = force_grace_timeout_seconds
        self.stopping = False
        self.forced = False
        self.children = []
        self.services = []
        self.registry = registry

    @serialized
    def save_registry(self):
        values = list(self.orphans)
        for child in self.children:
            if child.returncode is None:
                try:
                    start = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                    values.append(dict(pid=child.pid, identity=start))
                except FileNotFoundError:
                    pass
        temporary = self.registry.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.registry)
        descriptor = os.open(self.registry.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def recover(self, preserve=None):
        """Only signal old groups with a verified leader start identity.

        Unknown surviving groups block daemon startup instead of risking PID reuse.
        This is recovery of non-child processes, not Popen polling/reaping.
        """
        if not self.registry or not self.registry.exists():
            return
        values = json.loads(self.registry.read_text())
        for value in values:
            pid = value['pid']
            if type(pid) is not int or pid <= 1:
                raise RuntimeError('Invalid process recovery registry')
            def members():
                result = []
                for stat in Path('/proc').glob('[0-9]*/stat'):
                    try:
                        fields = stat.read_text().rsplit(')', 1)[1].split()
                        if int(fields[2]) == pid and fields[0] != 'Z':
                            result.append(stat)
                    except FileNotFoundError:
                        pass
                return result
            if not members():
                continue
            if preserve and preserve(pid):
                self.orphans.append(value)
                continue
            try:
                start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            except FileNotFoundError:
                raise RuntimeError('Unverifiable surviving task group; inspect process registry') from None
            if start != value['identity']:
                raise RuntimeError('Recovery process identity mismatch; inspect process registry')
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + self.force_grace_timeout_seconds
            while members() and time.monotonic() < deadline:
                time.sleep(.02)
            if members():
                os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while members():
                if time.monotonic() >= deadline:
                    raise RuntimeError('Recovered process group did not stop')
                time.sleep(.02)
        if self.orphans:
            self.save_registry()
        else:
            self.registry.unlink()

    def request_stop(self, force=False):
        self.stopping = True
        self.forced |= force

    @serialized
    def _active(self):
        active = []
        for child in self.children:
            # Never poll/wait the leader until its entire group is gone. Even a
            # zombie leader reserves its PID, preventing PGID reuse during killpg.
            # Callers must likewise not reap a submitted child themselves.
            if child.returncode is not None:
                raise RuntimeError('Task leader was reaped outside Lifecycle')
            try:
                os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError as exc:
                raise RuntimeError('Task leader was reaped outside Lifecycle') from exc
            live = False
            for stat in Path('/proc').glob('[0-9]*/stat'):
                try:
                    fields = stat.read_text().rsplit(')', 1)[1].split()
                except FileNotFoundError:
                    continue
                if int(fields[2]) == child.pid and fields[0] != 'Z':
                    live = True
                    break
            if live:
                active.append(child)
            else:
                child.wait()
        self.children = active
        self.services = [child for child in self.services if child in active]
        return active

    @serialized
    def submit(self, argv, *, service=False, before_exec=None, during_drain=False, **kwargs):
        if self.stopping and not (during_drain and not self.forced):
            raise RuntimeError('Dispatcher is stopping')
        if len([c for c in self._active() if c not in self.services]) >= self.max_parallel_tasks and not service:
            raise RuntimeError('Parallel task limit reached')
        gate_read = gate_write = None
        if self.registry or before_exec:
            gate_read, gate_write = os.pipe()
            kwargs['pass_fds'] = (*kwargs.get('pass_fds', ()), gate_read)
            argv = [sys.executable, str(Path(__file__).with_name('_child.py')),
                    str(gate_read), str(os.getpid()), *argv]
        try:
            child = subprocess.Popen(argv, start_new_session=True, **kwargs)
            self.children.append(child)
            if self.registry:
                self.save_registry()
            if before_exec:
                before_exec(child)
            if gate_write is not None:
                os.write(gate_write, b'1')
            if service:
                self.services.append(child)
            return child
        except (OSError, RuntimeError):
            if 'child' in locals():
                self.cancel_child(child)
            raise
        finally:
            if gate_read is not None:
                os.close(gate_read)
                os.close(gate_write)

    @serialized
    def cancel_child(self, child):
        if child not in self._active():
            return
        os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + self.force_grace_timeout_seconds
        while child in self._active() and time.monotonic() < deadline:
            time.sleep(0.02)
        if child in self._active():
            os.killpg(child.pid, signal.SIGKILL)
        while child in self._active():
            time.sleep(0.02)

    @serialized
    def prune_orphans(self):
        surviving = [o for o in self.orphans if group_members(o['pid'])]
        if len(surviving) != len(self.orphans):
            self.orphans = surviving
            if self.registry:
                self.save_registry()

    @serialized
    def stop_attempt(self, pid, expected_identity):
        if not group_members(pid):
            return 'not_required', True
        child = next((c for c in self.children if c.pid == pid), None)
        # An unreaped owned leader pins PGID. Recovery requires start identity,
        # including a zombie leader; identity() intentionally excludes zombies.
        try:
            actual = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        except FileNotFoundError:
            return 'not_required', False
        if not expected_identity or actual != expected_identity:
            return 'not_required', False
        method = 'SIGTERM'
        try:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + self.force_grace_timeout_seconds
            while group_members(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            if group_members(pid):
                method = 'SIGKILL'
                os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + self.force_grace_timeout_seconds
            while group_members(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            confirmed = not group_members(pid)
            if confirmed and child is not None:
                self._active()
            return method, confirmed
        except (ProcessLookupError, PermissionError):
            return method, not group_members(pid)

    def drain(self):
        while self._active() or any(group_members(o['pid']) for o in self.orphans):
            if self.forced:
                self.cancel()
                return
            time.sleep(0.05)

    @serialized
    def cancel(self):
        for orphan in list(self.orphans):
            _, confirmed = self.stop_attempt(orphan['pid'], orphan['identity'])
            if not confirmed:
                raise RuntimeError('Unverifiable surviving task group; artifacts preserved')
        self.orphans = []
        for child in self._active():
            os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + self.force_grace_timeout_seconds
        while self._active() and time.monotonic() < deadline:
            time.sleep(0.02)
        for child in self._active():
            os.killpg(child.pid, signal.SIGKILL)
        # Do not release the leader PID until all live group members have gone.
        while self._active():
            time.sleep(0.02)


def serve(config):
    from dispatcher.initialization import ready
    if not any(ready(p) for p in config.projects):
        raise RuntimeError('No initialized enabled projects; run init')
    os.umask(0o077)
    root = prepare(config)
    from dispatcher.storage.migrations import prepare as migrate
    migrate(root / 'tasks.sqlite3')
    fd = lock(root)
    if fd is None:
        return
    lifecycle = Lifecycle(config.max_parallel_tasks, config.force_grace_timeout_seconds, root / 'children.json')
    signal.signal(signal.SIGTERM, lambda *_: lifecycle.request_stop())
    signal.signal(signal.SIGINT, lambda *_: lifecycle.request_stop())
    signal.signal(signal.SIGUSR1, lambda *_: lifecycle.request_stop(True))
    state = root / 'state.json'
    temporary = root / 'state.tmp'
    try:
        # Detach recovery must not turn a natural drain into a forced stop.
        from dispatcher.storage import Store
        recovery_store = Store(root / 'tasks.sqlite3')
        try:
            def preserve(pid):
                return recovery_store.db.execute("""SELECT 1 FROM attempts a JOIN tasks t
                    ON t.repository=a.repository AND t.number=a.number
                    JOIN stop_requests s ON s.issue_id=t.issue_id
                    WHERE a.pid=? AND s.active=1
                    UNION SELECT 1 FROM attempt_processes p JOIN attempts a USING(attempt_id)
                    JOIN tasks t ON t.repository=a.repository AND t.number=a.number
                    JOIN stop_requests s ON s.issue_id=t.issue_id
                    WHERE p.pid=? AND p.finished_at IS NULL AND s.active=1""", (pid, pid)).fetchone() is not None
            lifecycle.recover(preserve)
        finally:
            recovery_store.close()
        state_fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(state_fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(dict(pid=os.getpid(), identity=identity(os.getpid()))))
        temporary.replace(state)
        event(config.logs / 'dispatcher.jsonl', 'started')
        from dispatcher.scheduler import Dispatcher
        dispatcher = Dispatcher(config, lifecycle)
        try:
            dispatcher.run()
        finally:
            dispatcher.close()
        lifecycle.drain()
        event(config.logs / 'dispatcher.jsonl', 'stopped_force' if lifecycle.forced else 'stopped')
    finally:
        lifecycle.cancel()
        state.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        os.close(fd)
