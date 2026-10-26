"""Durable per-Issue stop intent, recovery and deduplicated diagnostics.

Lifecycle owns signaling/reaping. This module never substitutes a global stop.
Network calls do not run under a SQLite writer transaction.
"""
import fcntl
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from dispatcher.github import Adapter, GitHubError
from dispatcher.storage import Store, now
from dispatcher.workspace import TaskError


def live_processes(store, attempt):
    from dispatcher.runtime import group_members
    if not attempt:
        return []
    candidates = [dict(attempt)] if attempt['pid'] else []
    candidates += [dict(p) for p in store.db.execute(
        'SELECT pid,process_identity FROM attempt_processes WHERE attempt_id=? AND finished_at IS NULL',
        (attempt['attempt_id'],))]
    return [p for p in candidates if group_members(p['pid'])]


def request_in_transaction(store, row, reason, evidence):
    """Caller owns BEGIN IMMEDIATE; row is the verified current generation."""
    db = store.db
    if reason == 'ready_removed' and row['state'] in ('done','cancelled'):
        return None
    prior = db.execute('SELECT * FROM stop_requests WHERE issue_id=? AND active=1', (row['issue_id'],)).fetchone()
    if prior:
        if reason == 'emergency_stop' and prior['reason'] != reason:
            db.execute("UPDATE stop_requests SET reason=?,evidence=?,error=NULL WHERE request_id=?",
                       (reason, json.dumps(evidence), prior['request_id']))
        return prior['request_id']
    attempt = db.execute('SELECT * FROM attempts WHERE repository=? AND number=? ORDER BY rowid DESC LIMIT 1',
                         (row['repository'], row['number'])).fetchone()
    phase = 'idle'
    if attempt and attempt['state'] == 'running':
        phase = 'codex' if attempt['codex_started'] and attempt['exit_code'] is None else 'preparation'
        if db.execute("SELECT 1 FROM publication_operations WHERE operation_key=?", ('pr:' + attempt['attempt_id'],)).fetchone():
            phase = 'publication'
    cursor = db.execute('''INSERT INTO stop_requests(issue_id,iteration_id,version,attempt_id,reason,
        previous_state,stage,requested_at,evidence) VALUES(?,?,?,?,?,?,?,?,?)''',
        (row['issue_id'], row['current_iteration_id'], row['version'], attempt['attempt_id'] if attempt else None,
         reason, row['state'], phase, now(), json.dumps(evidence)))
    db.execute('UPDATE tasks SET available=0,approved_version=NULL WHERE issue_id=? AND version=? AND current_iteration_id IS ?',
               (row['issue_id'], row['version'], row['current_iteration_id']))
    store._history(row['repository'], row['number'], row['version'], reason, json.dumps(evidence), now())
    from dispatcher.runtime import group_members
    cleaning = db.execute("SELECT 1 FROM cleanup_jobs WHERE issue_id=? AND state='running'", (row['issue_id'],)).fetchone()
    if not cleaning and (not attempt or attempt['state'] != 'running') and not live_processes(store, attempt):
        if row['state'] not in ('done','cancelled','detached'):
            db.execute("UPDATE tasks SET state='detached' WHERE issue_id=?", (row['issue_id'],))
            store._record_transition(row, 'detached', 'detach:' + str(cursor.lastrowid), 'stop_controller', evidence)
        db.execute("UPDATE stop_requests SET process_status='confirmed',finalized_at=? WHERE request_id=?", (now(), cursor.lastrowid))
    return cursor.lastrowid


def accept_observation(store, row, server_at):
    """Caller owns the writer; older remote snapshots cannot stop a new cycle."""
    if not server_at:
        return True  # Legacy snapshots; never sufficient for automatic reattach.
    from dispatcher.queue import timestamp
    try:
        stamp = timestamp(server_at)
        if stamp.tzinfo is None:
            return False
        previous = store.db.execute('SELECT server_at FROM control_observations WHERE issue_id=?', (row['issue_id'],)).fetchone()
        if previous and stamp < timestamp(previous['server_at']):
            return False
    except (ValueError, TypeError):
        return False
    store.db.execute('INSERT OR REPLACE INTO control_observations VALUES(?,?)', (row['issue_id'], server_at))
    return True


