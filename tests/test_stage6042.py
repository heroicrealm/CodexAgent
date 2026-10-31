"""Issue #42: metadata isolation and real disposable Git repositories."""
from dataclasses import replace
import pytest
from dispatcher.issue_parameters import parse
from dispatcher.workspace import GitWorkspaceManager, TaskError
from test_stage04 import env, git, issue


@pytest.mark.parametrize('fence', ['```', '````', '~~~', '   ```'])
def test_structural_cleaning(fence):
    body = 'Task\n' + fence + 'yaml\ndispatcher:\n  branch: feature/task\n' + fence + '\n```yaml\nexample: retained\n```\n'
    params, cleaned = parse(body)
    assert params == {'branch_name': 'feature/task'}
    assert cleaned == 'Task\n```yaml\nexample: retained\n```\n'


@pytest.mark.parametrize('content', [
    'dispatcher:\n  branch:', 'dispatcher:\n  branch: 5',
    'dispatcher:\n  branch: x\n  branch: y', 'dispatcher:\n  other: x',
    'dispatcher: []', 'dispatcher:\n  branch: [', '{dispatcher: [',
    'dispatcher: {branch: x}\ndispatcher: {}',
    '"dispatcher": {branch: x, branch: y}',
    'dispatcher: &params {branch: x}',
])
def test_invalid(content):
    with pytest.raises(TaskError):
        parse('```yaml\n' + content + '\n```')


@pytest.mark.parametrize('body', [
    '```yaml\ndispatcher: {}',
    '```yaml\ndispatcher: {}\n```\n```yaml\ndispatcher: {}\n```',
    'Target branch: x\n```yaml\ndispatcher: {branch: x}\n```',
    'Target branch: x\nЦелевая ветка: x',
])
def test_ambiguous(body):
    with pytest.raises(TaskError):
        parse(body)


def test_legacy_and_nested_examples():
    body = 'Task\nTarget branch: feature/task\n```text\nTarget branch: keep\n```\n'
    assert parse(body) == ({'branch_name': 'feature/task'}, 'Task\n```text\nTarget branch: keep\n```\n')
    assert parse('````text\n```yaml\ndispatcher: {}\n```\n````')[1].startswith('````text')


@pytest.mark.parametrize('existing,base', [(False, 'main'), (False, 'develop'), (True, 'missing')])
def test_origin_source(env, existing, base):
    config, project, _, seed, remote = env
    git(seed, 'checkout', '-b', 'develop')
    (seed / 'develop').write_text('new base')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Develop')
    git(seed, 'push', str(remote), 'develop')
    if existing:
        git(seed, 'push', str(remote), 'develop:feature/task')
    manager = GitWorkspaceManager(config)
    task = issue(project, body=f'```yaml\ndispatcher:\n  branch: feature/task\n  base_branch: {base}\n```')
    branch = manager.branch(project, task)
    worktree, sha = manager.prepare(replace(project, base_branch=base), task, branch)
    assert sha == git(remote, 'rev-parse', 'feature/task' if existing else base)
    assert git(worktree, 'rev-parse', 'HEAD') == sha


def test_missing_base_and_protection(env):
    config, project, *_ = env
    manager = GitWorkspaceManager(config)
    with pytest.raises(TaskError, match='default_branch_protected'):
        manager.branch(project, issue(project, body='Target branch: main'))
    with pytest.raises(TaskError, match='base_branch_missing'):
        manager.prepare(replace(project, base_branch='missing'), issue(project), 'feature/task')


def test_prompt_every_attempt_and_original_snapshot(env):
    from dispatcher.codex import CodexExecutor
    from dispatcher.storage import Store
    from unittest.mock import Mock
    config, project, *_ = env
    body = 'Do work\n```yaml\ndispatcher: {branch: feature/task}\n```\n```yaml\nexample: kept\n```\n'
    task_issue = issue(project, body=body)
    database = Store(config.workspace / '.runtime' / 'metadata-test.sqlite3')
    try:
        import json
        database.apply(project, [task_issue])
        saved = json.loads(database.get(project.repository, 1)['snapshot'])
        assert saved['body'] == body
        manager = GitWorkspaceManager(config)
        _, task, _ = manager.paths(project, 1)
        task.mkdir(parents=True)
        executor = CodexExecutor(config, Mock())
        executor.checked = True
        for attempt in ('first', 'repeat'):
            executor.start(saved, 'feature/task', [], task, task, attempt)
            prompt = (task / (attempt + '.prompt')).read_text()
            assert 'dispatcher:' not in prompt
            assert '```yaml\nexample: kept\n```' in prompt
        assert saved['body'] == body
    finally:
        database.close()


