"""Exercise installed wheel and original run --once without GitHub access."""
import os
from pathlib import Path
import sys
import tempfile
from importlib.metadata import version
from dispatcher import releases
from dispatcher.config import load_config
from dispatcher.db_guard import maintain
from dispatcher.__main__ import main

assert version('codex-dispatcher-v2') == '2.5.2.post1'
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    config = root / 'config.yaml'
    config.write_text(f'config_schema_version: 1\ndispatcher:\n  root_path: {root}\nprojects: []\n')
    maintain(load_config(config), 'bootstrap')
    releases.request_json = lambda url: {} if url == releases.API else {'tag_name': 'v2.5.2.post1'}
    sys.argv = ['codex-dispatcher', '--config', str(config), 'run', '--once']
    main()
    assert (root / 'runtime/dispatcher.sqlite3').is_file()
print('Installed wheel: metadata, bootstrap and controlled run --once PASS')
