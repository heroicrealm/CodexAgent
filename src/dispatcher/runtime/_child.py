"""Launch gate: no command runs before its group identity is durably registered."""
import ctypes
import os
import signal
import sys


def main():
    descriptor, parent = int(sys.argv[1]), int(sys.argv[2])
    # Linux PR_SET_PDEATHSIG. Before the registry commit, only this inert wrapper
    # may exist. A dead parent or a closed gate must never execute the command.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        return 125
    if os.getppid() != parent:
        return 125
    gate = os.read(descriptor, 1)
    os.close(descriptor)
    if gate != b'1':
        return 125
    # The registry is now durable. Leave the leader alive for identity-checked
    # recovery if the dispatcher crashes after exec or while descendants run.
    if libc.prctl(1, 0, 0, 0, 0) != 0:
        return 125
    os.execvp(sys.argv[3], sys.argv[3:])


if __name__ == '__main__':
    sys.exit(main())
