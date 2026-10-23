"""Versioned SQLite snapshots. Transactions serialize polling and future approval."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone


MUTABLE = ('discovered', 'pending', 'revoked')


def now():
    return datetime.now(timezone.utc).isoformat()


def content_hash(issue):
    # Labels, timestamps and other transport metadata do not invalidate approval.
    value = {key: issue[key] for key in ('id', 'title', 'body', 'target_branch', 'author')}
    value['author'] = value['author'].casefold()
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        with self.db:
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tasks(
                repository TEXT NOT NULL, number INTEGER NOT NULL, issue_id INTEGER NOT NULL,
                project_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'discovered',
                available INTEGER NOT NULL CHECK(available IN (0,1)),
                version INTEGER NOT NULL, content_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
                observed TEXT NOT NULL, approved_version INTEGER,
                created_at TEXT NOT NULL, synced_at TEXT NOT NULL,
                PRIMARY KEY(repository, number));
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                version INTEGER NOT NULL, event TEXT NOT NULL, snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(repository,number) REFERENCES tasks(repository,number));
            INSERT OR IGNORE INTO schema_migrations VALUES(1);
            ''')
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                version INTEGER NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT NOT NULL, event_id TEXT,
                UNIQUE(repository,event_id));
            """)
        try:
            self._migrate_states()
        except sqlite3.Error:
            self.db.close()
            raise

    def _migrate_states(self):
        """Version 2: pending means a current, available, confirmed snapshot.

        Serialize the marker check with conversion so concurrent opens cannot
        repeat migration. Execution snapshots, versions and history stay intact.
        """
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
                return
            self.db.execute('''UPDATE tasks SET
                state=CASE
                    WHEN available=0 THEN 'revoked'
                    WHEN approved_version=version THEN 'pending'
                    ELSE 'discovered' END,
                approved_version=CASE
                    WHEN available=1 AND approved_version=version THEN approved_version
                    ELSE NULL END
                WHERE state IN ('approved','pending')''')
            self.db.execute("UPDATE tasks SET approved_version=NULL WHERE state IN ('discovered','revoked')")
            self.db.execute('INSERT INTO schema_migrations VALUES(2)')

    def close(self):
        self.db.close()

    def get(self, repository, number):
        return self.db.execute('SELECT * FROM tasks WHERE repository=? AND number=?',
                               (repository.casefold(), number)).fetchone()

    def confirm(self, repository, number, expected_version):
        """Stage 3 contract: confirm only the available, current discovered version.

        Polling never invokes this operation. A stale version or ineligible
        state returns False without changing the record or its history.
        """
        repository = repository.casefold()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cursor = self.db.execute("""UPDATE tasks SET state='pending',approved_version=version
                WHERE repository=? AND number=? AND version=? AND available=1
                AND state='discovered'""", (repository, number, expected_version))
            if cursor.rowcount:
                row = self.get(repository, number)
                self._history(repository, number, row['version'], 'confirmed', row['snapshot'], now())
            return cursor.rowcount == 1

    def queue(self, repository=None):
        if repository:
            return self.db.execute('SELECT * FROM tasks WHERE repository=? ORDER BY number',
                                   (repository.casefold(),)).fetchall()
        return self.db.execute('SELECT * FROM tasks ORDER BY repository,number').fetchall()

    def decide(self, repository, number, version, action, source='cli', event_id=None, valid=True, expected_synced_at=None):
        """First committed decision wins, including across independent connections."""
        if action not in ('approve', 'reject') or source not in ('cli', 'github'):
            raise ValueError('Invalid decision')
        repository = repository.casefold()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if event_id is not None and self.db.execute(
                    'SELECT 1 FROM decisions WHERE repository=? AND event_id=?', (repository, str(event_id))).fetchone():
                return 'duplicate'
            row = self.get(repository, number)
            valid = valid and (expected_synced_at is None or (row is not None and row['synced_at'] == expected_synced_at))
            result = 'unverified' if not valid else 'conflict'
            if valid and row and row['version'] == version and row['available'] and row['state'] == 'discovered':
                state = 'pending' if action == 'approve' else 'rejected'
                self.db.execute('UPDATE tasks SET state=?,approved_version=? WHERE repository=? AND number=?',
                                (state, version if action == 'approve' else None, repository, number))
                self._history(repository, number, version, action, row['snapshot'], now())
                result = 'applied'
            elif valid and row and row['version'] == version and row['state'] == ('pending' if action == 'approve' else 'rejected'):
                result = 'already_applied'
            self.db.execute('INSERT INTO decisions(repository,number,version,source,action,result,created_at,event_id) VALUES(?,?,?,?,?,?,?,?)',
                            (repository, number, version, source, action, result, now(),
                             str(event_id) if event_id is not None else None))
            return result

    def reset_confirmation(self, repository, number, expected_version):
        """Compare-and-reset operation for stage 3; never changes active attempts."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cursor = self.db.execute('''UPDATE tasks SET approved_version=NULL,state='discovered'
                WHERE repository=? AND number=? AND version=? AND available=1
                AND state IN ('pending','discovered')''',
                (repository.casefold(), number, expected_version))
            return cursor.rowcount == 1

    def apply(self, project, issues, *, observation=None):
        counts = dict(discovered=0, updated=0, unchanged=0, revoked=0, skipped_untrusted=0)
        repository, stamp = project.repository.casefold(), now()
        seen = set()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for issue in issues:
                number = issue['number']
                seen.add(number)
                row = self.get(repository, number)
                if not issue['eligible']:
                    if issue['author'].casefold() not in {a.casefold() for a in project.allowed_authors}:
                        counts['skipped_untrusted'] += 1
                    if row:
                        self._revoke(row, stamp, json.dumps(issue), counts)
                    continue
                snapshot, digest = json.dumps(issue, sort_keys=True), content_hash(issue)
                if row is None:
                    self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (repository, number, issue['id'], project.id, 'discovered', 1, 1, digest,
                         snapshot, snapshot, None, stamp, stamp))
                    self._history(repository, number, 1, 'discovered', snapshot, stamp)
                    counts['discovered'] += 1
                    continue
                changed = row['content_hash'] != digest
                if row['state'] in MUTABLE:
                    version = row['version'] + int(changed)
                    state = 'discovered' if changed or not row['available'] or row['state'] == 'revoked' else row['state']
                    approved = row['approved_version'] if state == 'pending' else None
                    self.db.execute('''UPDATE tasks SET snapshot=?,observed=?,content_hash=?,version=?,
                        state=?,available=1,approved_version=?,synced_at=? WHERE repository=? AND number=?''',
                        (snapshot, snapshot, digest, version, state, approved, stamp, repository, number))
                    if changed or not row['available']:
                        self._history(repository, number, version, 'updated' if changed else 'restored', snapshot, stamp)
                    counts['updated' if changed or not row['available'] else 'unchanged'] += 1
                else:
                    if row['observed'] != snapshot:
                        self._history(repository, number, row['version'], 'observed', snapshot, stamp)
                    self.db.execute('UPDATE tasks SET observed=?,synced_at=? WHERE repository=? AND number=?',
                                    (snapshot, stamp, repository, number))
                    counts['updated' if changed else 'unchanged'] += 1
            for row in self.db.execute('SELECT * FROM tasks WHERE repository=?', (repository,)).fetchall():
                if row['number'] not in seen:
                    self._revoke(row, stamp, row['observed'], counts)
            if observation is not None:
                observation.update({row['number']: dict(row) for row in self.queue(repository)})
        return counts

    def _history(self, repository, number, version, event, snapshot, stamp):
        self.db.execute('INSERT INTO history(repository,number,version,event,snapshot,created_at) VALUES(?,?,?,?,?,?)',
                        (repository, number, version, event, snapshot, stamp))

    def _revoke(self, row, stamp, observed, counts):
        mutable = row['state'] in MUTABLE
        self.db.execute('''UPDATE tasks SET observed=?,synced_at=?,available=0,
            state=?,approved_version=? WHERE repository=? AND number=?''',
            (observed, stamp, 'revoked' if mutable else row['state'],
             None if mutable else row['approved_version'], row['repository'], row['number']))
        if row['available']:
            self._history(row['repository'], row['number'], row['version'], 'revoked', observed, stamp)
            counts['revoked'] += 1
