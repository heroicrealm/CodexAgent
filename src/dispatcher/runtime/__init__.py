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
    def __init__(self, max_parallel_tasks=1):
        self.max_parallel_tasks = max_parallel_tasks
        self.stopping = False
        self.forced = False
        self.children = []

    def request_stop(self, force=False):
        self.stopping = True
        self.forced |= force

    def submit(self, argv):
        if self.stopping:
            raise RuntimeError('Dispatcher is stopping')
        if sum(child.poll() is None for child in self.children) >= self.max_parallel_tasks:
            raise RuntimeError('Parallel task limit reached')
        child = subprocess.Popen(argv, start_new_session=True)
        self.children.append(child)
        return child

    def drain(self):
        while any(child.poll() is None for child in self.children):
            if self.forced:
                self.cancel()
                return
            time.sleep(0.05)
        if self.forced:
            self.cancel()

    def cancel(self):
        # Kill the whole owned group even when the immediate child already exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for child in self.children:
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
            if sig == signal.SIGTERM:
                time.sleep(0.2)
        for child in self.children:
            child.wait()


def serve(config):
    os.umask(0o077)
    root = prepare(config)
    fd = lock(root)
    if fd is None:
        return
    lifecycle = Lifecycle(config.max_parallel_tasks)
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
