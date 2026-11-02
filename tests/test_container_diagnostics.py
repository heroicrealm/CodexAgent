"""Diagnostics preserve pending, branch ownership and durable delivery receipts."""
from dispatcher.container_diagnostic import reconcile
from dispatcher.github import GitHubError
from dispatcher.storage import Store
from dispatcher.containers import ContainerService
from dispatcher.workspace import GitWorkspaceManager
from test_stage04 import env, git
from test_stage6433 import setup, raw


def comments(adapter):
    published = []
    original = adapter.write.side_effect
    def write(endpoint, method, payload):
        if endpoint.endswith('/comments'):
            comment = dict(id=len(published) + 1, body=payload['body'])
            published.append(comment)
            if len(published) == 1:
                raise GitHubError(kind='network')  # Server accepted; response lost.
            return comment
        return original(endpoint, method, payload)
    adapter.pages.side_effect = lambda endpoint: list(published)
    adapter.write.side_effect = write
    return published


def test_collision_restart_and_yaml_change(env):
    config, project, _, seed, remote = env
    git(seed, 'push', str(remote), 'main:feature/container')
    sha = git(remote, 'rev-parse', 'feature/container')
    store, parent, adapter, service = setup(env)
    published = comments(adapter)
    service.prepare(project, store.get(project.repository, 1))
    assert len(published) == 1
    assert 'branch_ownership_conflict' in published[0]['body']
    assert 'новую Issue' in published[0]['body']
    store.close()
    store = Store(config.workspace / '.runtime' / 'containers.sqlite3')
    service = ContainerService(store, adapter, GitWorkspaceManager(config))
    service.prepare(project, store.get(project.repository, 1))
    assert len(published) == 1
    assert store.db.execute('SELECT delivered FROM container_diagnostic_outbox').fetchone()[0] == 1
    parent['body'] = parent['body'].replace('feature/container', 'feature/new')
    parent['updated_at'] = '2026-10-25T01:00:00Z'
    store.apply(project, [parent])
    row = store.get(project.repository, 1)
    assert store.confirm(project.repository, 1, row['version'])
    adapter.request.return_value = raw(project, parent)
    service.prepare(project, store.get(project.repository, 1))
    assert len(published) == 2
    assert 'container_branch_conflict' in published[-1]['body']
    assert store.get(project.repository, 1)['state'] == 'pending'
    assert git(remote, 'rev-parse', 'feature/container') == sha
    assert 'refs/heads/feature/new' not in git(remote, 'show-ref')
    store.close()


def test_transient_threshold_and_confirmed_recovery(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    published = comments(adapter)
    row = store.get(project.repository, 1)
    service.block(row, 'container_github_unavailable')
    for _ in range(2):
        reconcile(store, adapter, row)
    assert published == []
    reconcile(store, adapter, row)
    assert len(published) == 1
    service.prepare(project, row)
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert len(published) == 2
    assert 'возобновлена' in published[-1]['body']
    reconcile(store, adapter, row)
    assert len(published) == 2
    store.close()


def test_child_identity_and_safe_text(env):
    _, project, *_ = env
    from test_stage04 import issue
    child = issue(project, id=2, number=23)
    store, _, adapter, service = setup(env, [child])
    published = comments(adapter)
    row = store.get(project.repository, 1)
    service.block(row, 'child_body_conflict', 2)
    reconcile(store, adapter, row)
    assert '#23' in published[0]['body']
    assert 'не перезаписывайте' in published[0]['body']
    assert str(env[0].workspace) not in published[0]['body']
    store.close()


def test_short_outage_has_no_comments(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    published = comments(adapter)
    row = store.get(project.repository, 1)
    service.block(row, 'container_github_unavailable')
    reconcile(store, adapter, row)
    service.prepare(project, row)
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert published == []
    store.close()


def test_recovery_delivery_after_restart(env):
    config, project, *_ = env
    store, _, adapter, service = setup(env)
    published = comments(adapter)
    row = store.get(project.repository, 1)
    service.block(row, 'fetch_failed')
    for _ in range(4):
        reconcile(store, adapter, row)
    assert len(published) == 1
    real_pages = adapter.pages.side_effect
    adapter.pages.side_effect = GitHubError(kind='network')
    service.prepare(project, row)
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert len(published) == 1
    store.close()
    store = Store(config.workspace / '.runtime' / 'containers.sqlite3')
    adapter.pages.side_effect = real_pages
    service = ContainerService(store, adapter, GitWorkspaceManager(config))
    service.recover_diagnostics([project])
    service.recover_diagnostics([project])
    assert len(published) == 2
    assert 'возобновлена' in published[-1]['body']
    assert store.db.execute('SELECT count(*) FROM container_diagnostic_outbox WHERE delivered=0').fetchone()[0] == 0
    store.close()


def test_crash_between_transition_and_recovery_event(env, monkeypatch):
    import pytest
    from dispatcher import container_diagnostic
    config, project, _, seed, remote = env
    store, _, adapter, service = setup(env)
    published = comments(adapter)
    row = store.get(project.repository, 1)
    service.block(row, 'fetch_failed')
    for _ in range(4):
        reconcile(store, adapter, row)
    with monkeypatch.context() as patch:
        patch.setattr(container_diagnostic, 'reconcile', lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError):
            service.prepare(project, row)
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    store.close()
    store = Store(config.workspace / '.runtime' / 'containers.sqlite3')
    service = ContainerService(store, adapter, GitWorkspaceManager(config))
    sha = git(remote, 'rev-parse', 'feature/container')
    git(remote, 'update-ref', '-d', 'refs/heads/feature/container')
    service.recover_diagnostics([project])
    assert len(published) == 1  # Local transition alone cannot prove recovery.
    git(remote, 'update-ref', 'refs/heads/feature/container', sha)
    service.recover_diagnostics([project])
    service.recover_diagnostics([project])
    assert len(published) == 2
    assert 'возобновлена' in published[-1]['body']
    store.close()
