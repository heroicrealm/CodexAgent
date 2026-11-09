import argparse
from pathlib import Path
import socket
import json
from .config import load_config
from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser(description='Codex Task Dispatcher V2')
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--once', action='store_true')
    stop = commands.add_parser('stop')
    stop.add_argument('--force', action='store_true')
    commands.add_parser('init-labels')
    commands.add_parser('queue')
    commands.add_parser('status')
    retry = commands.add_parser('publish')
    retry.add_argument('execution_id', type=int)
    db = commands.add_parser('db')
    maintenance = db.add_subparsers(dest='db_command', required=True)
    for name in ('check', 'bootstrap', 'bind'):
        maintenance.add_parser(name)
    for name in ('backup', 'restore', 'register'):
        maintenance.add_parser(name).add_argument('path', type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == 'db':
        from .db_guard import maintain
        from .storage import SchemaError
        import sqlite3
        try:
            print(maintain(config, args.db_command, getattr(args, 'path', None)))
        except (SchemaError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
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
    elif args.command in ('queue', 'status'):
        where = 'WHERE i.is_virtual=0' if args.command == 'queue' else ''
        for row in runtime.storage.rows('SELECT i.issue_id,i.repository,i.issue_number,i.state,i.is_virtual,'
                'i.execution_group_id,r.base_branch,r.work_branch FROM issues i '
                'LEFT JOIN issues r ON r.issue_id=i.execution_group_id ' + where + ' ORDER BY i.issue_id'):
            value = dict(row)
            value['children'] = [dict(r) for r in runtime.storage.rows(
                'SELECT c.issue_id,c.state,c.is_ready FROM issue_relations r JOIN issues c '
                'ON c.issue_id=r.child_issue_id WHERE r.parent_issue_id=?', (row['issue_id'],))]
            value['prs'] = [dict(r) for r in runtime.storage.rows(
                'SELECT pr_number,status,base_branch FROM pull_requests WHERE issue_id=?', (row['issue_id'],))]
            errors = runtime.storage.rows('SELECT error_code,error_stage FROM error_log '
                                          'WHERE issue_id=? ORDER BY id DESC LIMIT 1', (row['issue_id'],))
            value['last_error'] = dict(errors[0]) if errors else None
            print(json.dumps(value, ensure_ascii=False))
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
