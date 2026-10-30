"""Legacy reset boundary 602 and additive, transactional stage migrations."""
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
        if 'tasks' in tables and {'repository_id', 'current_iteration_id'} <= {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:
            return
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
 UNIQUE(repository_id,pr_number));
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
            CREATE TABLE IF NOT EXISTS attempt_summaries(
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=8192));
            CREATE TABLE IF NOT EXISTS result_outbox(
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), body TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0, comment_id TEXT);
            CREATE TABLE IF NOT EXISTS quota_observation(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL,
                observed_at TEXT NOT NULL, diagnostic TEXT NOT NULL);

            """

SCHEMA += """
CREATE TABLE IF NOT EXISTS pr_resolutions(
 pr_id INTEGER NOT NULL REFERENCES task_pull_requests(pr_id),
 iteration_id INTEGER NOT NULL REFERENCES task_iterations(iteration_id),
 target TEXT NOT NULL, metadata TEXT NOT NULL, issue_closed_at TEXT,
 comment_id INTEGER, error TEXT, PRIMARY KEY(pr_id,iteration_id));
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


def upgrade604(db):
    """Remove the old one-PR-per-Issue constraint without losing PR history."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='task_pull_requests'").fetchone():
        return
    unique_issue = any([r[2] for r in db.execute('PRAGMA index_info(' + index[1] + ')')] == ['issue_id']
                       for index in db.execute('PRAGMA index_list(task_pull_requests)') if index[2])
    if not unique_issue:
        return
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        statement = next(s for s in SCHEMA.split(';') if 'CREATE TABLE IF NOT EXISTS task_pull_requests(' in s)
        db.execute(statement.replace('task_pull_requests(', 'pr_upgrade604('))
        db.execute('INSERT INTO pr_upgrade604 SELECT * FROM task_pull_requests')
        db.execute('DROP TRIGGER IF EXISTS pr_iteration_identity')
        db.execute('DROP TABLE task_pull_requests')
        db.execute('ALTER TABLE pr_upgrade604 RENAME TO task_pull_requests')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise MigrationError('PR history migration validation failed')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')

SCHEMA += """
CREATE TABLE IF NOT EXISTS cancellations(
 issue_id INTEGER PRIMARY KEY REFERENCES tasks(issue_id),
 issue_closed INTEGER NOT NULL DEFAULT 0,
 cleaned INTEGER NOT NULL DEFAULT 0, error TEXT);
