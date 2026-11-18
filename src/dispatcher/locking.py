"""Process exclusion shared by runtime and maintenance; inherited by Codex."""
import fcntl
import os
import sys
from pathlib import Path


def installation_lock_path():
    """Installed generations share exclusion even with a custom --config.

    This fd joins existing inherited runtime locks, protecting active children.
    Source checkouts and installations not managed by Standalone have no extra lock.
    """
    prefix = Path(sys.prefix).resolve()
    if prefix.name.startswith(".venv-standalone-") and (prefix.parent / ".standalone-install.json").is_file():
        return prefix.parent / ".standalone-install.lock"
    return None


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, config):
        self.paths = sorted({config.database.resolve().with_suffix('.lock'),
                             config.workspace.resolve() / '.dispatcher-v2.lock'}, key=str)
        self.installation = installation_lock_path()
        installation = self.installation
        if installation is not None:
            self.paths = sorted({*self.paths, installation}, key=str)
        self.handles = []

    @property
    def fds(self):
        return tuple(handle.fileno() for handle in self.handles)

    def __enter__(self):
        try:
            for path in self.paths:
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise AlreadyRunning('Symlink lock path forbidden')
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a+')
                try:
                    fcntl.flock(handle, (fcntl.LOCK_SH if path == self.installation else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise AlreadyRunning('Dispatcher or maintenance is active') from exc
                self.handles.append(handle)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        # close only: explicit LOCK_UN would also release the child-inherited lock.
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()
