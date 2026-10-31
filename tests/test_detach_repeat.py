"""Detach reuses its published canonical SHA; ambiguity preserves refs."""
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
    # Both committed and dirty output must survive on the canonical remote.
    (worktree / 'abandoned.txt').write_text('saved commit')
    git(worktree, 'add', '.')
    git(worktree, 'commit', '-m', 'Saved result')
    (worktree / 'dirty.txt').write_text('saved dirty output')
    CleanupService(config, d.store).tick()
    assert not worktree.exists()
    assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'completed'
    old = d.execution.get(attempt)
    old_sha = old['result_sha']
    assert git(remote, 'rev-parse', old['branch']) == old_sha
    control.write_text(json.dumps({'exec': 'dirty'}))
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


def test_published_sha_and_full_lifecycle(detached):
    d, project, items, old, old_sha, base = detached
    d.admit()
    assert len(d.active) == 1
    attempt = next(iter(d.active))
    current = d.execution.get(attempt)
    assert current['initial_sha'] == old_sha
    worktree = Path(current['worktree'])
    assert (worktree / 'dirty.txt').read_text() == 'saved dirty output'
    assert (worktree / 'abandoned.txt').exists()
    assert not (worktree / 'fresh.txt').exists()
    finish(d)
    assert d.store.queue()[0]['state'] == 'review'
    assert d.execution.get(attempt)['pushed']


@pytest.mark.parametrize('failure', ['foreign', 'remote', 'missing_remote', 'cleanup', 'identity', 'project', 'legacy', 'missing_ref'])
def test_conflicts_preserve_refs(detached, failure):
    d, project, _, old, old_sha, base = detached
    repo, _, _ = d.git.paths(project, 1)
    if failure == 'foreign':
        git(repo, 'update-ref', 'refs/heads/' + old['branch'], base)
    elif failure == 'remote':
        git(repo, 'push', 'origin', 'main:refs/heads/' + old['branch'], '--force')
    elif failure == 'missing_remote':
        git(repo, 'push', 'origin', '--delete', old['branch'])
    elif failure == 'missing_ref':
        git(repo, 'update-ref', '-d', 'refs/heads/' + old['branch'])
    elif failure == 'legacy':
        with d.store.db:
            d.store.db.execute('DELETE FROM detach_publications')
    elif failure == 'cleanup':
        with d.store.db:
            d.store.db.execute("UPDATE cleanup_jobs SET state='pending'")
    elif failure == 'identity':
        with d.store.db:
            d.store.db.execute("UPDATE stop_requests SET process_status='unconfirmed'")
    else:
        project = replace(project, id='another-project')
        other, _, _ = d.git.paths(project, 1)
        other.parent.mkdir(parents=True, exist_ok=True)
        git(repo.parent, 'clone', '--no-checkout', str(repo), str(other))
    before = git(repo, 'show-ref', '--heads')
    with pytest.raises(TaskError, match='detach_'):
        prepare_again(detached, project)
    assert git(repo, 'show-ref', '--heads') == before


def test_crash_before_worktree_add_and_restart(detached, monkeypatch):
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
    from dispatcher.storage import Store
    from dispatcher.execution_store import ExecutionStore
    restarted = Store(d.config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        fresh = GitWorkspaceManager(d.config, execution=ExecutionStore(restarted))
        path, sha = fresh.prepare(project, dict(detached[2][0], detach_attempt=old['attempt_id']), old['branch'])
        assert sha == old_sha
        assert (path / 'dirty.txt').exists()
    finally:
        restarted.close()


@pytest.mark.parametrize('detached', ['custom'], indirect=True)
def test_custom_base_and_branch(detached):
    d, project, _, old, old_sha, base = detached
    path, sha = prepare_again(detached)
    assert old['branch'] == 'tasks/custom-42'
    assert sha == old_sha
    assert git(path, 'symbolic-ref', '--short', 'HEAD') == 'tasks/custom-42'


def test_second_detach_preserves_each_iteration(detached):
    d, project, items, old, old_sha, base = detached
    d.admit()
    second = next(iter(d.active))
    items[0]['labels'] = []
    d.adapter.response_date = '2026-10-24T00:12:00Z'
    d.stops.observe(project, d.store.queue()[0])
    finish(d)
    CleanupService(d.config, d.store).tick()
    second_sha = d.execution.get(second)['result_sha']
    items[0] = issue(project, server_observed_at='2026-10-24T00:13:00Z')
    d.store.apply(project, items)
    assert d.store.confirm(project.repository, 1, 1)
    d.admit()
    assert len(d.active) == 1
    third = next(iter(d.active))
    assert d.execution.get(third)['initial_sha'] == second_sha
    assert d.execution.get(old['attempt_id'])['result_sha'] == old_sha
    finish(d)
    assert d.execution.get(third)['pushed']


def test_missing_base_preserves_branch(detached):
    d, project, _, old, old_sha, _ = detached
    with pytest.raises(TaskError, match='base_branch_missing'):
        prepare_again(detached, replace(project, base_branch='missing-base'))
    repo, _, _ = d.git.paths(project, 1)
    assert git(repo, 'rev-parse', old['branch']) == old_sha


def test_active_worktree_preserved(detached, tmp_path):
    d, project, _, old, old_sha, _ = detached
    repo, _, _ = d.git.paths(project, 1)
    elsewhere = tmp_path / 'active-worktree'
    git(repo, 'worktree', 'add', str(elsewhere), old['branch'])
    with pytest.raises(TaskError, match='detach_active_worktree'):
        prepare_again(detached)
    assert git(elsewhere, 'rev-parse', 'HEAD') == old_sha
