"""Durable cleanup acceptance with local Git, controllable time, independent DBs."""
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from test_stage04 import env, git, issue, adapter, register, finish
from dispatcher.cleanup import CleanupService, CleanupWorker
from dispatcher.execution_store import ExecutionStore
from dispatcher.runtime import Lifecycle, identity
from dispatcher.scheduler import Dispatcher
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError, register_artifacts


@pytest.fixture
def terminal(env):
    config, project, *_ = env
    dispatcher = Dispatcher(config, Lifecycle(), adapter([issue(project)]))
    register(dispatcher, project, [issue(project)])
    dispatcher.admit()
    finish(dispatcher)
    with dispatcher.store.db as db:
        db.execute("UPDATE tasks SET state='done'")
    try:
        yield dispatcher, project, CleanupService(config, dispatcher.store)
    finally:
        dispatcher.close()


def job(store):
    return dict(store.db.execute('SELECT * FROM cleanup_jobs ORDER BY cleanup_id DESC LIMIT 1').fetchone())


@pytest.mark.parametrize('state', ['done', 'cancelled'])
def test_terminal_preserves_git_refs_database_and_pr(terminal, state):
    d, p, service = terminal
    with d.store.db as db:
        db.execute('UPDATE tasks SET state=?', (state,))
    repo, task, worktree = d.git.paths(p, 1)
    refs = git(repo, 'show-ref')
    history = {name: [tuple(r) for r in d.store.db.execute('SELECT * FROM ' + name)]
               for name in ('attempts', 'task_pull_requests', 'history', 'iteration_pr_history')}
    service.tick()
    service.tick()
    assert not task.exists() and repo.exists()
    assert git(repo, 'show-ref') == refs
    assert git(repo, 'worktree', 'list', '--porcelain').count('worktree ') == 1
    assert job(d.store)['state'] == 'completed' and job(d.store)['attempts'] == 1
    for name, rows in history.items():
        assert rows == [tuple(r) for r in d.store.db.execute('SELECT * FROM ' + name)]
    events = [json.loads(line) for line in (d.config.logs / 'dispatcher.jsonl').read_text().splitlines()]
    assert any(e.get('cleanup_id') == job(d.store)['cleanup_id'] and e.get('result') == 'completed' for e in events)


def test_enqueue_is_atomic_and_idempotent(terminal):
    d, p, service = terminal
    db = d.store.db
    with db:
        db.execute('DELETE FROM cleanup_jobs')
        db.execute("UPDATE tasks SET state='review'")
    with pytest.raises(RuntimeError):
        with db:
            db.execute("UPDATE tasks SET state='done'")
            assert db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 1
            raise RuntimeError('rollback decision and cleanup')
    assert db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 0
    with db:
        db.execute("UPDATE tasks SET state='done'")
        db.execute("UPDATE tasks SET synced_at='later'")
    service.reconcile()
    assert db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['review', 'running', 'pending', 'failed', 'interrupted', 'revoked', 'rejected'])
def test_nonterminal_never_deleted(terminal, state):
    d, p, service = terminal
    with d.store.db as db:
        db.execute('DELETE FROM cleanup_jobs')
        db.execute('UPDATE tasks SET state=?', (state,))
    service.tick()
    assert d.git.paths(p, 1)[2].exists()
    assert d.store.db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 0


@pytest.mark.parametrize('mutation,code', [
    ('dirty','dirty_worktree'), ('unknown','cleanup_unexpected_file'), ('symlink','unsafe_path'),
    ('registry','cleanup_invalid_registry'), ('registry_object','cleanup_invalid_registry'),
    ('manifest','cleanup_invalid_manifest'), ('foreign_git','cleanup_git_ownership_mismatch'),
    ('branch','cleanup_ownership_unproven'), ('sha','cleanup_ownership_unproven'),
    ('active','cleanup_active_execution'), ('running_attempt','cleanup_active_execution'),
    ('unknown_archive','cleanup_unexpected_file'),
    ('gitdir_symlink','unsafe_path'), ('metadata_pointer','cleanup_ownership_unproven')])
