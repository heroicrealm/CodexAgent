"""Container preparation boundary: real Git, SQLite recovery, no remote writes."""
import json
from unittest.mock import Mock
import pytest
from dispatcher.containers import ContainerService
from dispatcher.github import Adapter, GitHubError
from dispatcher.issue_parameters import parse, with_base_branch
from dispatcher.storage import Store
from dispatcher.storage.migrations import upgrade614
from dispatcher.workspace import GitWorkspaceManager, TaskError
from test_stage04 import env, issue, git


@pytest.mark.parametrize('kind,run', [('issue', True), ('container', False), ('container', True), ('issue', False)])
def test_parameters(kind, run):
    body = f'Task\n```yaml\ndispatcher: {{issue_type: {kind}, run_codex: {str(run).lower()}}}\n```\n'
    assert parse(body) == ({'issue_type': kind, 'run_codex': run}, 'Task\n')


@pytest.mark.parametrize('content', ['issue_type: other', 'issue_type: false', 'run_codex: "false"', 'run_codex: 0', 'run_codex: null', 'run_codex: yes', 'run_codex: off', 'run_codex: true, run_codex: false'])
def test_strict(content):
    with pytest.raises(TaskError):
        parse('```yaml\ndispatcher: {' + content + '}\n```')


@pytest.mark.parametrize('body', ['Task\n', 'Task\n```yaml\ndispatcher: {}\n```\n', '```yaml\ndispatcher:\n  branch: feature/x # keep\n```\n', '```yaml\ndispatcher:\n  base_branch: old # keep\n```\n', '```yaml\ndispatcher: {branch: feature/x, base_branch: old}\n```\n'])
def test_proposal(body):
    changed = with_base_branch(body, 'feature/container')
    assert parse(changed)[0]['base_branch'] == 'feature/container'
    assert with_base_branch(changed, 'feature/container') == changed
    if '# keep' in body:
        assert '# keep' in changed
    assert parse(changed)[1] == parse(body)[1]


def raw(project, item):
    return dict(id=item['id'], number=item['number'], title=item['title'], body=item['body'],
                state='open', user=dict(login=item['author']), labels=[dict(name=n) for n in item['labels']],
                updated_at=item['updated_at'], repository_url=f'https://api.github.com/repos/{project.repository}',
                url=f'https://api.github.com/repos/{project.repository}/issues/{item["number"]}')


def setup(env, children=()):
    config, project, *_ = env
    store = Store(config.workspace / '.runtime' / 'containers.sqlite3')
    parent = issue(project, body='```yaml\ndispatcher: {issue_type: container, branch: feature/container}\n```')
    store.apply(project, [parent, *children])
    assert store.confirm(project.repository, 1, 1)
    adapter = Mock(spec=Adapter)
    adapter.response_date = '2026-10-24T01:00:00Z'
    adapter.request.return_value = raw(project, parent)
    adapter.list_direct_children.return_value = [raw(project, c) for c in children]
    def create_ref(endpoint, method, payload):
        assert endpoint == f'repos/{project.repository}/git/refs' and method == 'POST'
        git(env[-1], 'update-ref', payload['ref'], payload['sha'], '0' * 40)
        return dict(ref=payload['ref'], object=dict(sha=payload['sha']))
    adapter.write.side_effect = create_ref
    service = ContainerService(store, adapter, GitWorkspaceManager(config))
    return store, parent, adapter, service


def test_container_happy_and_repeat(env):
    config, project, _, _, remote = env
    store, _, adapter, service = setup(env)
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert git(remote, 'rev-parse', 'feature/container') == git(remote, 'rev-parse', 'main')
    assert git(remote, 'rev-list', '--count', '--all') == '1'
    service.prepare(project, store.get(project.repository, 1))
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 0
    assert all(call.args[0].endswith('/git/refs') for call in adapter.write.call_args_list)
    store.close()


