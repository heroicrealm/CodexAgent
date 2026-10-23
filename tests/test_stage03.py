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
                state='open', labels=[project.task_label], updated_at='2026-10-24T00:00:00Z')
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
    stamp = datetime.now(timezone.utc) + timedelta(seconds=2)
    event = dict(id=event_id, event='labeled', label=dict(name=project.label(action).name),
                 actor=dict(login=actor, type='User'), created_at=stamp.isoformat())
    snap = json.loads(row['snapshot'])
    proof = dict(databaseId=1, title=snap['title'], body=snap['body'], author=dict(login=snap['author']), lastEditedAt=edited)
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
    with pytest.raises(RuntimeError, match='conflicts'):
        decide_cli(store, adapter, project, 1, 'reject' if action == 'approve' else 'approve')
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 3
    assert adapter.reconcile.call_count == 2


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
        with pytest.raises(GitHubError):
            commands(store, adapter, project, [item], before)
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
    with pytest.raises(GitHubError):
        commands(store, adapter, project, [item], before)
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
    assert len(deletes) == 3 and all('ready' not in url and 'other' not in url for url in deletes)
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
    assert not any(level == 'FAIL' for level, _ in checks)
    adapter.write.assert_not_called()
    assert config.source.read_bytes() == before
    assert not config.workspace.exists() and not config.logs.exists()


def test_adapter_command_proof_and_write_sanitization(setup):
    _, project, _ = setup
    adapter = Adapter()
    adapter.pages = Mock(return_value=[])
    adapter.write = Mock(return_value={'data': {'repository': {'issue': dict(databaseId=1, title='Task', body='', author=dict(login='alice'), lastEditedAt=None)}}})
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