def test_push_conflict_preserves_commit(env):
    config, project, _, seed, remote = env
    manager = GitWorkspaceManager(config)
    worktree, _ = manager.prepare(project, issue(project), 'feature/task')
    (worktree / 'local').write_text('local')
    git(worktree, 'add', '.')
    git(worktree, 'commit', '-m', 'Local')
    local = git(worktree, 'rev-parse', 'HEAD')
    (seed / 'remote').write_text('remote')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Remote')
    git(seed, 'push', str(remote), 'HEAD:feature/task')
    with pytest.raises(TaskError):
        manager.push(project, worktree, 'feature/task')
    assert git(worktree, 'rev-parse', 'HEAD') == local
    assert git(remote, 'rev-parse', 'feature/task') != local


def test_local_collision_preserves_ref(env):
    config, project, *_ = env
    manager = GitWorkspaceManager(config)
    repo, _, worktree = manager.paths(project, 1)
    git(repo, 'branch', 'feature/task', 'origin/main')
    before = git(repo, 'rev-parse', 'feature/task')
    with pytest.raises(TaskError, match='branch_ownership_conflict'):
        manager.prepare(project, issue(project), 'feature/task')
    assert git(repo, 'rev-parse', 'feature/task') == before
    assert not worktree.exists()


def test_bad_issue_does_not_block_other_issue(env):
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    from test_stage04 import adapter, register, finish
    config, project, *_ = env
    items = [issue(project, body='```yaml\ndispatcher: {branch: []}\n```'),
             issue(project, id=2, number=2)]
    d = Dispatcher(config, Lifecycle(), adapter(items))
    try:
        register(d, project, items)
        d.admit()
        finish(d)
        assert d.store.get(project.repository, 1)['state'] == 'discovered'
        assert d.store.get(project.repository, 2)['state'] == 'review'
        assert d.store.db.execute('SELECT count(*) FROM configuration_errors').fetchone()[0] == 1
        assert not d.store.db.execute('SELECT 1 FROM attempts WHERE number=1 AND codex_started=1').fetchone()
    finally:
        d.lifecycle.cancel()
        d.close()


def test_collision_is_diagnosed_without_launch(env):
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    from test_stage04 import adapter, register, finish
    config, project, control, *_ = env
    control.write_text('{"exec": "dirty"}')
    body = '```yaml\ndispatcher: {branch: feature/shared}\n```'
    items = [issue(project, body=body), issue(project, id=2, number=2, body=body)]
    d = Dispatcher(config, Lifecycle(), adapter(items))
    try:
        register(d, project, items)
        d.admit()
        finish(d)
        d.admit()
        assert d.store.get(project.repository, 2)['state'] == 'discovered'
        assert d.store.db.execute('SELECT code FROM configuration_errors').fetchone()[0] == 'branch_ownership_conflict'
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        attempt = d.store.db.execute('SELECT * FROM attempts').fetchone()
        assert attempt['branch_existed'] == 0
        assert attempt['source_base_branch'] == 'main'
        assert attempt['initial_sha']
        assert d.store.get(project.repository, 1)['branch_source'] == 'explicit'
    finally:
        d.lifecycle.cancel()
        d.close()


def test_additive_migration(env):
    from dispatcher.storage import Store
    config, *_ = env
    path = config.workspace / '.runtime' / 'upgrade-test.sqlite3'
    store = Store(path)
    with store.db:
        store.db.execute('ALTER TABLE attempts DROP COLUMN source_base_branch')
        store.db.execute('ALTER TABLE attempts DROP COLUMN branch_existed')
        store.db.execute('DELETE FROM schema_migrations WHERE version=613')
    store.close()
    for _ in range(2):
        store = Store(path)
        assert {'source_base_branch', 'branch_existed'} <= {r[1] for r in store.db.execute('PRAGMA table_info(attempts)')}
        assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        store.close()
