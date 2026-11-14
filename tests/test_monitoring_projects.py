"""V2.4.3 local-only CLI contracts; all remote checks are stubs."""
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import socket
from types import SimpleNamespace
import pytest
import yaml
from rich.console import Console
from dispatcher.config import Config, Project, load_config, resolve_config
from dispatcher.db_guard import maintain, connection
from dispatcher.locking import InstanceLock
from dispatcher.models import IssueObservation
from dispatcher.storage import Storage, SchemaError
from dispatcher.monitoring import (snapshot, queue_db, status, show, daemon, publish_metadata, final_result)
from dispatcher.monitoring_output import render
from dispatcher.projects import edit, mutate, validate_remote
from dispatcher.purge import plan


@pytest.fixture
def instance(tmp_path):
    config = Config(tmp_path / 'workspace', tmp_path / 'runtime' / 'db',
                    (Project('p', 'owner/repo'), Project('empty', 'owner/empty', enabled=False)))
    config.database.parent.mkdir(mode=0o700)
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    settings = tmp_path / 'config.yaml'
    settings.write_text(yaml.safe_dump({'custom': {'keep': 42}, 'dispatcher': {
        'root_path': str(tmp_path), 'workspace': 'workspace', 'database': 'runtime/db'},
        'projects': [dict(id=p.id, repository=p.repository, enabled=p.enabled) for p in config.projects]}))
    return config, store, settings


def register(store, number=1, state='discovered', timestamp='2026-01-01T00:00:00+00:00', closed=False):
    store.register_issue(IssueObservation(number, 'owner/repo', number, 'Long title ' * 30), 'p')
    with store.transaction() as db:
        db.execute('UPDATE issues SET state=?, github_state=?,updated_at=? WHERE issue_id=?',
                   (state, 'closed' if closed else 'open', '2099-01-01', number))
        db.execute('INSERT INTO state_history(issue_id,old_state,new_state,reason,created_at) VALUES(?,?,?,?,?)',
                   (number, 'pending' if state != 'pending' else 'discovered', state, 'test', timestamp))


def files(root):
    return {str(p): (p.read_bytes(), p.stat().st_mode) for p in root.rglob('*') if p.is_file()}


def test_resolver(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', 'relative')
    assert resolve_config() == tmp_path / '.config/codex-dispatcher/config.yaml'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert resolve_config() == tmp_path / 'xdg/codex-dispatcher/config.yaml'
    assert resolve_config(tmp_path / 'explicit') == tmp_path / 'explicit'


def test_root_cwd_and_secure_config(instance, monkeypatch, tmp_path):
    config, _, settings = instance
    monkeypatch.chdir('/')
    loaded = load_config(settings)
    assert loaded.workspace == config.workspace
    assert loaded.database == config.database
    mutate(settings, 'disable', 'p')
    assert settings.stat().st_mode & 0o077 == 0
    assert yaml.safe_load(settings.read_text())['custom'] == {'keep': 42}
    assert load_config(settings).projects[0].enabled is False
    mutate(settings, 'disable', 'p')
    mutate(settings, 'enable', 'p')
    assert load_config(settings).projects[0].enabled


@pytest.mark.parametrize('content', ['', '[]', 'dispatcher: []', 'dispatcher: {}',
    'dispatcher: {root_path: relative}', 'dispatcher: {root_path: /tmp, debug: []}',
    'dispatcher: {root_path: /tmp, workspace: []}', 'dispatcher: {root_path: /tmp}\nprojects: {}'])
def test_invalid_configs(tmp_path, content):
    path = tmp_path / 'bad.yaml'
    path.write_text(content)
    with pytest.raises((ValueError, TypeError)):
        load_config(path)


def test_readonly_recovery_and_status(instance, monkeypatch, tmp_path):
    config, store, _ = instance
    register(store)
    with connection(config.database) as db:
        db.execute('UPDATE dispatcher_metadata SET recovery_required=1')
    before = files(tmp_path)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('external call'))
    result = status(config)
    assert result['database'] == 'OK' and result['recovery'] == 'BLOCKED'
    assert result['projects'][0]['open'] == 1 and result['projects'][1]['open'] == 0
    with snapshot(config) as (db, _):
        assert queue_db(db, config)[0]['issue_number'] == 1
        with pytest.raises(Exception, match='readonly'):
            db.execute('DELETE FROM issues')
    assert show(config, 'p', 1)['state'] == 'discovered'
    assert files(tmp_path) == before