def test_unsafe_preserved_and_diagnostic(terminal, mutation, code):
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    if mutation == 'dirty': (worktree / 'human').write_text('keep')
    if mutation == 'unknown': (task / 'human').write_text('keep')
    if mutation == 'symlink': (task / 'escape').symlink_to(d.config.source)
    if mutation == 'registry': (task / '.dispatcher-artifacts.jsonl').write_text('"../outside"\n')
    if mutation == 'registry_object': (task / '.dispatcher-artifacts.jsonl').write_text('{}\n')
    if mutation == 'manifest':
        (task / 'attachments').mkdir(exist_ok=True)
        (task / 'attachments' / 'manifest.json').write_text('{')
    if mutation == 'foreign_git':
        # Same commit and branch is insufficient; common-dir must be this clone.
        other = task.parent / 'foreign'
        git(task, 'clone', str(repo), str(other))
        git(repo, 'worktree', 'remove', str(worktree))
        git(other, 'worktree', 'add', str(worktree), 'codex/issue-1')
    if mutation == 'gitdir_symlink':
        dotgit = worktree / '.git'
        backup = task / 'git-pointer-backup'
        dotgit.rename(backup)
        dotgit.symlink_to(backup)
    if mutation == 'metadata_pointer':
        metadata = Path((worktree / '.git').read_text().strip().removeprefix('gitdir: '))
        (metadata / 'gitdir').write_text(str(task / 'foreign' / '.git'))
    if mutation == 'branch': git(worktree, 'switch', '-c', 'human')
    if mutation == 'sha':
        (worktree / 'human').write_text('keep')
        git(worktree, 'add', '.')
        git(worktree, 'commit', '-m', 'human')
    if mutation in ('active', 'running_attempt'):
        with d.store.db as db:
            if mutation == 'active':
                db.execute('UPDATE attempts SET pid=?,process_identity=?', (os.getpid(), identity(os.getpid())))
            else:
                db.execute("UPDATE attempts SET state='running'")
    if mutation == 'unknown_archive': (task / ('previous-' + 'f'*32)).mkdir()
    service.tick()
    assert worktree.exists() and task.exists()
    assert job(d.store)['last_error'] == code
    assert d.store.get(p.repository, 1)['state'] == 'done'


def test_retries_restart_exhaustion_manual(terminal):
    d, p, service = terminal
    stamp = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    service.clock = lambda: stamp[0]
    _, task, worktree = d.git.paths(p, 1)
    (worktree / 'human').write_text('keep')
    for index, delay in enumerate((60, 300, 900, None), 1):
        service.tick()
        value = job(d.store)
        assert value['attempts'] == index
        assert value['state'] == ('pending' if delay else 'failed')
        service.tick()
        assert job(d.store)['attempts'] == index
        if delay:
            assert datetime.fromisoformat(value['next_retry_at']) == stamp[0] + timedelta(seconds=delay)
            stamp[0] += timedelta(seconds=delay-1)
            service.tick()
            assert job(d.store)['attempts'] == index
            stamp[0] += timedelta(seconds=1)
            restored = Store(d.config.workspace / '.runtime' / 'tasks.sqlite3')
            try:
                assert job(restored)['attempts'] == index
            finally:
                restored.close()
    stamp[0] += timedelta(days=1)
    service.tick()
    assert job(d.store)['attempts'] == 4
    (worktree / 'human').unlink()
    assert service.retry(p, 1)
    assert not service.retry(p, 1)
    service.tick()
    assert job(d.store)['state'] == 'completed' and not task.exists()


