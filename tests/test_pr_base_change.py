"""Retargeting preserves canonical identity and guarded delivery."""
import json

import pytest

from test_stage603 import publication
from test_stage602 import store
from test_stage604 import monitored, merged, poll, PROJECT
from test_stage6434 import monitor_setup, status, codes
from test_stage04 import env
from dispatcher.container_monitor import ContainerMonitor
from dispatcher.storage import Store


@pytest.mark.parametrize('decision', ['open', 'merged', 'rejected'])
def test_retarget_resolution(monitored, decision):
    store, api, pr, remote, comments, writes, _ = monitored
    expected = pr['base']['ref']
    pr['base']['ref'] = 'different-base'
    if decision == 'open':
        pr.update(state='open', closed_at=None)
    elif decision == 'merged':
        merged(monitored)
    store.publication('resolution:301', store.get(PROJECT.repository, 1)['current_iteration_id'], 'retry', 'failed', retry=True)
    assert poll(monitored) == []
    assert store.get(PROJECT.repository, 1)['state'] == dict(open='review', merged='done', rejected='rejected')[decision]
    assert store.db.execute('SELECT base FROM task_pull_requests').fetchone()[0] == expected
    if decision == 'merged':
        metadata = json.loads(store.db.execute('SELECT metadata FROM pr_resolutions').fetchone()[0])
        assert metadata['merged_base'] == 'different-base'
        assert metadata['expected_base'] == expected
        assert remote['state_reason'] == 'completed'
        assert poll(monitored) == []
        assert len(comments) == 1
        assert store.db.execute("SELECT outcome FROM publication_operations WHERE operation_key='resolution:301'").fetchone()[0] == 'completed'


@pytest.mark.parametrize('change', ['id', 'head', 'repo', 'merge_sha', 'merge_time', 'head_sha'])
def test_retarget_invalid_merge(monitored, change):
    merged(monitored)
    pr = monitored[2]
    pr['base']['ref'] = 'different-base'
    if change == 'id': pr['id'] += 1
    if change == 'head': pr['head']['ref'] = 'foreign'
    if change == 'repo': pr['base']['repo']['id'] += 1
    if change == 'merge_sha': pr['merge_commit_sha'] = None
    if change == 'merge_time': pr['merged_at'] = None
    if change == 'head_sha': pr['head']['sha'] = 'invalid'
    assert poll(monitored) == [1]
    assert monitored[0].get(PROJECT.repository, 1)['state'] == 'review'
    assert not monitored[4]


def test_wrong_base_explicit_removal_restart(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    pr['base']['ref'] = 'main'
    monitor.poll(env[1])
    assert codes(store) == ['child_merge_wrong_base']
    assert not status(store)['ready_for_integration']
    assert 'main' in comments[0]['body'] and 'feature/container' in comments[0]['body']
    assert 'Sub-issues' in comments[0]['body']
    api.list_direct_children.return_value = []
    monitor.poll(env[1])
    assert not codes(store)
    assert status(store)['ready_for_integration']
    assert store.get(env[1].repository, 2)['state'] == 'done'
    relation = store.db.execute('SELECT * FROM sub_issue_relations').fetchone()
    assert relation['participation'] == 'ignored'
    assert relation['conflict_code'] == 'child_merge_wrong_base'
    generation = status(store)['generation']
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    store.close()
    store = Store(path)
    ContainerMonitor(store, api).poll(env[1])
    assert status(store)['generation'] == generation
    assert status(store)['ready_for_integration'] and not codes(store)
    assert len(comments) == 2
    store.close()


@pytest.mark.parametrize('change', ['id', 'base', 'state'])
def test_removal_requires_same_merge(env, change):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    pr['base']['ref'] = 'main'
    monitor.poll(env[1])
    api.list_direct_children.return_value = []
    if change == 'id': pr['id'] += 1
    if change == 'base': pr['base']['ref'] = 'other'
    if change == 'state': pr.update(state='open', merged=False, merged_at=None)
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    assert codes(store)
    store.close()


def test_wrong_base_removing_ready_label_is_not_resolution(env):
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    pr['base']['ref'] = 'main'
    children = api.list_direct_children.return_value
    children[0]['labels'] = []
    monitor.poll(env[1])
    assert codes(store) == ['child_merge_wrong_base']
    assert not status(store)['ready_for_integration']
    store.close()


def test_removed_wrong_base_child_does_not_hide_waiting_sibling(env):
    from test_stage04 import issue
    from test_stage6433 import raw
    store, api, monitor, row, pr, head, comments = monitor_setup(env)
    pr['base']['ref'] = 'main'
    monitor.poll(env[1])
    sibling = issue(env[1], id=3, number=3)
    store.apply(env[1], [sibling], partial=True)
    api.list_direct_children.return_value = [raw(env[1], sibling)]
    monitor.poll(env[1])
    assert not codes(store)
    assert not status(store)['ready_for_integration']
    assert status(store)['waiting_count'] == 1
    store.close()
