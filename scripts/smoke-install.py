"""Offline installer orchestration with a real wheel/venv/pip, fake release API.

Usage: python scripts/smoke-install.py dist/PACKAGE.whl /absolute/wheelhouse [legacy-checkout]
The optional legacy checkout must contain the 2.5.2 or 2.5.2.post1 wheel in dist/.
The wheelhouse must contain runtime dependencies; no tokens or GitHub calls.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location('standalone_distribution', Path(__file__).with_name('standalone_distribution.py'))
standalone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standalone)
wheel = Path(sys.argv[1]).resolve()
wheelhouse = Path(sys.argv[2]).resolve()
payloads = {'install-standalone.sh': Path('install-standalone.sh').read_bytes(), wheel.name: wheel.read_bytes(), 'config.example.yaml': Path('config/config.example.yaml').read_bytes(), 'uninstall-standalone.sh': Path('uninstall-standalone.sh').read_bytes()}
payloads['SHA256SUMS'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in payloads.items()).encode()
release = {'tag_name': 'v2.5.2.post2', 'assets': [{'name': name, 'browser_download_url': 'https://offline/' + name} for name in payloads]}
standalone.fetch = lambda url: json.dumps(release).encode() if url == standalone.LATEST else payloads[url.rsplit('/', 1)[1]]
original_main = standalone.main
standalone.main = lambda uninstall=False: original_main(uninstall, Path('install-standalone.sh'))
standalone.confirm = lambda _: True
standalone.shutil.which = lambda _: '/offline/mock'
run = subprocess.run
standalone.subprocess.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0) if argv[0] in ('gh', 'codex') else run(argv, **kwargs)
os.environ['PIP_NO_INDEX'] = '1'
os.environ['PIP_FIND_LINKS'] = str(wheelhouse)
with tempfile.TemporaryDirectory(prefix='dispatcher-smoke-') as temporary:
    home = Path(temporary)
    os.environ['HOME'] = str(home)
    os.environ['XDG_CONFIG_HOME'] = str(home / 'xdg')
    # Optional legacy checkout + wheel exercises the actual old installer.
    if len(sys.argv) > 3:
        legacy_root = Path(sys.argv[3]).resolve()
        legacy_spec = importlib.util.spec_from_file_location('legacy_distribution', legacy_root / 'scripts/standalone_distribution.py')
        legacy = importlib.util.module_from_spec(legacy_spec); legacy_spec.loader.exec_module(legacy)
        old_wheel = next((legacy_root / 'dist').glob('*.whl'))
        old_payloads = {old_wheel.name: old_wheel.read_bytes(), 'config.example.yaml': (legacy_root / 'config/config.example.yaml').read_bytes()}
        old_version = old_wheel.name.split('-')[1]
        if old_version == '2.5.2.post1':
            old_payloads['uninstall-standalone.sh'] = (legacy_root / 'uninstall-standalone.sh').read_bytes()
        old_payloads['SHA256SUMS'] = ''.join(hashlib.sha256(b).hexdigest() + '  ' + n + '\n' for n, b in old_payloads.items()).encode()
        old_release = {'tag_name': 'v' + old_version, 'assets': [{'name': n, 'browser_download_url': 'https://offline/' + n} for n in old_payloads]}
        legacy.fetch = lambda url: json.dumps(old_release).encode() if url == legacy.LATEST else old_payloads[url.rsplit('/', 1)[1]]
        legacy.confirm = lambda _: True
        legacy.main()
        assert str(legacy.installed_version(home / 'codex-dispatcher/.venv')) == old_version
        old_cli = home / '.local/bin/codex-dispatcher'
        subprocess.run([str(old_cli), 'db', 'bootstrap'], check=True)
        legacy_database = (home / 'codex-dispatcher/runtime/dispatcher.sqlite3').read_bytes()
        legacy_config = (home / 'xdg/codex-dispatcher/config.yaml').read_text()
    standalone.main()
    base = home / 'codex-dispatcher'
    cli = home / '.local/bin/codex-dispatcher'
    subprocess.run([str(cli), '--help'], check=True)
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    if len(sys.argv) > 3:
        assert settings.read_text() == legacy_config.replace(old_version, '2.5.2.post2')
        assert (base / 'runtime/dispatcher.sqlite3').read_bytes() == legacy_database
    else:
        subprocess.run([str(cli), 'db', 'bootstrap'], check=True)
    subprocess.run([str(cli), '--version'], check=True)
    for command in (['db', 'check'], ['queue', '--json'], ['status', '--json']):
        subprocess.run([str(cli), *command], check=True)
    data = base / 'runtime/dispatcher.sqlite3'
    database_before = data.read_bytes()
    standalone.main()
    assert settings.read_bytes() == original
    assert str(standalone.installed_version(base / '.venv')) == '2.5.2.post2'
    # Execute the generated shell through the delivered symlink, exercising $0
    # propagation, bootstrap and SHA of the real shell bytes with an offline API.
    mock = home / 'mock-api'
    mock.mkdir()
    for name, payload in payloads.items():
        (mock / name).write_bytes(payload)
    (mock / 'release.json').write_text(json.dumps(release))
    (mock / 'sitecustomize.py').write_text(
        'import io, os, pathlib, urllib.request\n'
        'root = pathlib.Path(os.environ["MOCK_RELEASE_ROOT"])\n'
        'def offline(request, **kwargs):\n'
        '    url = request.full_url\n'
        '    name = "release.json" if url.endswith("/releases/latest") else url.rsplit("/", 1)[1]\n'
        '    return io.BytesIO((root / name).read_bytes())\n'
        'urllib.request.urlopen = offline\n')
    tools = home / 'mock-bin'
    tools.mkdir()
    for tool in ('git', 'gh', 'codex'):
        path = tools / tool
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    env = {**os.environ, 'PYTHONPATH': str(mock), 'MOCK_RELEASE_ROOT': str(mock),
           'PATH': str(tools) + os.pathsep + os.environ['PATH']}
    installer = home / '.local/bin/codex-dispatcher-install'
    repeat = subprocess.run([str(installer)], env=env, capture_output=True, text=True)
    assert repeat.returncode == 0 and 'is current' in repeat.stdout, repeat.stderr
    altered = home / 'altered-installer.sh'
    altered.write_bytes(installer.read_bytes() + b'\n# changed byte test\n')
    refused = subprocess.run(['sh', str(altered)], env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and 'Installer SHA-256 mismatch' in refused.stderr, refused.stderr
    assert settings.read_bytes() == original
    assert data.read_bytes() == database_before
    release['tag_name'] = 'v2.5.3'
    payloads[wheel.name] = b'corrupt wheel'
    try:
        standalone.main()
    except ValueError as exc:
        assert 'SHA-256 mismatch' in str(exc)
    else:
        raise AssertionError('Corrupt update accepted')
    assert str(standalone.installed_version(base / '.venv')) == '2.5.2.post2'
    removal = home / '.local/bin/codex-dispatcher-uninstall'
    assert removal.is_symlink() and os.access(removal, os.X_OK)
    credentials = home / '.codex/auth.json'
    credentials.parent.mkdir(); credentials.write_bytes(b'credential sentinel')
    workspace = base / 'workspace/sentinel'
    workspace.parent.mkdir(exist_ok=True); workspace.write_bytes(b'workspace sentinel')
    # Hold the installed runtime's real shared maintenance lock in another process.
    probe = "from dispatcher.config import load_config; from dispatcher.locking import InstanceLock; import sys; lock=InstanceLock(load_config(sys.argv[1])); lock.__enter__(); print('locked', flush=True); sys.stdin.read()"
    process = subprocess.Popen([str(base / '.venv/bin/python'), '-c', probe, str(settings)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        refused = subprocess.run([str(removal)], capture_output=True, text=True)
        assert refused.returncode != 0 and 'active' in refused.stderr, refused.stderr
        assert removal.exists()
    finally:
        process.communicate('stop', timeout=10)
    refused = subprocess.run([str(removal)], capture_output=True, text=True)
    assert refused.returncode != 0 and 'Interactive terminal required' in refused.stderr
    import pty
    import select
    master, slave = pty.openpty()
    process = subprocess.Popen([str(removal)], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b''
    import time
    deadline = time.monotonic() + 60
    while b'Proceed with uninstall? [y/N]:' not in output:
        assert time.monotonic() < deadline, output
        if select.select([master], [], [], 1)[0]:
            output += os.read(master, 65536)
    os.write(master, b'y\n')
    assert process.wait(timeout=30) == 0
    os.close(master)
    assert not removal.is_symlink()
    assert credentials.read_bytes() == b'credential sentinel'
    assert workspace.read_bytes() == b'workspace sentinel'
    assert settings.read_bytes() == original
    assert data.read_bytes() == database_before
    assert not cli.is_symlink()
print('Real offline venv install, repeat, corrupt update, uninstall: PASS')
