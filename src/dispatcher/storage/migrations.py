"""One-time, offline operational reset for the stage 6 schema."""
import fcntl
import os
import sqlite3
import uuid
from pathlib import Path
from contextlib import closing

VERSION = 602


class MigrationError(RuntimeError):
    """Safe operator-facing migration failure."""


class MigrationBusy(MigrationError):
    """A running dispatcher owns the workspace lock."""



def prepare(path):
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' not in tables or ('schema_migrations' in tables and db.execute(
                'SELECT 1 FROM schema_migrations WHERE version=?', (VERSION,)).fetchone()):
            return
        fd = os.open(path.parent / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationBusy('Schema migration requires a stopped dispatcher') from None
            # Recheck under the workspace lock: another opener may have
            # completed the migration since our initial schema read.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone() and db.execute(
                    'SELECT 1 FROM schema_migrations WHERE version=?', (VERSION,)).fetchone():
                return
            backup = path.with_name(path.name + '.pre-602-' + uuid.uuid4().hex + '.bak')
            # No SQLite write transaction may be held during backup. The
            # workspace lock excludes the dispatcher and other migrators, while
            # SQLite backup supplies a consistent snapshot including WAL pages.
            backup_fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(backup_fd)
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
            with backup.open('rb') as stream:
                os.fsync(stream.fileno())
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # Reserve the writer only after the backup is verified and durable.
            # DDL, reset, validation and the marker remain one transaction.
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone() and db.execute(
                    'SELECT 1 FROM schema_migrations WHERE version=?', (VERSION,)).fetchone():
                db.rollback()
                return
            for name in ('result_outbox', 'attempts', 'history', 'decisions', 'command_guards',
                         'command_quarantine', 'quota_observation', 'tasks'):
                db.execute(f'DROP TABLE IF EXISTS {name}')
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
            # Schema creation and marker are committed by initialize(), below.
            initialize(db)
            db.execute('INSERT INTO schema_migrations VALUES(?)', (VERSION,))
            if db.execute('PRAGMA foreign_key_check').fetchall() or db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Schema migration validation failed')
            db.commit()
        except MigrationBusy:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise MigrationError('SQLite migration failed; original data retained; inspect backup') from None
        finally:
            os.close(fd)
    finally:
        db.close()


def initialize(db):
    # execute individual statements: executescript would implicitly commit DDL.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    for statement in TRIGGERS:
        db.execute(statement)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks(
 repository TEXT NOT NULL, number INTEGER NOT NULL, issue_id INTEGER NOT NULL UNIQUE CHECK(typeof(issue_id)='integer' AND issue_id>0),
 project_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'discovered',
 available INTEGER NOT NULL CHECK(available IN (0,1)), version INTEGER NOT NULL,
 content_hash TEXT NOT NULL, snapshot TEXT NOT NULL, observed TEXT NOT NULL, approved_version INTEGER,
 created_at TEXT NOT NULL, synced_at TEXT NOT NULL,
 repository_id INTEGER CHECK(repository_id IS NULL OR (typeof(repository_id)='integer' AND repository_id>0)), current_iteration_id INTEGER,
 FOREIGN KEY(current_iteration_id,issue_id) REFERENCES task_iterations(iteration_id,issue_id),
 PRIMARY KEY(repository,number), UNIQUE(repository_id,number), UNIQUE(issue_id,repository_id));
CREATE TABLE IF NOT EXISTS task_iterations(
 iteration_id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
 iteration_number INTEGER NOT NULL CHECK(iteration_number>0), approved_version INTEGER NOT NULL,
 decision_id INTEGER REFERENCES decisions(id), created_at TEXT NOT NULL,
 UNIQUE(issue_id,iteration_number), UNIQUE(iteration_id,issue_id));
CREATE TABLE IF NOT EXISTS task_pull_requests(
 pr_id INTEGER PRIMARY KEY CHECK(pr_id>0), issue_id INTEGER NOT NULL, repository_id INTEGER NOT NULL,
 pr_number INTEGER NOT NULL CHECK(pr_number>0), head TEXT NOT NULL, base TEXT NOT NULL,
 head_sha TEXT NOT NULL, state TEXT NOT NULL, merged_at TEXT,
 FOREIGN KEY(issue_id,repository_id) REFERENCES tasks(issue_id,repository_id),
 UNIQUE(repository_id,pr_number), UNIQUE(issue_id));
