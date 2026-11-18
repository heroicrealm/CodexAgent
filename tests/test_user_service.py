import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from dispatcher import service
from dispatcher.__main__ import main
from dispatcher.config import load_config
from dispatcher.db_guard import maintain


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / 'home %d $d with spaces'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    unit = home / '.config/systemd/user' / service.UNIT
    stable = home / '.local/bin/codex-dispatcher'
    stable.parent.mkdir(parents=True)
    target = home / 'codex-dispatcher/.venv-standalone-test/bin/codex-dispatcher'
    target.parent.mkdir(parents=True)
    target.touch(mode=0o700)
    stable.symlink_to(target)
    config = tmp_path / 'xdg/codex-dispatcher/config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({'config_schema_version': 1,
        'dispatcher': {'root_path': str(home)}}))
    calls = []
    errors = {}
    dropins = []

    def run(argv, **options):
        calls.append(argv)
        assert options.get('stdin') == subprocess.DEVNULL
        assert not options.get('shell')
        if argv[0] == 'loginctl':
            return subprocess.CompletedProcess(argv, 0, 'yes\n')
        assert argv[:2] in (['systemctl', '--user'], ['journalctl', '--user'])
        action = argv[2]
        if action in errors:
            return subprocess.CompletedProcess(argv, 1, '', errors[action])
        output = ''
        if action == 'show':
            output = '\n'.join([
                'LoadState=' + ('loaded' if unit.is_file() else 'not-found'),
                'FragmentPath=' + (str(unit) if unit.is_file() else ''),
                'DropInPaths=' + ' '.join(dropins),
                'ActiveState=inactive', 'UnitFileState=enabled', 'MainPID=0'])
        return subprocess.CompletedProcess(argv, 0, output)

    monkeypatch.setattr(service.subprocess, 'run', run)
    return home, unit, stable, config, calls, errors, dropins


def test_install_idempotent_and_uses_existing_xdg_resolver(manager, monkeypatch):
    home, unit, cli, config, calls, _, _ = manager
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', 'service', 'install'])
    main()
    original = unit.stat()
    main()
    assert unit.stat().st_ino == original.st_ino
    assert unit.stat().st_mode & 0o777 == 0o600
    body = unit.read_text()
    assert f'ExecStart=:"{str(cli).replace("%", "%%")}" --config "{config}" run' in body
    for setting in ('Type=exec', 'WorkingDirectory=%h', 'Environment="HOME=', 'Environment="PATH=',
                    'Restart=on-failure', 'RestartSec=10s', 'KillMode=mixed',
                    'TimeoutStopSec=infinity', 'StandardOutput=journal', 'StandardError=journal',
                    'WantedBy=default.target', 'StartLimitIntervalSec=300', 'StartLimitBurst=5'):
        assert setting in body
    assert all(value not in body for value in ('User=', '/etc/', '/var/lib/', '.venv-standalone-'))
    assert [argv[2] for argv in calls if argv[2] != 'show'] == ['daemon-reload', 'enable'] * 2


@pytest.mark.parametrize('kind', ['foreign', 'symlink', 'permissions', 'edited', 'drop-in'])
def test_unsafe_unit_is_preserved(manager, kind):
    _, unit, _, config, calls, _, dropins = manager
    service.execute('install', config)
    if kind == 'symlink':
        unit.unlink()
        unit.symlink_to(config)
    elif kind == 'foreign':
        unit.write_text('[Unit]\nDescription=Foreign\n')
    elif kind == 'permissions':
        unit.chmod(0o666)
    elif kind == 'edited':
        unit.write_text(unit.read_text() + 'User=root\n')
    else:
        dropins.append(str(unit) + '.d/override.conf')
    before = unit.read_bytes()
    calls.clear()
    for action in ('install', 'uninstall'):
        with pytest.raises(RuntimeError):
            service.execute(action, config)
    assert unit.read_bytes() == before
    assert all(argv[2] == 'show' for argv in calls)


@pytest.mark.parametrize('action', ['show', 'daemon-reload', 'enable'])
def test_install_failure_reports_manager_and_can_retry(manager, action):
    _, unit, _, config, calls, errors, _ = manager
    errors[action] = 'Failed to connect to bus'
    with pytest.raises(RuntimeError, match='user D-Bus'):
        service.execute('install', config)
    assert unit.exists() == (action != 'show')
    assert not any(argv[2] == 'start' for argv in calls)
    if action == 'daemon-reload':
        assert not any(argv[2] == 'enable' for argv in calls)
    errors.clear()
    service.execute('install', config)


@pytest.mark.parametrize('missing', ['config', 'cli'])
def test_install_diagnoses_missing_inputs_without_writing_unit(manager, missing):
    _, unit, cli, config, calls, _, _ = manager
    if missing == 'config':
        config.unlink()
        message = 'configuration'
    else:
        cli.unlink()
        message = 'Standalone executable'
    with pytest.raises(RuntimeError, match=message):
        service.execute('install', config)
    assert not unit.exists()
    assert all(argv[2] == 'show' for argv in calls)


def test_lifecycle_with_corrupt_database(manager, monkeypatch, capsys):
    home, unit, _, config, calls, errors, _ = manager
    custom = config.with_name('custom config.yaml')
    config.rename(custom)
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--config', str(custom), 'service', 'install'])
    main()
    service.execute('status', None)
    assert f'Config={custom}' in capsys.readouterr().out
    settings = load_config(custom)
    settings.database.parent.mkdir()
    settings.database.write_bytes(b'corrupt')
    settings.workspace.mkdir()
    uuid = settings.workspace / '.dispatcher-instance'
    uuid.write_text('saved UUID')
    service.execute('status', None)
    status = capsys.readouterr().out
    assert 'Linger=yes' in status and 'MainPID=0' in status and f'Config={custom}' in status
    service.execute('logs', None, lines=9, follow=True)
    assert calls[-1] == ['journalctl', '--user', '--unit', service.UNIT, '--lines', '9', '--no-pager', '--follow']
    calls.clear()
    for action in ('start', 'stop', 'restart'):
        service.execute(action, None)
    errors['stop'] = 'stop failed'
    with pytest.raises(RuntimeError, match='stop failed'):
        service.execute('uninstall', None)
    assert unit.exists()
    errors.clear()
    service.execute('uninstall', None)
    service.execute('uninstall', None)
    assert [argv[2] for argv in calls if argv[2] != 'show'] == [
        'start', 'stop', 'restart', 'stop', 'stop', 'disable', 'daemon-reload']
    assert not unit.exists()
    assert uuid.read_text() == 'saved UUID'
    assert settings.database.read_bytes() == b'corrupt'
    assert custom.is_file() and home.is_dir()


def test_cli_run_without_tty_after_release_timeout(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'config_schema_version': 1,
        'dispatcher': {'root_path': str(tmp_path)}}))
    settings = load_config(config)
    settings.database.parent.mkdir(mode=0o700)
    maintain(settings, 'bootstrap')
    code = """
import sys
from dispatcher import releases
from dispatcher.__main__ import main

def offline(_):
    raise TimeoutError()

releases.request_json = offline
assert not sys.stdin.isatty()
main()
"""
    result = subprocess.run([sys.executable, '-c', code, '--config', str(config), 'run', '--once'],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        env={**os.environ, 'PYTHONPATH': str(Path('src').resolve())})
    assert result.returncode == 0, result.stderr
    assert 'Unable to check' in result.stderr
    assert not (tmp_path / 'workspace/.control.sock').exists()
