"""Offline release/installer tests. Never contact GitHub or use credentials."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from packaging.version import Version
from dispatcher import releases
from dispatcher.config import validate_schema

spec = importlib.util.spec_from_file_location('standalone_distribution', Path('scripts/standalone_distribution.py'))
standalone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standalone)
REAL_CONFIRM = standalone.confirm
TEMPLATE = Path('config/config.example.yaml').read_text()


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize('error', [OSError, TimeoutError])
def test_offline_release_check_does_not_read_stdin(monkeypatch, capsys, error):
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2.post2')
    monkeypatch.setattr(releases, 'request_json', lambda _: (_ for _ in ()).throw(error()))
    monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('release check prompted'))
    releases.before_run()
    assert 'Unable to check' in capsys.readouterr().err


@pytest.mark.parametrize('tag,message', [('v2.5.10', 'New dispatcher version'), ('v2.5.2.post2', ''), ('v2.5.1', '')])
def test_release_check(monkeypatch, capsys, tag, message):
    calls = []
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2.post2')
    def request(url):
        calls.append(url)
        return {'tag_name': tag}
    monkeypatch.setattr(releases, 'request_json', request)
    releases.before_run()
    assert calls == [releases.LATEST]
    assert capsys.readouterr().out.startswith(message)


def test_releases_error_and_timeout(monkeypatch, capsys):
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2.post2')
    request_json = releases.request_json
    monkeypatch.setattr(releases, 'request_json', lambda url: None)
    releases.before_run()
    assert 'Unable to check' in capsys.readouterr().err
    def urlopen(request, timeout):
        assert timeout == 3
        raise TimeoutError()
    monkeypatch.setattr(releases, 'urlopen', urlopen)
    with pytest.raises(TimeoutError):
        request_json(releases.API)


@pytest.mark.parametrize('schema', [None, True, '1', 2, 'broken'])
def test_schema_rejects_without_side_effects(tmp_path, schema):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'config_schema_version': schema, 'dispatcher': {'root_path': str(tmp_path)}}))
    with pytest.raises(ValueError, match='expected 1'):
        validate_schema(path)
    assert list(tmp_path.iterdir()) == [path]


def test_config_preserves_bytes_comments_and_root(tmp_path):
    path = tmp_path / 'config.yaml'
    old = TEMPLATE.replace('"2.5.2.post2"', '"2.5.1" # my comment').replace('/home/user/codex-dispatcher', '/custom/root')
    path.write_text(old)
    target, text = standalone.config_update(path, TEMPLATE, tmp_path, Version('2.5.2.post2'), 1)
    assert target == path
    assert text == old.replace('"2.5.1"', '"2.5.2.post2"')
    assert path.read_text() == old
    fresh = standalone.render_template(TEMPLATE, tmp_path)
    data = yaml.safe_load(fresh)
    assert data['dispatcher']['root_path'] == str(tmp_path)
    assert 'root_path' not in data


@pytest.mark.parametrize('schema', ['', 'config_schema_version: 2\n', 'config_schema_version: nonsense\n'])
def test_new_config_does_not_touch_old(tmp_path, schema, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(schema + 'dispatcher: {}\n')
    original = path.read_bytes()
    target, text = standalone.config_update(path, TEMPLATE, tmp_path, Version('2.5.2.post2'), 1)
    assert target.name == 'config.new.yaml'
    target.write_text('keep')
    monkeypatch.setattr(standalone, 'confirm', lambda _: False)
    with pytest.raises(RuntimeError, match='preserved'):
        standalone.config_update(path, TEMPLATE, tmp_path, Version('2.5.2.post2'), 1)
    assert path.read_bytes() == original
    assert target.read_text() == 'keep'


def test_cli_schema_before_runtime_and_network(tmp_path, monkeypatch, capsys):
    from dispatcher.__main__ import main
    path = tmp_path / 'config.yaml'
    path.write_text(f'dispatcher:\n  root_path: {tmp_path}\n')
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--config', str(path), 'run', '--once'])
    monkeypatch.setattr(releases, 'before_run', lambda: pytest.fail('network before schema'))
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1
    assert 'expected 1' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [path]


def test_run_once_passes_to_old_runtime(tmp_path, monkeypatch):
    from dispatcher import __main__ as cli
    from dispatcher import db_guard
    path = tmp_path / 'config.yaml'
    path.write_text(standalone.render_template(TEMPLATE, tmp_path))
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--config', str(path), 'run', '--once'])
    monkeypatch.setattr(releases, 'before_run', lambda: True)
    monkeypatch.setattr(db_guard, 'check', lambda *a, **kw: None)
    called = []
    class Runtime:
        def __init__(self, config):
            called.append(config.root_path)
        def run(self, *, once):
            called.append(once)
    monkeypatch.setattr(cli, 'Runtime', Runtime)
    cli.main()
    assert called == [tmp_path, True]


def test_real_locks_prevent_update(tmp_path):
    from dispatcher.config import load_config
    from dispatcher.locking import InstanceLock
    path = tmp_path / 'config.yaml'
    path.write_text(standalone.render_template(TEMPLATE, tmp_path))
    with InstanceLock(load_config(path)):
        with pytest.raises(RuntimeError, match='shutdown not proven'):
            with standalone.stopped(path, sys.executable, str(Path('src').resolve())):
                pytest.fail('entered maintenance')


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setattr(standalone.shutil, 'which', lambda _: '/fake/tool')
    monkeypatch.setattr(standalone, 'confirm', lambda _: True)
    original_main = standalone.main
    monkeypatch.setattr(standalone, 'main', lambda uninstall=False, script_path=Path('install-standalone.sh'): original_main(uninstall, script_path))
    wheel = 'codex_dispatcher_v2-2.5.2.post2-py3-none-any.whl'
    payloads = {'install-standalone.sh': Path('install-standalone.sh').read_bytes(), wheel: b'wheel', 'config.example.yaml': TEMPLATE.encode(), 'uninstall-standalone.sh': Path('uninstall-standalone.sh').read_bytes()}
    payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in payloads.items()).encode()
    release = {'tag_name': 'v2.5.2.post2', 'assets': [{'name': n, 'browser_download_url': 'https://assets/' + n} for n in payloads]}
    def fetch(url):
        return json.dumps(release).encode() if url == standalone.LATEST else payloads[url.rsplit('/', 1)[1]]
    monkeypatch.setattr(standalone, 'fetch', fetch)
    versions = {}
    def run(argv, **kwargs):
        if '-m' in argv and 'venv' in argv:
            gen = Path(argv[-1]); (gen / 'bin').mkdir(); (gen / 'bin/python').touch()
        if 'install' in argv:
            versions[Path(argv[0]).parent.parent.name] = Version('2.5.2.post2')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(standalone.subprocess, 'run', run)
    def installed(path):
        name = path.resolve().name
        return versions.get(name)
    monkeypatch.setattr(standalone, 'installed_version', installed)
    monkeypatch.setattr(standalone, 'call', lambda argv: '1' if '--config-schema-version' in argv else str(Path('src/dispatcher').resolve()))
    return tmp_path, payloads, release, versions, run


def test_install_repeat_uninstall_preserves_data(installer):
    home, *_ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    data = base / 'workspace/user-data'
    data.parent.mkdir(); data.write_text('precious')
    standalone.main()
    assert settings.read_bytes() == original
    assert len(list(base.glob('.venv-standalone-*'))) == 1
    standalone.main(uninstall=True)
    assert settings.read_bytes() == original
    assert data.read_text() == 'precious'
    assert not (base / '.venv').exists()
    assert not (home / '.local/bin/codex-dispatcher').is_symlink()
    assert not (home / '.local/bin/codex-dispatcher-uninstall').is_symlink()
    assert not list(base.glob('.venv-standalone-*'))
    assert not (base / '.standalone-install.json').exists()


@pytest.mark.parametrize('failure', ['checksum', 'download', 'pip', 'active'])
def test_failed_upgrade_preserves_old(installer, monkeypatch, failure):
    home, payloads, release, versions, run = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    original = (base / '.venv').resolve()
    versions[original.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    before = settings.read_bytes()
    if failure == 'checksum':
        payloads['config.example.yaml'] = b'bad'
    elif failure == 'download':
        monkeypatch.setattr(standalone, 'fetch', lambda _: (_ for _ in ()).throw(TimeoutError()))
    elif failure == 'pip':
        def fail(argv, **kw):
            if 'install' in argv:
                raise subprocess.CalledProcessError(1, argv)
            return run(argv, **kw)
        monkeypatch.setattr(standalone.subprocess, 'run', fail)
    else:
        @contextmanager
        def busy(*a):
            raise RuntimeError('active')
            yield
        monkeypatch.setattr(standalone, 'stopped', busy)
    with pytest.raises(Exception):
        standalone.main()
    assert (base / '.venv').resolve() == original
    assert settings.read_bytes() == before
    assert len(list(base.glob('.venv-standalone-*'))) == 1


def test_downgrade_and_foreign_cli(installer):
    home, _, _, versions, _ = installer
    standalone.main()
    versions[(home / 'codex-dispatcher/.venv').resolve().name] = Version('2.5.10')
    with pytest.raises(RuntimeError, match='Downgrade'):
        standalone.main()
    cli = home / '.local/bin/codex-dispatcher'
    cli.unlink(); cli.write_text('foreign')
    with pytest.raises(RuntimeError, match='Foreign'):
        standalone.main(uninstall=True)
    assert cli.read_text() == 'foreign'


def test_successful_compatible_upgrade(installer):
    home, _, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    previous = (base / '.venv').resolve()
    versions[previous.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_text().replace('"2.5.2.post2"', '"2.5.1" # preserve').replace(str(base), str(home / 'custom/work'))
    settings.write_text(original)
    # Use a separate custom root for the lock probe; no application data removed.
    standalone.main()
    assert settings.read_text() == original.replace('"2.5.1"', '"2.5.2.post2"')
    assert (base / '.venv').resolve() != previous
    assert previous.exists()
    assert standalone.installed_version(base / '.venv') == Version('2.5.2.post2')


def test_schema_upgrade_repeat_uses_package_metadata(installer, monkeypatch):
    home, payloads, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    versions[(base / '.venv').resolve().name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    settings.write_text(settings.read_text().replace('"2.5.2.post2"', '"2.5.1"'))
    original = settings.read_bytes()
    payloads['config.example.yaml'] = TEMPLATE.replace('config_schema_version: 1', 'config_schema_version: 2').encode()
    payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in payloads.items() if n != 'SHA256SUMS').encode()
    monkeypatch.setattr(standalone, 'call', lambda argv: '2' if '--config-schema-version' in argv else str(Path('src/dispatcher').resolve()))
    standalone.main()
    assert settings.read_bytes() == original
    assert yaml.safe_load(settings.with_name('config.new.yaml').read_text())['config_schema_version'] == 2
    generation = (base / '.venv').resolve()
    standalone.main()
    assert (base / '.venv').resolve() == generation
    assert settings.read_bytes() == original


def test_activation_failure_rolls_back(installer, monkeypatch):
    home, _, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    previous = (base / '.venv').resolve()
    versions[previous.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    real_replace = standalone.os.replace
    def refuse(src, dst):
        if Path(dst) == base / '.venv':
            raise OSError('simulated activation failure')
        return real_replace(src, dst)
    monkeypatch.setattr(standalone.os, 'replace', refuse)
    with pytest.raises(OSError):
        standalone.main()
    assert (base / '.venv').resolve() == previous
    assert settings.read_bytes() == original
    assert standalone.owned_state(base)['current'] == previous.name
    assert not (base / '.venv-next').is_symlink()
    monkeypatch.setattr(standalone.os, 'replace', real_replace)
    standalone.main()
    assert (base / '.venv').resolve() != previous


def test_installed_global_lock_covers_custom_config(tmp_path, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    base = tmp_path / 'installation'
    prefix = base / '.venv-standalone-test'
    prefix.mkdir(parents=True)
    (base / '.standalone-install.json').write_text('{}')
    monkeypatch.setattr(locking.sys, 'prefix', str(prefix))
    config = Config(tmp_path / 'custom/workspace', tmp_path / 'custom/db', ())
    with locking.InstanceLock(config) as lock:
        assert base / '.standalone-install.lock' in lock.paths
        # A different process must observe the real inherited exclusion.
        result = subprocess.run([sys.executable, '-c',
            'import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            str(base / '.standalone-install.lock')], capture_output=True)
        assert result.returncode != 0


@pytest.mark.parametrize('command', ['queue', 'status'])
def test_read_commands_never_check_network(tmp_path, monkeypatch, command):
    from dispatcher.__main__ import main
    from dispatcher.config import load_config
    from dispatcher.db_guard import maintain
    path = tmp_path / 'config.yaml'
    path.write_text(standalone.render_template(TEMPLATE, tmp_path))
    maintain(load_config(path), 'bootstrap')
    monkeypatch.setattr(releases, 'before_run', lambda: pytest.fail('network from monitoring'))
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--config', str(path), command, '--json'])
    main()


def test_global_runtime_lock_allows_independent_configs(tmp_path, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    prefix = tmp_path / '.venv-standalone-runtime'
    prefix.mkdir()
    (tmp_path / '.standalone-install.json').write_text('{}')
    monkeypatch.setattr(locking.sys, 'prefix', str(prefix))
    first = Config(tmp_path / 'one/work', tmp_path / 'one/db', ())
    second = Config(tmp_path / 'two/work', tmp_path / 'two/db', ())
    with locking.InstanceLock(first), locking.InstanceLock(second):
        pass


def test_missing_metadata_and_unicode_root(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(TEMPLATE.replace('dispatcher_version: "2.5.2.post2"\n', ''))
    target, content = standalone.config_update(path, TEMPLATE, tmp_path, Version('2.5.2.post2'), 1)
    assert target == path
    assert content == 'dispatcher_version: "2.5.2.post2"\n' + path.read_text()
    root = tmp_path / 'имя пользователя'
    assert yaml.safe_load(standalone.render_template(TEMPLATE, root))['dispatcher']['root_path'] == str(root)


def test_active_custom_runtime_blocks_installer_and_uninstaller(installer, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    home, *_ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    monkeypatch.setattr(locking.sys, 'prefix', str((base / '.venv').resolve()))
    config = Config(home / 'custom/work', home / 'custom/db', ())
    with locking.InstanceLock(config):
        for uninstall in (False, True):
            with pytest.raises(RuntimeError, match='active'):
                standalone.main(uninstall=uninstall)
    assert (base / '.venv').is_symlink()


def test_worktree_apply_rejects_old_schema(tmp_path, monkeypatch, capsys):
    from dispatcher.tools.worktree_cleanup import main
    path = tmp_path / 'config.yaml'
    path.write_text(f'dispatcher:\n  root_path: {tmp_path}\n')
    monkeypatch.setattr(sys, 'argv', ['worktree-cleanup', '--config', str(path), '--apply'])
    with pytest.raises(SystemExit):
        main()
    assert 'expected 1' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [path]


def test_uninstall_preserves_data_inside_program_directory(installer):
    home, *_ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    settings.write_text(settings.read_text().replace('workspace: workspace', 'workspace: .venv/user-workspace'))
    user_file = base / '.venv/user-workspace/precious'
    user_file.parent.mkdir(); user_file.write_text('preserve')
    with pytest.raises(RuntimeError, match='overlaps'):
        standalone.main(uninstall=True)
    assert user_file.read_text() == 'preserve'
    assert (base / '.venv').is_symlink()


def test_legacy_252_upgrade_delivers_uninstaller(installer):
    home, _, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    old = (base / '.venv').resolve()
    versions[old.name] = Version('2.5.2')
    (home / '.local/bin/codex-dispatcher-uninstall').unlink()
    (old / 'uninstall-standalone.sh').unlink()
    marker = base / '.standalone-install.json'
    state = json.loads(marker.read_text()); state.pop('uninstaller')
    marker.write_text(json.dumps(state))
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    settings.write_text(settings.read_text().replace('2.5.2.post2', '2.5.2'))
    standalone.main()
    command = home / '.local/bin/codex-dispatcher-uninstall'
    assert command.is_symlink() and os.access(command, os.X_OK)
    assert command.read_bytes() == Path('uninstall-standalone.sh').read_bytes()
    assert standalone.installed_version(base / '.venv') == Version('2.5.2.post2')
    assert yaml.safe_load(settings.read_text())['dispatcher_version'] == '2.5.2.post2'
    assert yaml.safe_load(settings.read_text())['config_schema_version'] == 1


@pytest.mark.parametrize('upgrade', [False, True])
def test_uninstaller_checksum_refused(installer, upgrade):
    home, payloads, _, versions, _ = installer
    if upgrade:
        standalone.main()
        versions[(home / 'codex-dispatcher/.venv').resolve().name] = Version('2.5.2')
    payloads['uninstall-standalone.sh'] += b'corruption'
    with pytest.raises(ValueError, match='SHA-256 mismatch: uninstall'):
        standalone.main()
    command = home / '.local/bin/codex-dispatcher-uninstall'
    if upgrade:
        assert command.read_bytes() == Path('uninstall-standalone.sh').read_bytes()
    else:
        assert not command.is_symlink()


def test_foreign_uninstaller_preserved(installer):
    home, *_ = installer
    command = home / '.local/bin/codex-dispatcher-uninstall'
    command.parent.mkdir(parents=True); command.write_text('foreign')
    with pytest.raises(RuntimeError, match='Foreign uninstall'):
        standalone.main()
    assert command.read_text() == 'foreign'


def test_link_failure_restores_uninstaller_and_package(installer, monkeypatch):
    home, _, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    old = (base / '.venv').resolve()
    versions[old.name] = Version('2.5.2')
    command = home / '.local/bin/codex-dispatcher-uninstall'
    command.unlink()
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    before = settings.read_bytes()
    real = Path.symlink_to
    def fail(path, target, *args, **kwargs):
        if path == command:
            raise OSError('uninstall link failure')
        return real(path, target, *args, **kwargs)
    monkeypatch.setattr(Path, 'symlink_to', fail)
    with pytest.raises(OSError, match='uninstall link'):
        standalone.main()
    assert (base / '.venv').resolve() == old
    assert settings.read_bytes() == before
    assert (old / 'uninstall-standalone.sh').exists()


def test_upgrade_switches_uninstaller_payload(installer):
    home, payloads, _, versions, _ = installer
    standalone.main()
    base = home / 'codex-dispatcher'
    previous = (base / '.venv').resolve()
    versions[previous.name] = Version('2.5.2')
    old_bytes = (previous / 'uninstall-standalone.sh').read_bytes()
    payloads['uninstall-standalone.sh'] += b'\n# next release uninstaller\n'
    payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in payloads.items() if n != 'SHA256SUMS').encode()
    standalone.main()
    command = home / '.local/bin/codex-dispatcher-uninstall'
    assert command.read_bytes() == payloads['uninstall-standalone.sh']
    assert (previous / 'uninstall-standalone.sh').read_bytes() == old_bytes


@pytest.mark.parametrize('failure', ['bytes', 'missing', 'digest', 'manifest', 'api'])
@pytest.mark.parametrize('existing', [False, True])
def test_installer_preflight_no_changes(installer, monkeypatch, failure, existing):
    home, payloads, release, *_ = installer
    if existing:
        standalone.main()
    def snapshot():
        return {str(p.relative_to(home)): ('link', os.readlink(p)) if p.is_symlink()
                else ('file', p.read_bytes()) if p.is_file() else ('dir',)
                for p in home.rglob('*')}
    before = snapshot()
    if failure == 'bytes':
        script = home / 'altered.sh'
        script.write_bytes(payloads['install-standalone.sh'] + b'\n# comment\n')
        before = snapshot()
        with pytest.raises(RuntimeError, match='Installer SHA-256 mismatch.*Release:'):
            standalone.main(script_path=script)
    else:
        if failure == 'missing':
            payloads['SHA256SUMS'] = b''
        elif failure == 'digest':
            next(a for a in release['assets'] if a['name'] == 'SHA256SUMS')['digest'] = 'sha256:bad'
        elif failure == 'manifest':
            payloads['SHA256SUMS'] = b'invalid manifest'
        else:
            monkeypatch.setattr(standalone, 'fetch', lambda _: (_ for _ in ()).throw(TimeoutError()))
        with pytest.raises(Exception):
            standalone.main()
    assert snapshot() == before


def test_owned_installer_symlink_and_repair(installer):
    home, *_ = installer
    standalone.main()
    command = home / '.local/bin/codex-dispatcher-install'
    assert os.access(command, os.X_OK)
    standalone.main(script_path=command)
    command.unlink()
    standalone.main()
    assert command.is_symlink()
    standalone.main(uninstall=True)
    assert not command.is_symlink()


def test_foreign_installer_symlink_and_file(installer):
    home, *_ = installer
    command = home / '.local/bin/codex-dispatcher-install'
    command.parent.mkdir(parents=True)
    command.symlink_to(Path('install-standalone.sh').resolve())
    with pytest.raises(RuntimeError, match='Unowned installer symlink'):
        standalone.main(script_path=command)
    with pytest.raises(RuntimeError, match='Foreign installer'):
        standalone.main()
    command.unlink()
    command.write_text('foreign')
    with pytest.raises(RuntimeError, match='Foreign installer'):
        standalone.main()
    assert command.read_text() == 'foreign'


@pytest.mark.parametrize('answer', ['n\n', '\n', '', 'yes\n', 'interrupt', 'non-tty'])
def test_uninstall_cancel_keeps_program(installer, monkeypatch, answer):
    home, *_ = installer
    standalone.main()
    original = (home / 'codex-dispatcher/.venv').resolve()
    # Restore the real confirmation function mocked by the installer fixture.
    monkeypatch.setattr(standalone, 'confirm', REAL_CONFIRM)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n') if answer == 'non-tty' else Terminal(answer))
    if answer == 'interrupt':
        monkeypatch.setattr('builtins.input', lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    if answer == 'non-tty':
        with pytest.raises(RuntimeError, match='Interactive terminal'):
            standalone.main(uninstall=True)
    else:
        standalone.main(uninstall=True)
    assert (home / 'codex-dispatcher/.venv').resolve() == original
    assert (home / '.local/bin/codex-dispatcher-install').exists()


@pytest.mark.parametrize('missing', [False, True])
def test_cli_version_without_state(tmp_path, monkeypatch, capsys, missing):
    from dispatcher import __main__ as cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--version'])
    def version(name):
        assert name == 'codex-dispatcher-v2'
        if missing:
            raise cli.metadata.PackageNotFoundError(name)
        return '2.5.2.post2'
    monkeypatch.setattr(cli.metadata, 'version', version)
    monkeypatch.setattr(cli, 'load_config', lambda *_: pytest.fail('config read'))
    monkeypatch.setattr(releases, 'before_run', lambda: pytest.fail('network'))
    with pytest.raises(SystemExit) as result:
        cli.main()
    output = capsys.readouterr()
    assert result.value.code == (1 if missing else 0)
    assert ('metadata unavailable' in output.err) if missing else output.out == 'Codex Dispatcher 2.5.2.post2\n'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('upgrade', [False, True])
def test_installer_delivery_link_failure_rolls_back(installer, monkeypatch, upgrade):
    home, _, _, versions, _ = installer
    base = home / 'codex-dispatcher'
    command = home / '.local/bin/codex-dispatcher-install'
    if upgrade:
        standalone.main()
        previous = (base / '.venv').resolve()
        versions[previous.name] = Version('2.5.2.post1')
        command.unlink()
    before = (base / '.standalone-install.json').read_bytes() if upgrade else None
    real = Path.symlink_to
    def fail(path, target, *args, **kwargs):
        if path == command:
            raise OSError('installer link failure')
        return real(path, target, *args, **kwargs)
    monkeypatch.setattr(Path, 'symlink_to', fail)
    with pytest.raises(OSError, match='installer link failure'):
        standalone.main()
    assert not command.is_symlink()
    if upgrade:
        assert (base / '.venv').resolve() == previous
        assert (base / '.standalone-install.json').read_bytes() == before
        assert (previous / 'install-standalone.sh').exists()
    else:
        assert not (base / '.venv').is_symlink()
        assert not (base / '.standalone-install.json').exists()
    assert len(list(base.glob('.venv-standalone-*'))) == int(upgrade)


def test_downloaded_installer_checksum_rejected(installer, monkeypatch):
    home, payloads, _, _, _ = installer
    original_fetch = standalone.fetch
    monkeypatch.setattr(standalone, 'fetch', lambda url: b'corrupt' if url.endswith('/install-standalone.sh') else original_fetch(url))
    with pytest.raises(ValueError, match='SHA-256 mismatch: install-standalone.sh'):
        standalone.main()
    assert not (home / 'codex-dispatcher/.standalone-install.json').exists()
    assert not list((home / 'codex-dispatcher').glob('.venv-standalone-*'))


def test_valid_manifest_digest(installer):
    _, payloads, release, *_ = installer
    next(a for a in release['assets'] if a['name'] == 'SHA256SUMS')['digest'] = 'sha256:' + hashlib.sha256(payloads['SHA256SUMS']).hexdigest()
    standalone.main()


def test_uninstall_explicit_y_holds_maintenance_lock(installer, monkeypatch, capsys):
    import fcntl
    home, *_ = installer
    standalone.main()
    def confirm(message):
        assert message == 'Proceed with uninstall?'
        with (home / 'codex-dispatcher/.standalone-install.lock').open('a+') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return REAL_CONFIRM(message)
    monkeypatch.setattr(standalone, 'confirm', confirm)
    monkeypatch.setattr(sys, 'stdin', Terminal(' Y \n'))
    standalone.main(uninstall=True)
    assert 'Proceed with uninstall? [y/N]:' in capsys.readouterr().out
    assert not (home / 'codex-dispatcher/.venv').is_symlink()


@pytest.mark.parametrize('entry', ['codex-dispatcher.service', 'default.target.wants/codex-dispatcher.service'])
@pytest.mark.parametrize('symlink', [False, True])
def test_uninstall_refuses_service_registration(installer, entry, symlink):
    home, *_ = installer
    standalone.main()
    registration = home / '.config/systemd/user' / entry
    registration.parent.mkdir(parents=True)
    if symlink:
        registration.symlink_to(home / 'missing.service')
    else:
        registration.touch()
    with pytest.raises(RuntimeError, match='service uninstall'):
        standalone.main(uninstall=True)
    assert os.path.lexists(registration)
    assert (home / '.local/bin/codex-dispatcher').is_symlink()