def newer_observation(stop, issue):
    from dispatcher.queue import timestamp
    try:
        before = json.loads(stop['evidence']).get('observed_at')
        after = issue.get('server_observed_at')
        return bool(before and after and timestamp(after) > timestamp(before))
    except (ValueError, TypeError):
        return False


class StopController:
    def __init__(self, config, store, adapter):
        self.config, self.store, self.adapter = config, store, adapter

    def observe(self, project, row, raw=None):
        """Targeted read covers closed Issues too. Emergency requires human proof."""
        raw = self.adapter.control_issue(project, row["number"]) if raw is None else raw
        if not isinstance(raw, dict) or raw.get('id') != row['issue_id'] or raw.get('number') != row['number']:
            raise GitHubError(kind='invalid_response')
        names = {v['name'].casefold() for v in raw['labels']}
        emergency = project.label('emergency_stop').name.casefold()
        ready = project.task_label.casefold()
        reason, proof = None, {'issue_id': row['issue_id'], 'observed_at': self.adapter.response_date}
        if emergency in names:
            events, issue = self.adapter.command_events(project, row['number'])
            latest = next((e for e in sorted(events, key=lambda e: e['id'], reverse=True)
                           if e.get('event') in ('labeled','unlabeled') and
                           e.get('label', {}).get('name', '').casefold() == emergency), None)
            actor = (latest or {}).get('actor') or {}
            current_names = {v['name'].casefold() for v in issue['labels']['nodes']}
            if (issue['databaseId'] != row['issue_id'] or issue['labels']['pageInfo']['hasNextPage']
                    or emergency not in current_names or not latest or latest['event'] != 'labeled'
                    or actor.get('type') != 'User'
                    or actor.get('login','').casefold() not in {a.casefold() for a in project.allowed_authors}):
                raise GitHubError(kind='invalid_response')
            from dispatcher.queue import timestamp
            if not self.adapter.response_date or timestamp(latest['created_at']) > timestamp(self.adapter.response_date):
                raise GitHubError(kind='invalid_response')
            reason, proof = 'emergency_stop', {'event': latest, 'observed_at': self.adapter.response_date}
        elif ready not in names:
            reason = 'ready_removed'
        with self.store.db as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.store.get(project.repository, row['number'])
            if (current and current['issue_id'] == row['issue_id'] and current['version'] == row['version']
                    and current['current_iteration_id'] == row['current_iteration_id']
                    and accept_observation(self.store, current, proof.get('observed_at'))):
                if reason:
                    request_in_transaction(self.store, current, reason, proof)
        return names

    def signal(self, lifecycle, stop, attempt):
        # Share Lifecycle ownership, and recheck durable backoff under its lock:
        # simultaneous watcher/recovery must not repeatedly TERM a stuck group.
        with lifecycle.mutex:
            current = self.store.db.execute('SELECT * FROM stop_requests WHERE request_id=?', (stop['request_id'],)).fetchone()
            stamp = datetime.now(timezone.utc)
            if current['next_signal_at'] and datetime.fromisoformat(current['next_signal_at']) > stamp:
                return
            method, confirmed = lifecycle.stop_attempt(attempt['pid'], attempt['process_identity'])
            with self.store.db as db:
                db.execute("""UPDATE stop_requests SET method=CASE WHEN ?='not_required' THEN method ELSE ? END,
                    process_status=?,error=?,next_signal_at=? WHERE request_id=?""",
                    (method, method, 'confirmed' if confirmed else 'unconfirmed',
                     None if confirmed else 'process_stop_unconfirmed',
                     None if confirmed else (datetime.now(timezone.utc) + timedelta(seconds=self.config.emergency_poll_interval_seconds)).isoformat(), stop['request_id']))

    def finalize(self, row, *, recovered=False):
        """Only the scheduler calls this for running attempts after its work ends."""
        from dispatcher.runtime import group_members
        db = self.store.db
        stop = db.execute('SELECT * FROM stop_requests WHERE issue_id=? AND active=1', (row['issue_id'],)).fetchone()
        if not stop:
            return False
        if stop['finalized_at']:
            return True
        attempt = db.execute('SELECT * FROM attempts WHERE attempt_id=?', (stop['attempt_id'],)).fetchone()
        if live_processes(self.store, attempt):
            return False
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM cleanup_jobs WHERE issue_id=? AND state='running'", (row['issue_id'],)).fetchone():
                return False
            current = self.store.get(row['repository'], row['number'])
            if current['version'] != stop['version'] or current['current_iteration_id'] != stop['iteration_id']:
                return False
            if attempt:
                db.execute('UPDATE attempt_processes SET finished_at=COALESCE(finished_at,?) WHERE attempt_id=?', (now(), attempt['attempt_id']))
            if attempt and attempt['state'] == 'running':
                # A successful natural exit remains successful, while task detaches.
                outcome = 'completed' if attempt['exit_code'] == 0 else 'interrupted' if stop['reason'] == 'emergency_stop' or recovered else 'failed'
                db.execute('''UPDATE attempts SET state=?,finished_at=COALESCE(finished_at,?),
                    error_code=?,category='DISPATCHER' WHERE attempt_id=? AND state='running' ''',
                    (outcome, now(), stop['reason'], attempt['attempt_id']))
            if current['state'] not in ('done','cancelled','detached'):
                changed = db.execute("""UPDATE tasks SET state='detached',available=0,approved_version=NULL
                    WHERE issue_id=? AND version=? AND current_iteration_id IS ? AND state=?""",
                    (current['issue_id'], current['version'], current['current_iteration_id'], current['state'])).rowcount
                if changed:
                    self.store._record_transition(current, 'detached', 'detach:' + str(stop['request_id']),
                                                  'stop_controller', json.loads(stop['evidence']))
            db.execute("UPDATE stop_requests SET finalized_at=COALESCE(finalized_at,?),process_status='confirmed',error=NULL WHERE request_id=?",
                       (now(), stop['request_id']))
        return True

    def diagnostics(self):
        """One durable, reconcilable comment; pending cleanup is explicitly reported.

        Later cleanup progress edits the same comment, never creates a second one.
        """
        db = self.store.db
        for stop in db.execute("SELECT s.*,t.repository,t.number FROM stop_requests s JOIN tasks t USING(issue_id) WHERE reason='emergency_stop'").fetchall():
            cleanup = db.execute('SELECT state,last_error FROM cleanup_jobs WHERE issue_id=? AND generation=? AND version=?',
                                 (stop['issue_id'], stop['iteration_id'] or 0, stop['version'])).fetchone()
            attempt = db.execute('SELECT * FROM attempts WHERE attempt_id=?', (stop['attempt_id'],)).fetchone()
            published = dict(branch=attempt['branch'], sha=attempt['result_sha'], remote_sha=attempt['remote_sha'], pushed=attempt['pushed']) if attempt else {}
            if published and (not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', published['branch']) or re.search(r'(?:gh[pousr]_|github_pat_|sk-|AKIA)', published['branch'])):
                published['branch'] = '[redacted]'
            prs = [r[0] for r in db.execute('SELECT pr_number FROM task_pull_requests WHERE issue_id=?', (stop['issue_id'],))]
            prs += [r[0] for r in db.execute('SELECT p.pr_number FROM attempt_publications p JOIN attempts a USING(attempt_id) WHERE a.repository=? AND a.number=?', (stop['repository'], stop['number'])) if r[0] not in prs]
            body = ('Emergency stop\n' + json.dumps(dict(reason=stop['reason'], previous_state=stop['previous_state'],
                stage=stop['stage'], attempt=stop['attempt_id'], method=stop['method'],
                process_status=stop['process_status'], cleanup=dict(cleanup) if cleanup else {'state':'pending'},
                published=published, prs=prs, error=stop['error'],
                publication=[dict(p) for p in db.execute('SELECT phase,outcome FROM publication_operations WHERE iteration_id=?', (stop['iteration_id'],))],
                publication_diagnostic=attempt['recovery_diagnostic'] if attempt else None), ensure_ascii=True, sort_keys=True) +
                f'\n<!-- codex-stop:{stop["request_id"]}:{stop["issue_id"]} -->')
            # Escape JSON as a code block to keep branch metadata out of Markdown.
            lines = body.splitlines()
            body = lines[0] + '\n```json\n' + lines[1].replace('`', '\\u0060').replace('<','\\u003c') + '\n```\n' + lines[2]
            with db:
                db.execute('''INSERT INTO stop_outbox(request_id,body) VALUES(?,?)
                    ON CONFLICT(request_id) DO UPDATE SET body=excluded.body,
                    delivered=CASE WHEN body=excluded.body THEN delivered ELSE 0 END''', (stop['request_id'], body))
        database = Path(db.execute('PRAGMA database_list').fetchone()[2])
        with (database.parent / 'stop-outbox.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for out in db.execute('''SELECT o.*,t.repository,t.number,t.issue_id FROM stop_outbox o
                    JOIN stop_requests s USING(request_id) JOIN tasks t USING(issue_id) WHERE delivered=0''').fetchall():
                try:
                    endpoint = f'repos/{out["repository"]}/issues/{out["number"]}/comments'
                    marker = f'<!-- codex-stop:{out["request_id"]}:{out["issue_id"]} -->'
                    comments = self.adapter.pages(endpoint)
                    if not isinstance(comments, list):
                        raise GitHubError(kind='invalid_response')
                    found = next((c for c in comments if marker in c.get('body','')), None)
                    if found is None:
                        found = self.adapter.write(endpoint, 'POST', {'body':out['body']})
                    elif found.get('body') != out['body']:
                        found = self.adapter.write(endpoint + '/' + str(found['id']), 'PATCH', {'body':out['body']})
                    if not isinstance(found, dict) or not found.get('id'):
                        raise GitHubError(kind='invalid_response')
                    with db:
                        db.execute('UPDATE stop_outbox SET delivered=1,comment_id=?,error=NULL WHERE request_id=? AND body=?',
                                   (str(found['id']), out['request_id'], out['body']))
                except (GitHubError, KeyError, TypeError):
                    with db:
                        db.execute("UPDATE stop_outbox SET error='delivery_failed' WHERE request_id=?", (out['request_id'],))


def fence(store, adapter, project, attempt):
    """Fresh labels plus durable generation check before each irreversible step."""
    task = store.get(attempt['repository'], attempt['number'])
    if store.stop_requested(task):
        raise TaskError('task_detached', 'DISPATCHER')
    StopController(None, store, adapter).observe(project, task)
    task = store.get(attempt['repository'], attempt['number'])
    if (store.stop_requested(task) or task['version'] != attempt['version']
            or task['current_iteration_id'] != attempt['iteration_id']):
        raise TaskError('task_detached', 'DISPATCHER')


class StopWatcher:
    """Independent rate-limited observation even while Git/HTTP blocks scheduler."""
    def __init__(self, config, lifecycle):
        self.config, self.lifecycle = config, lifecycle
        self.stop = Event()
        self.thread = Thread(target=self.run, name='issue-stop-watcher', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        store = None
        try:
            while not self.stop.is_set():
                try:
                    if store is None:
                        store = Store(self.config.workspace / '.runtime' / 'tasks.sqlite3')
                    adapter = Adapter(self.config.github_executable)
                    controller = StopController(self.config, store, adapter)
                    for row in store.queue():
                        if row['state'] != 'running':
                            continue
                        project = next((p for p in self.config.projects if p.repository.casefold() == row['repository']), None)
                        if project is None:
                            continue
                        try:
                            controller.observe(project, row)
                        except (GitHubError, KeyError, TypeError, ValueError):
                            pass
                        saved = store.db.execute("SELECT * FROM stop_requests WHERE issue_id=? AND active=1 AND reason='emergency_stop'", (row['issue_id'],)).fetchone()
                        if saved:
                            attempt = store.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (saved['attempt_id'],)).fetchone()
                            for process in live_processes(store, attempt):
                                controller.signal(self.lifecycle, saved, process)
                    controller.diagnostics()
                except Exception:
                    # Intent stays durable; a transient adapter/SQLite failure
                    # cannot disable future emergency observations.
                    from dispatcher.logging import event
                    self.config.logs.mkdir(parents=True, exist_ok=True)
                    event(self.config.logs / 'dispatcher.jsonl', 'stop_watcher_failed', level='WARN', reason='stop_observation_unavailable')
                self.stop.wait(self.config.emergency_poll_interval_seconds)
        finally:
            if store is not None:
                store.close()

    def close(self):
        self.stop.set()
        self.thread.join()
