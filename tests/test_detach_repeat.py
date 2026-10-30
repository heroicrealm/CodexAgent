"""Detach restart uses a fresh base, with real refs and durable cleanup evidence."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_stage04 import env, issue, adapter, register, finish, git
from test_detachment import wire
from dispatcher.cleanup import CleanupService
from dispatcher.runtime import Lifecycle
from dispatcher.scheduler import Dispatcher
from dispatcher.workspace import GitWorkspaceManager, TaskError


@pytest.fixture
def detached(env, request):
    config, project, control, seed, remote = env
    Path(config.executable).write_text(Path(config.executable).read_text().replace('time.sleep(60)', 'time.sleep(.1)'))
    control.write_text(json.dumps({'exec': 'sleep'}))
    custom = getattr(request, 'param', None) == 'custom'
    if custom:
        project = replace(project, base_branch='release')
        git(seed, 'checkout', '-b', 'release')
        git(seed, 'push', str(remote), 'release')
    body = '```codex-config\nbase_branch: release\nbranch_name: tasks/custom-42\n```' if custom else 'Requirements'
    items = [issue(project, body=body)]
    api = adapter(items)
    wire(api, project, items)
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, api)
    register(d, project, items)
    d.admit()
    attempt = next(iter(d.active))
    items[0]['labels'] = []
    d.stops.observe(project, d.store.queue()[0])
    finish(d)
    old = d.execution.get(attempt)
    repo, _, worktree = d.git.paths(project, 1)
    # Both committed and dirty abandoned output must disappear from the new run.
    (worktree / 'abandoned.txt').write_text('abandoned commit')
    git(worktree, 'add', '.')
    git(worktree, 'commit', '-m', 'Abandoned result')
    (worktree / 'dirty.txt').write_text('abandoned')
    old_sha = git(worktree, 'rev-parse', 'HEAD')
    CleanupService(config, d.store).tick()
    assert not worktree.exists()
    assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'completed'
    (seed / 'fresh.txt').write_text('fresh base')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Advance base')
    git(seed, 'push', str(remote), project.base_branch)
    git(repo, 'fetch', 'origin')
    items[0] = issue(project, body=body, server_observed_at='2026-10-24T00:11:00Z')
    d.store.apply(project, items)
    assert d.store.confirm(project.repository, 1, 1)
    try:
        yield d, project, items, old, old_sha, git(seed, 'rev-parse', 'HEAD')
    finally:
        lifecycle.cancel()
        d.close()


def prepare_again(data, project=None):
    d, original, items, old, *_ = data
    project = project or original
    manager = GitWorkspaceManager(d.config, execution=d.execution)
    return manager.prepare(project, dict(items[0], detach_attempt=old['attempt_id']), old['branch'])


def test_exact_git_failure_reproduced(detached):
    d, project, _, old, *_ = detached
    repo, _, worktree = d.git.paths(project, 1)
    result = d.git.git(repo, 'worktree', 'add', '-b', old['branch'], str(worktree), 'origin/main', check=False)
    assert result.returncode != 0
    assert 'already exists' in result.stderr
    with pytest.raises(TaskError, match='git_operation_failed'):
        d.git.git(repo, 'worktree', 'add', '-b', old['branch'], str(worktree), 'origin/main')
    records = [json.loads(line) for line in (d.config.logs / 'dispatcher.jsonl').read_text().splitlines()]
    assert any(r.get('reason') == 'branch_already_exists' and r.get('action') == 'worktree' for r in records)


def test_fresh_base_archive_and_full_lifecycle(detached):
    d, project, items, old, old_sha, base = detached
    d.admit()
    assert len(d.active) == 1
    attempt = next(iter(d.active))
    current = d.execution.get(attempt)
    assert current['initial_sha'] == base
    worktree = Path(current['worktree'])
    assert (worktree / 'fresh.txt').exists()
    assert not (worktree / 'dirty.txt').exists()
    repo, _, _ = d.git.paths(project, 1)
    assert git(repo, 'rev-parse', 'archive/detach/' + old['attempt_id']) == old_sha
    finish(d)
    assert d.store.queue()[0]['state'] == 'review'
    assert d.execution.get(attempt)['pushed']


@pytest.mark.parametrize('failure', ['foreign', 'archive', 'remote', 'cleanup', 'identity', 'project'])
def test_conflicts_preserve_refs(detached, failure):
    d, project, _, old, old_sha, base = detached
    repo, _, _ = d.git.paths(project, 1)
    archive = 'archive/detach/' + old['attempt_id']
    if failure == 'foreign':
        git(repo, 'update-ref', 'refs/heads/' + old['branch'], base)
    elif failure == 'archive':
        git(repo, 'branch', archive, base)
    elif failure == 'remote':
        git(repo, 'push', 'origin', old['branch'])
    elif failure == 'cleanup':
        with d.store.db:
            d.store.db.execute("UPDATE cleanup_jobs SET state='pending'")
    elif failure == 'identity':
        with d.store.db:
            d.store.db.execute("UPDATE stop_requests SET process_status='unconfirmed'")
    else:
        project = replace(project, id='another-project')
        # Use the same repo explicitly to exercise identity rather than clone.
        other, _, _ = d.git.paths(project, 1)
        other.parent.mkdir(parents=True, exist_ok=True)
        git(repo.parent, 'clone', '--no-checkout', str(repo), str(other))
    before = git(repo, 'show-ref', '--heads')
    with pytest.raises(TaskError, match='detach_'):
        prepare_again(detached, project)
    assert git(repo, 'show-ref', '--heads') == before


def test_crash_after_rename_and_restart(detached, monkeypatch):
    d, project, _, old, old_sha, base = detached
    manager = GitWorkspaceManager(d.config, execution=d.execution)
    original = manager.git
    def crash(cwd, *args, **kwargs):
        if args[:2] == ('worktree', 'add'):
            raise TaskError('injected_crash', 'GIT')
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(manager, 'git', crash)
    with pytest.raises(TaskError, match='injected_crash'):
        manager.prepare(project, dict(detached[2][0], detach_attempt=old['attempt_id']), old['branch'])
    # Reopen SQLite as a restarted process; journal and renamed ref must agree.
    from dispatcher.storage import Store
    from dispatcher.execution_store import ExecutionStore
    restarted = Store(d.config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        fresh = GitWorkspaceManager(d.config, execution=ExecutionStore(restarted))
        path, sha = fresh.prepare(project, dict(detached[2][0], detach_attempt=old['attempt_id']), old['branch'])
        assert sha == base
        assert not (path / 'result.txt').exists()
        repo, _, _ = fresh.paths(project, 1)
        assert git(repo, 'rev-parse', 'archive/detach/' + old['attempt_id']) == old_sha
        assert len(git(repo, 'branch', '--list', 'archive/detach/*').splitlines()) == 1
    finally:
        restarted.close()


def test_absent_ref_creates_no_archive(detached):
    d, project, _, old, _, base = detached
    repo, _, _ = d.git.paths(project, 1)
    git(repo, 'update-ref', '-d', 'refs/heads/' + old['branch'])
    with d.store.db:
        d.store.db.execute('UPDATE detach_refs SET sha=NULL')
    path, sha = prepare_again(detached)
    assert sha == base
    assert not git(repo, 'branch', '--list', 'archive/detach/*')
    assert not (path / 'result.txt').exists()


@pytest.mark.parametrize('detached', ['custom'], indirect=True)
def test_custom_base_and_branch(detached):
    d, project, _, old, old_sha, base = detached
    path, sha = prepare_again(detached)
    assert old['branch'] == 'tasks/custom-42'
    assert sha == base
    assert git(path, 'symbolic-ref', '--short', 'HEAD') == 'tasks/custom-42'
    repo, _, _ = d.git.paths(project, 1)
    assert git(repo, 'rev-parse', 'archive/detach/' + old['attempt_id']) == old_sha


def test_second_detach_archives_each_iteration(detached):
    d, project, items, old, old_sha, base = detached
    d.admit()
    second = next(iter(d.active))
    items[0]['labels'] = []
    d.adapter.response_date = '2026-10-24T00:12:00Z'
    d.stops.observe(project, d.store.queue()[0])
    finish(d)
    assert d.store.queue()[0]['state'] == 'detached'
    CleanupService(d.config, d.store).tick()
    items[0] = issue(project, server_observed_at='2026-10-24T00:13:00Z')
    d.store.apply(project, items)
    assert d.store.confirm(project.repository, 1, 1)
    d.admit()
    assert len(d.active) == 1
    third = next(iter(d.active))
    assert d.execution.get(third)['initial_sha'] == base
    repo, _, _ = d.git.paths(project, 1)
    assert len(git(repo, 'branch', '--list', 'archive/detach/*').splitlines()) == 2
    assert git(repo, 'rev-parse', 'archive/detach/' + old['attempt_id']) == old_sha
    finish(d)
    assert d.execution.get(third)['pushed']


def test_legacy_completed_cleanup(detached):
    d, project, _, old, old_sha, base = detached
    with d.store.db:
        d.store.db.execute('DELETE FROM detach_refs')
    path, sha = prepare_again(detached)
    assert sha == base
    assert not (path / 'abandoned.txt').exists()
    repo, _, _ = d.git.paths(project, 1)
    assert git(repo, 'rev-parse', 'archive/detach/' + old['attempt_id']) == old_sha


def test_missing_base_does_not_archive(detached):
    d, project, _, old, old_sha, _ = detached
    with pytest.raises(TaskError, match='base_branch_missing'):
        prepare_again(detached, replace(project, base_branch='missing-base'))
    repo, _, _ = d.git.paths(project, 1)
    assert git(repo, 'rev-parse', old['branch']) == old_sha
    assert not git(repo, 'branch', '--list', 'archive/detach/*')


def test_active_worktree_preserved(detached, tmp_path):
    d, project, _, old, old_sha, _ = detached
    repo, _, _ = d.git.paths(project, 1)
    elsewhere = tmp_path / 'active-worktree'
    git(repo, 'worktree', 'add', str(elsewhere), old['branch'])
    with pytest.raises(TaskError, match='detach_active_worktree'):
        prepare_again(detached)
    assert git(elsewhere, 'rev-parse', 'HEAD') == old_sha


def test_legacy_replaced_ref_fails_closed(detached):
    d, project, _, old, _, base = detached
    repo, _, _ = d.git.paths(project, 1)
    with d.store.db:
        d.store.db.execute('DELETE FROM detach_refs')
    git(repo, 'update-ref', 'refs/heads/' + old['branch'], base)
    with pytest.raises(TaskError, match='detach_branch_ownership_conflict'):
        prepare_again(detached)
    assert git(repo, 'rev-parse', old['branch']) == base
    assert not git(repo, 'branch', '--list', 'archive/detach/*')


def test_legacy_absent_ref_creates_no_archive(detached):
    d, project, _, old, _, base = detached
    repo, _, _ = d.git.paths(project, 1)
    with d.store.db:
        d.store.db.execute('DELETE FROM detach_refs')
    git(repo, 'update-ref', '-d', 'refs/heads/' + old['branch'])
    _, sha = prepare_again(detached)
    assert sha == base
    assert not git(repo, 'branch', '--list', 'archive/detach/*')
