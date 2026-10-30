"""Stage 3 acceptance boundaries: no real GitHub or Codex processes."""
import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from dispatcher.cli import main
from dispatcher.config import COLORS, ConfigError, Label, load
from dispatcher.doctor import diagnose
from dispatcher.github import Adapter, GitHubError
from dispatcher.github.sync import SyncService
from dispatcher.initialization import fingerprint, initialize, ready, save_result
from dispatcher.queue import commands, decide_cli
from dispatcher.storage import Store


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(dict(paths=dict(workspace=str(tmp_path / 'work'), logs=str(tmp_path / 'logs')),
        projects=[dict(id='one', enabled=True, repository='owner/repo', security=dict(allowed_authors=['alice']))])))
    config = load(path)
    save_result(config, config.projects[0], True)
    config = load(path)
    store = Store(tmp_path / 'tasks.db')
    yield config, config.projects[0], store
    store.close()


def issue(project, **changes):
    item = dict(id=1, number=1, project_id=project.id, repository=project.repository,
                title='Task', body='Requirements', author='alice', target_branch='main', eligible=True,
                state='open', labels=[project.task_label], updated_at='2026-10-24T00:00:00Z', server_observed_at='2026-10-24T00:00:00+00:00')
    item.update(changes)
    return item


def adapter_for(project, items):
    adapter = Mock(spec=Adapter)
    adapter.fetch.return_value = items
    adapter.labels.return_value = []
    adapter.request.return_value = dict(permissions=dict(push=True))
    return adapter


def event_proof(project, store, action='approve', actor='alice', edited=None, event_id=7):
    row = store.get(project.repository, 1)
    stamp = datetime(2026, 10, 24, 0, 1, tzinfo=timezone.utc)
    event = dict(id=event_id, event='labeled', label=dict(name=project.label(action).name),
                 actor=dict(login=actor, type='User'), created_at=stamp.isoformat())
    snap = json.loads(row['snapshot'])
    proof = dict(databaseId=1, title=snap['title'], body=snap['body'], author=dict(login=snap['author']), lastEditedAt=edited, state='OPEN',
                 labels=dict(nodes=[dict(name=project.task_label), dict(name=project.label(action).name)], pageInfo=dict(hasNextPage=False)))
    return event, proof


@pytest.mark.parametrize('action,state', [('approve', 'pending'), ('reject', 'rejected')])
def test_cli_decisions_repeat_conflict_and_labels(setup, action, state):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    adapter = adapter_for(project, [item])
    assert decide_cli(store, adapter, project, 1, action) == ('applied', True)
    assert store.get(project.repository, 1)['state'] == state
    assert decide_cli(store, adapter, project, 1, action) == ('already_applied', True)
    if action == 'approve':
        with pytest.raises(RuntimeError, match='conflicts'):
            decide_cli(store, adapter, project, 1, 'reject')
    else:
        assert decide_cli(store, adapter, project, 1, 'approve') == ('applied', True)
        assert store.get(project.repository, 1)['state'] == 'pending'
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 3
    assert adapter.reconcile.call_count == (2 if action == 'approve' else 3)


def test_cli_stale_unavailable_and_sync_retry(setup):
    config, project, store = setup
    store.apply(project, [issue(project)])
    adapter = adapter_for(project, [issue(project, title='Edited')])
    with pytest.raises(RuntimeError):
        decide_cli(store, adapter, project, 1, 'approve')
    assert store.get(project.repository, 1)['state'] == 'discovered'
    adapter.fetch.side_effect = GitHubError('unavailable')
    with pytest.raises(GitHubError):
        decide_cli(store, adapter, project, 1, 'approve')
    assert store.get(project.repository, 1)['approved_version'] is None
    adapter.fetch.side_effect = None
    adapter.reconcile.side_effect = GitHubError('network')
    assert decide_cli(store, adapter, project, 1, 'approve') == ('applied', False)
    adapter.reconcile.side_effect = None
    assert not SyncService(config, store, adapter).poll()['errors']
    assert store.get(project.repository, 1)['state'] == 'pending'


def test_decision_race_first_writer_wins(setup, tmp_path):
    _, project, store = setup
    store.apply(project, [issue(project)])
    def decide(action):
        connection = Store(tmp_path / 'tasks.db')
        try:
            return action, connection.decide(project.repository, 1, 1, action)
        finally:
            connection.close()
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(decide, ['approve', 'reject']))
    assert sorted(result for _, result in results) == ['applied', 'conflict']
    winner = next(action for action, result in results if result == 'applied')
    assert store.get(project.repository, 1)['state'] == ('pending' if winner == 'approve' else 'rejected')


