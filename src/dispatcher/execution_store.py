"""Stage 4 transactional admission, durable attempts and result outbox."""
import json
import uuid
from datetime import datetime
from dispatcher.storage import now


class ExecutionStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        with self.db:
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS attempts(
                attempt_id TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                version INTEGER NOT NULL, branch TEXT NOT NULL, state TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, duration REAL,
                worktree TEXT, initial_sha TEXT, result_sha TEXT, exit_code INTEGER,
                error_code TEXT, category TEXT, codex_started INTEGER NOT NULL DEFAULT 0,
                pushed INTEGER NOT NULL DEFAULT 0, pid INTEGER, process_identity TEXT,
                origin_url TEXT, remote_sha TEXT, recovery_diagnostic TEXT,
                UNIQUE(repository,number,version));
            CREATE UNIQUE INDEX IF NOT EXISTS attempt_branch ON attempts(repository,branch)
                WHERE state != 'done';
            CREATE TABLE IF NOT EXISTS result_outbox(
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), body TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0, comment_id TEXT);
            CREATE TABLE IF NOT EXISTS quota_observation(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL,
                observed_at TEXT NOT NULL, diagnostic TEXT NOT NULL);
            INSERT OR IGNORE INTO schema_migrations VALUES(3);
            ''')

    def reserve(self, row, branch, maximum, observation):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            guard = self.store.guard(row['repository'], row['number'])
            if not guard or guard['observation_token'] != observation:
                return None
            if self.db.execute("SELECT count(*) FROM attempts WHERE state='running'").fetchone()[0] >= maximum:
                return None
            if self.db.execute("SELECT 1 FROM attempts WHERE repository=? AND branch=? AND state!='done'",
                               (row['repository'], branch)).fetchone():
                return None
            cursor = self.db.execute("""UPDATE tasks SET state='running' WHERE repository=? AND number=?
                AND version=? AND state='pending' AND available=1 AND approved_version=version
                AND content_hash=?""", (row['repository'], row['number'], row['version'], row['content_hash']))
            if not cursor.rowcount:
                return None
            attempt = uuid.uuid4().hex
            self.db.execute('''INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at)
                VALUES(?,?,?,?,?,'running',?)''',
                (attempt, row['repository'], row['number'], row['version'], branch, now()))
            self.store._history(row['repository'], row['number'], row['version'], 'running', row['snapshot'], now())
            return attempt

    def update(self, attempt, **values):
        allowed = {'worktree','initial_sha','result_sha','exit_code','codex_started','pushed','pid','process_identity',
                   'origin_url','remote_sha','recovery_diagnostic'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid attempt update')
        with self.db:
            self.db.execute('UPDATE attempts SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE attempt_id=?',
                            (*values.values(), attempt))

    def get(self, attempt):
        return self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()

    def finish(self, attempt, state, code=None, category='CODEX', duration=None):
        from dispatcher.publisher import comment_body
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(attempt)
            if row['state'] != 'running':
                return
            finished = now()
            if duration is None:
                duration = max(0, (datetime.fromisoformat(finished) - datetime.fromisoformat(row['started_at'])).total_seconds())
            self.db.execute('''UPDATE attempts SET state=?,error_code=?,category=?,finished_at=?,duration=?
                WHERE attempt_id=?''', (state, code, category, finished, duration, attempt))
            self.db.execute('UPDATE tasks SET state=? WHERE repository=? AND number=? AND state=\'running\'',
                            (state, row['repository'], row['number']))
            task = self.store.get(row['repository'], row['number'])
            self.store._history(row['repository'], row['number'], row['version'], state, task['snapshot'], finished)
            body = comment_body(self.get(attempt))
            self.db.execute('INSERT OR IGNORE INTO result_outbox(attempt_id,body) VALUES(?,?)', (attempt, body))

    def quota(self, state, diagnostic):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO quota_observation VALUES(1,?,?,?)', (state, now(), diagnostic))
