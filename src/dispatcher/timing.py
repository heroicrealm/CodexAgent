"""Optional, bounded, content-free timing evidence. Never part of task transactions."""
import csv
import functools
import io
import json
import logging
import sqlite3
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_context = ContextVar('dispatcher_timing', default={})
_warned = False
SCHEMA = '''CREATE TABLE IF NOT EXISTS dispatcher_timing_events (
 id INTEGER PRIMARY KEY, timestamp_utc TEXT NOT NULL, project_id TEXT,
 repository TEXT, issue_id INTEGER, issue_number INTEGER, cycle_id TEXT,
 operation_id TEXT, request_id TEXT, event TEXT NOT NULL, phase TEXT NOT NULL,
 duration_ms REAL, queue_wait_ms REAL, http_status INTEGER, details TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS timing_project_issue ON dispatcher_timing_events(project_id,issue_number,id);
 CREATE INDEX IF NOT EXISTS timing_timestamp ON dispatcher_timing_events(timestamp_utc);'''


def bootstrap(db):
    # Additive bootstrap: no rewrite of task data or schema-version downgrade.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


class Trace:
    def __init__(self, path, enabled=False):
        self.path, self.enabled = path, enabled
        self.projects = {}

    def emit(self, event, phase='observed', **values):
        global _warned
        if not self.enabled:
            return
        # Only named numeric/identity fields; arbitrary payloads never enter traces.
        fields = dict(_context.get())
        if ('issue_id' not in values and (
                'issue_number' in values and values['issue_number'] != fields.get('issue_number')
                or 'repository' in values and values['repository'] != fields.get('repository'))):
            fields.pop('issue_id', None)
        fields.update(values)
        if not fields.get('project_id') and fields.get('repository'):
            fields['project_id'] = self.projects.get(fields['repository'])
        if not fields.get('project_id') and not fields.get('repository') and not fields.get('issue_number') and self.projects:
            # Global dispatcher boundaries belong to every configured timeline.
            for repository, project_id in self.projects.items():
                self.emit(event, phase, **dict(fields, repository=repository, project_id=project_id))
            return
        allowed = ('project_id', 'repository', 'issue_id', 'issue_number', 'cycle_id',
                   'operation_id', 'request_id', 'duration_ms', 'queue_wait_ms', 'http_status')
        record = {k: fields[k] for k in allowed if fields.get(k) is not None}
        safe_details = {k: fields[k] for k in ('method', 'endpoint', 'parent_operation_id', 'state', 'target', 'retry') if fields.get(k) is not None}
        record.update(timestamp_utc=datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                      event=event, phase=phase, details=json.dumps(safe_details))
        try:
            with sqlite3.connect(self.path, timeout=.01) as db:
                db.execute(f"INSERT INTO dispatcher_timing_events ({','.join(record)}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))
        except (sqlite3.Error, OSError):
            if not _warned:
                logging.getLogger(__name__).warning('Timing storage unavailable; diagnostic events dropped')
                _warned = True


def attach(store, config, adapter=None):
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    store.timing = Trace(path, getattr(config, 'timing_enabled', False))
    store.timing.projects = {p.repository.casefold(): p.id for p in config.projects}
    if adapter is not None:
        adapter.timing = store.timing
        adapter.timing_projects = {p.repository.casefold(): p.id for p in config.projects}


def measured(event):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self, *args, **kwargs):
            trace = getattr(self, 'timing', None)
            if trace is None:
                trace = getattr(getattr(self, 'store', None), 'timing', None)
            if trace is None:
                trace = getattr(getattr(self, 'adapter', None), 'timing', None)
            if trace is None or not trace.enabled:
                return fn(self, *args, **kwargs)
            fields = dict(_context.get())
            fields['parent_operation_id'] = fields.get('operation_id')
            fields['operation_id'] = uuid.uuid4().hex
            for key, target in (('repository', 'repository'), ('number', 'issue_number'), ('target', 'target')):
                if key in kwargs:
                    fields[target] = kwargs[key]
            if event == 'outbox_enqueue' and args:
                fields['operation_id'] = args[0]  # Durable key survives retry/restart.
                fields['retry'] = bool(kwargs.get('retry'))
                if len(args) > 1:
                    row = self.db.execute('SELECT t.* FROM tasks t JOIN task_iterations i ON i.issue_id=t.issue_id WHERE i.iteration_id=?', (args[1],)).fetchone()
                    if row:
                        fields.update(project_id=row['project_id'], repository=row['repository'], issue_id=row['issue_id'], issue_number=row['number'])
            if event == 'container_intent' and len(args) > 1:
                row = args[0]
                fields['operation_id'] = f'container:{row["issue_id"]}:{row["current_iteration_id"]}:{args[1]}'
            if event == 'poll':
                fields['cycle_id'] = uuid.uuid4().hex
            for arg in args:
                if hasattr(arg, 'repository') and hasattr(arg, 'id'):
                    fields.update(project_id=arg.id, repository=arg.repository.casefold())
                if isinstance(arg, (dict, sqlite3.Row)):
                    for key, target in (('project_id', 'project_id'), ('repository', 'repository'),
                                        ('issue_id', 'issue_id'), ('number', 'issue_number')):
                        if key in arg.keys():
                            fields[target] = arg[key]
            token = _context.set(fields)
            start = time.monotonic_ns()
            trace.emit(event, 'start')
            before_states = {}
            if event in ('sqlite_sync', 'approval', 'state_transition'):
                try:
                    before_states = {r['issue_id']: r['state'] for r in self.queue()}
                except sqlite3.Error:
                    pass
            try:
                result = fn(self, *args, **kwargs)
                if event in ('sqlite_sync', 'approval', 'state_transition'):
                    try:
                        after_states = self.queue()
                    except sqlite3.Error:
                        after_states = []
                    for row in after_states:
                        if before_states.get(row['issue_id']) != row['state']:
                            trace.emit('state_changed', project_id=row['project_id'], repository=row['repository'],
                                       issue_id=row['issue_id'], issue_number=row['number'], state=row['state'])
                trace.emit(event, 'success', duration_ms=(time.monotonic_ns()-start)/1e6)
                if event == 'poll' and isinstance(result, dict) and not any(result.get(k) for k in ('discovered', 'updated', 'revoked', 'commands', 'errors')):
                    trace.emit('poll_empty')
                return result
            except BaseException:
                trace.emit(event, 'failure', duration_ms=(time.monotonic_ns()-start)/1e6)
                raise
            finally:
                _context.reset(token)
        return wrapped
    return decorate


