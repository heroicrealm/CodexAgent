"""Offline release/installer tests. Never contact GitHub or use credentials."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from packaging.version import Version
from dispatcher import releases
from dispatcher.config import validate_schema

spec = importlib.util.spec_from_file_location('wsl_distribution', Path('scripts/wsl_distribution.py'))
wsl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wsl)
TEMPLATE = Path('config/config.example.yaml').read_text()


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize('answer,allowed', [('y', True), ('Y', True), ('n', False), ('', False)])
def test_offline_answers(monkeypatch, answer, allowed):
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2')
    monkeypatch.setattr(releases, 'request_json', lambda _: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(sys, 'stdin', Terminal(answer + '\n'))
    assert releases.before_run() is allowed


def test_offline_nonterminal_and_interrupt(monkeypatch):
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2')
    monkeypatch.setattr(releases, 'request_json', lambda _: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n'))
    assert not releases.before_run()
    monkeypatch.setattr(sys, 'stdin', Terminal())
    monkeypatch.setattr('builtins.input', lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert not releases.before_run()


@pytest.mark.parametrize('tag,message', [('v2.5.10', 'New dispatcher version'), ('v2.5.2', ''), ('v2.5.1', '')])
def test_release_check(monkeypatch, capsys, tag, message):
    calls = []
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2')
    def request(url):
        calls.append(url)
        return {'tag_name': tag}
    monkeypatch.setattr(releases, 'request_json', request)
    assert releases.before_run()
    assert calls == [releases.API, releases.LATEST]
    assert capsys.readouterr().out.startswith(message)


def test_releases_error_and_timeout(monkeypatch, capsys):
    monkeypatch.setattr(releases, 'version', lambda _: '2.5.2')
    request_json = releases.request_json
    monkeypatch.setattr(releases, 'request_json', lambda url: {} if url == releases.API else None)
    assert releases.before_run()
    assert 'Unable to check' in capsys.readouterr().out
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
    old = TEMPLATE.replace('"2.5.2"', '"2.5.1" # my comment').replace('/home/user/codex-dispatcher', '/custom/root')
    path.write_text(old)
    target, text = wsl.config_update(path, TEMPLATE, tmp_path, Version('2.5.2'), 1)
    assert target == path
    assert text == old.replace('"2.5.1"', '"2.5.2"')
    assert path.read_text() == old
    fresh = wsl.render_template(TEMPLATE, tmp_path)
    data = yaml.safe_load(fresh)
    assert data['dispatcher']['root_path'] == str(tmp_path)
    assert 'root_path' not in data


@pytest.mark.parametrize('schema', ['', 'config_schema_version: 2\n', 'config_schema_version: nonsense\n'])
def test_new_config_does_not_touch_old(tmp_path, schema, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(schema + 'dispatcher: {}\n')
    original = path.read_bytes()
    target, text = wsl.config_update(path, TEMPLATE, tmp_path, Version('2.5.2'), 1)
    assert target.name == 'config.new.yaml'
    target.write_text('keep')
    monkeypatch.setattr(wsl, 'confirm', lambda _: False)
    with pytest.raises(RuntimeError, match='preserved'):
        wsl.config_update(path, TEMPLATE, tmp_path, Version('2.5.2'), 1)
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
    path.write_text(wsl.render_template(TEMPLATE, tmp_path))
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
    path.write_text(wsl.render_template(TEMPLATE, tmp_path))
    with InstanceLock(load_config(path)):
        with pytest.raises(RuntimeError, match='shutdown not proven'):
            with wsl.stopped(path, sys.executable, str(Path('src').resolve())):
                pytest.fail('entered maintenance')


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setattr(wsl.shutil, 'which', lambda _: '/fake/tool')
    monkeypatch.setattr(wsl, 'confirm', lambda _: True)
    wheel = 'codex_dispatcher_v2-2.5.2-py3-none-any.whl'
    payloads = {wheel: b'wheel', 'config.example.yaml': TEMPLATE.encode()}
    payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in payloads.items()).encode()
    release = {'tag_name': 'v2.5.2', 'assets': [{'name': n, 'browser_download_url': 'https://assets/' + n} for n in payloads]}
    def fetch(url):
        return json.dumps(release).encode() if url == wsl.LATEST else payloads[url.rsplit('/', 1)[1]]
    monkeypatch.setattr(wsl, 'fetch', fetch)
    versions = {}
    def run(argv, **kwargs):
        if '-m' in argv and 'venv' in argv:
            gen = Path(argv[-1]); (gen / 'bin').mkdir(); (gen / 'bin/python').touch()
        if 'install' in argv:
            versions[Path(argv[0]).parent.parent.name] = Version('2.5.2')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(wsl.subprocess, 'run', run)
    def installed(path):
        name = path.resolve().name
        return versions.get(name)
    monkeypatch.setattr(wsl, 'installed_version', installed)
    monkeypatch.setattr(wsl, 'call', lambda argv: '1' if '--config-schema-version' in argv else str(Path('src/dispatcher').resolve()))
    return tmp_path, payloads, release, versions, run


def test_install_repeat_uninstall_preserves_data(installer):
    home, *_ = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    data = base / 'workspace/user-data'
    data.parent.mkdir(); data.write_text('precious')
    wsl.main()
    assert settings.read_bytes() == original
    assert len(list(base.glob('.venv-wsl-*'))) == 1
    wsl.main(uninstall=True)
    assert settings.read_bytes() == original
    assert data.read_text() == 'precious'
    assert not (base / '.venv').exists()
    assert not (home / '.local/bin/codex-dispatcher').is_symlink()


@pytest.mark.parametrize('failure', ['checksum', 'download', 'pip', 'active'])
def test_failed_upgrade_preserves_old(installer, monkeypatch, failure):
    home, payloads, release, versions, run = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    original = (base / '.venv').resolve()
    versions[original.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    before = settings.read_bytes()
    if failure == 'checksum':
        payloads['config.example.yaml'] = b'bad'
    elif failure == 'download':
        monkeypatch.setattr(wsl, 'fetch', lambda _: (_ for _ in ()).throw(TimeoutError()))
    elif failure == 'pip':
        def fail(argv, **kw):
            if 'install' in argv:
                raise subprocess.CalledProcessError(1, argv)
            return run(argv, **kw)
        monkeypatch.setattr(wsl.subprocess, 'run', fail)
    else:
        @contextmanager
        def busy(*a):
            raise RuntimeError('active')
            yield
        monkeypatch.setattr(wsl, 'stopped', busy)
    with pytest.raises(Exception):
        wsl.main()
    assert (base / '.venv').resolve() == original
    assert settings.read_bytes() == before
    assert len(list(base.glob('.venv-wsl-*'))) == 1


def test_downgrade_and_foreign_cli(installer):
    home, _, _, versions, _ = installer
    wsl.main()
    versions[(home / 'codex-dispatcher/.venv').resolve().name] = Version('2.5.10')
    with pytest.raises(RuntimeError, match='Downgrade'):
        wsl.main()
    cli = home / '.local/bin/codex-dispatcher'
    cli.unlink(); cli.write_text('foreign')
    with pytest.raises(RuntimeError, match='Foreign'):
        wsl.main(uninstall=True)
    assert cli.read_text() == 'foreign'


def test_successful_compatible_upgrade(installer):
    home, _, _, versions, _ = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    previous = (base / '.venv').resolve()
    versions[previous.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_text().replace('"2.5.2"', '"2.5.1" # preserve').replace(str(base), str(home / 'custom/work'))
    settings.write_text(original)
    # Use a separate custom root for the lock probe; no application data removed.
    wsl.main()
    assert settings.read_text() == original.replace('"2.5.1"', '"2.5.2"')
    assert (base / '.venv').resolve() != previous
    assert previous.exists()
    assert wsl.installed_version(base / '.venv') == Version('2.5.2')


def test_schema_upgrade_repeat_uses_package_metadata(installer, monkeypatch):
    home, payloads, _, versions, _ = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    versions[(base / '.venv').resolve().name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    settings.write_text(settings.read_text().replace('"2.5.2"', '"2.5.1"'))
    original = settings.read_bytes()
    payloads['config.example.yaml'] = TEMPLATE.replace('config_schema_version: 1', 'config_schema_version: 2').encode()
    payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in payloads.items() if n != 'SHA256SUMS').encode()
    monkeypatch.setattr(wsl, 'call', lambda argv: '2' if '--config-schema-version' in argv else str(Path('src/dispatcher').resolve()))
    wsl.main()
    assert settings.read_bytes() == original
    assert yaml.safe_load(settings.with_name('config.new.yaml').read_text())['config_schema_version'] == 2
    generation = (base / '.venv').resolve()
    wsl.main()
    assert (base / '.venv').resolve() == generation
    assert settings.read_bytes() == original


def test_activation_failure_rolls_back(installer, monkeypatch):
    home, _, _, versions, _ = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    previous = (base / '.venv').resolve()
    versions[previous.name] = Version('2.5.1')
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    real_replace = wsl.os.replace
    def refuse(src, dst):
        if Path(dst) == base / '.venv':
            raise OSError('simulated activation failure')
        return real_replace(src, dst)
    monkeypatch.setattr(wsl.os, 'replace', refuse)
    with pytest.raises(OSError):
        wsl.main()
    assert (base / '.venv').resolve() == previous
    assert settings.read_bytes() == original
    assert wsl.owned_state(base)['current'] == previous.name
    assert not (base / '.venv-next').is_symlink()
    monkeypatch.setattr(wsl.os, 'replace', real_replace)
    wsl.main()
    assert (base / '.venv').resolve() != previous


def test_installed_global_lock_covers_custom_config(tmp_path, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    base = tmp_path / 'installation'
    prefix = base / '.venv-wsl-test'
    prefix.mkdir(parents=True)
    (base / '.wsl-install.json').write_text('{}')
    monkeypatch.setattr(locking.sys, 'prefix', str(prefix))
    config = Config(tmp_path / 'custom/workspace', tmp_path / 'custom/db', ())
    with locking.InstanceLock(config) as lock:
        assert base / '.wsl-install.lock' in lock.paths
        # A different process must observe the real inherited exclusion.
        result = subprocess.run([sys.executable, '-c',
            'import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            str(base / '.wsl-install.lock')], capture_output=True)
        assert result.returncode != 0


@pytest.mark.parametrize('command', ['queue', 'status'])
def test_read_commands_never_check_network(tmp_path, monkeypatch, command):
    from dispatcher.__main__ import main
    from dispatcher.config import load_config
    from dispatcher.db_guard import maintain
    path = tmp_path / 'config.yaml'
    path.write_text(wsl.render_template(TEMPLATE, tmp_path))
    maintain(load_config(path), 'bootstrap')
    monkeypatch.setattr(releases, 'before_run', lambda: pytest.fail('network from monitoring'))
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', '--config', str(path), command, '--json'])
    main()


def test_global_runtime_lock_allows_independent_configs(tmp_path, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    prefix = tmp_path / '.venv-wsl-runtime'
    prefix.mkdir()
    (tmp_path / '.wsl-install.json').write_text('{}')
    monkeypatch.setattr(locking.sys, 'prefix', str(prefix))
    first = Config(tmp_path / 'one/work', tmp_path / 'one/db', ())
    second = Config(tmp_path / 'two/work', tmp_path / 'two/db', ())
    with locking.InstanceLock(first), locking.InstanceLock(second):
        pass


def test_missing_metadata_and_unicode_root(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(TEMPLATE.replace('dispatcher_version: "2.5.2"\n', ''))
    target, content = wsl.config_update(path, TEMPLATE, tmp_path, Version('2.5.2'), 1)
    assert target == path
    assert content == 'dispatcher_version: "2.5.2"\n' + path.read_text()
    root = tmp_path / 'имя пользователя'
    assert yaml.safe_load(wsl.render_template(TEMPLATE, root))['dispatcher']['root_path'] == str(root)


def test_active_custom_runtime_blocks_installer_and_uninstaller(installer, monkeypatch):
    from dispatcher import locking
    from dispatcher.config import Config
    home, *_ = installer
    wsl.main()
    base = home / 'codex-dispatcher'
    monkeypatch.setattr(locking.sys, 'prefix', str((base / '.venv').resolve()))
    config = Config(home / 'custom/work', home / 'custom/db', ())
    with locking.InstanceLock(config):
        for uninstall in (False, True):
            with pytest.raises(RuntimeError, match='active'):
                wsl.main(uninstall=uninstall)
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
    wsl.main()
    base = home / 'codex-dispatcher'
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    settings.write_text(settings.read_text().replace('workspace: workspace', 'workspace: .venv/user-workspace'))
    user_file = base / '.venv/user-workspace/precious'
    user_file.parent.mkdir(); user_file.write_text('preserve')
    with pytest.raises(RuntimeError, match='overlaps'):
        wsl.main(uninstall=True)
    assert user_file.read_text() == 'preserve'
    assert (base / '.venv').is_symlink()