def test_status_crash_and_manual_slot(instance):
    config, store, _ = instance
    register(store, state='running')
    result = status(config)
    assert result['daemon']['state'] == 'STOPPED'
    assert not result['executing']['verified_live']
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='manual-running'")
    assert status(config)['executing'] is None


def test_daemon_verified_and_stale(instance):
    config, _, _ = instance
    assert daemon(config)['state'] == 'STOPPED'
    with InstanceLock(config):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(config.workspace / '.control.sock'))
        server.close()
        publish_metadata(config, 'last', 'next')
        assert daemon(config)['state'] == 'RUNNING'
        assert status(config)['last_poll'] == 'last'
        path = config.workspace / '.monitor.json'
        value = json.loads(path.read_text())
        value['updated_at'] = '2000-01-01T00:00:00+00:00'
        path.write_text(json.dumps(value))
        assert daemon(config)['state'] == 'UNKNOWN'
        assert status(config)['last_poll'] is None
    (config.workspace / '.control.sock').unlink()
    assert daemon(config)['state'] == 'STOPPED'


def test_daemon_pid_reuse(instance):
    config, _, _ = instance
    with InstanceLock(config):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(config.workspace / '.control.sock'))
        server.close()
        publish_metadata(config)
        path = config.workspace / '.monitor.json'
        value = json.loads(path.read_text())
        value['process_token'] = 'wrong'
        path.write_text(json.dumps(value))
        assert daemon(config)['state'] == 'UNKNOWN'


def test_queue_order_age_and_archive(instance):
    config, store, _ = instance
    states = ['done', 'discovered', 'awaiting-children', 'pending', 'running', 'preparing',
              'manual-running', 'need-fix', 'rejected', 'review', 'error', 'cancelled']
    for n, state in enumerate(states, 1):
        register(store, n, state)
    register(store, 20, 'error', '2025-01-01T00:00:00+00:00')
    register(store, 21, 'error', closed=True)
    with snapshot(config) as (db, _):
        rows = queue_db(db, config)
        assert [r['issue_number'] for r in rows] == [20, 11, 10, 9, 8, 7, 5, 6, 4, 3, 2]
        assert rows[0]['state_since'].startswith('2025')
        assert rows[0]['age_seconds'] > 86400
        assert len(queue_db(db, config, all_records=True)) == 14
        assert len(queue_db(db, config, 'p', 'review')) == 1
        assert queue_db(db, config, 'empty') == []
        with pytest.raises(ValueError, match='Unknown project'):
            queue_db(db, config, 'unknown')


def test_age_missing_not_updated_at(instance):
    config, store, _ = instance
    register(store)
    with store.transaction() as db:
        db.execute('DELETE FROM state_history')
    with snapshot(config) as (db, _):
        assert queue_db(db, config)[0]['age_seconds'] is None


def test_show_blocking_children_and_closed(instance):
    config, store, _ = instance
    for n, state in enumerate(('awaiting-children', 'pending', 'done', 'cancelled'), 1):
        register(store, n, state, closed=n == 1)
    with store.transaction() as db:
        db.executemany('INSERT INTO issue_relations VALUES(1,?)', [(2,), (3,), (4,)])
    value = show(config, 'p', 1)
    assert [c['issue_number'] for c in value['blocking_children']] == [2]
    assert value['result'] is None
    with pytest.raises(ValueError, match='Issue not found'):
        show(config, 'p', 999)


def test_final_stdout_and_jsonl(tmp_path):
    path = tmp_path / 'result'
    assert final_result(path) is None
    path.write_text('Final message\nwith details')
    assert final_result(path) == 'Final message\nwith details'
    path.write_text('\n'.join(json.dumps(e) for e in [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'earlier'}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'final'}},
        {'type': 'turn.completed'}]))
    assert final_result(path) == 'final'
    path.write_text('{"type":"turn.failed"}')
    assert final_result(path) is None


@pytest.mark.parametrize('width', [80, 120])
def test_human_width_and_json(instance, width, capsys):
    config, store, _ = instance
    register(store, state='review')
    with snapshot(config) as (db, _):
        queue = {'issues': queue_db(db, config)}
    for command, data in [('status', status(config)), ('queue', queue), ('show', show(config, 'p', 1))]:
        stream = StringIO()
        render(command, data, console=Console(file=stream, width=width, no_color=True), verbose=True)
        text = stream.getvalue()
        assert '\x1b' not in text
        assert max(map(len, text.splitlines())) <= width
        render(command, data, as_json=True)
        assert json.loads(capsys.readouterr().out) == data
    stream = StringIO()
    render('queue', queue, state='review', console=Console(file=stream, width=width))
    assert 'State' not in stream.getvalue() and 'review' in stream.getvalue()


