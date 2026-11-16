"""Offline installer orchestration with a real wheel/venv/pip, fake release API.

Usage: python scripts/smoke-install.py dist/PACKAGE.whl /absolute/wheelhouse
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

spec = importlib.util.spec_from_file_location('wsl_distribution', Path(__file__).with_name('wsl_distribution.py'))
wsl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wsl)
wheel = Path(sys.argv[1]).resolve()
wheelhouse = Path(sys.argv[2]).resolve()
payloads = {wheel.name: wheel.read_bytes(), 'config.example.yaml': Path('config/config.example.yaml').read_bytes()}
payloads['SHA256SUMS'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in payloads.items()).encode()
release = {'tag_name': 'v2.5.2', 'assets': [{'name': name, 'browser_download_url': 'https://offline/' + name} for name in payloads]}
wsl.fetch = lambda url: json.dumps(release).encode() if url == wsl.LATEST else payloads[url.rsplit('/', 1)[1]]
wsl.confirm = lambda _: True
wsl.shutil.which = lambda _: '/offline/mock'
run = subprocess.run
wsl.subprocess.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0) if argv[0] in ('gh', 'codex') else run(argv, **kwargs)
os.environ['PIP_NO_INDEX'] = '1'
os.environ['PIP_FIND_LINKS'] = str(wheelhouse)
with tempfile.TemporaryDirectory(prefix='dispatcher-smoke-') as temporary:
    home = Path(temporary)
    os.environ['HOME'] = str(home)
    os.environ['XDG_CONFIG_HOME'] = str(home / 'xdg')
    wsl.main()
    base = home / 'codex-dispatcher'
    cli = home / '.local/bin/codex-dispatcher'
    subprocess.run([str(cli), '--help'], check=True)
    settings = home / 'xdg/codex-dispatcher/config.yaml'
    original = settings.read_bytes()
    data = base / 'runtime/user.sqlite3'
    data.parent.mkdir(); data.write_bytes(b'user sentinel')
    wsl.main()
    assert settings.read_bytes() == original
    assert str(wsl.installed_version(base / '.venv')) == '2.5.2'
    release['tag_name'] = 'v2.5.3'
    payloads[wheel.name] = b'corrupt wheel'
    try:
        wsl.main()
    except ValueError as exc:
        assert 'SHA-256 mismatch' in str(exc)
    else:
        raise AssertionError('Corrupt update accepted')
    assert str(wsl.installed_version(base / '.venv')) == '2.5.2'
    wsl.main(uninstall=True)
    assert settings.read_bytes() == original
    assert data.read_bytes() == b'user sentinel'
    assert not cli.is_symlink()
print('Real offline venv install, repeat, corrupt update, uninstall: PASS')