@pytest.mark.parametrize('action,state', [('approve', 'pending'), ('reject', 'rejected')])
@pytest.mark.parametrize('actor', ['alice', 'stranger'])
def test_github_actor_and_repeat_delivery(setup, action, state, actor):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    before = {1: dict(store.get(project.repository, 1))}
    event, proof = event_proof(project, store, action, actor)
    item['labels'].append(project.label(action).name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([event], proof)
    if actor == 'stranger':
        reports = commands(store, adapter, project, [item], before)
        assert reports[0]['reason'] == 'untrusted_actor'
        state = 'discovered'
    else:
        commands(store, adapter, project, [item], before)
    commands(store, adapter, project, [item], before)
    assert store.get(project.repository, 1)['state'] == state
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 1


@pytest.mark.parametrize('case', ['edited', 'same_second', 'bot', 'no_previous', 'changed', 'restored', 'renamed', 'ready_churn'])
def test_github_fail_closed_version_binding(setup, case):
    _, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    before = {1: dict(store.get(project.repository, 1))}
    event, proof = event_proof(project, store)
    if case == 'edited':
        proof['lastEditedAt'] = (datetime.fromisoformat(event['created_at']) + timedelta(seconds=1)).isoformat()
    if case == 'same_second':
        proof['lastEditedAt'] = event['created_at']
    if case == 'bot':
        event['actor']['type'] = 'Bot'
    if case == 'no_previous':
        before = {}
    if case == 'changed':
        item['title'] = 'New version'
        proof['title'] = item['title']
        store.apply(project, [item])
    if case == 'restored':
        store.apply(project, [])
        before = {1: dict(store.get(project.repository, 1))}
        store.apply(project, [item])
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    events = [event]
    if case in ('renamed', 'ready_churn'):
        events.append(dict(id=8, event='renamed' if case == 'renamed' else 'unlabeled',
                           label=dict(name=project.task_label), created_at=event['created_at']))
    adapter.command_events.return_value = (events, proof)
    reports = commands(store, adapter, project, [item], before)
    assert reports[0]['outcome'] == 'rejected'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_pending_is_indicator_and_no_executor(setup):
    config, project, store = setup
    item = issue(project, labels=[project.task_label, project.label('pending').name])
    adapter = adapter_for(project, [item])
    with patch('subprocess.Popen', side_effect=AssertionError('Executor forbidden')):
        assert SyncService(config, store, adapter).poll()['discovered'] == 1
    assert store.get(project.repository, 1)['state'] == 'discovered'
    adapter.command_events.assert_not_called()


@pytest.mark.parametrize('state', ['pending', 'rejected', 'discovered', 'revoked'])
def test_reconcile_preserves_ready_and_unrelated_and_retries(setup, state):
    _, project, store = setup
    store.apply(project, [issue(project)])
    row = dict(store.get(project.repository, 1), state=state)
    current = [project.task_label, 'other', project.label('approve').name,
               project.label('reject').name, project.label('running').name]
    adapter = Adapter()
    adapter.pages = Mock(return_value=[dict(name=name) for name in current])
    adapter.write = Mock()
    adapter.reconcile(project, row)
    calls = adapter.write.call_args_list
    deletes = [c.args[0] for c in calls if c.args[1] == 'DELETE']
    assert len(deletes) == 1 and all('ready' not in url and 'other' not in url for url in deletes)
    assert calls[-1].args[2] == dict(labels=[project.label(state).name])
    adapter.pages.return_value = [dict(name=name) for name in [project.task_label, 'other', project.label(state).name]]
    adapter.write.reset_mock()
    adapter.reconcile(project, row)
    adapter.write.assert_not_called()


def test_init_create_noop_update_failure_and_permissions(setup):
    config, project, _ = setup
    config.source.chmod(0o640)
    adapter = adapter_for(project, [])
    assert initialize(config, adapter=adapter) == [('one', True)]
    assert adapter.write.call_count == len(COLORS)
    assert ready(load(config.source).projects[0])
    assert config.source.stat().st_mode & 0o777 == 0o640
    adapter.labels.return_value = [dict(name=project.label(key).name) for key in COLORS]
    adapter.write.reset_mock()
    initialize(config, adapter=adapter)
    adapter.write.assert_not_called()
    initialize(config, update=True, adapter=adapter)
    assert adapter.write.call_count == len(COLORS)
    assert all(c.args[1] == 'PATCH' for c in adapter.write.call_args_list)
    adapter.labels.return_value = []
    adapter.write.side_effect = [None, GitHubError('SECRET')]
    assert initialize(config, adapter=adapter) == [('one', False)]
    failed = load(config.source).projects[0]
    assert failed.init_status == 'fail' and failed.init_fingerprint == ''
    assert failed.init_message and 'SECRET' not in config.source.read_text()
    assert not any(c.args[1] == 'DELETE' for c in adapter.write.call_args_list)


def test_atomic_yaml_conflict_and_multiple_project_results(setup):
    config, project, _ = setup
    data = yaml.safe_load(config.source.read_text())
    data['projects'].append(dict(id='two', enabled=True, repository='other/repo'))
    config.source.write_text(yaml.safe_dump(data))
    current = load(config.source)
    save_result(current, current.projects[1], True)
    save_result(config, project, False)
    projects = load(config.source).projects
    assert projects[0].init_status == 'fail' and ready(projects[1])
    data['projects'][0]['repository'] = 'changed/repo'
    config.source.write_text(yaml.safe_dump(data))
    before = config.source.read_bytes()
    with pytest.raises(ConfigError, match='changed'):
        save_result(config, project, True)
    assert config.source.read_bytes() == before


def test_fingerprint_and_label_validation(setup):
    config, project, _ = setup
    assert not ready(replace(project, repository='new/repo'))
    assert not ready(replace(project, labels={**project.labels, 'pending': Label('new-pending', 'FFFFFF')}))
    assert ready(replace(project, labels={**project.labels, 'pending': replace(project.label('pending'), color='FFFFFF', description='new')}))
    for labels in [dict(ready='SAME', approve='same'), dict(ready=dict(name='ready', color='#ffffff'))]:
        data = yaml.safe_load(config.source.read_text())
        data['projects'][0]['labels'] = labels
        config.source.write_text(yaml.safe_dump(data))
        with pytest.raises(ConfigError):
            load(config.source)


def test_init_isolates_projects(setup):
    config, project, _ = setup
    data = yaml.safe_load(config.source.read_text())
    data['projects'].append(dict(id='two', enabled=True, repository='other/repo'))
    config.source.write_text(yaml.safe_dump(data))
    config = load(config.source)
    adapter = adapter_for(project, [])
    adapter.request.side_effect = [GitHubError('SECRET'), {}]
    assert initialize(config, adapter=adapter) == [('one', False), ('two', True)]
    one, two = load(config.source).projects
    assert not ready(one) and ready(two)


def test_poll_skips_unprepared_and_continues_ready(setup, capsys):
    config, project, store = setup
    other = replace(project, id='two', repository='other/repo', init_status='')
    adapter = adapter_for(project, [issue(project)])
    result = SyncService(replace(config, projects=(other, project)), store, adapter).poll()
    assert result['skipped'] == ['two'] and result['discovered'] == 1
    assert adapter.fetch.call_count == 1
    assert 'two' in capsys.readouterr().err
    logs = (config.logs / 'dispatcher.jsonl').read_text()
    assert 'project_not_initialized' in logs


@pytest.mark.parametrize('args', [['start'], ['_serve'], ['show', 'one', '1'], ['approve', 'one', '1'], ['reject', 'one', '1']])
def test_commands_cannot_bypass_readiness(setup, args):
    config, project, _ = setup
    save_result(config, project, False)
    with patch('subprocess.Popen', side_effect=AssertionError('must not start')):
        assert main(['--config', str(config.source), *args]) == 1


def test_cli_commands_stopped(setup, capsys):
    config, project, _ = setup
    runtime = config.workspace / '.runtime'
    runtime.mkdir(parents=True)
    store = Store(runtime / 'tasks.sqlite3')
    store.apply(project, [issue(project)])
    store.close()
    adapter = adapter_for(project, [issue(project)])
    with patch('dispatcher.github.Adapter', return_value=adapter):
        for args in [['queue'], ['show', 'one', '1'], ['approve', 'one', '1']]:
            assert main(['--config', str(config.source), *args]) == 0
    assert 'applied' in capsys.readouterr().out


def test_doctor_read_only(setup):
    config, project, _ = setup
    adapter = adapter_for(project, [])
    adapter.labels.return_value = [dict(name=project.label(k).name) for k in COLORS]
    before = config.source.read_bytes()
    with patch('dispatcher.doctor.Adapter', return_value=adapter):
        checks = diagnose(config, which=lambda _: '/stub',
                          run=Mock(return_value=subprocess.CompletedProcess([], 0, stdout='')), environ={})
    assert not any(level == 'FAIL' and 'Descendant containment:' not in message for level, message in checks)
    assert any(level == 'WARN' and 'Descendant containment:' in message for level, message in checks)
    adapter.write.assert_not_called()
    assert config.source.read_bytes() == before
    assert not config.workspace.exists() and not config.logs.exists()


def test_adapter_command_proof_and_write_sanitization(setup):
    _, project, _ = setup
    adapter = Adapter()
    adapter.pages = Mock(return_value=[])
    adapter.write = Mock(return_value={'data': {'repository': {'issue': dict(databaseId=1, title='Task', body='', author=dict(login='alice'), lastEditedAt=None, state='OPEN', labels=dict(nodes=[], pageInfo=dict(hasNextPage=False)))}}})
    assert adapter.command_events(project, 1)[0] == []
    assert adapter.write.call_args.args[0] == 'graphql'
    for reply in [subprocess.CompletedProcess([], 1, stdout='SECRET'), subprocess.TimeoutExpired('SECRET', 30)]:
        runner = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
        with pytest.raises(GitHubError) as exc:
            Adapter(run=runner).write('endpoint', 'POST', {})
        assert 'SECRET' not in str(exc.value)


def test_github_pending_label_sync_failure_then_retry(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([event], proof)
    adapter.reconcile.side_effect = GitHubError('SECRET')
    assert SyncService(config, store, adapter).poll()['errors']
    assert store.get(project.repository, 1)['state'] == 'pending'
    adapter.reconcile.side_effect = None
    assert not SyncService(config, store, adapter).poll()['errors']
    assert adapter.reconcile.call_args.args[1]['state'] == 'pending'
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 1
    assert 'SECRET' not in (config.logs / 'dispatcher.jsonl').read_text()


def test_sqlite_cas_detects_concurrent_readiness_cycle(setup):
    _, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    previous = dict(store.get(project.repository, 1))
    store.apply(project, [])
    store.apply(project, [item])
    assert store.decide(project.repository, 1, 1, 'approve', expected_synced_at=previous['synced_at']) == 'unverified'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_atomic_save_replace_failure_keeps_yaml(setup):
    config, project, _ = setup
    original = config.source.read_bytes()
    with patch('dispatcher.initialization.os.replace', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            save_result(config, project, False)
    assert config.source.read_bytes() == original
    assert not list(config.source.parent.glob('.config.yaml*'))


def test_watch_skips_uninitialized_project(setup):
    import select
    import signal
    import sys
    config, project, _ = setup
    save_result(config, project, False)
    child = subprocess.Popen([sys.executable, '-m', 'dispatcher', '--config', str(config.source), 'poll', '--watch'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert json.loads(child.stdout.readline())['skipped'] == ['one']
        child.send_signal(signal.SIGTERM)
        _, error = child.communicate(timeout=5)
        assert child.returncode == 0 and 'run init' in error
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_cli_observation_is_captured_inside_refresh_transaction(setup):
    _, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    adapter = adapter_for(project, [item])
    original = store.decide
    def interleaved(*args, **kwargs):
        store.apply(project, [])
        store.apply(project, [item])
        return original(*args, **kwargs)
    with patch.object(store, 'decide', side_effect=interleaved):
        with pytest.raises(RuntimeError, match='conflicts'):
            decide_cli(store, adapter, project, 1, 'approve')
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_doctor_reports_missing_labels_stale_init_and_unknown_write(setup):
    config, project, _ = setup
    stale = replace(project, init_fingerprint='old')
    config = replace(config, projects=(stale,))
    adapter = adapter_for(project, [])
    adapter.request.return_value = {}
    with patch('dispatcher.doctor.Adapter', return_value=adapter):
        checks = diagnose(config, which=lambda _: '/stub',
                          run=Mock(return_value=subprocess.CompletedProcess([], 0, stdout='')), environ={})
    assert any(level == 'FAIL' and 'stale' in message for level, message in checks)
    assert any(level == 'FAIL' and 'labels missing' in message for level, message in checks)
    assert any(level == 'WARN' and 'write permission' in message for level, message in checks)
    adapter.write.assert_not_called()


def test_start_continues_with_one_ready_project(setup, capsys):
    config, project, _ = setup
    data = yaml.safe_load(config.source.read_text())
    data['projects'].append(dict(id='two', enabled=True, repository='other/repo'))
    config.source.write_text(yaml.safe_dump(data))
    with patch('dispatcher.cli.running', return_value={'pid': 42}):
        assert main(['--config', str(config.source), 'start']) == 0
    output = capsys.readouterr()
    assert 'two' in output.err and 'already running' in output.out


def test_current_label_without_proven_history_is_diagnosed(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    _, proof = event_proof(project, store)
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([], proof)
    result = SyncService(config, store, adapter).poll()
    assert result['errors'] and store.get(project.repository, 1)['state'] == 'discovered'
    assert 'command_verification_failed' in (config.logs / 'dispatcher.jsonl').read_text()


def test_concurrent_init_metadata_writers_preserve_both_projects(setup):
    config, project, _ = setup
    data = yaml.safe_load(config.source.read_text())
    data['projects'].append(dict(id='two', enabled=True, repository='other/repo'))
    config.source.write_text(yaml.safe_dump(data))
    config = load(config.source)
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(lambda p: save_result(config, p, True), config.projects))
    assert all(ready(p) for p in load(config.source).projects)


@pytest.mark.parametrize('local_now', ['1999-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00'])
@pytest.mark.parametrize('action', ['approve', 'reject'])
def test_server_fence_ignores_local_clock_skew(setup, local_now, action):
    config, project, store = setup
    item = issue(project)
    with patch('dispatcher.storage.now', return_value=local_now):
        store.apply(project, [item])
        event, proof = event_proof(project, store, action)
        item['labels'].append(project.label(action).name)
        adapter = adapter_for(project, [item])
        adapter.command_events.return_value = ([event], proof)
        result = SyncService(config, store, adapter).poll()
    assert result['commands'][0]['outcome'] == 'applied'
    assert store.get(project.repository, 1)['state'] == ('pending' if action == 'approve' else 'rejected')


def test_delayed_poll_uses_first_server_observation_of_version(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    item.update(server_observed_at='2026-10-24T06:00:00+00:00', updated_at='2026-10-24T00:01:00Z')
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([event], proof)
    result = SyncService(config, store, adapter).poll()
    assert result['commands'][0]['outcome'] == 'applied'
    assert store.guard(project.repository, 1)['server_at'] == '2026-10-24T00:00:00+00:00'


@pytest.mark.parametrize('boundary', ['observation', 'body_edit', 'title_edit', 'ready_restore'])
def test_same_server_second_is_ambiguous(setup, boundary):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    events = [event]
    if boundary == 'observation':
        event['created_at'] = item['server_observed_at']
    elif boundary == 'body_edit':
        proof['lastEditedAt'] = event['created_at']
    else:
        events.insert(0, dict(id=6, event='renamed' if boundary == 'title_edit' else 'labeled',
                             label=dict(name=project.task_label), created_at=event['created_at']))
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = (events, proof)
    receipt = SyncService(config, store, adapter).poll()['commands'][0]
    assert receipt['outcome'] == 'rejected' and receipt['reason'] == 'ambiguous_event_time'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_old_event_cannot_approve_new_version_and_new_event_can(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    old, proof = event_proof(project, store)
    item.update(body='Version two', server_observed_at='2026-10-24T00:03:00Z')
    item['labels'].append(project.label('approve').name)
    proof.update(body=item['body'], lastEditedAt='2026-10-24T00:02:00Z')
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([old], proof)
    service = SyncService(config, store, adapter)
    assert service.poll()['commands'][0]['reason'] == 'stale_version'
    assert service.poll()['commands'][0]['outcome'] == 'duplicate'
    assert store.get(project.repository, 1)['state'] == 'discovered'
    fresh = dict(old, id=9, created_at='2026-10-24T00:04:00Z')
    adapter.command_events.return_value = ([old, fresh], proof)
    assert service.poll()['commands'][0]['outcome'] == 'applied'
    assert store.get(project.repository, 1)['approved_version'] == 2
    assert service.poll()['commands'][0]['outcome'] == 'duplicate'
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 2


@pytest.mark.parametrize('churn', ['body_edit_revert', 'title_edit_revert', 'ready_remove_restore'])
def test_server_history_blocks_invisible_churn_despite_clock_skew(setup, churn):
    config, project, store = setup
    item = issue(project)
    with patch('dispatcher.storage.now', return_value='1990-01-01T00:00:00+00:00'):
        store.apply(project, [item])
    event, proof = event_proof(project, store)
    events = [event]
    if churn == 'body_edit_revert':
        proof['lastEditedAt'] = '2026-10-24T00:02:00Z'
    elif churn == 'title_edit_revert':
        events.extend([dict(id=8, event='renamed', created_at='2026-10-24T00:02:00Z'),
                       dict(id=9, event='renamed', created_at='2026-10-24T00:03:00Z')])
    else:
        events.extend([dict(id=8, event='unlabeled', label=dict(name=project.task_label), created_at='2026-10-24T00:02:00Z'),
                       dict(id=9, event='labeled', label=dict(name=project.task_label), created_at='2026-10-24T00:03:00Z')])
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = (events, proof)
    assert SyncService(config, store, adapter).poll()['commands'][0]['reason'] == 'ambiguous_event_time'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_missing_server_date_fails_closed_and_is_not_retroactively_approved(setup):
    config, project, store = setup
    item = issue(project, server_observed_at=None)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([event], proof)
    service = SyncService(config, store, adapter)
    assert service.poll()['commands'][0]['reason'] == 'missing_server_fence'
    item['server_observed_at'] = '2026-10-24T00:03:00Z'
    assert service.poll()['commands'][0]['outcome'] == 'duplicate'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_proof_failure_quarantine_survives_recovery_and_cleanup_failure(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    item['labels'].extend([project.label('approve').name, 'unrelated'])
    adapter = adapter_for(project, [item])
    adapter.command_events.side_effect = GitHubError('SECRET')
    adapter.reconcile.side_effect = GitHubError('SECRET')
    service = SyncService(config, store, adapter)
    first = service.poll()
    assert first['commands'][0]['reason'] == 'proof_unavailable'
    adapter.command_events.side_effect = None
    adapter.command_events.return_value = ([event], proof)
    second = service.poll()
    assert second['commands'][0]['outcome'] == 'quarantined'
    assert adapter.command_events.call_count == 1
    assert store.get(project.repository, 1)['state'] == 'discovered'
    logs = (config.logs / 'dispatcher.jsonl').read_text()
    assert logs.count('command_verification_failed') == 1 and 'SECRET' not in logs
    # Absence must be observed, so a stale label cannot become consent on recovery.
    item['labels'].remove(project.label('approve').name)
    item['server_observed_at'] = '2026-10-24T00:03:00Z'
    adapter.reconcile.side_effect = None
    service.poll()
    item['labels'].append(project.label('approve').name)
    fresh = dict(event, id=9, created_at='2026-10-24T00:04:00Z')
    adapter.command_events.return_value = ([event, fresh], proof)
    assert service.poll()['commands'][0]['outcome'] == 'applied'
    assert 'unrelated' in item['labels']


def test_one_rejected_issue_does_not_block_other_commands(setup):
    config, project, store = setup
    first = issue(project)
    second = issue(project, id=2, number=2)
    store.apply(project, [first, second])
    event, proof = event_proof(project, store)
    for item in (first, second):
        item['labels'].append(project.label('approve').name)
    adapter = adapter_for(project, [first, second])
    adapter.command_events.side_effect = [([dict(event, actor=dict(type='User', login='stranger'))], proof),
                                         ([dict(event, id=8)], dict(proof, databaseId=2))]
    result = SyncService(config, store, adapter).poll()
    assert [r['outcome'] for r in result['commands']] == ['rejected', 'applied']
    assert result['errors'][0]['reason'] == 'untrusted_actor' and 'retry' in result['errors'][0]
    assert store.get(project.repository, 2)['state'] == 'pending'
    records = [json.loads(line) for line in (config.logs / 'dispatcher.jsonl').read_text().splitlines()]
    assert records[0]['task'] == 1 and records[0]['reason'] == 'untrusted_actor'


def test_random_cas_token_detects_race_even_with_frozen_local_time(setup):
    _, project, store = setup
    item = issue(project)
    with patch('dispatcher.storage.now', return_value='2026-10-24T00:00:00Z'):
        observation = {}
        store.apply(project, [item], observation=observation)
        old = observation[1]
        store.apply(project, [])
        store.apply(project, [item])
        assert store.decide(project.repository, 1, 1, 'approve', expected_synced_at=old['synced_at'],
                            expected_observation=old['observation_token']) == 'unverified'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def http_reply(status, body, extra=''):
    return subprocess.CompletedProcess([], 0 if 200 <= status < 300 else 1,
        stdout=f'HTTP/2.0 {status} Response\n{extra}\n' + json.dumps(body), stderr='SECRET token=/private/secret')


@pytest.mark.parametrize('date,expected', [('Sat, 24 Oct 2026 00:00:00 GMT', '2026-10-24T00:00:00+00:00'),
                                          ('invalid SECRET', None)])
def test_adapter_captures_server_date_for_issue_page(setup, date, expected, monkeypatch):
    monkeypatch.setattr(Adapter, "repository_identity", lambda *args: 100)
    from test_github import raw
    _, project, _ = setup
    runner = Mock(return_value=http_reply(200, [raw(user=dict(login='alice'), labels=[dict(name=project.task_label)])], f'Date: {date}\n'))
    adapter = Adapter(run=runner)
    assert adapter.fetch(project)[0]['server_observed_at'] == expected
    assert 'Cache-Control: no-cache' in runner.call_args.args[0]


@pytest.mark.parametrize('status', [401, 403, 404, 422, 429, 500])
@pytest.mark.parametrize('write', [False, True])
def test_safe_http_status_metadata_without_raw_output(status, write):
    adapter = Adapter(run=Mock(return_value=http_reply(status, {'message': 'SECRET'})), attempts=1)
    with pytest.raises(GitHubError) as error:
        adapter.write('endpoint', 'POST', {}) if write else adapter.request('endpoint')
    assert error.value.status == status
    assert str(status) in error.value.safe_reason() and 'SECRET' not in error.value.safe_reason()


@pytest.mark.parametrize('operation', ['access', 'list', 'create', 'update'])
def test_init_persists_specific_safe_failure_then_clears_on_success(setup, operation):
    config, project, _ = setup
    adapter = adapter_for(project, [])
    error = GitHubError('SECRET /private/token', status=403)
    if operation == 'access':
        adapter.request.side_effect = error
    elif operation == 'list':
        adapter.labels.side_effect = error
    else:
        adapter.write.side_effect = error
        if operation == 'update':
            adapter.labels.return_value = [dict(name=project.label(key).name) for key in COLORS]
    reports = []
    assert initialize(config, update=operation == 'update', adapter=adapter, reports=reports) == [('one', False)]
    failed = load(config.source).projects[0]
    expected = {'access': 'Repository access check', 'list': 'Label listing', 'create': 'Label creation', 'update': 'Label update'}[operation]
    assert expected in failed.init_message and 'HTTP 403' in failed.init_message
    assert failed.init_message == reports[0]['message'] and reports[0]['saved']
    assert 'SECRET' not in config.source.read_text() and '/private' not in config.source.read_text()
    adapter.request.side_effect = adapter.labels.side_effect = adapter.write.side_effect = None
    assert initialize(config, adapter=adapter) == [('one', True)]
    assert load(config.source).projects[0].init_message == ''


def test_init_save_failure_visible_in_cli_and_logs_without_claiming_persistence(setup, capsys):
    import errno
    config, project, _ = setup
    adapter = adapter_for(project, [])
    before = config.source.read_bytes()
    with patch('dispatcher.initialization.Adapter', return_value=adapter), patch(
            'dispatcher.initialization.save_result', side_effect=PermissionError(errno.EACCES, 'SECRET', '/private/token')):
        assert main(['--config', str(config.source), 'init']) == 1
    output = capsys.readouterr().out
    assert 'permission denied' in output and 'YAML save not confirmed' in output
    assert 'failure saved in YAML' not in output
    assert 'SECRET' not in output and '/private' not in output
    assert config.source.read_bytes() == before
    logs = (config.logs / 'dispatcher.jsonl').read_text()
    assert 'init_save_failed' in logs and 'configuration_save_failed' in logs
    assert 'SECRET' not in logs


def test_init_save_failure_for_one_project_does_not_stop_others(setup):
    config, project, _ = setup
    data = yaml.safe_load(config.source.read_text())
    data['projects'].append(dict(id='two', enabled=True, repository='other/repo'))
    config.source.write_text(yaml.safe_dump(data))
    config = load(config.source)
    adapter = adapter_for(project, [])
    original = save_result
    def save(current, target, success, message=None):
        if target.id == 'one':
            raise OSError('SECRET')
        return original(current, target, success, message)
    reports = []
    with patch('dispatcher.initialization.save_result', side_effect=save):
        assert initialize(config, adapter=adapter, reports=reports) == [('one', False), ('two', True)]
    assert reports[0]['saved'] is False and reports[1]['saved'] is True
    assert ready(load(config.source).projects[1])


@pytest.mark.parametrize('flags', [dict(push=True), dict(maintain=True), dict(admin=True), {}])
def test_doctor_separates_read_access_reported_flags_and_unproven_label_rights(setup, capsys, flags):
    config, project, _ = setup
    adapter = adapter_for(project, [])
    adapter.request.return_value = dict(permissions=flags)
    adapter.labels.return_value = [dict(name=project.label(key).name) for key in COLORS]
    def run_diagnose(config):
        return diagnose(config, which=lambda _: '/stub',
                        run=Mock(return_value=subprocess.CompletedProcess([], 0, stdout='')), environ={})
    with patch('dispatcher.doctor.Adapter', return_value=adapter), patch('dispatcher.cli.diagnose', side_effect=run_diagnose):
        assert main(['--config', str(config.source), 'doctor']) == 0
    output = capsys.readouterr().out
    assert 'PASS: one: repository accessible (read access)' in output
    assert 'INFO: one: GitHub repository permission flags:' in output
    assert 'WARN: one: label write permission is not confirmed' in output
    assert 'init --update' in output
    assert not any(line.startswith('PASS:') and 'write permission' in line for line in output.splitlines())
    adapter.write.assert_not_called()
    assert not config.workspace.exists() and not config.logs.exists()


def test_unknown_command_hold_is_durable_and_enforced_inside_decision_transaction(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    store.quarantine(project.repository, 1, 'approve', item['server_observed_at'], None, 'proof_unavailable')
    connection = Store(config.source.parent / 'tasks.db')
    try:
        assert connection.decide(project.repository, 1, 1, 'approve', source='github', event_id=7) == 'unverified'
        assert connection.get(project.repository, 1)['state'] == 'discovered'
        # Explicit fresh CLI consent is a separate authorized channel.
        adapter = adapter_for(project, [item])
        assert decide_cli(connection, adapter, project, 1, 'approve') == ('applied', True)
    finally:
        connection.close()


def test_unknown_hold_cannot_be_cleared_by_stale_or_same_second_absence(setup):
    _, project, store = setup
    item = issue(project)
    first = {}
    store.apply(project, [item], observation=first)
    store.quarantine(project.repository, 1, 'approve', '2026-10-24T00:02:00Z', None, 'proof_unavailable')
    second = {}
    store.apply(project, [item], observation=second)
    store.clear_block(project.repository, 1, 'approve', '2026-10-24T00:03:00Z', first[1]['observation_token'])
    assert store.blocked(project.repository, 1, 'approve')
    store.clear_block(project.repository, 1, 'approve', '2026-10-24T00:02:00Z', second[1]['observation_token'])
    assert store.blocked(project.repository, 1, 'approve')
    store.clear_block(project.repository, 1, 'approve', '2026-10-24T00:03:00Z', second[1]['observation_token'])
    assert store.blocked(project.repository, 1, 'approve') is None


def test_unknown_hold_without_date_requires_two_dated_absences(setup):
    _, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    store.quarantine(project.repository, 1, 'approve', None, None, 'proof_unavailable')
    store.clear_block(project.repository, 1, 'approve', None)
    assert store.blocked(project.repository, 1, 'approve')
    store.clear_block(project.repository, 1, 'approve', '2026-10-24T00:02:00Z')
    assert store.blocked(project.repository, 1, 'approve')
    store.clear_block(project.repository, 1, 'approve', '2026-10-24T00:03:00Z')
    assert store.blocked(project.repository, 1, 'approve') is None


def test_history_is_read_after_graphql_content_proof(setup):
    _, project, store = setup
    store.apply(project, [issue(project)])
    _, proof = event_proof(project, store)
    sequence = []
    adapter = Adapter()
    adapter.write = Mock(side_effect=lambda *args: (sequence.append('content'), {'data': {'repository': {'issue': proof}}})[1])
    adapter.pages = Mock(side_effect=lambda *args: (sequence.append('events'), [])[1])
    adapter.command_events(project, 1)
    assert sequence == ['content', 'events']


def test_live_proof_of_ready_removal_blocks_command(setup):
    config, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    event, proof = event_proof(project, store)
    item['labels'].append(project.label('approve').name)
    proof['labels']['nodes'] = [dict(name=project.label('approve').name)]
    adapter = adapter_for(project, [item])
    adapter.command_events.return_value = ([event], proof)
    assert SyncService(config, store, adapter).poll()['commands'][0]['reason'] == 'eligibility_changed'
    assert store.get(project.repository, 1)['state'] == 'discovered'


def test_init_configuration_conflict_is_reported_without_raw_error(setup):
    config, project, _ = setup
    adapter = adapter_for(project, [])
    reports = []
    with patch('dispatcher.initialization.save_result', side_effect=ConfigError('SECRET /private/token')):
        assert initialize(config, adapter=adapter, reports=reports) == [('one', False)]
    assert reports[0]['reason'] == 'configuration_changed_or_invalid' and not reports[0]['saved']
    assert 'SECRET' not in str(reports) and '/private' not in str(reports)


def test_transport_server_date_does_not_rewrite_execution_snapshot_or_history(setup):
    _, project, store = setup
    item = issue(project)
    store.apply(project, [item])
    with store.db:
        store.db.execute("UPDATE tasks SET state='running',approved_version=version")
    original = dict(store.get(project.repository, 1))
    history_count = store.db.execute('SELECT count(*) FROM history').fetchone()[0]
    store.apply(project, [dict(item, server_observed_at='2026-10-24T00:03:00Z')])
    current = dict(store.get(project.repository, 1))
    assert current['snapshot'] == original['snapshot']
    assert current['observed'] == original['observed']
    assert current['approved_version'] == original['approved_version']
    assert store.db.execute('SELECT count(*) FROM history').fetchone()[0] == history_count


@pytest.mark.parametrize('failure', ['repository', 'labels'])
def test_doctor_read_failures_do_not_hide_limit_of_label_rights_check(setup, failure):
    config, project, _ = setup
    adapter = adapter_for(project, [])
    if failure == 'repository':
        adapter.request.side_effect = GitHubError('SECRET', status=403)
    else:
        adapter.labels.side_effect = GitHubError('SECRET', status=403)
    with patch('dispatcher.doctor.Adapter', return_value=adapter):
        checks = diagnose(config, which=lambda _: '/stub',
                          run=Mock(return_value=subprocess.CompletedProcess([], 0, stdout='')), environ={})
    assert any(level == 'WARN' and 'label write permission' in message and 'run init' in message for level, message in checks)
    assert any(level == 'FAIL' and 'cannot be verified' in message for level, message in checks)
    if failure == 'labels':
        assert ('PASS', 'one: repository accessible (read access)') in checks
        assert ('INFO', 'one: GitHub repository permission flags: push') in checks
    assert 'SECRET' not in str(checks)
    adapter.write.assert_not_called()