@pytest.mark.parametrize('crash', ['before_remove', 'after_remove', 'after_unlink', 'before_commit'])
def test_crash_restart_same_attempt(terminal, monkeypatch, crash):
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    class Crash(BaseException): pass
    if crash in ('before_remove', 'after_remove'):
        original = service.manager.git
        def call(cwd, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'):
                if crash == 'before_remove': raise Crash()
                result = original(cwd, *args, **kwargs)
                raise Crash()
            return original(cwd, *args, **kwargs)
        monkeypatch.setattr(service.manager, 'git', call)
    elif crash == 'after_unlink':
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name.endswith('.prompt'): raise Crash()
            return result
        monkeypatch.setattr(Path, 'unlink', unlink)
    else:
        original = service.cleanup
        def clean(*args):
            original(*args)
            raise Crash()
        monkeypatch.setattr(service, 'cleanup', clean)
    with pytest.raises(Crash): service.tick()
    assert job(d.store)['state'] == 'running'
    monkeypatch.undo()
    restored = Store(d.config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        CleanupService(d.config, restored).tick()
        assert job(restored)['state'] == 'completed' and job(restored)['attempts'] == 1
        assert not task.exists()
    finally:
        restored.close()


def test_concurrent_workers_and_reactivation_guard(terminal):
    d, p, service = terminal
    entered, release = Event(), Event()
    path = d.config.workspace / '.runtime' / 'tasks.sqlite3'
    def worker():
        store = Store(path)
        try:
            service = CleanupService(d.config, store)
            original = service.cleanup
            def paused(*args):
                entered.set()
                assert release.wait(10)
                original(*args)
            service.cleanup = paused
            service.tick()
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        try:
            service.tick()
            assert job(d.store)['attempts'] == 1
            with pytest.raises(sqlite3.IntegrityError, match='Cleanup owns'):
                with d.store.db as db: db.execute("UPDATE tasks SET state='pending'")
            with pytest.raises(sqlite3.IntegrityError, match='Cleanup owns'):
                with d.store.db as db: db.execute("UPDATE attempts SET state='running'")
        finally:
            release.set()
        future.result(timeout=10)
    assert job(d.store)['state'] == 'completed'


def test_stale_generation_preserved(terminal):
    d, p, service = terminal
    with d.store.db as db:
        db.execute("UPDATE tasks SET state='pending',version=version+1")
    service.tick()
    assert job(d.store)['last_error'] == 'cleanup_generation_changed'
    assert d.git.paths(p, 1)[2].exists()


def test_legacy_reconciliation_and_unknown_ownership(terminal):
    d, p, service = terminal
    with d.store.db as db:
        db.execute('DELETE FROM cleanup_jobs')
    service.tick()
    assert job(d.store)['state'] == 'completed'
    # A task directory alone, without attempts, must never authorize a worktree.
    item = issue(p, id=102, number=2)
    d.store.apply(p, [issue(p), item])
    worktree, _ = d.git.prepare(p, item, 'legacy')
    with d.store.db as db:
        db.execute("UPDATE tasks SET state='done' WHERE number=2")
        db.execute('DELETE FROM cleanup_jobs WHERE number=2')
    service.tick()
    assert worktree.exists() and job(d.store)['last_error'] == 'cleanup_ownership_unproven'


def test_many_archives_partial_and_legacy(terminal):
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    # Three older attempts, including detached and pre-registry archives.
    sha = git(worktree, 'rev-parse', 'HEAD')
    for index in range(3):
        attempt = str(index)*32
        branch = 'archive-test-' + str(index)
        archived = task / ('previous-' + attempt)
        git(repo, 'worktree', 'add', '-b', branch, str(archived), sha)
        if index == 1: git(archived, 'checkout', '--detach')
        with d.store.db as db:
            # Append history without making it the current attempt.
            db.execute('''INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,worktree,result_sha)
                VALUES(?,?,1,1,?,'review',?,?,?)''', (attempt, p.repository.casefold(), branch, '2020', str(archived), sha))
        attachments = task / ('attachments-' + attempt)
        attachments.mkdir()
        (attachments / '001-file.txt').write_text('owned')
        (attachments / 'manifest.json').write_text(json.dumps([dict(local_path=str(task / 'attachments' / '001-file.txt'))]))
        (task / (attempt + '.prompt')).write_text('legacy prompt')
    # Reorder current attempt so task identity remains the newest attempt.
    with d.store.db as db:
        current = db.execute('SELECT attempt_id FROM cleanup_jobs').fetchone()[0]
        newest = db.execute('SELECT MAX(rowid)+1 FROM attempts').fetchone()[0]
        db.execute('UPDATE attempts SET rowid=? WHERE attempt_id=?', (newest, current))
    git(repo, 'worktree', 'remove', str(task / ('previous-' + '0'*32)))
    service.tick()
    assert job(d.store)['state'] == 'completed' and not task.exists()
    assert 'refs/heads/archive-test-1' in git(repo, 'show-ref')


def test_worker_without_poll(terminal):
    d, p, service = terminal
    worker = CleanupWorker(d.config)
    worker.start()
    try:
        import time
        deadline = time.monotonic() + 5
        while job(d.store)['state'] != 'completed' and time.monotonic() < deadline:
            time.sleep(.01)
        assert job(d.store)['state'] == 'completed'
    finally:
        worker.close()


def test_migration_reopen_and_project_isolation(terminal):
    d, p, service = terminal
    second = replace(p, id='two', repository='other/repo')
    config = replace(d.config, projects=(p, second))
    # A second clone and Issue #1 with a distinct GitHub identity.
    target = config.workspace / second.id / 'repository'
    target.parent.mkdir()
    git(target.parent, 'clone', str(d.git.paths(p, 1)[0]), str(target))
    item = issue(second, id=202)
    d.store.apply(second, [item])
    d.store.confirm(second.repository, 1, 1)
    row = d.store.get(second.repository, 1)
    execution = ExecutionStore(d.store)
    attempt = execution.reserve(row, 'other-task', 2, d.store.guard(second.repository, 1)['observation_token'])
    manager = GitWorkspaceManager(config)
    worktree, sha = manager.prepare(second, item, 'other-task')
    execution.update(attempt, worktree=str(worktree), initial_sha=sha, result_sha=sha)
    with d.store.db as db:
        db.execute("UPDATE attempts SET state='review' WHERE attempt_id=?", (attempt,))
        db.execute("UPDATE tasks SET state='cancelled' WHERE project_id='two'")
    # Block project one; the other job must still complete.
    (d.git.paths(p, 1)[2] / 'human').write_text('keep')
    service = CleanupService(config, d.store)
    service.tick()
    assert job(d.store)['state'] == 'completed' and not worktree.parent.exists()
    assert d.git.paths(p, 1)[2].exists()
    restored = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        assert restored.db.execute('SELECT 1 FROM schema_migrations WHERE version=607').fetchone()
        assert restored.db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert restored.db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 2
    finally:
        restored.close()


def test_process_death_releases_lock_and_recovers(terminal):
    d, p, service = terminal
    marker = d.config.workspace / 'worker-entered'
    script = '''
import sys, time
from pathlib import Path
from dispatcher.config import load
from dispatcher.storage import Store
from dispatcher.cleanup import CleanupService
config = load(sys.argv[1])
store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
service = CleanupService(config, store)
def pause(*args):
    Path(sys.argv[2]).write_text('claimed')
    time.sleep(30)
service.cleanup = pause
service.tick()
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(d.config.source), str(marker)])
    try:
        import time
        deadline = time.monotonic() + 5
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists() and job(d.store)['state'] == 'running'
        service.tick()
        assert job(d.store)['state'] == 'running'
    finally:
        child.kill()
        child.wait(timeout=5)
    service.tick()
    assert job(d.store)['state'] == 'completed' and job(d.store)['attempts'] == 1


def test_missing_worktree_with_metadata_recovers(terminal):
    import shutil
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    # Simulate crash/filesystem removal before Git unregistered the worktree.
    shutil.rmtree(worktree)
    service.tick()
    assert job(d.store)['state'] == 'completed'
    assert git(repo, 'worktree', 'list', '--porcelain').count('worktree ') == 1


@pytest.mark.parametrize('point', ['before_move', 'after_move', 'after_attachments'])
def test_crash_during_archival_write_ahead_ownership(terminal, monkeypatch, point):
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    attempt = d.store.db.execute('SELECT attempt_id FROM attempts').fetchone()[0]
    attachments = task / 'attachments'
    attachments.mkdir(exist_ok=True)
    (attachments / 'manifest.json').write_text('[]')
    class Crash(BaseException): pass
    original = d.git.git
    def call(cwd, *args, **kwargs):
        if args[:2] == ('worktree', 'move'):
            if point == 'before_move': raise Crash()
            result = original(cwd, *args, **kwargs)
            if point == 'after_move': raise Crash()
            return result
        if point == 'after_attachments' and args[:2] == ('fetch', '--prune'):
            raise Crash()
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(d.git, 'git', call)
    item = issue(p, repeat_branch=True, previous_attempt=attempt)
    with pytest.raises(Crash): d.git.prepare(p, item, 'codex/issue-1')
    monkeypatch.undo()
    service.tick()
    assert job(d.store)['state'] == 'completed' and not task.exists()


def test_upgrade606_retains_data_and_backfills_on_reconcile(terminal):
    d, p, service = terminal
    database = d.config.workspace / '.runtime' / 'tasks.sqlite3'
    with d.store.db as db:
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'cleanup_%'").fetchall():
            db.execute('DROP TRIGGER ' + name)
        db.execute('DROP TABLE cleanup_jobs')
        db.execute('DELETE FROM schema_migrations WHERE version=607')
    history = [tuple(r) for r in d.store.db.execute('SELECT * FROM attempts')]
    restored = Store(database)
    try:
        assert history == [tuple(r) for r in restored.db.execute('SELECT * FROM attempts')]
        service = CleanupService(d.config, restored)
        service.tick()
        assert job(restored)['state'] == 'completed'
    finally:
        restored.close()


def test_cli_diagnostics_and_retry(terminal, capsys):
    from dispatcher.cli import main
    d, p, service = terminal
    with d.store.db as db:
        db.execute("UPDATE cleanup_jobs SET state='failed',attempts=4,last_error='dirty_worktree'")
    prefix = ['--config', str(d.config.source)]
    assert main(prefix + ['show', p.id, '1', '--format', 'json']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['state'] == 'done' and value['cleanup_jobs'][0]['last_error'] == 'dirty_worktree'
    assert main(prefix + ['show', p.id, '1']) == 0
    assert 'Cleanup jobs:' in capsys.readouterr().out
    assert main(prefix + ['cleanup-retry', p.id, '1']) == 0
    assert capsys.readouterr().out.strip() == 'queued'
    assert job(d.store)['state'] == 'pending' and job(d.store)['attempts'] == 0
    service.tick()
    assert job(d.store)['state'] == 'completed'


def test_slow_project_does_not_block_other_background_cleanup(terminal, monkeypatch):
    d, p, service = terminal
    other = replace(p, id='two', repository='other/repo')
    config = replace(d.config, projects=(p, other))
    d.store.apply(other, [issue(other, id=202)])
    with d.store.db as db:
        db.execute("UPDATE tasks SET state='done' WHERE project_id='two'")
    entered, release = Event(), Event()
    original = CleanupService.cleanup
    def paused(self, project, row):
        if project.id == p.id:
            entered.set()
            assert release.wait(10)
        return original(self, project, row)
    monkeypatch.setattr(CleanupService, 'cleanup', paused)
    worker = CleanupWorker(config)
    worker.start()
    try:
        assert entered.wait(5)
        import time
        deadline = time.monotonic() + 5
        while job(d.store)['state'] != 'completed' and time.monotonic() < deadline:
            time.sleep(.01)
        assert job(d.store)['project_id'] == other.id and job(d.store)['state'] == 'completed'
        assert d.git.paths(p, 1)[2].exists()
    finally:
        release.set()
        worker.close()


def test_unknown_missing_archive_metadata_is_preserved(terminal):
    import shutil
    d, p, service = terminal
    repo, task, worktree = d.git.paths(p, 1)
    git(repo, 'worktree', 'add', '-b', 'human', str(task / 'previous-human'), 'HEAD')
    shutil.rmtree(task)
    registered = git(repo, 'worktree', 'list', '--porcelain')
    service.tick()
    assert job(d.store)['last_error'] == 'cleanup_ownership_unproven'
    assert git(repo, 'worktree', 'list', '--porcelain') == registered



def test_changed_attempt_identity_preserves_task(terminal):
    d, p, service = terminal
    with d.store.db as db:
        db.execute("UPDATE cleanup_jobs SET attempt_id=?", ('f'*32,))
    service.tick()
    assert job(d.store)['last_error'] == 'cleanup_generation_changed'
    assert d.git.paths(p, 1)[2].exists()
