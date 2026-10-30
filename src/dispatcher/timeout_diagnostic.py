"""Diagnostic deadlines with durable delivery and lost-response reconciliation."""
import fcntl
from pathlib import Path
from datetime import datetime, timezone

from dispatcher.github import GitHubError


class TimeoutDiagnostics:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter

    def record(self, attempt, elapsed, limit):
        body = (f'Превышено время выполнения Codex: {elapsed:.1f} с, лимит {limit:g} с. '
                'Codex продолжает работать; выполнение не прерывается.\n'
                f'<!-- codex-timeout:{attempt} -->')
        with self.store.db as db:
            db.execute('''INSERT OR IGNORE INTO timeout_outbox
                (attempt_id,elapsed_seconds,limit_seconds,body) VALUES(?,?,?,?)''',
                (attempt, elapsed, limit, body))

    def recovered(self, limit):
        """Recover diagnostic deadlines even when the daemon cannot reap old Codex."""
        from dispatcher.runtime import group_members
        for row in self.store.db.execute("SELECT * FROM attempts WHERE state='running' AND codex_started=1").fetchall():
            if row['pid'] and group_members(row['pid']):
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row['codex_started_at'] or row['started_at'])).total_seconds()
                if elapsed >= limit:
                    self.record(row['attempt_id'], elapsed, limit)

    def flush(self):
        db = self.store.db
        database = Path(db.execute('PRAGMA database_list').fetchone()[2])
        with (database.parent / 'timeout-outbox.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for row in db.execute('''SELECT o.*,a.repository,a.number FROM timeout_outbox o
                    JOIN attempts a USING(attempt_id) WHERE delivered=0''').fetchall():
                try:
                    endpoint = f'repos/{row["repository"]}/issues/{row["number"]}/comments'
                    comments = self.adapter.pages(endpoint)
                    if not isinstance(comments, list):
                        raise GitHubError(kind='invalid_response')
                    marker = f'<!-- codex-timeout:{row["attempt_id"]} -->'
                    found = next((c for c in comments if marker in c.get('body', '')), None)
                    if found is None:
                        found = self.adapter.write(endpoint, 'POST', {'body': row['body']})
                    if not isinstance(found, dict) or not found.get('id'):
                        raise GitHubError(kind='invalid_response')
                    with db:
                        db.execute('UPDATE timeout_outbox SET delivered=1,comment_id=?,error=NULL WHERE attempt_id=?',
                                   (str(found['id']), row['attempt_id']))
                except (GitHubError, KeyError, TypeError):
                    with db:
                        db.execute("UPDATE timeout_outbox SET error='delivery_failed' WHERE attempt_id=?", (row['attempt_id'],))