"""


BRANCH_VERSION = 605
BRANCH_COLUMNS = {
    'base_branch': 'TEXT',
    'work_branch': 'TEXT',
    'branch_source': "TEXT CHECK(branch_source IN ('explicit','generated'))",
    'branch_status': "TEXT DEFAULT 'unresolved' CHECK(branch_status IN ('unresolved','resolved','prepared','conflict','failed'))",
    'branch_error': 'TEXT',
    'branch_prepared_at': 'TEXT',
}
# Failed/rejected/interrupted Issues can be approved again and retain their claim.
ACTIVE_BRANCH = "state NOT IN ('done','cancelled','revoked')"


def validate(db):
    if db.execute('PRAGMA foreign_key_check').fetchall() or [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
        raise MigrationError('SQLite schema validation failed')


def upgrade605(db):
    """Caller owns BEGIN IMMEDIATE; all DDL and marker roll back together.

    Inspect columns even when a marker exists, repairing interrupted/manual schemas.
    Historical attempt branches are deliberately never backfilled into tasks.
    """
    columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
    for name, definition in BRANCH_COLUMNS.items():
        if name not in columns:
            db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
    db.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS task_work_branch
        ON tasks(repository,work_branch) WHERE work_branch IS NOT NULL AND {ACTIVE_BRANCH}""")
    # Cross-table checks cover legacy attempts whose tasks have no work_branch.
    # Guard both directions and state changes, including repeat iterations.
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS task_branch_claim_{operation.lower()}
            BEFORE {operation} ON tasks
            WHEN NEW.work_branch IS NOT NULL AND NEW.state NOT IN ('done','cancelled','revoked')
            AND EXISTS (SELECT 1 FROM attempts a JOIN tasks t
                ON t.repository=a.repository AND t.number=a.number
                WHERE a.repository=NEW.repository AND a.branch=NEW.work_branch AND a.number!=NEW.number
                AND (a.state='running' OR t.state NOT IN ('done','cancelled','revoked')))
            BEGIN SELECT RAISE(ABORT,'Work branch reserved by attempt'); END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS attempt_branch_claim_{operation.lower()}
            BEFORE {operation} ON attempts WHEN NEW.state='running' AND EXISTS (
                SELECT 1 FROM tasks t WHERE t.repository=NEW.repository AND t.number!=NEW.number
                AND t.work_branch=NEW.branch AND t.state NOT IN ('done','cancelled','revoked'))
            BEGIN SELECT RAISE(ABORT,'Work branch reserved by Issue'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS task_branch_reactivate BEFORE UPDATE OF state ON tasks
        WHEN NEW.state NOT IN ('done','cancelled','revoked')
        AND OLD.state IN ('done','cancelled','revoked') AND EXISTS (
            SELECT 1 FROM attempts a JOIN tasks t ON t.repository=a.repository
            AND t.number!=a.number AND t.work_branch=a.branch
            WHERE a.repository=NEW.repository AND a.number=NEW.number
            AND t.state NOT IN ('done','cancelled','revoked'))
        BEGIN SELECT RAISE(ABORT,'Historical branch reserved by Issue'); END""")
    validate(db)
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?)', (BRANCH_VERSION,))


def upgrade606(db):
    """Retain one resolution per PR/iteration, atomically with the schema marker."""
    keys = [r[1] for r in db.execute('PRAGMA table_info(pr_resolutions)') if r[5]]
    if keys == ['pr_id']:
        statement = next(s for s in SCHEMA.split(';') if 'CREATE TABLE IF NOT EXISTS pr_resolutions(' in s)
        db.execute(statement.replace('pr_resolutions(', 'resolutions_upgrade606('))
        db.execute('INSERT INTO resolutions_upgrade606 SELECT * FROM pr_resolutions')
        db.execute('DROP TABLE pr_resolutions')
        db.execute('ALTER TABLE resolutions_upgrade606 RENAME TO pr_resolutions')
    validate(db)
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(606)')


def upgrade607(db):
    """Durable cleanup generations; triggers include all SQL state writers."""
    db.execute('''CREATE TABLE IF NOT EXISTS cleanup_jobs(
        cleanup_id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES tasks(issue_id), project_id TEXT NOT NULL,
        repository TEXT NOT NULL, number INTEGER NOT NULL,
        generation INTEGER NOT NULL, version INTEGER NOT NULL, attempt_id TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','completed','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        started_at TEXT, completed_at TEXT, next_retry_at TEXT, last_error TEXT,
        UNIQUE(issue_id,generation,version))''')
    db.execute('CREATE INDEX IF NOT EXISTS cleanup_job_state ON cleanup_jobs(state,next_retry_at)')
    db.execute('CREATE INDEX IF NOT EXISTS cleanup_attempt_task ON attempts(repository,number)')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS cleanup_enqueue_{operation.lower()}
            AFTER {operation} ON tasks WHEN NEW.state IN ('done','cancelled')
            BEGIN
                INSERT OR IGNORE INTO cleanup_jobs(issue_id,project_id,repository,number,generation,version,
                    attempt_id,created_at,updated_at)
                VALUES(NEW.issue_id,NEW.project_id,NEW.repository,NEW.number,COALESCE(NEW.current_iteration_id,0),NEW.version,
                    (SELECT attempt_id FROM attempts WHERE repository=NEW.repository AND number=NEW.number ORDER BY rowid DESC LIMIT 1),
                    strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
            END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS cleanup_generation_guard BEFORE UPDATE ON tasks
        WHEN EXISTS(SELECT 1 FROM cleanup_jobs j WHERE j.issue_id=OLD.issue_id AND j.state='running')
        AND (NEW.state!=OLD.state OR NEW.issue_id!=OLD.issue_id OR NEW.project_id!=OLD.project_id
             OR NEW.repository!=OLD.repository OR NEW.number!=OLD.number OR NEW.version!=OLD.version
             OR NEW.current_iteration_id IS NOT OLD.current_iteration_id)
        BEGIN SELECT RAISE(ABORT,'Cleanup owns task generation'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS cleanup_attempt_guard BEFORE INSERT ON attempts
        WHEN EXISTS(SELECT 1 FROM cleanup_jobs j WHERE j.repository=NEW.repository AND j.number=NEW.number AND j.state='running')
        BEGIN SELECT RAISE(ABORT,'Cleanup owns task generation'); END''')
    db.execute("""CREATE TRIGGER IF NOT EXISTS cleanup_attempt_update_guard BEFORE UPDATE ON attempts
        WHEN EXISTS(SELECT 1 FROM cleanup_jobs j WHERE j.repository=OLD.repository AND j.number=OLD.number AND j.state='running')
        BEGIN SELECT RAISE(ABORT,'Cleanup owns attempt history'); END""")
    validate(db)
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(607)')


def upgrade608(db):
    """Terminal decision and immutable delivery survive publication failures."""
    db.execute("""CREATE TABLE IF NOT EXISTS terminal_errors(
        issue_id INTEGER PRIMARY KEY REFERENCES tasks(issue_id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
        iteration_id INTEGER NOT NULL, version INTEGER NOT NULL,
        reason TEXT NOT NULL, pr_number INTEGER NOT NULL, pr_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, publication TEXT NOT NULL DEFAULT 'pending',
        diagnostic TEXT, body TEXT, comment_id TEXT, delivered INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS configuration_errors(
        issue_id INTEGER NOT NULL REFERENCES tasks(issue_id), content_hash TEXT NOT NULL,
        code TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL,
        delivered INTEGER NOT NULL, comment_id TEXT, PRIMARY KEY(issue_id,content_hash))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS terminal_state_guard BEFORE UPDATE OF state ON tasks
        WHEN OLD.state='review_on_error' AND NEW.state!=OLD.state
        BEGIN SELECT RAISE(ABORT,'Terminal review_on_error'); END""")
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(608)')


def upgrade609(db):
    """Stop intent and diagnostic outbox; repair old claim/cleanup predicates."""
    db.execute('''CREATE TABLE IF NOT EXISTS stop_requests(
        request_id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
        iteration_id INTEGER, version INTEGER NOT NULL, attempt_id TEXT REFERENCES attempts(attempt_id),
        reason TEXT NOT NULL, previous_state TEXT NOT NULL, stage TEXT NOT NULL,
        requested_at TEXT NOT NULL, finalized_at TEXT, active INTEGER NOT NULL DEFAULT 1,
        process_status TEXT NOT NULL DEFAULT 'pending', method TEXT NOT NULL DEFAULT 'not_required',
        error TEXT, evidence TEXT NOT NULL, next_signal_at TEXT)''')
    if 'next_signal_at' not in {r[1] for r in db.execute('PRAGMA table_info(stop_requests)')}:
        db.execute('ALTER TABLE stop_requests ADD COLUMN next_signal_at TEXT')
    db.execute('''CREATE TABLE IF NOT EXISTS control_observations(
        issue_id INTEGER PRIMARY KEY REFERENCES tasks(issue_id), server_at TEXT NOT NULL)''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS stop_active ON stop_requests(issue_id) WHERE active=1')
    db.execute('''CREATE TABLE IF NOT EXISTS stop_outbox(
        request_id INTEGER PRIMARY KEY REFERENCES stop_requests(request_id), body TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0, comment_id TEXT, error TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS attempt_publications(
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), pr_id INTEGER,
        pr_number INTEGER NOT NULL, observed_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS attempt_processes(
        process_id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        pid INTEGER NOT NULL, process_identity TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, exit_code INTEGER)''')
    # Rebuild definitions whose partial predicates change. Caller owns writer lock.
    db.execute('DROP INDEX IF EXISTS task_work_branch')
    db.execute("""CREATE UNIQUE INDEX task_work_branch ON tasks(repository,work_branch)
        WHERE work_branch IS NOT NULL AND state NOT IN ('done','cancelled','revoked','detached')""")
    for name in ('task_branch_claim_insert','task_branch_claim_update',
                 'attempt_branch_claim_insert','attempt_branch_claim_update','task_branch_reactivate'):
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if sql:
            db.execute('DROP TRIGGER ' + name)
            db.execute(sql[0].replace("'done','cancelled','revoked'", "'done','cancelled','revoked','detached'"))
    for operation in ('insert', 'update'):
        name = 'cleanup_enqueue_' + operation
        sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
        db.execute('DROP TRIGGER ' + name)
        db.execute(sql.replace("'done','cancelled'", "'done','cancelled','detached'"))
    # review_on_error remains terminal except for a journaled stop request.
    db.execute('DROP TRIGGER IF EXISTS terminal_state_guard')
    db.execute("""CREATE TRIGGER terminal_state_guard BEFORE UPDATE OF state ON tasks
        WHEN OLD.state='review_on_error' AND NEW.state!=OLD.state AND NOT
        (NEW.state='detached' AND EXISTS(SELECT 1 FROM stop_requests s
          WHERE s.issue_id=OLD.issue_id AND s.active=1))
        BEGIN SELECT RAISE(ABORT,'Terminal review_on_error'); END""")
    validate(db)
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(609)')


def upgrade610(db):
    """Durable per-attempt diagnostic timeout delivery; no execution transition."""
    db.execute('''CREATE TABLE IF NOT EXISTS timeout_outbox(
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
        elapsed_seconds REAL NOT NULL, limit_seconds REAL NOT NULL,
        body TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
        comment_id TEXT, error TEXT)''')
    if 'codex_started_at' not in {r[1] for r in db.execute('PRAGMA table_info(attempts)')}:
        db.execute('ALTER TABLE attempts ADD COLUMN codex_started_at TEXT')
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(610)')


def upgrade611(db):
    """Write-ahead ref ownership and archive intent survive cleanup and restart."""
    db.execute('''CREATE TABLE IF NOT EXISTS detach_refs(
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
        branch TEXT NOT NULL, sha TEXT, archive TEXT UNIQUE NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(611)')