@pytest.mark.parametrize('permissions', [{'pull': False}, {'push': False}])
def test_add_insufficient_permissions(permissions):
    with pytest.raises(ValueError, match='Insufficient'):
        validate_remote('owner/repo', lambda *a: dict(full_name='owner/repo', permissions=permissions),
                        lambda *a, **k: pytest.fail('git called'))


@pytest.mark.parametrize('code', [403, 404, 503, None])
def test_add_api_errors(code):
    from dispatcher.github import GitHubError
    def transport(*args):
        raise GitHubError(code)
    with pytest.raises(GitHubError):
        validate_remote('owner/repo', transport, lambda *a, **k: pytest.fail('git called'))


def test_add_warning_and_git_error():
    calls = []
    def transport(*args):
        calls.append(args)
        return dict(full_name='owner/repo', has_issues=True)
    def run(args, **kwargs):
        assert args == ['git', 'ls-remote', 'git@github.com:owner/repo.git']
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        return SimpleNamespace(returncode=0)
    assert validate_remote('owner/repo', transport, run).startswith('WARN')
    assert calls == [('GET', '/repos/owner/repo', None)]
    with pytest.raises(ValueError, match='Git remote'):
        validate_remote('owner/repo', transport, lambda *a, **k: SimpleNamespace(returncode=1))


def test_project_remove_history_duplicate_and_add(instance, monkeypatch):
    config, store, path = instance
    register(store)
    before = path.read_bytes()
    monkeypatch.setattr('dispatcher.projects.validate_remote', lambda *a: pytest.fail('network called'))
    for name, repo in [('p', 'owner/new'), ('new', 'owner/repo')]:
        with pytest.raises(ValueError):
            mutate(path, 'add', name, repository=repo)
        assert path.read_bytes() == before
    mutate(path, 'remove', 'p')
    assert store.issue(1)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='retained local history'):
        mutate(path, 'add', 'renamed', repository='owner/repo')
    assert path.read_bytes() == before
    monkeypatch.setattr('dispatcher.projects.validate_remote', lambda *a: 'WARN test')
    assert mutate(path, 'add', 'new', repository='owner/new') == 'WARN test'
    assert load_config(path).projects[-1].id == 'new'


def test_failed_add_unchanged(instance, monkeypatch):
    _, _, path = instance
    before = path.read_bytes()
    def fail(*args):
        raise ValueError('denied')
    monkeypatch.setattr('dispatcher.projects.validate_remote', fail)
    with pytest.raises(ValueError, match='denied'):
        mutate(path, 'add', 'new', repository='owner/new')
    assert path.read_bytes() == before


def test_external_editor_conflict(instance):
    _, _, path = instance
    changed = path.read_text() + '\n# external editor\n'
    with pytest.raises(ValueError, match='changed outside'):
        with edit(path) as (_, data):
            data['custom']['keep'] = 7
            path.write_text(changed)
    assert path.read_text() == changed
    assert not list(path.parent.glob('.config-*'))


def test_parallel_cli_writers(instance):
    from concurrent.futures import ThreadPoolExecutor
    _, _, path = instance
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda name: mutate(path, 'disable', name), ['p', 'empty']))
    assert all(not p.enabled for p in load_config(path).projects)


def test_purge_inventory_read_only_and_refusal(instance, monkeypatch, tmp_path, capsys):
    config, store, path = instance
    register(store)
    before = files(tmp_path)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('external call'))
    inventory = plan(config, 'p')
    assert inventory['counts']['issues'] == 1
    assert inventory['executable'] is False and inventory['blockers']
    assert files(tmp_path) == before
    from dispatcher.__main__ import main
    for flags in (['--purge', '--dry-run'], ['--purge']):
        monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(path), 'project', 'remove', 'p', *flags])
        if '--dry-run' in flags:
            main()
        else:
            with pytest.raises(SystemExit) as result:
                main()
            assert result.value.code == 1
        assert files(tmp_path) == before


def test_cli_json_no_runtime(instance, monkeypatch, capsys):
    _, store, path = instance
    register(store)
    from dispatcher.__main__ import main
    monkeypatch.setattr('dispatcher.__main__.Runtime', lambda *a: pytest.fail('Runtime'))
    for args in (['status'], ['queue'], ['show', 'p', '1']):
        monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(path), *args, '--json'])
        main()
        assert isinstance(json.loads(capsys.readouterr().out), dict)