def test_crash_after_push_recovery(env):
    config, project, _, _, remote = env
    store, _, adapter, service = setup(env)
    operation = service.operation
    def crash(row, kind, payload, phase='intent', remote=None):
        if kind == 'branch' and phase == 'confirmed':
            raise RuntimeError('simulated crash')
        return operation(row, kind, payload, phase, remote)
    service.operation = crash
    with pytest.raises(RuntimeError):
        service.prepare(project, store.get(project.repository, 1))
    sha = git(remote, 'rev-parse', 'feature/container')
    store.close()
    store = Store(config.workspace / '.runtime' / 'containers.sqlite3')
    service = ContainerService(store, adapter, GitWorkspaceManager(config))
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert git(remote, 'rev-parse', 'feature/container') == sha
    store.close()


@pytest.mark.parametrize('configured', [False, True])
def test_child_prepared_or_fail_closed(env, configured):
    _, project, *_ = env
    child = issue(project, id=2, number=2, body='Child\n' + ('```yaml\ndispatcher: {base_branch: feature/container}\n```' if configured else ''))
    store, _, adapter, service = setup(env, [child])
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == ('awaiting_children' if configured else 'pending')
    assert store.get(project.repository, 2)['state'] == 'discovered'
    assert store.get(project.repository, 2)['approved_version'] is None
    if not configured:
        assert store.db.execute('SELECT code FROM container_blockers').fetchone()[0] == 'child_body_conflict'
    assert all(call.args[0].endswith('/git/refs') for call in adapter.write.call_args_list)
    store.close()


def test_existing_remote_collision(env):
    _, project, _, seed, remote = env
    git(seed, 'push', str(remote), 'main:feature/container')
    store, _, _, service = setup(env)
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'pending'
    assert store.db.execute('SELECT code FROM container_blockers').fetchone()[0] == 'branch_ownership_conflict'
    store.close()


@pytest.mark.parametrize('count', [0, 1, 101])
def test_direct_children_pagination(env, count):
    _, project, *_ = env
    adapter = Adapter()
    parent = dict(issue_id=1, number=1)
    children = [raw(project, issue(project, id=n, number=n)) for n in range(2, count + 2)]
    def request(endpoint):
        if '/sub_issues?' not in endpoint:
            return raw(project, issue(project))
        page = int(endpoint.split('page=')[-1])
        return children[(page-1)*100:page*100]
    adapter.request = request
    assert len(adapter.list_direct_children(project, parent)) == count


@pytest.mark.parametrize('failure', ['duplicate', 'foreign', 'partial', 'churn'])
def test_direct_children_fail_closed(env, failure):
    _, project, *_ = env
    adapter = Adapter()
    child = raw(project, issue(project, id=2, number=2))
    adapter.request = Mock(return_value=raw(project, issue(project)))
    if failure == 'duplicate':
        adapter.pages = Mock(return_value=[child, child])
    elif failure == 'foreign':
        child['repository_url'] += '-other'
        adapter.pages = Mock(return_value=[child])
    elif failure == 'partial':
        adapter.pages = Mock(side_effect=GitHubError())
    else:
        adapter.pages = Mock(side_effect=[[child], []])
    with pytest.raises(GitHubError):
        adapter.list_direct_children(project, dict(issue_id=1, number=1))


