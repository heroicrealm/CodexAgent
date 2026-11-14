import argparse
from pathlib import Path
import socket
import json
from .config import load_config, resolve_config
from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser(description='Codex Task Dispatcher V2')
    parser.add_argument('--config', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--once', action='store_true')
    stop = commands.add_parser('stop')
    stop.add_argument('--force', action='store_true')
    commands.add_parser('init-labels')
    queue = commands.add_parser('queue')
    queue.add_argument('--project')
    from .models import State
    queue.add_argument('--state', choices=[str(s) for s in State])
    queue.add_argument('--all', action='store_true')
    queue.add_argument('--json', action='store_true')
    commands.add_parser('status').add_argument('--json', action='store_true')
    show = commands.add_parser('show')
    show.add_argument('project')
    show.add_argument('issue', type=int)
    show.add_argument('-v', '--verbose', action='store_true')
    show.add_argument('--json', action='store_true')
    projects = commands.add_parser('project').add_subparsers(dest='project_command', required=True)
    projects.add_parser('list')
    for action in ('show', 'enable', 'disable', 'remove', 'add'):
        sub = projects.add_parser(action)
        sub.add_argument('project')
        if action == 'remove':
            sub.add_argument('--purge', action='store_true')
            sub.add_argument('--dry-run', action='store_true')
        if action == 'add':
            sub.add_argument('repository')
            sub.add_argument('--base-branch', default='main')
            sub.add_argument('--checkout')
    retry = commands.add_parser('publish')
    retry.add_argument('execution_id', type=int)
    db = commands.add_parser('db')
    maintenance = db.add_subparsers(dest='db_command', required=True)
    for name in ('check', 'bootstrap', 'bind', 'upgrade'):
        maintenance.add_parser(name)
    for name in ('backup', 'restore', 'register'):
        maintenance.add_parser(name).add_argument('path', type=Path)
    recover = maintenance.add_parser('recover')
    recover.add_argument('--dry-run', action='store_true')
    recover.add_argument('--json', action='store_true')
    recovery = maintenance.add_parser('recovery').add_subparsers(dest='recovery_command', required=True)
    recovery.add_parser('status').add_argument('--json', action='store_true')
    for name in ('accept', 'retry', 'cancel'):
        sub = recovery.add_parser(name)
        sub.add_argument('--json', action='store_true')
        sub.add_argument('project')
        sub.add_argument('issue', type=int)
    args = parser.parse_args()
    import sqlite3
    import yaml
    from .storage import SchemaError
    try:
        config = load_config(args.config)
    except (ValueError, OSError, TypeError, KeyError, yaml.YAMLError) as exc:
        parser.exit(1, f'FAIL configuration: {exc}\n')
    if args.command in ('status', 'queue', 'show', 'project'):
        try:
            from . import monitoring as queries
            from .monitoring_output import render, table
            if args.command == 'status':
                data = queries.status(config)
            elif args.command == 'queue':
                with queries.snapshot(config) as (db, _):
                    data = {'issues': queries.queue_db(db, config, args.project, args.state, args.all)}
            elif args.command == 'show':
                data = queries.show(config, args.project, args.issue)
            else:
                from .projects import inspect_projects, mutate
                from rich.console import Console
                from rich.panel import Panel
                from rich.text import Text
                action = args.project_command
                if action in ('list', 'show'):
                    values = inspect_projects(config, getattr(args, 'project', None))
                    if action == 'list':
                        table(Console(), ['№', 'Project ID', 'Repository', 'Status'],
                              [[n, v['id'], v['repository'], 'enabled' if v['enabled'] else 'disabled'] for n, v in enumerate(values, 1)])
                    else:
                        Console().print(Panel(Text(json.dumps(values[0], ensure_ascii=False, indent=2))))
                elif action == 'remove' and args.purge:
                    from .purge import plan
                    value = plan(config, args.project)
                    Console().print(Panel(Text(json.dumps(value, ensure_ascii=False, indent=2)), title='Purge inventory · BLOCKED'))
                    if not args.dry_run:
                        raise ValueError('Purge refused: ' + '; '.join(value['blockers']))
                else:
                    if getattr(args, 'dry_run', False):
                        raise ValueError('--dry-run requires --purge')
                    warning = mutate(resolve_config(args.config), action, args.project,
                        repository=getattr(args, 'repository', None), base_branch=getattr(args, 'base_branch', 'main'),
                        checkout=getattr(args, 'checkout', None))
                    if warning:
                        print(warning)
                    print('Restart dispatcher to apply changes')
                return
            render(args.command, data, as_json=args.json, config=config,
                   state=getattr(args, 'state', None), verbose=getattr(args, 'verbose', False))
            if args.command == 'status' and data['database'] != 'OK':
                parser.exit(1)
        except (SchemaError, RuntimeError, ValueError, OSError, sqlite3.DatabaseError, yaml.YAMLError) as exc:
            parser.exit(1, f'FAIL {exc}\n')
        return
    if args.command == 'db':
        from .db_guard import maintain
        from .storage import SchemaError
        import sqlite3
        try:
            if args.db_command in ('recover', 'recovery'):
                from .recovery import Recovery
                from .github import GitHubAdapter
                from .git import Git
                # Recovery installs its memory-only logger before any adapter call.
                service = Recovery(config, GitHubAdapter(config, None), Git(config))
                if args.db_command == 'recover':
                    result = service.recover(args.dry_run)
                elif args.recovery_command == 'status':
                    result = service.status()
                else:
                    operation = service.command(args.recovery_command, args.project, args.issue)
                    status = service.status()
                    target = next(o for o in status['operations'] if o['operation_id'] == operation)
                    result = {'operation_id': operation, 'recovery_required': status['recovery_required'],
                              'quarantine': status['quarantine'],
                              **{k: target[k] for k in ('issue_id', 'repository', 'issue_number', 'action', 'phase')}}
                from .recovery_output import render_recovery
                admission = service.status() if args.db_command == 'recover' and not args.json else None
                render_recovery(result, mode='recover' if args.db_command == 'recover' else args.recovery_command,
                                as_json=args.json, dry_run=getattr(args, 'dry_run', False), status=admission)
            else:
                print(maintain(config, args.db_command, getattr(args, 'path', None)))
        except (SchemaError, OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
            parser.exit(1, f'FAIL {exc}\n')
        return
    if args.command == 'stop':
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            connection.connect(str(config.workspace.resolve() / '.control.sock'))
            connection.sendall(b'force' if args.force else b'stop')
            print(connection.recv(32).decode())
        return
    from .db_guard import check
    from .storage import SchemaError
    try:
        check(config, execution=args.command not in ('queue', 'status'))
    except (SchemaError, OSError) as exc:
        parser.exit(1, f'FAIL {exc}\n')
    runtime = Runtime(config)
    if args.command == 'run':
        runtime.run(once=args.once)
    else:
        from .locking import InstanceLock
        with InstanceLock(config):
            check(config, execution=True)
            if args.command == 'init-labels':
                for project in config.projects:
                    if not project.enabled:
                        continue
                    runtime.github.initialize_labels(project)
            else:
                runtime.publisher.publish(args.execution_id)


if __name__ == '__main__':
    main()
