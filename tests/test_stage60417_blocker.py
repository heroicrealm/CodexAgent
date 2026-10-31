"""Real escaped descendants: cleanup refusal on unresolved process completion.

These tests do not claim a strong containment boundary.
A subprocess subreaper owns and reaps every escaped child created here.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from dispatcher.cleanup import CleanupService
from dispatcher.config import Config, Project
from dispatcher.detachment import StopController, request_in_transaction
from dispatcher.execution_store import ExecutionStore
from dispatcher.runtime import Lifecycle, identity, prepare
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskWorkspaceCleanup, TaskError


def reproduce(root, escape='setsid', reason='emergency_stop', leader_exit=False):
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    project = Project('one', True, 'owner/repo', 'main', 'ready', ('alice',))
    config = Config(root/'unused.yaml', root, root/'logs', 1, 1, 'codex', (project,))
    prepare(config)
    manager = GitWorkspaceManager(config)
    repo, task, worktree = manager.paths(project, 1)
    repo.mkdir()
    def git(cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo/'initial').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    git(repo, 'worktree', 'add', '-b', 'task', str(worktree))
    # A real registered worktree with valid branch/SHA/metadata ownership.
    (worktree/'user-file').write_text('preserve me')
    store = Store(root/'.runtime'/'tasks.sqlite3')
    store.apply(project, [dict(id=101, repository_id=11, number=1, title='Task', body='',
        author='alice', target_branch='task', eligible=True, labels=['ready'],
        state='open', server_observed_at='2026-10-26T10:00:00Z')])
    assert store.confirm(project.repository, 1, 1)
    execution = ExecutionStore(store)
    attempt = execution.reserve(store.queue()[0], 'task', 1,
                                store.guard(project.repository, 1)['observation_token'])
    execution.update(attempt, worktree=str(worktree), initial_sha=git(worktree,'rev-parse','HEAD'))
    marker = root/'escaped-pid'
    change_group = 'os.setpgid(0,0); ' if escape == 'setpgid' else ''
    escaped = ('import os,pathlib,time; ' + change_group +
               f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
    leader_code = ('import subprocess,sys,time; '
                   f'subprocess.Popen([sys.executable,"-c",{escaped!r}], start_new_session={escape == "setsid"}); '
                   + ('time.sleep(.1)' if leader_exit else 'time.sleep(60)'))
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    pid = None
    try:
        # Model a real attempt with an escaped child and unresolved completion.
        neighbor = lifecycle.submit([sys.executable,'-c','import time;time.sleep(60)'], service=True, cwd=root)
        leader = lifecycle.submit([sys.executable, '-c', leader_code], cwd=worktree)
        execution.update(attempt, codex_started=1, pid=leader.pid, process_identity=identity(leader.pid))
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pid = int(marker.read_text())
        if leader_exit:
            while leader in lifecycle._active() and time.monotonic() < deadline:
                time.sleep(.01)
        else:
            lifecycle.cancel_child(leader)
        assert leader.returncode is not None
        assert leader not in lifecycle._active()
        assert neighbor in lifecycle._active()
        assert os.getpgid(pid) != leader.pid
        assert Path(f'/proc/{pid}/cwd').resolve() == worktree
        assert int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1]) == os.getpid()
        with store.db:
            request_in_transaction(store,store.queue()[0],reason,{'test':'legacy escaped child'})
        controller = StopController(config,store,None)
        assert not controller.finalize(store.queue()[0],recovered=True)
        saved = store.db.execute('SELECT * FROM stop_requests').fetchone()
        assert saved['process_status'] == 'unconfirmed' and saved['finalized_at'] is None
        assert saved['error'] == 'process_completion_unconfirmed'
        # Also cover a historical false-finalized task/job from the previous
        # release. Valid ownership and an empty original group must not delete it.
        with store.db:
            store.db.execute("UPDATE attempts SET state='interrupted'")
            store.db.execute("UPDATE tasks SET state='detached'")
        CleanupService(config,store).tick()
        job = store.db.execute('SELECT * FROM cleanup_jobs').fetchone()
        assert job['state'] == 'pending' and job['last_error'] == 'cleanup_process_unconfirmed'
        assert (worktree/'user-file').read_text() == 'preserve me'
        assert neighbor in lifecycle._active()
        store.close()
        store = Store(root/'.runtime'/'tasks.sqlite3')
        with store.db:
            store.db.execute('UPDATE cleanup_jobs SET next_retry_at=NULL')
        CleanupService(config,store).tick()
        assert (worktree/'user-file').read_text() == 'preserve me'
        assert store.db.execute('SELECT last_error FROM cleanup_jobs').fetchone()[0] == 'cleanup_process_unconfirmed'
        assert Path(f'/proc/{pid}/cwd').resolve() == worktree
        print('original group stopped; escaped descendant still uses worktree; cleanup refused', flush=True)
    finally:
        lifecycle.cancel()
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        store.close()


@pytest.mark.parametrize('escape', ['setsid','setpgid'])
@pytest.mark.parametrize('reason', ['ready_removed','emergency_stop'])
@pytest.mark.parametrize('leader_exit', [False,True])
def test_escaped_descendant_preserved_across_cleanup_and_restart(tmp_path, escape, reason, leader_exit):
    result = subprocess.run([sys.executable, __file__, str(tmp_path/'probe'), escape, reason, str(int(leader_exit))],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'escaped descendant still uses worktree; cleanup refused' in result.stdout


if __name__ == '__main__':
    reproduce(Path(sys.argv[1]), sys.argv[2] if len(sys.argv)>2 else 'setsid',
              sys.argv[3] if len(sys.argv)>3 else 'emergency_stop',
              bool(int(sys.argv[4])) if len(sys.argv)>4 else False)
