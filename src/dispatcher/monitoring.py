"""Local snapshot queries. Never construct Runtime or writable Storage."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import os
import sqlite3
from .db_guard import inspection, validate, marker
from .storage import SchemaError

PRIORITY = {s: n for n, group in enumerate((('error',), ('review',), ('rejected',),
    ('need-fix',), ('manual-running',), ('preparing', 'running'), ('pending',),
    ('awaiting-children',), ('discovered',), ('done', 'cancelled'))) for s in group}


def seconds(timestamp):
    if not timestamp:
        return None
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds())
    except (ValueError, TypeError):
        return None


@contextmanager
def snapshot(config):
    with inspection(config.database) as db:
        meta = validate(db)
        identity = marker(config)
        if (any(p.is_symlink() for p in (identity, *identity.absolute().parents))
                or not identity.is_file() or identity.stat().st_mode & 0o077
                or identity.read_text().strip() != meta[2]):
            raise SchemaError('Workspace identity mismatch')
        if meta[1] != 2:
            raise SchemaError('Explicit db upgrade required')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db, meta


def project(config, name):
    found = next((p for p in config.projects if p.id == name), None)
    if found is None:
        raise ValueError(f'Unknown project: {name}')
    return found


def process_token(pid):
    # starttime distinguishes PID reuse; /proc stat comm may contain spaces.
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def daemon(config):
    paths = (config.database.resolve().with_suffix('.lock'), config.workspace.resolve() / '.dispatcher-v2.lock')
    locked = []
    try:
        for path in paths:
            if any(p.is_symlink() for p in (path, *path.parents)):
                return {'state': 'UNKNOWN', 'diagnostic': 'Unsafe lock path'}
            if not path.exists():
                locked.append(False)
                continue
            with path.open('rb') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked.append(False)
                except BlockingIOError:
                    locked.append(True)
        sock = config.workspace / '.control.sock'
        if not any(locked) and not sock.exists():
            return {'state': 'STOPPED'}
        meta = json.loads((config.workspace / '.monitor.json').read_text())
        if (all(locked) and sock.is_socket() and meta['instance_id'] == marker(config).read_text().strip()
                and type(meta['pid']) is int and meta['pid'] > 0
                and process_token(meta['pid']) == meta['process_token']
                and 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(meta['updated_at'])).total_seconds() < 10):
            return {**meta, 'state': 'RUNNING'}
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        pass
    return {'state': 'UNKNOWN', 'diagnostic': 'No fresh, verified daemon evidence'}


def publish_metadata(config, last_poll=None, next_poll=None):
    from .models import utc_now
    import tempfile
    target = config.workspace / '.monitor.json'
    fd, temporary = tempfile.mkstemp(prefix='.monitor-', dir=config.workspace)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(pid=os.getpid(), process_token=process_token(os.getpid()),
                instance_id=marker(config).read_text().strip(), updated_at=utc_now(),
                last_poll=last_poll, next_poll=next_poll), stream)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def queue_db(db, config, project_id=None, state=None, all_records=False):
    projects = (project(config, project_id),) if project_id else config.projects
    result = []
    for p in projects:
        rows = db.execute('SELECT i.issue_id,i.repository,i.issue_number,i.project_id,i.execution_group_id,'
            'i.title,i.state,i.github_state,i.created_at,i.updated_at,i.last_seen_at, (SELECT created_at FROM state_history h WHERE h.issue_id=i.issue_id '
            'AND h.old_state IS NOT h.new_state AND h.new_state=i.state ORDER BY h.id DESC LIMIT 1) AS state_since '
            'FROM issues i WHERE lower(repository)=? AND is_virtual=0', (p.repository,)).fetchall()
        items = []
        for row in rows:
            value = dict(row)
            if not all_records and (value['github_state'] != 'open' or value['state'] in ('done', 'cancelled')):
                continue
            if state and value['state'] != state:
                continue
            value['project'] = p.id
            value['age_seconds'] = seconds(value['state_since'])
            value['pull_requests'] = [dict(r) for r in db.execute('SELECT * FROM pull_requests WHERE issue_id=?', (row['issue_id'],))]
            items.append(value)
        items.sort(key=lambda r: (PRIORITY.get(r['state'], 99), datetime.fromisoformat(r['state_since']).timestamp() if r['age_seconds'] is not None else float('inf'), r['issue_number']))
        result.extend(items)
    return result


def status(config):
    result = dict(daemon=daemon(config), database='OK', recovery='READY', last_poll=None, next_poll=None,
                  executing=None, projects=[])
    if result['daemon']['state'] == 'RUNNING':
        for key in ('last_poll', 'next_poll'):
            result[key] = result['daemon'].get(key)
    try:
        with snapshot(config) as (db, meta):
            blocked = db.execute("SELECT 1 FROM recovery_quarantine UNION SELECT 1 FROM recovery_operations WHERE phase<>'completed' LIMIT 1").fetchone()
            result['recovery'] = 'BLOCKED' if meta[3] or blocked else 'READY'
            for p in config.projects:
                rows = db.execute("SELECT state FROM issues WHERE lower(repository)=? AND is_virtual=0 AND github_state='open'", (p.repository,)).fetchall()
                counts = {key: sum(r['state'] == key for r in rows) for key in ('discovered', 'pending', 'review', 'rejected', 'error')}
                result['projects'].append(dict(project=p.id, enabled=p.enabled, open=len(rows), **counts))
            active = db.execute("SELECT i.project_id,i.issue_number,i.state,e.started_at FROM issues i LEFT JOIN executions e USING(issue_id) WHERE i.state IN ('preparing','running')").fetchall()
            if active:
                result['executing'] = dict(active[0])
                result['executing']['elapsed_seconds'] = seconds(active[0]['started_at'])
                result['executing']['verified_live'] = result['daemon']['state'] == 'RUNNING'
    except (SchemaError, OSError, sqlite3.DatabaseError) as exc:
        result.update(database=str(exc), recovery='UNKNOWN')
    return result


def final_result(path):
    try:
        text = path.read_text()
    except (OSError, UnicodeError):
        return None
    # `codex exec` default stdout is the final message; --json emits JSONL.
    lines = text.splitlines()
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            return text
    event_types = {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                   'item.started', 'item.updated', 'item.completed', 'error'}
    if any(not isinstance(e, dict) or e.get('type') not in event_types for e in events):
        return text
    messages = [e['item'].get('text', '') for e in events if isinstance(e, dict)
                and e.get('type') == 'item.completed' and isinstance(e.get('item'), dict)
                and e['item'].get('type') == 'agent_message' and isinstance(e['item'].get('text'), str)]
    return messages[-1] if messages else (None if events else text)


def show(config, project_id, number):
    p = project(config, project_id)
    with snapshot(config) as (db, _):
        row = db.execute('SELECT * FROM issues WHERE lower(repository)=? AND issue_number=? AND is_virtual=0', (p.repository, number)).fetchone()
        if row is None:
            raise ValueError(f'Issue not found: {p.id} #{number}')
        value = dict(row)
        for key in ('approved_body', 'approved_input', 'output_body'):
            value.pop(key, None)
        value['project'] = p.id
        root = db.execute('SELECT base_branch,work_branch FROM issues WHERE issue_id=?', (row['execution_group_id'],)).fetchone()
        value['branches'] = dict(root) if root else None
        for table in ('executions', 'pull_requests', 'state_history', 'error_log', 'recovery_events', 'recovery_quarantine'):
            value[table] = [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE issue_id=?', (row['issue_id'],))]
        value['result'] = None
        if value['executions']:
            execution = value['executions'][-1]
            value['result'] = final_result(config.workspace / 'results' / f"{execution['execution_id']}.txt")
        from .services import GroupService
        value['blocking_children'] = GroupService(None).blocking_children_db(db, row['issue_id'])
        return value
