"""Manage the Standalone systemd user unit without opening the database."""
from contextlib import ExitStack
import fcntl
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import yaml

from .config import load_config, resolve_config, validate_schema
from .locking import installation_lock_path

UNIT = 'codex-dispatcher.service'
MARKER = '# codex-dispatcher managed unit v1 sha256='
CONFIG = '# config='


def register(commands):
    actions = commands.add_parser('service').add_subparsers(dest='service_command', required=True)
    for name in ('install', 'start', 'stop', 'restart', 'status', 'uninstall'):
        actions.add_parser(name)
    logs = actions.add_parser('logs')
    logs.add_argument('-n', '--lines', type=int, default=50)
    logs.add_argument('-f', '--follow', action='store_true')


def quote(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Control characters in systemd paths are unsupported')
    return json.dumps(value.replace('%', '%%'), ensure_ascii=False)


def render(home, cli, config):
    path = f'{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin'
    environment = {'HOME': str(home), 'PATH': path}
    for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
        if Path(os.environ.get(key, '')).is_absolute():
            environment[key] = os.environ[key]
    body = f"""{CONFIG}{json.dumps(str(config))}
[Unit]
Description=Codex Dispatcher
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
ExecStart=:{quote(str(cli))} --config {quote(str(config))} run
WorkingDirectory={quote(str(home))}
Restart=on-failure
RestartSec=10s
TimeoutStopSec=infinity
KillMode=mixed
StandardOutput=journal
StandardError=journal
"""
    body += ''.join(f'Environment={quote(f"{key}={value}")}\n' for key, value in environment.items())
    body += '\n[Install]\nWantedBy=default.target\n'
    content = body.encode()
    return f'{MARKER}{hashlib.sha256(content).hexdigest()}\n'.encode() + content


def call(program, *args, capture=False):
    result = subprocess.run([program, '--user', *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, text=True)
    if result.returncode:
        detail = f': {result.stderr.strip()}' if result.stderr else ''
        raise RuntimeError(f'{program} --user {args[0]} failed ({result.returncode}){detail}. '
                           'Check the systemd user manager and user D-Bus session.')
    return result.stdout


def properties():
    output = call('systemctl', 'show', '-p',
                  'LoadState,FragmentPath,DropInPaths,ActiveState,UnitFileState,MainPID',
                  UNIT, capture=True)
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def check_namespace(unit, props):
    fragment = props.get('FragmentPath')
    if (props.get('DropInPaths') or (fragment and Path(fragment) != unit)
            or props.get('LoadState') not in ('loaded', 'not-found')):
        raise RuntimeError(f'Unexpected systemd fragment, drop-ins or load state for {UNIT}')


def safe_node(info, path, kind):
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError(f'Foreign or unsafe systemd path preserved: {path}')


def owned(unit):
    try:
        info = unit.lstat()
    except FileNotFoundError:
        return None
    safe_node(info, unit, stat.S_ISREG)
    with os.fdopen(os.open(unit, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        safe_node(os.fstat(stream.fileno()), unit, stat.S_ISREG)
        content = stream.read(65537)
    header, _, body = content.partition(b'\n')
    digest = hashlib.sha256(body).hexdigest()
    if len(content) > 65536 or header != f'{MARKER}{digest}'.encode() or not body.startswith(CONFIG.encode()):
        raise RuntimeError(f'Foreign or modified unit preserved: {unit}')
    return content


def write_unit(unit, expected):
    fd, temporary = tempfile.mkstemp(dir=unit.parent, prefix='.codex-dispatcher-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, unit)
    finally:
        Path(temporary).unlink()


def report(unit, cli, config):
    for key, value in properties().items():
        print(f'{key}={value}')
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        result = subprocess.run(['loginctl', 'show-user', '--property=Linger', '--value', user],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        linger = result.stdout.strip() if result.returncode == 0 else 'unknown'
    except (OSError, KeyError):
        linger = 'unknown'
    installed_config = None
    try:
        content = owned(unit)
        if content:
            installed_config = json.loads(content.splitlines()[1][len(CONFIG):])
    except (OSError, RuntimeError, ValueError):
        pass
    print(f'Linger={linger}\nCLI={cli}\nUnit={unit}\nConfig={installed_config or config}'
          f'\nVersion={version("codex-dispatcher-v2")}')


def execute(action, config_arg, *, lines=50, follow=False):
    if action == 'logs':
        if lines < 0:
            raise ValueError('--lines must be non-negative')
        call('journalctl', '--unit', UNIT, '--lines', str(lines), '--no-pager',
             *(['--follow'] if follow else []))
        return

    home = Path.home().absolute()
    unit = home / '.config/systemd/user' / UNIT
    cli = home / '.local/bin/codex-dispatcher'
    config = resolve_config(config_arg)
    if action == 'status':
        report(unit, cli, config)
        return
    if action in ('start', 'stop', 'restart'):
        call('systemctl', action, UNIT)
        return

    props = properties()
    check_namespace(unit, props)
    if action == 'install':
        if not cli.is_file() or not os.access(cli, os.X_OK):
            raise RuntimeError(f'Standalone executable missing or inaccessible: {cli}')
        load_config(config)
        validate_schema(config)
        expected = render(home, cli, config)
    with ExitStack() as stack:
        if action == 'install':
            lock = installation_lock_path()
            if lock is not None:
                guard = stack.enter_context(os.fdopen(os.open(lock, os.O_RDWR | os.O_NOFOLLOW), 'rb'))
                fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if not cli.is_file():
                    raise RuntimeError(f'Standalone executable disappeared: {cli}')
        for directory in (home, home / '.config', home / '.config/systemd', unit.parent):
            if action == 'install':
                directory.mkdir(mode=0o700, exist_ok=True)
            try:
                safe_node(directory.lstat(), directory, stat.S_ISDIR)
            except FileNotFoundError:
                return
        directory_fd = os.open(unit.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory_fd)
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        check_namespace(unit, properties())
        current = owned(unit)
        if action == 'install':
            if current is None:
                write_unit(unit, expected)
            elif current != expected:
                raise RuntimeError(f'Unit settings differ: {unit}. Run service uninstall before reinstalling.')
            call('systemctl', 'daemon-reload')
            check_namespace(unit, properties())
            call('systemctl', 'enable', UNIT)
        elif current is not None:
            call('systemctl', 'stop', UNIT)
            call('systemctl', 'disable', UNIT)
            if owned(unit) != current:
                raise RuntimeError(f'Unit changed during uninstall: {unit}')
            unit.unlink()
            call('systemctl', 'daemon-reload')
        elif props.get('LoadState') == 'loaded':
            call('systemctl', 'daemon-reload')


def dispatch(parser, args):
    try:
        execute(args.service_command, args.config, lines=getattr(args, 'lines', 50),
                follow=getattr(args, 'follow', False))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f'FAIL service: {exc}', file=sys.stderr)
        parser.exit(1)
