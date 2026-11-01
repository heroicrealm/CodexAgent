"""Stage 4 transactional admission, durable attempts and result outbox."""
import uuid
from datetime import datetime
from dispatcher.storage import now


class ExecutionStore:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def reserve(self, row, branch, maximum, observation):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return self._reserve(row, branch, maximum, observation)

    def _reserve(self, row, branch, maximum, observation, *, event_key=None, actor='scheduler', evidence=None):
        """Admission implementation; caller owns the SQLite transaction."""
        guard = self.store.guard(row['repository'], row['number'])
        if (row['issue_type'] != 'issue' or not row['run_codex']
                or self.store.stop_requested(row) or self.store.child_update_pending(row)):
            return None
        if not guard or guard['observation_token'] != observation:
            return None
        if self.db.execute("SELECT count(*) FROM attempts WHERE state='running'").fetchone()[0] >= maximum:
            return None
        if self.db.execute("SELECT 1 FROM attempts WHERE repository=? AND number=? AND state='running'",
                           (row['repository'], row['number'])).fetchone():
            return None
        if self.db.execute("SELECT 1 FROM attempts WHERE repository=? AND branch=? AND state='running'",
                           (row['repository'], branch)).fetchone():
            return None
        if self.db.execute("""SELECT 1 FROM attempts a JOIN tasks t ON t.repository=a.repository AND t.number=a.number
                           WHERE a.repository=? AND a.branch=? AND a.number!=?
                           AND (a.state='running' OR t.state NOT IN ('done','cancelled','revoked','detached'))""",
                           (row['repository'], branch, row['number'])).fetchone():
            return None
        if self.db.execute("""SELECT 1 FROM tasks WHERE repository=? AND number!=?
                AND work_branch=? AND state NOT IN ('done','cancelled','revoked','detached')""",
                (row['repository'], row['number'], branch)).fetchone():
            return None
        current = self.store.get(row['repository'], row['number'])
        if (not current or (current['work_branch'] is not None and current['work_branch'] != branch)
                or current['state'] != 'pending' or not current['available']
                or current['approved_version'] != current['version'] or current['content_hash'] != row['content_hash']
                or current['version'] != row['version'] or current['current_iteration_id'] != row['current_iteration_id']):
            return None
        if not self.store._set_state(current, 'running', current['approved_version']):
            return None
        current = self.store.get(row['repository'], row['number'])
        if current['current_iteration_id'] is None:
            raise RuntimeError('Attempt requires an approved iteration')
        if self.db.execute('SELECT 1 FROM attempts WHERE iteration_id=?', (current['current_iteration_id'],)).fetchone():
            raise RuntimeError('Automatic Codex retry is forbidden')
        attempt = uuid.uuid4().hex
        self.db.execute('''INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,iteration_id,attempt_number)
            VALUES(?,?,?,?,?,'running',?,?,1)''',
            (attempt, row['repository'], row['number'], row['version'], branch, now(), self.store.get(row['repository'], row['number'])['current_iteration_id']))
        self.store._record_transition(row, 'running', event_key or 'reserve:' + attempt, actor, evidence, iteration_id=current['current_iteration_id'])
        self.store._history(row['repository'], row['number'], row['version'], 'running', row['snapshot'], now())
        return attempt

    def update(self, attempt, **values):
        allowed = {'worktree','initial_sha','result_sha','exit_code','codex_started','pushed','pid','process_identity',
                   'source_base_branch','branch_existed','origin_url','remote_sha','recovery_diagnostic'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid attempt update')
        with self.db:
            self.db.execute('UPDATE attempts SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE attempt_id=?',
                            (*values.values(), attempt))

    def get(self, attempt):
        return self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()

    def finish(self, attempt, state, code=None, category='CODEX', duration=None):
        if state not in ('review', 'failed', 'interrupted'):
            raise ValueError('Invalid execution outcome')
        from dispatcher.publisher import comment_body
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(attempt)
            if self.store.stop_requested(self.store.get(row['repository'], row['number'])):
                return
            if row['state'] != 'running':
                return
            if state == 'review':
                task = self.store.get(row['repository'], row['number'])
                if task['state'] != 'review' or not self.db.execute(
                        'SELECT 1 FROM iteration_pr_history WHERE iteration_id=?', (row['iteration_id'],)).fetchone():
                    raise RuntimeError('Review requires verified PR evidence')
            finished = now()
            if duration is None:
                duration = max(0, (datetime.fromisoformat(finished) - datetime.fromisoformat(row['started_at'])).total_seconds())
            self.db.execute('''UPDATE attempts SET state=?,error_code=?,category=?,finished_at=?,duration=?
                WHERE attempt_id=?''', (state, code, category, finished, duration, attempt))
            before = self.store.get(row['repository'], row['number'])
            if before['state'] == 'running' and before['current_iteration_id'] == row['iteration_id']:
                self.store._set_state(before, state, before['approved_version'])
                self.store._record_transition(before, state, 'finish:' + attempt, 'scheduler', {'attempt_id': attempt, 'error_code': code})
            task = self.store.get(row['repository'], row['number'])
            self.store._history(row['repository'], row['number'], row['version'], state, task['snapshot'], finished)
            body = comment_body(self.get(attempt))
            self.db.execute('INSERT OR IGNORE INTO result_outbox(attempt_id,body) VALUES(?,?)', (attempt, body))
            self.db.execute("INSERT OR IGNORE INTO publication_operations VALUES(?,?,'intent','pending',0,?)",
                            ('comment:' + attempt, row['iteration_id'], finished))

    def quota(self, state, diagnostic):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO quota_observation VALUES(1,?,?,?)', (state, now(), diagnostic))
