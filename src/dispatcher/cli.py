"""Command-line entry point."""
import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

from dispatcher.config import ConfigError, load
from dispatcher.doctor import diagnose
from dispatcher.runtime import prepare, running, serve


def _display_task(row):
    """Render known embedded JSON as Unicode, keeping the public string types.

    Only the display copy changes; SQLite snapshots and literal escapes in
    source fields retain their meaning. Never decode arbitrary text escapes.
    """
    value = dict(row)
    for key in ('snapshot', 'observed'):
        try:
            value[key] = json.dumps(json.loads(value[key]), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            # Preserve legacy/non-JSON diagnostic values verbatim.
            pass
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog='codex-dispatcher')
    parser.add_argument('--config', default='config/config.yaml', help='YAML path (default: config/config.yaml)')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('start', 'status', 'doctor', '_serve'):
        sub.add_parser(command, help=argparse.SUPPRESS if command == '_serve' else None)
    sub.add_parser('queue')
    init = sub.add_parser('init')
    init.add_argument('--update', action='store_true')
    for command in ('show', 'approve', 'reject'):
        task = sub.add_parser(command)
        task.add_argument('project')
        task.add_argument('issue', type=int)
    poll_parser = sub.add_parser('poll')
    poll_parser.add_argument('--watch', action='store_true', help='Poll until SIGINT/SIGTERM')
    stop = sub.add_parser('stop')
    stop.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        from dispatcher.initialization import initialize, ready
        from dispatcher.github import Adapter
        from dispatcher.storage import Store
        if args.command == 'init':
            reports = []
            results = initialize(config, args.update, reports=reports)
            for report in reports:
                if report['success']:
                    print(f'{report["project"]}: success')
                else:
                    persistence = 'failure saved in YAML' if report['saved'] else 'YAML save not confirmed'
                    print(f'{report["project"]}: fail: {report["message"]} ({persistence})')
            return int(any(not success for _, success in results))
        if args.command == 'doctor':
            checks = diagnose(config)
            for level, message in checks:
                print(f'{level}: {message}')
            return int(any(level == 'FAIL' for level, _ in checks))
        root = prepare(config)
        if args.command in ('start', '_serve'):
            for project in config.projects:
                if project.enabled and not ready(project):
                    from dispatcher.logging import event
                    print(f'WARN: {project.id}: run init; initialization missing or stale', file=sys.stderr)
                    event(config.logs / 'dispatcher.jsonl', 'project_not_initialized', level='WARN', project=project.id)
            if not any(ready(p) for p in config.projects):
                raise RuntimeError('No initialized enabled projects; run init')
        if args.command in ('queue', 'show', 'approve', 'reject'):
            project = None
            if args.command != 'queue':
                project = next((p for p in config.projects if p.id == args.project), None)
                if project is None or not ready(project):
                    raise RuntimeError('Project disabled, unknown or uninitialized; run init')
            store = Store(root / 'tasks.sqlite3')
            try:
                if args.command == 'queue':
                    repos = {p.repository.casefold() for p in config.projects if ready(p)}
                    print(json.dumps([_display_task(r) for r in store.queue() if r['repository'] in repos], ensure_ascii=False))
                elif args.command == 'show':
                    row = store.get(project.repository, args.issue)
                    if row is None:
                        raise RuntimeError('Unknown issue')
                    value = _display_task(row)
                    value['decisions'] = [dict(r) for r in store.db.execute(
                        'SELECT * FROM decisions WHERE repository=? AND number=? ORDER BY id',
                        (project.repository.casefold(), args.issue))]
                    value['command_diagnostics'] = [dict(r) for r in store.db.execute(
                        'SELECT action,event_id,reason FROM command_quarantine WHERE repository=? AND number=?',
                        (project.repository.casefold(), args.issue))]
                    from dispatcher.execution_store import ExecutionStore
                    execution = ExecutionStore(store)
                    value['attempts'] = [dict(r) for r in store.db.execute(
                        'SELECT * FROM attempts WHERE repository=? AND number=? ORDER BY started_at',
                        (project.repository.casefold(), args.issue))]
                    value['quota'] = [dict(r) for r in store.db.execute('SELECT * FROM quota_observation')]
                    value['result_publication'] = [dict(r) for r in store.db.execute(
                        'SELECT o.attempt_id,o.published,o.comment_id FROM result_outbox o JOIN attempts a USING(attempt_id) '
                        'WHERE a.repository=? AND a.number=?', (project.repository.casefold(), args.issue))]
                    print(json.dumps(value, ensure_ascii=False))
                else:
                    from dispatcher.queue import decide_cli
                    result, synced = decide_cli(store, Adapter(config.github_executable), project, args.issue, args.command)
                    print(result)
                    if not synced:
                        from dispatcher.logging import event
                        event(config.logs / 'dispatcher.jsonl', 'label_sync_failed', level='WARN', project=project.id, task=args.issue)
                        print('WARN: decision saved; label sync failed; poll will retry', file=sys.stderr)
                return 0
            finally:
                store.close()
        if args.command == '_serve':
            serve(config)
            return 0
        if args.command == 'poll':
            from dispatcher.github.sync import SyncService
            from dispatcher.storage import Store
            store = Store(root / 'tasks.sqlite3')
            try:
                service = SyncService(config, store)
                if args.watch:
                    from threading import Event
                    stop_event = Event()
                    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
                    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
                    while not stop_event.is_set():
                        print(json.dumps(service.poll(), ensure_ascii=False), flush=True)
                        stop_event.wait(config.poll_interval_seconds)
                    return 0
                result = service.poll()
                print(json.dumps(result, ensure_ascii=False))
                return int(bool(result['errors']))
            finally:
                store.close()
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
    except sqlite3.Error:
        print('FAIL: SQLite operation failed; check storage and retry', file=sys.stderr)
        return 1
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