def test_show_current_execution_never_falls_back(instance):
    config, store, _ = instance
    register(store, state='review')
    with store.transaction() as db:
        db.execute("UPDATE issues SET output_body='OLD OUTPUT' WHERE issue_id=1")
        db.execute("INSERT INTO executions(issue_id,status,started_at) VALUES(1,'completed','2026-01-01T00:00:00+00:00')")
        eid = db.execute('SELECT execution_id FROM executions').fetchone()[0]
    assert show(config, 'p', 1)['result'] is None
    directory = config.workspace / 'results'
    directory.mkdir()
    (directory / f'{eid}.txt').write_text('Current answer')
    assert show(config, 'p', 1)['result'] == 'Current answer'
    with store.transaction() as db:
        db.execute('DELETE FROM executions')
        db.execute("INSERT INTO executions(issue_id,status,started_at) VALUES(1,'reserved','2026-01-02T00:00:00+00:00')")
    assert show(config, 'p', 1)['result'] is None


def test_json_final_message_is_not_mistaken_for_events(tmp_path):
    path = tmp_path / 'answer'
    for content in ('{"answer": 42}', '"final answer"', '[]', '{"type": "custom"}'):
        path.write_text(content)
        assert final_result(path) == content


def test_terminal_child_with_open_pr_blocks(instance):
    config, store, _ = instance
    register(store, 1, 'awaiting-children')
    register(store, 2, 'done')
    with store.transaction() as db:
        db.execute('INSERT INTO issue_relations VALUES(1,2)')
        db.execute('UPDATE issues SET is_ready=0 WHERE issue_id=2')
        db.execute("INSERT INTO pull_requests VALUES(10,2,'owner/repo',10,'work','main','open',NULL,'2026-01-01')")
    assert [r['issue_number'] for r in show(config, 'p', 1)['blocking_children']] == [2]


@pytest.mark.parametrize('damage', ['identity', 'schema', 'permissions', 'missing'])
def test_monitor_rejects_invalid_database(instance, damage):
    config, _, _ = instance
    if damage == 'identity':
        (config.workspace / '.dispatcher-instance').write_text('wrong')
    elif damage == 'schema':
        with connection(config.database) as db:
            db.execute('DROP INDEX idx_issues_state')
    elif damage == 'permissions':
        config.database.chmod(0o644)
    else:
        config.database.unlink()
    assert status(config)['database'] != 'OK'
    with pytest.raises(SchemaError):
        with snapshot(config):
            pass


def test_recovery_audit_inventory_preserved(instance, tmp_path):
    config, store, _ = instance
    register(store)
    with store.transaction() as db:
        db.execute("INSERT INTO recovery_events(issue_id,action,details,created_at) VALUES(1,'test','{}','2026-01-01')")
    before = files(tmp_path)
    inventory = plan(config, 'p')
    assert inventory['counts']['recovery_events'] == 1
    assert 'append-only' in inventory['blockers'][0]
    assert files(tmp_path) == before


def test_cli_help_and_no_color_pipe(instance):
    import sys
    _, store, path = instance
    register(store)
    base = [sys.executable, '-m', 'dispatcher', '--config', str(path)]
    for args in (['--help'], ['queue', '--help'], ['show', '--help'], ['project', '--help'],
                 ['status'], ['queue', '--project', 'p', '--state', 'discovered'],
                 ['show', 'p', '1', '-v'], ['show', 'p', '1', '--verbose'], ['project', 'list'], ['project', 'show', 'p']):
        result = subprocess.run([*base, *args], capture_output=True, text=True,
                                env={**os.environ, 'NO_COLOR': '1', 'COLUMNS': '80'})
        assert result.returncode == 0, result.stderr
        assert '\x1b' not in result.stdout


def test_yaml_validation_failure_and_symlink_do_not_replace(instance, tmp_path):
    _, _, path = instance
    before = path.read_bytes()
    with pytest.raises(ValueError):
        with edit(path) as (_, data):
            data['dispatcher']['root_path'] = 'relative'
    assert path.read_bytes() == before
    link = tmp_path / 'link.yaml'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='Symlink'):
        mutate(link, 'disable', 'p')
    assert path.read_bytes() == before


def test_cli_restart_notice(instance, monkeypatch, capsys):
    _, _, path = instance
    from dispatcher.__main__ import main
    monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(path), 'project', 'disable', 'p'])
    main()
    assert 'Restart dispatcher to apply changes' in capsys.readouterr().out
