"""Linux lifecycle and process ownership primitives."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

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


def prepare(config):
    for path in (config.workspace, config.logs, config.workspace / '.runtime'):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    for project in config.projects:
        (config.workspace / project.id / 'tasks').mkdir(parents=True, exist_ok=True, mode=0o700)
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
    def __init__(self, max_parallel_tasks=1, force_grace_timeout_seconds=5):
        self.max_parallel_tasks = max_parallel_tasks
        self.force_grace_timeout_seconds = force_grace_timeout_seconds
        self.stopping = False
        self.forced = False
        self.children = []

    def request_stop(self, force=False):
        self.stopping = True
        self.forced |= force

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
        return active

    def submit(self, argv):
        if self.stopping:
            raise RuntimeError('Dispatcher is stopping')
        if len(self._active()) >= self.max_parallel_tasks:
            raise RuntimeError('Parallel task limit reached')
        child = subprocess.Popen(argv, start_new_session=True)
        self.children.append(child)
        return child

    def drain(self):
        while self._active():
            if self.forced:
                self.cancel()
                return
            time.sleep(0.05)

    def cancel(self):
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
    os.umask(0o077)
    root = prepare(config)
    fd = lock(root)
    if fd is None:
        return
    lifecycle = Lifecycle(config.max_parallel_tasks, config.force_grace_timeout_seconds)
    signal.signal(signal.SIGTERM, lambda *_: lifecycle.request_stop())
    signal.signal(signal.SIGINT, lambda *_: lifecycle.request_stop())
    signal.signal(signal.SIGUSR1, lambda *_: lifecycle.request_stop(True))
    state = root / 'state.json'
    temporary = root / 'state.tmp'
    try:
        state_fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(state_fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(dict(pid=os.getpid(), identity=identity(os.getpid()))))
        temporary.replace(state)
        event(config.logs / 'dispatcher.jsonl', 'started')
        while not lifecycle.stopping:
            time.sleep(0.05)
        lifecycle.drain()
        event(config.logs / 'dispatcher.jsonl', 'stopped_force' if lifecycle.forced else 'stopped')
    finally:
        lifecycle.cancel()
        state.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        os.close(fd)
