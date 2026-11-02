"""Container monitor: actual SQLite publication, canonical evidence, restart."""
import fcntl
import sqlite3
from copy import deepcopy

import pytest

from dispatcher.container_monitor import ContainerMonitor
from dispatcher.github import GitHubError
from dispatcher.storage import Store
from test_stage04 import env, issue
from test_stage6433 import setup, raw


def monitor_setup(env, state='done', with_pr=True):
    project = env[1]
    child = issue(project, id=2, number=2, body='```yaml\ndispatcher: {base_branch: feature/container}\n```')
    store, parent, api, service = setup(env, [child])
    service.prepare(project, store.get(project.repository, 1))
    row = store.get(project.repository, 1)
    assert row['state'] == 'awaiting_children'
    with store.db as db:
        db.execute("UPDATE tasks SET state=?,work_branch='feature/child',base_branch='feature/container' WHERE issue_id=2", (state,))
        if with_pr:
            db.execute("INSERT INTO task_pull_requests VALUES(20,2,?,20,'feature/child','feature/container','abc','closed',?)", (row['repository_id'], '2026-10-23T00:00:00Z'))
    pr = dict(id=20, number=20, head=dict(ref='feature/child', sha='abc', repo=dict(id=row['repository_id'])),
        base=dict(ref='feature/container', repo=dict(id=row['repository_id'])), state='closed', merged=True,
        merged_at='2026-10-23T00:00:00Z', merge_commit_sha='def')
    head = dict(ref='refs/heads/feature/container', object=dict(type='commit', sha='parent-merged-sha'))
    def request(endpoint):
        if '/pulls/' in endpoint:
            return deepcopy(pr)
        if '/git/ref/' in endpoint:
            return deepcopy(head)
        return raw(project, parent)
    api.request.side_effect = request
    comments = []
    def pages(endpoint):
        if '/pulls?' in endpoint:
            assert 'state=all' in endpoint
            return [deepcopy(pr)] if with_pr else []
        return comments[:]
    def write(endpoint, method, payload):
        assert endpoint.endswith('/comments')
        value = dict(id=len(comments) + 1, body=payload['body'])
        comments.append(value)
        return value
    api.pages.side_effect = pages
    api.write.side_effect = write
    return store, api, ContainerMonitor(store, api), row, pr, head, comments


def status(store):
    return store.db.execute('SELECT * FROM container_readiness').fetchone()


def codes(store):
    return [r[0] for r in store.db.execute('SELECT code FROM container_blockers WHERE active=1')]


