"""Explicit database lifecycle. No GitHub or execution dependencies."""
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sqlite3
import tempfile
import uuid
from .storage import SchemaError, _validate

META_V1 = '''CREATE TABLE dispatcher_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    format_version INTEGER NOT NULL CHECK(format_version=1),
    instance_id TEXT NOT NULL,
    recovery_required INTEGER NOT NULL CHECK(recovery_required IN (0,1))
)'''


def metadata_ddl(version):
    return META_V1.replace('format_version=1', f'format_version={version}')


META = metadata_ddl(2)


def migrate(db):
    """Caller owns a transaction; executescript would implicitly commit."""
    if validate(db)[1] == 2:
        return
    from importlib.resources import files
    statement = ''
    for line in files('dispatcher').joinpath('recovery_schema.sql').read_text().splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise SchemaError('Incomplete migration DDL')
    db.execute('ALTER TABLE dispatcher_metadata RENAME TO old_metadata')
    db.execute(META)
    db.execute('INSERT INTO dispatcher_metadata SELECT singleton,2,instance_id,recovery_required FROM old_metadata')
    db.execute('DROP TABLE old_metadata')
    from .models import utc_now
    db.execute('INSERT INTO schema_migrations VALUES (2,?)', (utc_now(),))
    validate(db)


@contextmanager
def connection(path, **kwargs):
    db = sqlite3.connect(path, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()


def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SchemaError('Symlink database paths are forbidden')
    if not path.is_file():
        raise SchemaError('Database missing; use db bootstrap for a new installation')
    if path.stat().st_mode & 0o077:
        raise SchemaError('Database permissions must be 0600')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
            raise SchemaError('Symlink or nonregular SQLite sidecar is forbidden')
        if suffix == '-journal' and sidecar.exists() and sidecar.stat().st_size:
            raise SchemaError('Rollback journal present; verify shutdown and preserve source files')
    return path


@contextmanager
def inspection(path):
    """Inspect a private copy so even SQLite SHM recovery cannot touch the source.

    Concurrent modification can make the copy invalid: fail closed and retry
    after stopping the daemon. This is diagnostic, not the backup implementation.
    """
    path = safe_path(path)
    with tempfile.TemporaryDirectory(prefix='dispatcher-check-') as area:
        target = Path(area) / 'db'
        shutil.copyfile(path, target)
        wal = Path(str(path) + '-wal')
        if wal.exists():
            shutil.copyfile(wal, Path(str(target) + '-wal'))
        db = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
        try:
            yield db
        except sqlite3.DatabaseError as exc:
            raise SchemaError('Invalid or corrupt SQLite') from exc
        finally:
            db.close()


def validate(db, *, legacy=False):
    if not legacy and not db.execute("SELECT 1 FROM sqlite_master WHERE name='dispatcher_metadata'").fetchone():
        raise SchemaError('V2.3 metadata absent; stop service and use db register <backup>')
    _validate(db, metadata=not legacy)
    from .models import State
    valid_states = tuple(str(s) for s in State)
    if db.execute('SELECT 1 FROM issues WHERE is_virtual=0 AND state NOT IN (' +
                  ','.join('?' for _ in valid_states) + ')', valid_states).fetchone():
        raise SchemaError('Logical inconsistency: unknown issue state')
    for query in (
        "SELECT 1 FROM issues i JOIN issues r ON r.issue_id=i.execution_group_id WHERE lower(i.repository)<>lower(r.repository) OR r.is_virtual=1",
        "SELECT 1 FROM issue_relations x JOIN issues p ON p.issue_id=x.parent_issue_id JOIN issues c ON c.issue_id=x.child_issue_id WHERE lower(p.repository)<>lower(c.repository)",
        "SELECT 1 FROM pull_requests p JOIN issues i ON i.issue_id=p.issue_id WHERE lower(p.repository)<>lower(i.repository)",
        "SELECT 1 FROM error_log l JOIN executions e ON e.execution_id=l.execution_id WHERE l.issue_id<>e.issue_id",
        "SELECT 1 FROM issues GROUP BY lower(repository),issue_number HAVING count(*)>1",
    ):
        if db.execute(query).fetchone():
            raise SchemaError('Logical inconsistency: repository or execution identity')
    if db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 2:
        for table in ('recovery_execution_archive', 'recovery_approval_reset'):
            if db.execute('SELECT 1 FROM ' + table + ' a JOIN recovery_operations o '
                          'ON o.operation_id=a.operation_id WHERE a.issue_id<>o.issue_id').fetchone():
                raise SchemaError('Recovery operation identity mismatch')
        if db.execute("SELECT 1 FROM recovery_operations o WHERE phase<>'completed' AND NOT EXISTS "
                      "(SELECT 1 FROM recovery_quarantine q WHERE q.issue_id=o.issue_id)").fetchone():
            raise SchemaError('Active recovery operation has no quarantine')
    if legacy:
        return None
    rows = db.execute('SELECT singleton,format_version,instance_id,recovery_required FROM dispatcher_metadata').fetchall()
    if len(rows) != 1 or (rows[0][0] != 1 or rows[0][1] not in (1, 2)) or rows[0][3] not in (0, 1):
        raise SchemaError('Invalid dispatcher metadata')
    if rows[0][1] != db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]:
        raise SchemaError('Metadata and migration version mismatch')
    try:
        uuid.UUID(rows[0][2])
    except (ValueError, TypeError) as exc:
        raise SchemaError('Invalid instance identity') from exc
    return rows[0]


def marker(config):
    return config.workspace / '.dispatcher-instance'