def test_migration_repeat_and_rollback(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    store = Store(path)
    assert store.db.execute('SELECT 1 FROM schema_migrations WHERE version=614').fetchone()
    with store.db:
        upgrade614(store.db)
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    store.db.execute('BEGIN IMMEDIATE')
    store.db.execute('DROP TABLE container_blockers')
    store.db.execute('DELETE FROM schema_migrations WHERE version=614')
    upgrade614(store.db)
    store.db.rollback()
    store.close()
    reopened = Store(path)
    assert reopened.db.execute('SELECT count(*) FROM schema_migrations WHERE version=614').fetchone()[0] == 1
    reopened.close()


def test_non_ready_never_managed_is_ignored(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    child = issue(project, id=2, number=2, labels=[])
    adapter.list_direct_children.return_value = [raw(project, child)]
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert store.get(project.repository, 2) is None
    assert not store.db.execute('SELECT 1 FROM sub_issue_relations').fetchone()
    store.close()


def test_stale_parent_no_publication(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    adapter.request.return_value['body'] = 'edited by human'
    service.prepare(project, store.get(project.repository, 1))
    adapter.write.assert_not_called()
    assert store.db.execute('SELECT code FROM container_blockers').fetchone()[0] == 'container_stale'
    store.close()


def test_lost_ref_response_recovers_without_repeat_write(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    create = adapter.write.side_effect
    def lost(*args):
        create(*args)
        raise GitHubError(kind='timeout')
    adapter.write.side_effect = lost
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'pending'
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert adapter.write.call_count == 1
    store.close()


def test_project_lock_parallel_poll(env):
    import fcntl
    import os
    config, project, *_ = env
    store, _, adapter, service = setup(env)
    lock = config.workspace / project.id / '.container.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        service.prepare(project, store.get(project.repository, 1))
        adapter.request.assert_not_called()
        adapter.write.assert_not_called()
    finally:
        os.close(fd)
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    store.close()


def test_scheduler_routes_without_quota_or_executor(env):
    from dispatcher.scheduler import Dispatcher
    from types import SimpleNamespace
    _, project, *_ = env
    store, parent, adapter, service = setup(env)
    adapter.fetch.return_value = [parent]
    daemon = object.__new__(Dispatcher)
    daemon.store, daemon.adapter, daemon.containers = store, adapter, service
    daemon.projects, daemon.cursor, daemon.active = [project], 0, {}
    daemon.lifecycle = SimpleNamespace(stopping=False)
    daemon.config = SimpleNamespace(max_parallel_tasks=1)
    daemon.quota, daemon.executor, daemon.pr_publisher = Mock(), Mock(), Mock()
    daemon.admit()
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    daemon.quota.check.assert_not_called()
    daemon.executor.start.assert_not_called()
    daemon.pr_publisher.publish.assert_not_called()
    store.close()


def test_ordinary_manual_not_executed(env):
    from dispatcher.execution_store import ExecutionStore
    config, project, *_ = env
    store = Store(config.workspace / '.runtime' / 'manual.sqlite3')
    store.apply(project, [issue(project, body='```yaml\ndispatcher: {run_codex: false}\n```')])
    assert store.confirm(project.repository, 1, 1)
    task = store.get(project.repository, 1)
    assert ExecutionStore(store).reserve(task, 'feature/manual', 1, store.guard(project.repository, 1)['observation_token']) is None
    assert task['run_codex'] == 0
    store.close()


def test_upgrade613_defaults_and_atomic_failure(env, monkeypatch):
    import dispatcher.storage.migrations as migrations
    config, project, *_ = env
    path = config.workspace / '.runtime' / 'legacy614.sqlite3'
    store = Store(path)
    store.apply(project, [issue(project)])
    with store.db as db:
        for table in ('container_blockers', 'container_operations', 'sub_issue_relations'):
            db.execute('DROP TABLE ' + table)
        db.execute('ALTER TABLE tasks DROP COLUMN issue_type')
        db.execute('ALTER TABLE tasks DROP COLUMN run_codex')
        db.execute('DELETE FROM schema_migrations WHERE version=614')
    original = dict(store.get(project.repository, 1))
    validate = migrations.validate
    def fail(db):
        raise RuntimeError('simulated migration interruption')
    monkeypatch.setattr(migrations, 'validate', fail)
    with pytest.raises(RuntimeError), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        upgrade614(store.db)
    assert 'issue_type' not in {r[1] for r in store.db.execute('PRAGMA table_info(tasks)')}
    assert not store.db.execute('SELECT 1 FROM schema_migrations WHERE version=614').fetchone()
    monkeypatch.setattr(migrations, 'validate', validate)
    store.close()
    store = Store(path)
    restored = dict(store.get(project.repository, 1))
    assert restored.pop('issue_type') == 'issue'
    assert restored.pop('run_codex') == 1
    assert original == restored
    store.close()


def test_active_child_incompatible_base_blocks_without_edit(env):
    _, project, *_ = env
    child = issue(project, id=2, number=2, body='Child')
    store, _, adapter, service = setup(env, [child])
    with store.db:
        store.db.execute("UPDATE tasks SET state='running',work_branch='feature/child',base_branch='main' WHERE number=2")
    service.prepare(project, store.get(project.repository, 1))
    assert store.db.execute('SELECT code FROM container_blockers').fetchone()[0] == 'child_active_base_conflict'
    assert store.get(project.repository, 2)['base_branch'] == 'main'
    assert store.get(project.repository, 2)['state'] == 'running'
    assert all(c.args[0].endswith('/git/refs') for c in adapter.write.call_args_list)
    store.close()


def test_container_cannot_reserve_codex(env):
    from dispatcher.execution_store import ExecutionStore
    _, project, *_ = env
    store, _, _, _ = setup(env)
    assert ExecutionStore(store).reserve(store.get(project.repository, 1), 'feature/container', 1,
                                         store.guard(project.repository, 1)['observation_token']) is None
    store.close()


def test_api_error_never_confirms_partial_children(env):
    _, project, *_ = env
    store, _, adapter, service = setup(env)
    adapter.list_direct_children.side_effect = GitHubError()
    service.prepare(project, store.get(project.repository, 1))
    assert store.get(project.repository, 1)['state'] == 'pending'
    assert not store.db.execute("SELECT 1 FROM container_operations WHERE kind='prepare'").fetchone()
    assert not store.db.execute('SELECT 1 FROM sub_issue_relations').fetchone()
    store.close()


def test_missing_nested_base_does_not_publish(env):
    _, project, *_ = env
    store, parent, adapter, service = setup(env)
    parent['body'] = '```yaml\ndispatcher: {issue_type: container, branch: feature/container, base_branch: missing-parent}\n```'
    store.apply(project, [parent])
    assert store.confirm(project.repository, 1, 2)
    adapter.request.return_value = raw(project, parent)
    service.prepare(project, store.get(project.repository, 1))
    adapter.write.assert_not_called()
    assert store.get(project.repository, 1)['state'] == 'pending'
    store.close()


def test_proposal_retains_unrelated_invalid_yaml():
    body = 'Example:\n```yaml\nother: [\n```\n'
    assert with_base_branch(body, 'feature/container').startswith(body)


def controlled_setup(env, bodies=('Child\n',)):
    import copy
    _, project, *_ = env
    children = [issue(project, id=n, number=n, body=body) for n, body in enumerate(bodies, 2)]
    store, parent, adapter, service = setup(env, children)
    remote = {c['number']: raw(project, c) for c in [parent, *children]}
    adapter.request.side_effect = lambda endpoint: copy.deepcopy(remote[int(endpoint.split('/')[-1])])
    adapter.list_direct_children.side_effect = lambda *_: copy.deepcopy([remote[n] for n in remote if n != 1])
    adapter.can_update_issue.return_value = True
    create_ref = adapter.write.side_effect
    patches = []
    def write(endpoint, method, payload):
        if method != 'PATCH':
            return create_ref(endpoint, method, payload)
        number = int(endpoint.split('/')[-1])
        patches.append(number)
        remote[number].update(body=payload['body'], updated_at='2026-10-25T01:00:00Z')
        return dict(remote[number])
    adapter.write.side_effect = write
    return store, adapter, service, remote, patches


@pytest.mark.parametrize('body', ['Юникод\n```text\nexample\n```\n',
    '```yaml\ndispatcher: {}\n```\n', '```yaml\ndispatcher:\n  base_branch: old # сохранить\n```\n'])
def test_controlled_child_update(env, body):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env, [body])
    assert store.confirm(project.repository, 2, 1)
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2]
    child = store.get(project.repository, 2)
    assert child['state'] == 'pending' and child['approved_version'] == child['version'] == 1
    assert json.loads(child['snapshot'])['body'] == with_base_branch(body, 'feature/container')
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2]
    edited = json.loads(child['snapshot'])
    edited['body'] += '\nHuman edit'
    store.apply(project, [edited], partial=True)
    assert store.get(project.repository, 2)['approved_version'] is None
    assert store.get(project.repository, 2)['version'] == 2
    store.close()


@pytest.mark.parametrize('change', ['body', 'updated_at', 'labels', 'user', 'state', 'parent', 'local', 'permission'])
def test_controlled_conflict_before_patch(env, change):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    read = adapter.request.side_effect
    count = 0
    def request(endpoint):
        nonlocal count
        if endpoint.endswith('/2'):
            count += 1
            if count == 2:
                if change == 'parent':
                    adapter.list_direct_children.side_effect = lambda *_: []
                    raise GitHubError()
                elif change == 'local':
                    with store.db:
                        store.db.execute('UPDATE tasks SET version=version+1 WHERE number=2')
                elif change != 'permission':
                    remote[2][change] = dict(body='human', updated_at='later', labels=[], user={'login':'intruder'}, state='closed')[change]
        return read(endpoint)
    adapter.request.side_effect = request
    adapter.can_update_issue.return_value = change != 'permission'
    service.prepare(project, store.get(project.repository, 1))
    assert patches == []
    assert store.get(project.repository, 1)['state'] == 'pending'
    store.close()


@pytest.mark.parametrize('mode', ['timeout', 'crash', 'false_success', 'verify_failure'])
def test_controlled_recovery(env, mode):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    assert store.confirm(project.repository, 2, 1)
    write = adapter.write.side_effect
    def uncertain(endpoint, method, payload):
        if method != 'PATCH':
            return write(endpoint, method, payload)
        if mode == 'false_success':
            return {}
        result = write(endpoint, method, payload)
        if mode == 'crash':
            raise RuntimeError('process terminated')
        if mode == 'verify_failure':
            adapter.request.side_effect = GitHubError()
        if mode == 'timeout':
            raise GitHubError(kind='timeout')
        return result
    adapter.write.side_effect = uncertain
    if mode == 'crash':
        with pytest.raises(RuntimeError):
            service.prepare(project, store.get(project.repository, 1))
    else:
        service.prepare(project, store.get(project.repository, 1))
    if mode == 'false_success':
        assert store.get(project.repository, 1)['state'] == 'pending'
        assert not store.db.execute("SELECT 1 FROM container_operations WHERE kind='child:2' AND phase='confirmed'").fetchone()
    else:
        # Restart and poll before recovery must not invalidate legitimate approval.
        path = store.path if hasattr(store, 'path') else env[0].workspace / '.runtime' / 'containers.sqlite3'
        store.close()
        store = Store(path)
        item = json.loads(store.get(project.repository, 2)['snapshot'])
        item.update(body=remote[2]['body'], updated_at=remote[2]['updated_at'])
        store.apply(project, [item], partial=True)
        import copy
        adapter.request.side_effect = lambda endpoint: copy.deepcopy(remote[int(endpoint.split('/')[-1])])
        adapter.write.side_effect = write
        service = ContainerService(store, adapter, GitWorkspaceManager(env[0]))
        service.prepare(project, store.get(project.repository, 1))
        assert store.get(project.repository, 1)['state'] == 'awaiting_children'
        assert store.get(project.repository, 2)['approved_version'] == 1
        assert patches == [2]
    store.close()


def test_controlled_two_ready_and_never_ready(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env, ['First', 'Second'])
    remote[4] = raw(project, issue(project, id=4, number=4, labels=[], body='untouched'))
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2, 3]
    assert remote[4]['body'] == 'untouched'
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    assert not store.db.execute('SELECT 1 FROM task_pull_requests').fetchone()
    store.close()


