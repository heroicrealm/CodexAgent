"""Command-line entry point."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dispatcher.config import ConfigError, load
from dispatcher.doctor import diagnose
from dispatcher.runtime import prepare, running, serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog='codex-dispatcher')
    parser.add_argument('--config', default='config/config.yaml', help='YAML path (default: config/config.yaml)')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('start', 'status', 'doctor', '_serve'):
        sub.add_parser(command, help=argparse.SUPPRESS if command == '_serve' else None)
    stop = sub.add_parser('stop')
    stop.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if args.command == 'doctor':
            checks = diagnose(config)
            for level, message in checks:
                print(f'{level}: {message}')
            return int(any(level == 'FAIL' for level, _ in checks))
        root = prepare(config)
        if args.command == '_serve':
            serve(config)
            return 0
        state = running(root)
        if args.command == 'status':
            print('running' if state else 'stopped')
            return 0
        if args.command == 'start':
            if state:
                print('already running')
                return 0
            child = subprocess.Popen([sys.executable, '-m', 'dispatcher', '--config', str(config.source), '_serve'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            for _ in range(100):
                if running(root):
                    print('running')
                    return 0
                if child.poll() is not None:
                    # A concurrent start may own the lock and still be publishing state.
                    if child.returncode != 0:
                        break
                time.sleep(0.05)
            raise RuntimeError('Daemon did not become ready; check directory permissions')
        if not state:
            print('already stopped')
            return 0
        # pidfd prevents signalling an unrelated process after PID reuse.
        try:
            descriptor = os.pidfd_open(state['pid'])
        except ProcessLookupError:
            print('stopped')
            return 0
        try:
            if running(root) == state:
                signal.pidfd_send_signal(descriptor, signal.SIGUSR1 if args.force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        finally:
            os.close(descriptor)
        while running(root) == state:
            time.sleep(0.05)
        print('stopped')
        return 0
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