CREATE TABLE IF NOT EXISTS task_transitions(
 event_key TEXT PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES tasks(issue_id), actor TEXT NOT NULL,
 from_state TEXT NOT NULL, to_state TEXT NOT NULL, version INTEGER NOT NULL,
 iteration_id INTEGER REFERENCES task_iterations(iteration_id), evidence TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iteration_pr_history(
 id INTEGER PRIMARY KEY, iteration_id INTEGER NOT NULL REFERENCES task_iterations(iteration_id),
 pr_id INTEGER NOT NULL REFERENCES task_pull_requests(pr_id), head_sha TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_operations(
 operation_key TEXT PRIMARY KEY, iteration_id INTEGER NOT NULL REFERENCES task_iterations(iteration_id),
 phase TEXT NOT NULL, outcome TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
'''

SCHEMA += """
CREATE TABLE IF NOT EXISTS history(
 id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
 version INTEGER NOT NULL, event TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(repository,number) REFERENCES tasks(repository,number));
CREATE TABLE IF NOT EXISTS decisions(
 id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
 version INTEGER NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL, result TEXT NOT NULL,
 created_at TEXT NOT NULL, event_id TEXT, UNIQUE(repository,event_id));
CREATE TABLE IF NOT EXISTS command_guards(
 repository TEXT NOT NULL, number INTEGER NOT NULL, version INTEGER NOT NULL,
 content_hash TEXT NOT NULL, available INTEGER NOT NULL, server_at TEXT,
 observation_token TEXT NOT NULL, PRIMARY KEY(repository,number));
CREATE TABLE IF NOT EXISTS command_quarantine(
 repository TEXT NOT NULL, number INTEGER NOT NULL, action TEXT NOT NULL,
 server_at TEXT, event_id TEXT, reason TEXT NOT NULL, PRIMARY KEY(repository,number,action));
"""

SCHEMA += """
            CREATE TABLE IF NOT EXISTS attempts(
                attempt_id TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                version INTEGER NOT NULL, branch TEXT NOT NULL, state TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, duration REAL,
                worktree TEXT, initial_sha TEXT, result_sha TEXT, exit_code INTEGER,
                error_code TEXT, category TEXT, codex_started INTEGER NOT NULL DEFAULT 0,
                pushed INTEGER NOT NULL DEFAULT 0, pid INTEGER, process_identity TEXT,
                origin_url TEXT, remote_sha TEXT, recovery_diagnostic TEXT,
                iteration_id INTEGER REFERENCES task_iterations(iteration_id),
                attempt_number INTEGER NOT NULL DEFAULT 1 CHECK(attempt_number>0),
                FOREIGN KEY(repository,number) REFERENCES tasks(repository,number),
                UNIQUE(iteration_id,attempt_number));
            CREATE UNIQUE INDEX IF NOT EXISTS attempt_branch ON attempts(repository,branch)
                WHERE state = 'running';
            CREATE TABLE IF NOT EXISTS result_outbox(
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), body TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0, comment_id TEXT);
            CREATE TABLE IF NOT EXISTS quota_observation(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL,
                observed_at TEXT NOT NULL, diagnostic TEXT NOT NULL);

            """

TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS attempt_iteration_identity BEFORE INSERT ON attempts
    WHEN NEW.iteration_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM task_iterations i JOIN tasks t ON t.issue_id=i.issue_id
        WHERE i.iteration_id=NEW.iteration_id AND t.repository=NEW.repository AND t.number=NEW.number)
    BEGIN SELECT RAISE(ABORT,'Attempt iteration identity mismatch'); END""",
    """CREATE TRIGGER IF NOT EXISTS pr_iteration_identity BEFORE INSERT ON iteration_pr_history
    WHEN NOT EXISTS (SELECT 1 FROM task_iterations i JOIN task_pull_requests p ON p.issue_id=i.issue_id
        WHERE i.iteration_id=NEW.iteration_id AND p.pr_id=NEW.pr_id)
    BEGIN SELECT RAISE(ABORT,'PR iteration identity mismatch'); END""",
)