def check(config, *, execution=False, path=None):
    with inspection(path or config.database) as db:
        meta = validate(db)
    identity = marker(config)
    if (any(p.is_symlink() for p in (identity, *identity.absolute().parents))
            or not identity.is_file() or identity.stat().st_mode & 0o077
            or identity.read_text().strip() != meta[2]):
        raise SchemaError('Workspace identity mismatch; inspect configuration or use db bind')
    if execution and meta[1] != 2:
        raise SchemaError('Explicit db upgrade required')
    if execution and meta[3]:
        raise SchemaError('Recovery admission blocked: external reconciliation required (V2.4.2)')
    return meta


def bind(config, identity):
    config.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = marker(config)
    if any(p.is_symlink() for p in (target, *target.absolute().parents)):
        raise SchemaError('Symlink workspace paths are forbidden')
    with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
        stream.write(identity + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def backup(source, destination):
    source = safe_path(source)
    destination = Path(destination).absolute()
    if any(p.is_symlink() for p in destination.parents):
        raise SchemaError('Symlink backup directory is forbidden')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with connection(source.as_uri() + '?mode=ro', uri=True) as src:
            with connection(destination) as dst:
                src.backup(dst)
                # Both legacy and registered snapshots are supported.
                has_meta = dst.execute("SELECT 1 FROM sqlite_master WHERE name='dispatcher_metadata'").fetchone()
                validate(dst, legacy=not has_meta)
        with destination.open('rb') as stream:
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def maintain(config, command, argument=None):
    from .locking import InstanceLock
    from .storage import Storage
    if command == 'check':
        meta = check(config)
        return 'WARN recovery admission blocked' if meta[3] else 'PASS schema, integrity, identity'
    if command == 'backup':
        check(config)
        backup(config.database, argument)
        return 'PASS backup verified'
    if command in ('bootstrap', 'register', 'bind'):
        target = marker(config).absolute()
        if target.exists() or any(p.is_symlink() for p in (target, *target.parents)):
            raise SchemaError('Workspace identity already exists or path contains symlink')
    # Lock includes descriptors inherited by daemon-managed executor children.
    with InstanceLock(config):
        if command == 'bootstrap':
            if config.database.exists() or marker(config).exists():
                raise SchemaError('Bootstrap refuses existing database or identity')
            identity = str(uuid.uuid4())
            Storage.bootstrap(config.database)
            with connection(config.database) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(META)
                db.execute('INSERT INTO dispatcher_metadata VALUES (1,2,?,0)', (identity,))
                bind(config, identity)
        elif command == 'register':
            with inspection(config.database) as db:
                validate(db, legacy=True)
            if marker(config).exists():
                raise SchemaError('Workspace already registered')
            backup(config.database, argument)
            identity = str(uuid.uuid4())
            with connection(config.database) as db:
                db.execute('BEGIN IMMEDIATE')
                version = db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                db.execute(metadata_ddl(version))
                db.execute('INSERT INTO dispatcher_metadata VALUES (1,?,?,0)', (version, identity))
                migrate(db)
                bind(config, identity)
        elif command == 'bind':
            with inspection(config.database) as db:
                meta = validate(db)
            bind(config, meta[2])
        elif command == 'upgrade':
            meta = check(config)
            quiescent(config)
            if meta[1] == 2:
                return 'PASS upgrade; already current'
            backup(config.database, config.database.with_name(config.database.name + '.before-upgrade-' + uuid.uuid4().hex))
            with connection(config.database) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                migrate(db)
        elif command == 'restore':
            check(config)
            check(config, path=argument)
            # Pre-launch reservations are historical, not live process evidence.
            # Launched executions lack durable proof of supervisor/child shutdown.
            with inspection(config.database) as db:
                validate(db)
                if db.execute("SELECT 1 FROM executions WHERE "
                              "(status IN ('reserved','preparing') AND codex_started_at IS NOT NULL) "
                              "OR status IN ('executing','publishing')").fetchone():
                    raise SchemaError('Executor stop cannot be proven for launched execution; '
                                      'durable shutdown evidence unavailable; restore refused')
            quiescent(config)
            path = safe_path(config.database)
            old = path.with_name(path.name + '.before-restore-' + uuid.uuid4().hex)
            backup(path, old)
            # A read-only Backup API connection to a WAL-mode main file may
            # create empty sidecars. No pre-existing sidecar was allowed above.
            wal = Path(str(path) + '-wal')
            if wal.exists() and wal.stat().st_size:
                raise SchemaError('WAL appeared during archival; restore refused')
            for suffix in ('-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)
            staging_area = tempfile.mkdtemp(prefix='.restore-', dir=path.parent)
            staging = Path(staging_area) / 'db'
            try:
                backup(argument, staging)
                with connection(staging) as db:
                    db.execute('BEGIN IMMEDIATE')
                    migrate(db)
                    db.execute('UPDATE dispatcher_metadata SET recovery_required=1')
                check(config, path=staging)
                with open(staging, 'rb') as stream:
                    os.fsync(stream.fileno())
                os.replace(staging, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                shutil.rmtree(staging_area)
        else:
            raise SchemaError('Unknown maintenance command')
    return 'PASS ' + command + ('; recovery admission BLOCKED' if command == 'restore' else '')


def quiescent(config):
    """Inherited locks exclude managed children; never guess liveness from PIDs."""
    control = config.workspace / '.control.sock'
    if control.exists() or control.is_symlink():
        raise SchemaError('Control socket exists; verify service shutdown')
    path = safe_path(config.database)
    for suffix in ('-wal', '-shm'):
        if Path(str(path) + suffix).exists():
            raise SchemaError('Working WAL/SHM present; checkpoint after shutdown')
