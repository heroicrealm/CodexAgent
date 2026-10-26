"""Reproduce the existing process-containment gap; not Detach acceptance."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dispatcher.runtime import Lifecycle


def reproduce(worktree):
    # A separate harness becomes a subreaper so the escaped grandchild is both
    # terminated and reaped by this test, independently of the host's PID 1.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    worktree.mkdir()
    marker = worktree.parent / 'escaped-pid'
    escaped = ('import os,pathlib,time; '
               f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); '
               'time.sleep(60)')
    leader_code = ('import subprocess,sys,time; '
                   f'subprocess.Popen([sys.executable,"-c",{escaped!r}], start_new_session=True); '
                   'time.sleep(60)')
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    pid = None
    try:
        leader = lifecycle.submit([sys.executable, '-c', leader_code], cwd=worktree)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pid = int(marker.read_text())
        lifecycle.cancel_child(leader)
        assert leader.returncode is not None
        assert lifecycle._active() == []
        assert os.getpgid(pid) != leader.pid
        assert Path(f'/proc/{pid}/cwd').resolve() == worktree
        print('original group stopped; escaped descendant still uses worktree', flush=True)
    finally:
        lifecycle.cancel()
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        if pid is not None:
            # Now our unreaped adopted child: no PID reuse between proof/kill.
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_original_group_completion_does_not_prove_worktree_unused(tmp_path):
    result = subprocess.run([sys.executable, __file__, str(tmp_path / 'worktree')],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'escaped descendant still uses worktree' in result.stdout


if __name__ == '__main__':
    reproduce(Path(sys.argv[1]))