def test_documented_residual_get_patch_window(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    write = adapter.write.side_effect
    lost = []
    def race(endpoint, method, payload):
        if method == 'PATCH':
            remote[2]['body'] = 'Human text after final GET'
            lost.append(remote[2]['body'])
        return write(endpoint, method, payload)
    adapter.write.side_effect = race
    service.prepare(project, store.get(project.repository, 1))
    assert lost == ['Human text after final GET']
    assert lost[0] not in remote[2]['body']
    # Successful read-back cannot detect this loss; this is deliberately NOT CAS.
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    store.close()


def test_false_success_allows_controlled_retry(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    write = adapter.write.side_effect
    adapter.write.side_effect = lambda endpoint, method, payload: {} if method == 'PATCH' else write(endpoint, method, payload)
    service.prepare(project, store.get(project.repository, 1))
    adapter.write.side_effect = write
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2]
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    store.close()


def test_recovery_never_overwrites_third_party(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    write = adapter.write.side_effect
    def crash(endpoint, method, payload):
        result = write(endpoint, method, payload)
        if method == 'PATCH':
            raise RuntimeError()
        return result
    adapter.write.side_effect = crash
    with pytest.raises(RuntimeError):
        service.prepare(project, store.get(project.repository, 1))
    remote[2]['body'] = 'Human edit after crash'
    adapter.write.side_effect = write
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2]
    assert remote[2]['body'] == 'Human edit after crash'
    assert store.get(project.repository, 1)['state'] == 'pending'
    store.close()


@pytest.mark.parametrize('allowed,identity,errors', [(True,2,False), (False,2,False), (True,3,False), (True,2,True)])
def test_issue_specific_permission(env, allowed, identity, errors):
    _, project, *_ = env
    adapter = Adapter()
    adapter.write = Mock(return_value=dict(data=dict(repository=dict(issue=dict(databaseId=identity, viewerCanUpdate=allowed))), errors=['denied'] if errors else []))
    assert bool(adapter.can_update_issue(project, 2, 2)) == (allowed and identity == 2 and not errors)


def test_upgrade_proposal_only_intent(env):
    import hashlib
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    parent = store.get(project.repository, 1)
    service.operation(parent, 'child:2', dict(child_id=2, body_hash=hashlib.sha256(remote[2]['body'].encode()).hexdigest(),
                                            proposed_hash='legacy', base_branch='feature/container'))
    service.block(parent, 'child_body_cas_unavailable', 2)
    service.prepare(project, parent)
    assert patches == [2]
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert not store.db.execute('SELECT 1 FROM container_blockers WHERE active=1').fetchone()
    store.close()


def test_unverified_service_body_cannot_authorize_execution(env):
    from dispatcher.execution_store import ExecutionStore
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    assert store.confirm(project.repository, 2, 1)
    write = adapter.write.side_effect
    def crash(endpoint, method, payload):
        result = write(endpoint, method, payload)
        if method == 'PATCH':
            raise RuntimeError()
        return result
    adapter.write.side_effect = crash
    with pytest.raises(RuntimeError):
        service.prepare(project, store.get(project.repository, 1))
    child = store.get(project.repository, 2)
    assert store.child_update_pending(child)
    assert ExecutionStore(store).reserve(child, 'feature/child', 1, store.guard(project.repository, 2)['observation_token']) is None
    adapter.write.side_effect = write
    service.prepare(project, store.get(project.repository, 1))
    assert not store.child_update_pending(store.get(project.repository, 2))
    store.close()


def test_null_remote_body_is_empty_markdown(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env, [''])
    remote[2]['body'] = None
    service.prepare(project, store.get(project.repository, 1))
    assert patches == [2]
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    store.close()


def test_historical_intent_cannot_exempt_user_edits(env):
    _, project, *_ = env
    store, adapter, service, remote, patches = controlled_setup(env)
    assert store.confirm(project.repository, 2, 1)
    service.prepare(project, store.get(project.repository, 1))
    child = json.loads(store.get(project.repository, 2)['snapshot'])
    target = child['body']
    child['body'] = 'Child\n'
    store.apply(project, [child], partial=True)
    assert store.get(project.repository, 2)['version'] == 2
    assert store.confirm(project.repository, 2, 2)
    child['body'] = target
    store.apply(project, [child], partial=True)
    assert store.get(project.repository, 2)['version'] == 3
    assert store.get(project.repository, 2)['approved_version'] is None
    store.close()
