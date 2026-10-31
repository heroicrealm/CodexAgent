"""Commands arriving during asynchronous PR resolution delivery must survive."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from test_stage604 import monitored, poll
from test_stage603 import publication
from test_stage602 import store, PROJECT, item
from test_stage03 import event_proof
from dispatcher.github import Adapter, GitHubError
from dispatcher.queue import commands, decide_cli


def wire(monitored):
    store, api, _, _, comments, _, _ = monitored
    labels = ['ready', PROJECT.label('review').name]
    event, proof = event_proof(PROJECT, store)
    event.update(created_at='2026-10-24T00:21:00Z')
    proof['databaseId'] = 101
    proof['labels']['nodes'].append(dict(name=PROJECT.label('rejected').name))
    history = []
    api.command_events.side_effect = lambda *args: (deepcopy(history), deepcopy(proof))
    api.label_page = Mock(side_effect=lambda endpoint: [dict(name=n) for n in labels])
    original = api.write.side_effect
    def write(endpoint, method, payload):
        if '/labels' in endpoint:
            if method == 'DELETE':
                from urllib.parse import unquote
                labels.remove(unquote(endpoint.rsplit('/', 1)[1]))
            else:
                labels.extend(n for n in payload['labels'] if n not in labels)
            return {}
        return original(endpoint, method, payload)
    api.write.side_effect = write
    api.reconcile.side_effect = lambda project, row, **kwargs: Adapter.reconcile(api, project, row, **kwargs)
    def approve():
        labels.append(PROJECT.label('approve').name)
        history.append(deepcopy(event))
    def command(**changes):
        task = item(labels=list(labels), server_observed_at='2026-10-24T00:25:00Z', **changes)
        before = {1: dict(store.get(PROJECT.repository, 1))}
        observation = {}
        store.apply(PROJECT, [task], observation=observation)
        return commands(store, api, PROJECT, [task], before, observation)
    return labels, history, proof, approve, command


@pytest.mark.parametrize('timing', ['during_comment', 'after_comment', 'failed_comment'])
def test_first_approve_and_late_delivery(monitored, timing):
    store, api, _, _, comments, _, _ = monitored
    labels, history, proof, approve, command = wire(monitored)
    write = api.write.side_effect
    def delayed(endpoint, method, payload):
        if endpoint.endswith('/comments'):
            if timing == 'during_comment':
                assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
                # A concurrent poll has already exposed the committed state.
                api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
                assert PROJECT.label('rejected').name in labels
                approve()
            if timing == 'failed_comment':
                raise GitHubError()
        return write(endpoint, method, payload)
    api.write.side_effect = delayed
    assert poll(monitored) == ([1] if timing == 'failed_comment' else [])
    if timing != 'during_comment':
        approve()
    assert PROJECT.label('approve').name in labels
    old_iteration = store.get(PROJECT.repository, 1)['current_iteration_id']
    assert command()[0]['outcome'] == 'applied'
    current = store.get(PROJECT.repository, 1)
    assert current['state'] == 'pending' and current['current_iteration_id'] != old_iteration
    api.write.side_effect = write
    assert poll(monitored) == []
    api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
    assert labels == ['ready', PROJECT.label('pending').name]
    assert command() == []
    assert poll(monitored) == []
    assert len(comments) == 1
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 2
    assert store.db.execute("SELECT count(*) FROM decisions WHERE source='github' AND result='applied'").fetchone()[0] == 1
    assert store.get(PROJECT.repository, 1)['state'] == 'pending'


@pytest.mark.parametrize('bad,reason', [('old', 'ambiguous_event_time'), ('equal', 'ambiguous_event_time'),
    ('actor', 'untrusted_actor'), ('version', 'stale_version'), ('missing', 'no_event_proof')])
def test_negative_commands_remain_rejected(monitored, bad, reason):
    store, api, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    assert poll(monitored) == []
    approve()
    if bad == 'old': history[0]['created_at'] = '2026-10-24T00:19:00Z'
    if bad == 'equal': history[0]['created_at'] = '2026-10-24T00:20:00Z'
    if bad == 'actor': history[0]['actor']['login'] = 'intruder'
    if bad == 'missing': history.clear()
    if bad == 'version':
        result = command(body='Changed requirements')
    else:
        result = command()
    assert result[0]['reason'] == reason
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 1
    api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
    assert PROJECT.label('approve').name not in labels


def test_cli_reapprove(monitored):
    store, api, *_ = monitored
    assert poll(monitored) == []
    api.fetch.return_value = [item(server_observed_at='2026-10-24T00:25:00Z')]
    assert decide_cli(store, api, PROJECT, 1, 'approve') == ('applied', True)
    assert store.get(PROJECT.repository, 1)['state'] == 'pending'


def test_complete_cycle(monitored):
    from test_stage602 import move, pr
    from dispatcher.execution_store import ExecutionStore
    store, api, pull, remote, comments, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    assert poll(monitored) == []
    approve()
    assert command()[0]['outcome'] == 'applied'
    execution = ExecutionStore(store)
    task = store.get(PROJECT.repository, 1)
    attempt = execution.reserve(task, 'feature-2', 2, store.guard(PROJECT.repository, 1)['observation_token'])
    assert attempt and store.get(PROJECT.repository, 1)['state'] == 'running'
    assert move(store, 'review', 'second-review', evidence=replace(pr(store,
        observed_at='2026-10-24T00:30:00Z', event_at='2026-10-24T00:30:00Z',
        state_observed_at='2026-10-24T00:20:00Z'), pr_id=302, pr_number=3, head='feature-2')) == 'applied'
    execution.finish(attempt, 'review')
    pull.update(id=302, number=3, merged=True, merged_at='2026-10-24T00:35:00Z', closed_at='2026-10-24T00:35:00Z',
                merged_by=dict(login='merger'))
    pull['head']['ref'] = 'feature-2'
    api.response_date = '2026-10-24T00:40:00Z'
    assert poll(monitored) == []
    assert store.get(PROJECT.repository, 1)['state'] == 'done'
    assert remote['state_reason'] == 'completed'
    assert len(comments) == 2
    assert poll(monitored) == []
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 2


@pytest.mark.parametrize('state', ['running', 'done', 'cancelled'])
def test_terminal_or_running_never_restart(monitored, state):
    store, api, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    with store.db:
        store.db.execute('UPDATE tasks SET state=?', (state,))
    approve()
    assert command()[0]['reason'] == 'conflicting_decision'
    assert store.get(PROJECT.repository, 1)['state'] == state
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 1


def test_new_event_after_queue_snapshot_survives_reconcile(monitored):
    store, api, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    assert poll(monitored) == []
    approve()
    history[0]['actor']['login'] = 'intruder'
    assert command()[0]['reason'] == 'untrusted_actor'
    # A fresh event replaces the consumed command before label synchronization.
    labels.remove(PROJECT.label('approve').name)
    approve()
    history[-1].update(id=8, created_at='2026-10-24T00:26:00Z')
    api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
    assert PROJECT.label('approve').name in labels
    assert command()[0]['outcome'] == 'applied'


def test_proof_outage_stays_quarantined(monitored):
    store, api, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    assert poll(monitored) == []
    approve()
    read = api.command_events.side_effect
    api.command_events.side_effect = GitHubError()
    assert command()[0]['reason'] == 'proof_unavailable'
    api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
    assert PROJECT.label('approve').name not in labels
    api.command_events.side_effect = read
    approve()
    assert command()[0]['outcome'] == 'quarantined'
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'


def test_sync_race_between_commands_and_resolution(monitored, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from dispatcher.github.sync import SyncService
    store, api, *_ = monitored
    labels, history, proof, approve, command = wire(monitored)
    monkeypatch.setattr('dispatcher.github.sync.ready', lambda project: True)
    config = SimpleNamespace(projects=[PROJECT], logs=tmp_path / 'logs')
    api.fetch.side_effect = lambda project: [item(labels=list(labels), server_observed_at='2026-10-24T00:25:00Z')]
    write = api.write.side_effect
    def during_delivery(endpoint, method, payload):
        if endpoint.endswith('/comments'):
            api.reconcile(PROJECT, store.get(PROJECT.repository, 1), store=store)
            approve()
        return write(endpoint, method, payload)
    api.write.side_effect = during_delivery
    sync = SyncService(config, store, api)
    first = sync.poll()
    assert first['commands'] == [] and first['errors'] == []
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert PROJECT.label('approve').name in labels
    assert store.blocked(PROJECT.repository, 1, 'approve') is None
    second = sync.poll()
    assert second['errors'] == [] and second['commands'][0]['outcome'] == 'applied'
    assert store.get(PROJECT.repository, 1)['state'] == 'pending'
    assert sync.poll()['commands'] == []
    assert labels == ['ready', PROJECT.label('pending').name]