def test_merged_deleted_head_restart_immutable_and_generations(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    monitor.poll(env[1])
    assert status(store)['ready_for_integration'] == 1
    assert status(store)['generation'] == 1
    assert store.get(env[1].repository, 1)['state'] == 'awaiting_children'
    assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    assert not comments
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    store.close()
    store = Store(path)
    monitor = ContainerMonitor(store, api)
    monitor.poll(env[1])
    assert status(store)['generation'] == 1
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE container_integration_snapshots SET snapshot='{}'")
    head['object']['sha'] = 'new-parent'
    monitor.poll(env[1])
    assert status(store)['generation'] == 2
    assert store.db.execute('SELECT count(*) FROM container_integration_snapshots').fetchone()[0] == 2
    store.close()


@pytest.mark.parametrize('state', ['discovered', 'pending', 'running', 'review', 'rejected', 'revoked'])
def test_waiting_is_not_failure(env, state):
    store, api, monitor, row, pr, head, comments = monitor_setup(env, state, False)
    monitor.poll(env[1])
    assert status(store)['waiting_count'] == 1
    assert not status(store)['ready_for_integration']
    assert not codes(store) and not comments
    store.close()


@pytest.mark.parametrize('change,expected', [
    ('unmerged', 'child_merge_unverified'), ('missing', 'child_pr_identity_conflict'),
    ('wrong_base', 'child_merge_wrong_base'), ('head', 'child_pr_identity_conflict'),
    ('repo', 'child_pr_identity_conflict'), ('id', 'child_pr_identity_conflict'),
    ('future', 'child_merge_unverified'), ('ambiguous', 'child_pr_identity_conflict')])
def test_merge_conflicts(env, change, expected):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    if change == 'unmerged':
        pr.update(merged=False, merged_at=None)
    elif change == 'wrong_base':
        pr['base']['ref'] = 'main'
        with store.db as db:
            db.execute("UPDATE task_pull_requests SET base='main'")
    elif change == 'head':
        pr['head']['ref'] = 'foreign'
    elif change == 'repo':
        pr['base']['repo']['id'] += 1
    elif change == 'id':
        pr['id'] += 1
    elif change == 'future':
        pr['merged_at'] = '2027-01-01T00:00:00Z'
    else:
        original = api.pages.side_effect
        api.pages.side_effect = lambda endpoint: ([] if change == 'missing' else [pr, dict(pr, id=21)]) if '/pulls?' in endpoint else original(endpoint)
    monitor.poll(env[1])
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    assert expected in codes(store)
    assert len(comments) == 1
    store.close()


@pytest.mark.parametrize('open_pr,operation', [(False, False), (True, False), (False, True)])
def test_cancelled(env, open_pr, operation):
    store, api, monitor, row, pr, head, comments = monitor_setup(env, 'cancelled', open_pr)
    if open_pr:
        pr.update(state='open', merged=False, merged_at=None)
    if operation:
        with store.db as db:
            db.execute("INSERT INTO container_operations VALUES('child-op',2,NULL,'branch',NULL,'hash','{}','intent','pending',0,NULL,'now')")
    monitor.poll(env[1])
    assert bool(status(store)['ready_for_integration']) == (not open_pr and not operation)
    if open_pr or operation:
        assert 'child_cancelled_with_open_pr' in codes(store)
    store.close()


def test_detached_recovery_outbox_and_removed_relation(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    pr.update(state='open', merged=False, merged_at=None)
    api.list_direct_children.return_value[0]['labels'] = []
    for _ in range(3):
        monitor.poll(env[1])
    assert codes(store) == ['child_detached_with_open_pr']
    assert len(comments) == 1 and 'child_detached_with_open_pr' in comments[0]['body']
    pr.update(state='closed')
    monitor.poll(env[1])
    assert not codes(store) and status(store)['ready_for_integration']
    assert len(comments) == 2
    api.list_direct_children.return_value = []
    monitor.poll(env[1])
    assert 'child_relation_removed' in codes(store)
    assert not status(store)['ready_for_integration']
    assert store.db.execute('SELECT count(*) FROM sub_issue_relations').fetchone()[0] == 1
    store.close()


def test_stale_network_and_local_race_invalidate(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    monitor.poll(env[1])
    collect = monitor.collect
    calls = 0
    def churn(project, current):
        nonlocal calls
        result = collect(project, current)
        calls += 1
        if calls % 2 == 0:
            result['parent_head'] = 'racing-head'
        return result
    monitor.collect = churn
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    assert 'child_snapshot_stale' in codes(store)
    monitor.collect = collect
    monitor.poll(env[1])
    assert status(store)['generation'] == 2
    api.list_direct_children.side_effect = GitHubError(kind='network')
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    api.list_direct_children.side_effect = None
    def local_race(project, current):
        result = collect(project, current)
        with store.db as db:
            db.execute('UPDATE tasks SET version=version+1 WHERE issue_id=2')
        return result
    monitor.collect = local_race
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    store.close()


def test_parallel_lock(env):
    store, api, monitor, row, *_ = monitor_setup(env)
    from pathlib import Path
    database = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    with (database.parent / f'container-monitor-{row["issue_id"]}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monitor.poll(env[1])
        assert status(store) is None
    monitor.poll(env[1])
    assert status(store)['generation'] == 1
    store.close()


@pytest.mark.parametrize('count', [0, 1, 101])
def test_empty_and_ignored_children(env, count):
    store, api, monitor, row, *_ = monitor_setup(env, 'cancelled', False)
    api.list_direct_children.return_value = [dict(raw(env[1], issue(env[1], id=100+i, number=100+i)), labels=[]) for i in range(count)]
    with store.db as db:
        db.execute('DELETE FROM sub_issue_relations')
    monitor.poll(env[1])
    assert status(store)['ready_for_integration']
    assert status(store)['children_count'] == 0
    assert not store.db.execute('SELECT 1 FROM sub_issue_relations').fetchone()
    store.close()


def test_new_child_invalidates_and_nested_container_is_direct(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    monitor.poll(env[1])
    child = issue(env[1], id=3, number=3)
    store.apply(env[1], [child], partial=True)
    api.list_direct_children.return_value.append(raw(env[1], child))
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    assert status(store)['children_count'] == 2
    assert status(store)['waiting_count'] == 1
    # A child-container is verified using its own terminal state/PR only.
    with store.db as db:
        db.execute("UPDATE tasks SET issue_type='container' WHERE issue_id=2")
        db.execute("UPDATE tasks SET state='cancelled' WHERE issue_id=3")
    monitor.poll(env[1])
    assert status(store)['ready_for_integration']
    assert {call.args[1]['issue_id'] for call in api.list_direct_children.call_args_list} == {1}
    store.close()


def test_done_without_pr_and_bad_github_invalidates(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env, with_pr=False)
    monitor.poll(env[1])
    assert 'child_merge_unverified' in codes(store)
    assert not status(store)['ready_for_integration']
    api.request.side_effect = lambda endpoint: dict(id='invalid')
    monitor.poll(env[1])
    assert 'child_snapshot_stale' in codes(store)
    store.close()


def test_615_migration_atomic_upgrade(env):
    from dispatcher.storage.migrations import upgrade615
    store, *_ = monitor_setup(env)
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    with store.db as db:
        db.execute('DROP TABLE container_integration_snapshots')
        db.execute('DROP TABLE container_readiness')
        db.execute('DELETE FROM schema_migrations WHERE version=615')
    store.db.execute('BEGIN IMMEDIATE')
    upgrade615(store.db)
    store.db.rollback()
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='container_readiness'").fetchone()
    store.close()
    store = Store(path)
    assert store.db.execute('SELECT 1 FROM schema_migrations WHERE version=615').fetchone()
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    store.close()


def test_cli_readiness(env):
    from dispatcher.cli import _container_display
    from dispatcher.rendering import render_show, render_queue
    from io import StringIO
    store, api, monitor, row, *_ = monitor_setup(env)
    monitor.poll(env[1])
    value = _container_display(store, row)
    assert value['ready_for_integration'] == 1 and value['generation'] == 1
    shown, queued = StringIO(), StringIO()
    render_show(value, stream=shown)
    render_queue([value], {row['repository']: env[1].id}, stream=queued)
    assert 'Ready for integration: 1' in shown.getvalue()
    assert 'generation=1' in queued.getvalue()
    store.close()


def test_no_network_in_writer_lock_and_lost_comment_reply(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    request = api.request.side_effect
    def checked_request(endpoint):
        assert not store.db.in_transaction
        return request(endpoint)
    api.request.side_effect = checked_request
    write = api.write.side_effect
    first = True
    def uncertain_write(endpoint, method, payload):
        nonlocal first
        result = write(endpoint, method, payload)
        if first:
            first = False
            raise GitHubError(kind='network')
        return result
    api.write.side_effect = uncertain_write
    pr.update(merged=False, merged_at=None)
    monitor.poll(env[1])
    monitor.poll(env[1])
    assert len(comments) == 1
    assert store.db.execute('SELECT delivered FROM container_diagnostic_outbox').fetchone()[0] == 1
    store.close()


@pytest.mark.parametrize('detached', [False, True])
def test_open_pr_without_canonical_is_never_ignored(env, detached):
    store, api, monitor, row, pr, head, comments = monitor_setup(env, 'cancelled')
    with store.db as db:
        db.execute('DELETE FROM task_pull_requests')
    pr.update(state='open', merged=False, merged_at=None)
    if detached:
        api.list_direct_children.return_value[0]['labels'] = []
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    assert ('child_detached_with_open_pr' if detached else 'child_cancelled_with_open_pr') in codes(store)
    assert not store.db.execute('SELECT 1 FROM task_pull_requests').fetchone()
    store.close()


@pytest.mark.parametrize('count', [0, 1, 101])
def test_full_paginated_monitor(env, count):
    from dispatcher.github import Adapter
    store, api, monitor, row, pr, head, comments = monitor_setup(env, 'cancelled', False)
    children = [issue(env[1], id=i, number=i, body='```yaml\ndispatcher: {base_branch: feature/container}\n```') for i in range(2, count + 2)]
    if children:
        store.apply(env[1], children, partial=True)
    with store.db as db:
        db.execute("UPDATE tasks SET state='cancelled' WHERE issue_id!=1")
        if not count:
            db.execute('DELETE FROM sub_issue_relations')
    root = api.request.side_effect('parent')
    root['sub_issues_summary'] = dict(total=count)
    pages = []
    def request(endpoint):
        assert not store.db.in_transaction
        if '/sub_issues?' in endpoint:
            page = int(endpoint.split('page=')[-1])
            pages.append(page)
            return [raw(env[1], c) for c in children[(page-1)*100:page*100]]
        if '/git/ref/' in endpoint:
            return head
        if '/pulls?' in endpoint:
            return []
        return root
    api.request.side_effect = request
    api.pages.side_effect = lambda endpoint: Adapter.pages(api, endpoint) if '/sub_issues' in endpoint or '/pulls?' in endpoint else comments[:]
    api.list_direct_children.side_effect = lambda project, parent: Adapter.list_direct_children(api, project, parent)
    monitor.poll(env[1])
    assert status(store)['ready_for_integration']
    assert status(store)['children_count'] == count
    assert status(store)['completed_count'] == count
    assert (2 in pages) == (count > 100)
    store.close()


def test_project_fetch_failure_invalidates_ready(env, monkeypatch):
    from dispatcher.github.sync import SyncService
    store, api, monitor, row, *_ = monitor_setup(env)
    monitor.poll(env[1])
    assert status(store)['ready_for_integration']
    monkeypatch.setattr('dispatcher.github.sync.ready', lambda project: True)
    api.fetch.side_effect = GitHubError(kind='network')
    api.request.side_effect = GitHubError(kind='network')
    result = SyncService(env[0], store, api).poll()
    assert result['errors']
    assert not status(store)['ready_for_integration']
    assert 'child_snapshot_stale' in codes(store)
    store.close()


@pytest.mark.parametrize('change', ['timestamp', 'label', 'comment'])
def test_parent_transport_churn_recovers_durable_blocker(env, change):
    store, api, monitor, row, *rest = monitor_setup(env)
    comments = rest[-1]
    monitor.invalidate(row, 'child_snapshot_stale')
    request = api.request.side_effect
    ticks = 0
    def changed(endpoint):
        nonlocal ticks
        value = request(endpoint)
        if '/issues/' in endpoint:
            ticks += 1
            value['updated_at'] = f'transport-{ticks}'
            if change == 'label':
                value['labels'].append(dict(name='unrelated', color=str(ticks), description='metadata'))
            if change == 'comment':
                value['comments'] = ticks
        return value
    api.request.side_effect = changed
    monitor.poll(env[1])
    assert not codes(store)
    assert status(store)['generation'] == 1
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    store.close()
    store = Store(path)
    ContainerMonitor(store, api).poll(env[1])
    assert status(store)['generation'] == 1
    assert not codes(store) and not comments
    store.close()


@pytest.mark.parametrize('change', ['body', 'title', 'author', 'id', 'number', 'repository', 'closed', 'ready', 'emergency', 'reject'])
def test_parent_semantic_changes_fail_closed(env, change, caplog):
    store, api, monitor, row, *_ = monitor_setup(env)
    request = api.request.side_effect
    def changed(endpoint):
        value = request(endpoint)
        if '/issues/' in endpoint:
            if change in ('body', 'title'):
                value[change] += 'changed'
            elif change == 'author':
                value['user']['login'] = 'intruder'
            elif change in ('id', 'number'):
                value[change] += 1
            elif change == 'repository':
                value['repository_url'] += 'other'
            elif change == 'closed':
                value['state'] = 'closed'
            elif change == 'ready':
                value['labels'] = []
            else:
                value['labels'].append(dict(name=env[1].label('emergency_stop' if change == 'emergency' else 'reject').name))
        return value
    api.request.side_effect = changed
    monitor.poll(env[1])
    assert codes(store) == ['child_snapshot_stale']
    assert 'parent_mismatch' in caplog.text
    store.close()


@pytest.mark.parametrize('change', ['iteration', 'branch', 'guard'])
def test_parent_local_race_before_commit(env, change, caplog):
    store, api, monitor, row, *_ = monitor_setup(env)
    collect = monitor.collect
    def racing(project, current):
        result = collect(project, current)
        with store.db as db:
            if change == 'iteration':
                db.execute('UPDATE tasks SET current_iteration_id=NULL WHERE issue_id=1')
            elif change == 'branch':
                db.execute("UPDATE tasks SET work_branch='other' WHERE issue_id=1")
            else:
                db.execute("UPDATE command_guards SET observation_token='raced' WHERE number=1")
        return result
    monitor.collect = racing
    monitor.poll(env[1])
    assert codes(store) == ['child_snapshot_stale']
    assert 'local_race' in caplog.text
    store.close()


def test_two_closed_children_and_third_non_ready(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env, with_pr=False)
    second = issue(env[1], id=3, number=3, body='```yaml\ndispatcher: {base_branch: feature/container}\n```')
    ignored = dict(raw(env[1], issue(env[1], id=4, number=4)), labels=[])
    store.apply(env[1], [second], partial=True)
    with store.db as db:
        db.execute("UPDATE tasks SET state='done' WHERE issue_id=3")
    api.list_direct_children.return_value[0]['state'] = 'closed'
    api.list_direct_children.return_value.extend([dict(raw(env[1], second), state='closed'), ignored])
    request = api.request.side_effect
    def changed(endpoint):
        value = request(endpoint)
        if '/issues/' in endpoint:
            value['updated_at'] = 'later'
        return value
    api.request.side_effect = changed
    monitor.poll(env[1])
    assert status(store)['children_count'] == 2
    assert codes(store) == ['child_merge_unverified', 'child_merge_unverified']
    assert not status(store)['ready_for_integration']
    monitor.poll(env[1])
    assert len(comments) == 1 and status(store)['generation'] == 0
    store.close()


def test_delivered_stale_parent_blocker_restart_recovery(env):
    store, api, monitor, row, *rest = monitor_setup(env)
    comments = rest[-1]
    request = api.request.side_effect
    def stale(endpoint):
        value = request(endpoint)
        if '/issues/' in endpoint:
            value['title'] += 'unapproved'
        return value
    api.request.side_effect = stale
    monitor.poll(env[1])
    assert codes(store) == ['child_snapshot_stale'] and len(comments) == 1
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    store.close()
    store = Store(path)
    monitor = ContainerMonitor(store, api)
    monitor.poll(env[1])
    assert len(comments) == 1
    api.request.side_effect = request
    monitor.poll(env[1])
    assert not codes(store) and status(store)['generation'] == 1
    delivered = len(comments)
    monitor.poll(env[1])
    assert len(comments) == delivered and status(store)['generation'] == 1
    store.close()
