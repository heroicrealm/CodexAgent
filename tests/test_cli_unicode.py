"""CLI display contracts; no real GitHub or Codex calls."""
import json
import os
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest
import yaml

from dispatcher.cli import _display_task, main
from dispatcher.config import load
from dispatcher.initialization import save_result
from dispatcher.runtime import prepare
from dispatcher.storage import Store


@pytest.fixture
def unicode_task(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(dict(
        paths=dict(workspace=str(tmp_path / 'work'), logs=str(tmp_path / 'logs')),
        projects=[dict(id='ru-project', enabled=True, repository='owner/repo',
                       security=dict(allowed_authors=['alice']))])), encoding='utf-8')
    config = load(path)
    save_result(config, config.projects[0], True)
    config = load(path)
    project = config.projects[0]
    item = dict(id=1, number=1, project_id=project.id, repository=project.repository,
                title='Написать тесты 🚀', body='Строка\n"кавычки" ' + r'\u041d C:\temp\file',
                author='alice', target_branch='main', eligible=True, state='open',
                labels=[project.task_label], updated_at='2026-10-24T00:00:00Z')
    store = Store(prepare(config) / 'tasks.sqlite3')
    store.apply(project, [item])
    before = dict(store.get(project.repository, 1))
    store.close()
    return config, item, before


@pytest.mark.parametrize('command', ['queue', 'show'])
def test_task_json_utf8_and_unchanged_storage(unicode_task, command):
    config, item, before = unicode_task
    args = [command, '--format', 'json'] if command == 'queue' else [command, item['project_id'], '1', '--format', 'json']
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, '-m', 'dispatcher', '--config', str(config.source), *args],
                            capture_output=True, env=env, check=False)
    assert result.returncode == 0 and result.stderr == b''
    output = result.stdout.decode('utf-8')
    assert 'Написать тесты 🚀' in output and item['project_id'] in output
    assert r'\u041d\u0430' not in output
    decoded = json.loads(output)
    row = decoded[0] if command == 'queue' else decoded
    for key, value in before.items():
        assert type(row[key]) is type(value)
        if key in ('snapshot', 'observed'):
            assert json.loads(row[key]) == json.loads(value) == item
            assert json.loads(row[key])['body'] == item['body']
        else:
            assert row[key] == value
    if command == 'queue':
        assert set(row) == set(before)
    else:
        extra = {'stop_requests', 'stop_outbox', 'configuration_errors', 'terminal_errors', 'cleanup_jobs', 'decisions', 'command_diagnostics', 'attempts', 'quota', 'result_publication', 'pull_requests', 'pr_resolutions', 'pr_sync_operations'}
        assert set(row) == set(before) | extra
        assert all(row[key] == [] for key in extra)
    store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        assert dict(store.get(item['repository'], 1)) == before
    finally:
        store.close()


@pytest.mark.parametrize('watch', [False, True])
@pytest.mark.parametrize('has_errors', [False, True])
def test_poll_unicode_schema_exit_and_watch(unicode_task, monkeypatch, capsys, watch, has_errors):
    config, item, _ = unicode_task
    project = 'Русский проект'
    payload = dict(discovered=1, updated=0, unchanged=0, revoked=0, skipped_untrusted=0,
                   errors=[dict(project=project, error='GitHub polling or storage failure')] if has_errors else [],
                   skipped=[project], commands=[dict(project=project, issue=1, version=1,
                       action='approve', outcome='applied', reason=None, retry=False)])
    handlers = {}
    monkeypatch.setattr('dispatcher.cli.signal.signal', lambda sig, handler: handlers.update({sig: handler}))
    calls = []

    def poll(_self):
        calls.append(1)
        if watch and len(calls) == 2:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return payload

    # Exercise the actual watch loop and flush=True without a wall-clock wait.
    from threading import Event
    stop = Event()
    stop.wait = Mock(return_value=False)
    monkeypatch.setattr('threading.Event', lambda: stop)
    monkeypatch.setattr('dispatcher.github.sync.SyncService.poll', poll)
    args = ['poll', '--watch'] if watch else ['poll']
    assert main(['--config', str(config.source), *args]) == (0 if watch else int(has_errors))
    captured = capsys.readouterr()
    assert captured.err == ''
    lines = captured.out.splitlines()
    assert len(lines) == (2 if watch else 1)
    for line in lines:
        assert 'Русский проект' in line
        assert r'\u0420' not in line
        assert json.loads(line.encode('utf-8').decode('utf-8')) == payload
    assert stop.is_set() == watch


def test_display_does_not_decode_arbitrary_escapes():
    source = dict(snapshot='legacy ' + r'\u041d', observed=None,
                  project_id=r'\u041d', other='Написать')
    assert _display_task(source) == source


@pytest.mark.parametrize('command', ['queue', 'show'])
def test_unicode_project_display_with_mock_config(unicode_task, monkeypatch, capsys, command):
    # Production YAML restricts directory identifiers to ASCII. Inject a
    # Unicode project to check the CLI display boundary without changing that rule.
    from dataclasses import replace
    from dispatcher.initialization import fingerprint

    config, item, _ = unicode_task
    project = replace(config.projects[0], id='Русский проект')
    project = replace(project, init_fingerprint=fingerprint(project))
    config = replace(config, projects=(project,))
    monkeypatch.setattr('dispatcher.cli.load', lambda _: config)
    store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
    store.apply(project, [dict(item, project_id=project.id)])
    store.db.execute('UPDATE tasks SET project_id=?', (project.id,))
    store.db.commit()
    store.close()
    args = [command, '--format', 'json'] if command == 'queue' else [command, project.id, '1', '--format', 'json']
    assert main(['--config', str(config.source), *args]) == 0
    output = capsys.readouterr().out
    assert 'Русский проект' in output and 'Написать тесты 🚀' in output
    decoded = json.loads(output)
    row = decoded[0] if command == 'queue' else decoded
    assert row['project_id'] == project.id
    assert json.loads(row['snapshot'])['project_id'] == project.id
