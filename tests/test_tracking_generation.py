"""6.4.1.8: replay real snapshot/command boundaries across tracking cycles."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_stage602 import store, PROJECT, item, running
from test_stage603 import publication
from dispatcher.cleanup import CleanupService
from dispatcher.detachment import StopController
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import Adapter
from dispatcher.publisher import TaskResultPublisher
from dispatcher.queue import commands
from dispatcher.storage import Store


@pytest.fixture(autouse=True)
def cleanup_config(store, tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / '.runtime').mkdir(parents=True)
    store.audit_config = SimpleNamespace(workspace=workspace, logs=tmp_path / 'logs', projects=(PROJECT,))


def snapshot(minute, **changes):
    return item(server_observed_at=f'2026-10-24T00:{minute:02}:00Z', **changes)


def poll(store, minute, *, event_id=None, event_minute=1):
    task = snapshot(minute, labels=[PROJECT.task_label, PROJECT.label('approve').name])
    before = {1: dict(store.get(PROJECT.repository, 1))}
    observation = {}
    store.apply(PROJECT, [task], observation=observation)
    api = Mock(spec=Adapter)
    api.command_events.return_value = ([dict(id=event_id, event='labeled',
        label=dict(name=PROJECT.label('approve').name), actor=dict(login='alice', type='User'),
        created_at=f'2026-10-24T00:{event_minute:02}:00Z')], dict(databaseId=101,
        title=task['title'], body=task['body'], author=dict(login='alice'), lastEditedAt=None,
        state='OPEN', labels=dict(nodes=[dict(name=n) for n in task['labels']],
                                 pageInfo=dict(hasNextPage=False))))
    return commands(store, api, PROJECT, [task], before, observation)


def detach(store, minute):
    store.apply(PROJECT, [snapshot(minute, labels=[], eligible=False)])
    assert StopController(None, store, Mock()).finalize(store.get(PROJECT.repository, 1))
    assert store.get(PROJECT.repository, 1)['state'] == 'detached'
    CleanupService(store.audit_config, store).tick()
    assert not store.db.execute("SELECT 1 FROM cleanup_jobs WHERE state!='completed'").fetchone()


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('delivered', [False, True])
def test_late_approve_after_running_detach(store, restart, delivered):
    if delivered:
        assert poll(store, 2, event_id=7)[0]['outcome'] == 'applied'
        execution = ExecutionStore(store)
        attempt = execution.reserve(store.queue()[0], 'feature', 2,
                                    store.guard(PROJECT.repository, 1)['observation_token'])
    else:
        execution, attempt = running(store)
    assert attempt
    detach(store, 10)
    store.apply(PROJECT, [snapshot(11)])
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    active = Store(path) if restart else store
    try:
        for _ in range(2):
            assert poll(active, 12, event_id=7)[0]['outcome'] in ('duplicate', 'rejected')
            row = active.get(PROJECT.repository, 1)
            assert row['state'] == 'discovered' and row['approved_version'] is None
            assert row['current_iteration_id'] is None
            assert active.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 1
            assert active.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert poll(active, 14, event_id=8, event_minute=13)[0]['outcome'] == 'applied'
    finally:
        if restart:
            active.close()


@pytest.mark.parametrize('targeted', [False, True])
def test_two_cycles_ignore_old_removal_after_restart(store, targeted):
    for minute in (10, 20):
        detach(store, minute)
        store.apply(PROJECT, [snapshot(minute + 1)])
        assert store.confirm(PROJECT.repository, 1, 1)
    execution = ExecutionStore(store)
    assert execution.reserve(store.get(PROJECT.repository, 1), 'feature', 2,
                             store.guard(PROJECT.repository, 1)['observation_token'])
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    with_store = Store(path)
    try:
        before = dict(with_store.get(PROJECT.repository, 1))
        for minute in (19, 9, 19):  # reverse order and duplicate delivery
            if targeted:
                api = Mock(spec=Adapter)
                api.response_date = snapshot(minute)['server_observed_at']
                StopController(None, with_store, api).observe(PROJECT, before,
                    raw=dict(id=101, number=1, labels=[]))
            else:
                with_store.apply(PROJECT, [snapshot(minute, labels=[], eligible=False)])
            row = with_store.get(PROJECT.repository, 1)
            assert row['state'] == 'running'
            assert row['current_iteration_id'] == before['current_iteration_id']
            assert not with_store.stop_requested(row)
            assert with_store.db.execute('SELECT count(*) FROM stop_requests').fetchone()[0] == 2
            assert with_store.db.execute('SELECT count(*) FROM cleanup_jobs').fetchone()[0] == 2
    finally:
        with_store.close()


@pytest.mark.parametrize('state', ['done', 'cancelled', 'review_on_error'])
def test_terminal_state_never_revives_on_ready(store, state):
    with store.db:
        store.db.execute('UPDATE tasks SET state=?', (state,))
    store.apply(PROJECT, [snapshot(10, labels=[], eligible=False)])
    store.apply(PROJECT, [snapshot(11)])
    assert store.get(PROJECT.repository, 1)['state'] == state
    assert not store.confirm(PROJECT.repository, 1, 1)


def test_old_result_outbox_does_not_publish_in_new_iteration(store):
    execution, attempt = running(store)
    execution.finish(attempt, 'failed', 'internal_error', 'DISPATCHER')
    detach(store, 10)
    store.apply(PROJECT, [snapshot(11)])
    assert store.confirm(PROJECT.repository, 1, 1)
    api = Mock(spec=Adapter)
    TaskResultPublisher(execution, api, [PROJECT]).flush()
    api.pages.assert_not_called()
    api.write.assert_not_called()
    assert store.queue()[0]['state'] == 'pending'
    assert store.db.execute('SELECT published FROM result_outbox').fetchone()[0] == 0


def test_same_issue_number_in_other_project_is_isolated(store):
    other = replace(PROJECT, id='two', repository='other/repo')
    store.apply(other, [item(id=202, repository_id=22)])
    assert poll(store, 2, event_id=7)[0]['outcome'] == 'applied'
    assert store.decide(other.repository, 1, 1, 'approve', 'github', 7) == 'applied'
    detach(store, 10)
    assert store.get(other.repository, 1)['state'] == 'pending'
    assert not store.stop_requested(store.get(other.repository, 1))


def test_received_command_cannot_commit_after_cycle_and_restart(store):
    before = {}
    store.apply(PROJECT, [snapshot(2)], observation=before)
    received = before[1]
    detach(store, 10)
    store.apply(PROJECT, [snapshot(11)])
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    recovered = Store(path)
    try:
        assert recovered.decide(PROJECT.repository, 1, received['version'], 'approve',
            'github', 99, expected_observation=received['observation_token'],
            expected_synced_at=received['synced_at'], expected_state='discovered') == 'unverified'
        assert recovered.queue()[0]['state'] == 'discovered'
        assert not recovered.db.execute('SELECT * FROM task_iterations').fetchall()
    finally:
        recovered.close()


def test_old_pr_intent_cannot_publish_after_reattach(publication):
    publisher, execution, attempt, api, git, _ = publication
    store = execution.store
    # This stale-intent unit test has no filesystem result to publish.
    # Actual owned Git publication/cleanup is covered by test_detach_publication.
    execution.update(attempt, worktree=None, initial_sha=None, origin_url=None)
    detach(store, 10)
    store.apply(PROJECT, [snapshot(11)])
    assert store.confirm(PROJECT.repository, 1, 1)
    iteration = store.queue()[0]['current_iteration_id']
    publisher.publish(PROJECT, execution.get(attempt))
    git.publication_commit.assert_not_called()
    git.push.assert_not_called()
    api.write.assert_not_called()
    assert store.queue()[0]['state'] == 'pending'
    assert store.queue()[0]['current_iteration_id'] == iteration