def export(path, project, issue=None, limit=100, format='table', clear=False):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        where, args = 'project_id=?', [project]
        if issue is not None:
            where += ' AND issue_number=?'
            args.append(issue)
        if clear:
            db.execute('DELETE FROM dispatcher_timing_events WHERE ' + where, args)
            return ''
        rows = [dict(r) for r in db.execute('SELECT * FROM dispatcher_timing_events WHERE ' + where + ' ORDER BY id DESC LIMIT ?', [*args, limit])][::-1]
    if format == 'json':
        return json.dumps(rows, ensure_ascii=False)
    if format == 'csv':
        output = io.StringIO()
        columns = [c.strip().split()[0] for c in SCHEMA.split('(', 1)[1].split(');', 1)[0].split(',')]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        # Neutralize spreadsheet formulas in exported cells.
        writer.writerows({k: "'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k,v in r.items()} for r in rows)
        return output.getvalue().rstrip()
    lines = ['timestamp (UTC) | project | issue | event | phase | duration_ms | queue_wait_ms | HTTP | delta_ms | cycle / operation / request']
    previous = None
    for r in rows:
        stamp = datetime.fromisoformat(r['timestamp_utc'])
        delta = '' if previous is None else f'{(stamp-previous).total_seconds()*1000:.3f}'
        previous = stamp
        lines.append(' | '.join(str(v if v is not None else '') for v in (r['timestamp_utc'], r['project_id'], r['issue_number'], r['event'], r['phase'], r['duration_ms'], r['queue_wait_ms'], r['http_status'], delta, f"{r['cycle_id']} / {r['operation_id']} / {r['request_id']}")))
    return '\n'.join(lines)
