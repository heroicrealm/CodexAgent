"""Process exclusion shared by runtime and maintenance; inherited by Codex."""
import fcntl


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, config):
        self.paths = sorted({config.database.resolve().with_suffix('.lock'),
                             config.workspace.resolve() / '.dispatcher-v2.lock'}, key=str)
        self.handles = []

    @property
    def fds(self):
        return tuple(handle.fileno() for handle in self.handles)

    def __enter__(self):
        try:
            for path in self.paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open('a+')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
