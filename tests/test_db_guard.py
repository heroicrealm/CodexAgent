"""All fixtures are synthetic local SQLite; no GitHub clients are constructed."""
from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import pytest
from dispatcher.config import Config, Project
from dispatcher.storage import Storage, SchemaError
from dispatcher.db_guard import maintain, check, marker, inspection, validate, connection
from dispatcher.locking import InstanceLock, AlreadyRunning
from dispatcher.models import IssueObservation


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / 'work', tmp_path / 'db', (Project('p', 'owner/repo'),))


def tree(path):
    return {str(p.relative_to(path)): (p.stat().st_mode, p.read_bytes())
            for p in path.rglob('*') if p.is_file()}


def test_missing_never_creates(config, tmp_path):
    before = tree(tmp_path)
    for action in (lambda: Storage(config.database), lambda: maintain(config, 'check')):
        with pytest.raises(SchemaError):
            action()
    assert tree(tmp_path) == before
    assert not config.workspace.exists()


def test_bootstrap_identity_and_projects(config):
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    issue = IssueObservation(1, 'owner/repo', 1, 'synthetic')
    store.register_issue(issue, 'p')
    for projects in ((), (Project('renamed', 'OWNER/REPO'),), (Project('p', 'owner/different'),)):
        assert check(replace(config, projects=projects))[2] == check(config)[2]
    store.register_issue(issue, 'renamed')
    assert store.issue(1)['repository'] == 'owner/repo'
    with pytest.raises(ValueError):
        store.register_issue(replace(issue, repository='owner/different'), 'p')
    with pytest.raises((SchemaError, FileExistsError)):
        maintain(config, 'bootstrap')


def test_readonly_check_no_side_effects(config, tmp_path, monkeypatch):
    maintain(config, 'bootstrap')
    before = tree(tmp_path)
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('network'))
    assert maintain(config, 'check').startswith('PASS')
    assert tree(tmp_path) == before
    assert not Path(str(config.database) + '-wal').exists()


@pytest.mark.parametrize('damage', ['garbage', 'version', 'index', 'foreign', 'logical', 'truncate'])
def test_invalid_unchanged(config, tmp_path, damage):
    maintain(config, 'bootstrap')
    if damage == 'garbage':
        config.database.write_bytes(b'not SQLite')
    elif damage == 'truncate':
        config.database.write_bytes(config.database.read_bytes()[:200])
    elif damage == 'foreign':
        marker(config).write_text('wrong-instance')
    else:
        with connection(config.database) as db:
            if damage == 'version':
                db.execute('UPDATE schema_migrations SET version=99 WHERE version=1')
            elif damage == 'index':
                db.execute('DROP INDEX idx_issues_state')
            else:
                db.execute("INSERT INTO issues(issue_id,repository,issue_number,project_id,execution_group_id,state,title,created_at,updated_at) VALUES(1,'o/r',1,'p',1,'unknown','x','now','now')")
    before = tree(tmp_path)
    with pytest.raises(SchemaError):
        maintain(config, 'check')
    assert tree(tmp_path) == before


def test_legacy_registration_backup(config, tmp_path):
    store = Storage.bootstrap(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'synthetic'), 'p')
    before = config.database.read_bytes()
    with pytest.raises(SchemaError):
        check(config)
    backup = tmp_path / 'legacy.backup'
    maintain(config, 'register', backup)
    assert check(config)[3] == 0
    with inspection(backup) as db:
        validate(db, legacy=True)
        assert db.execute('SELECT title FROM issues').fetchone() == ('synthetic',)
    assert before != config.database.read_bytes()
    assert Storage(config.database).issue(1)['title'] == 'synthetic'


def test_wal_snapshot_and_check(config, tmp_path):
    maintain(config, 'bootstrap')
    db = sqlite3.connect(config.database)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO debug_operations(created_at,level,component,operation,result) VALUES('now','api','test','wal','ok')")
        db.commit()
        before = tree(tmp_path)
        maintain(config, 'check')
        assert tree(tmp_path) == before
        destination = tmp_path / 'snapshot'
        maintain(config, 'backup', destination)
        with connection(destination) as snapshot:
            assert snapshot.execute('SELECT operation FROM debug_operations').fetchall() == [('wal',)]
        assert destination.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            maintain(config, 'backup', destination)
    finally:
        db.close()


@pytest.mark.parametrize('state', ['pending', 'running'])
def test_restore_barrier_and_archive(config, tmp_path, state):
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'synthetic'), 'p')
    with store.transaction() as db:
        db.execute("UPDATE issues SET state=?,approved_at='now' WHERE issue_id=1", (state,))
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    with connection(config.database) as db:
        assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    assert 'BLOCKED' in maintain(config, 'restore', snapshot)
    assert list(tmp_path.glob('db.before-restore-*'))
    assert check(config)[3] == 1
    assert maintain(config, 'check').startswith('WARN')
    with pytest.raises(SchemaError, match='admission'):
        check(config, execution=True)
    with pytest.raises(SchemaError, match='admission'):
        Storage(config.database).reserve(1)


def test_restore_refuses_active_and_wal(config, tmp_path):
    maintain(config, 'bootstrap')
    backup = tmp_path / 'snapshot'
    maintain(config, 'backup', backup)
    before = config.database.read_bytes()
    with InstanceLock(config):
        with pytest.raises(AlreadyRunning):
            maintain(config, 'restore', backup)
    assert config.database.read_bytes() == before
    db = sqlite3.connect(config.database)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('SELECT * FROM issues').fetchall()
        with pytest.raises(SchemaError, match='WAL'):
            maintain(config, 'restore', backup)
    finally:
        db.close()
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'x'), 'p')
    with store.transaction() as db:
        db.execute("INSERT INTO executions(issue_id,status,started_at) VALUES(1,'executing','now')")
    with pytest.raises(SchemaError, match='stop'):
        maintain(config, 'restore', backup)


def test_restore_failed_replace_preserves_original(config, tmp_path, monkeypatch):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    before = config.database.read_bytes()
    monkeypatch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('synthetic crash')))
    with pytest.raises(OSError):
        maintain(config, 'restore', snapshot)
    assert config.database.read_bytes() == before
    assert list(tmp_path.glob('db.before-restore-*'))
    assert not list(tmp_path.glob('.restore-*'))


def test_invalid_restore_source_and_symlinks(config, tmp_path):
    maintain(config, 'bootstrap')
    invalid = tmp_path / 'invalid'
    invalid.write_bytes(b'invalid')
    invalid.chmod(0o600)
    before = config.database.read_bytes()
    with pytest.raises(SchemaError):
        maintain(config, 'restore', invalid)
    assert config.database.read_bytes() == before
    link = tmp_path / 'link'
    link.symlink_to(config.database)
    with pytest.raises(SchemaError, match='Symlink'):
        check(config, path=link)


def test_explicit_workspace_relocation(config, tmp_path):
    maintain(config, 'bootstrap')
    moved = replace(config, workspace=tmp_path / 'moved')
    with pytest.raises(SchemaError, match='identity'):
        check(moved)
    maintain(moved, 'bind')
    assert check(config)[2] == check(moved)[2]


def test_foreign_backup_refused(config, tmp_path):
    maintain(config, 'bootstrap')
    other = Config(tmp_path / 'other-work', tmp_path / 'other-db', ())
    maintain(other, 'bootstrap')
    snapshot = tmp_path / 'foreign-snapshot'
    maintain(other, 'backup', snapshot)
    before = config.database.read_bytes()
    with pytest.raises(SchemaError, match='identity'):
        maintain(config, 'restore', snapshot)
    assert config.database.read_bytes() == before


def test_recovery_runtime_constructs_no_adapters(config, tmp_path, monkeypatch):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    maintain(config, 'restore', snapshot)
    from dispatcher.runtime import Runtime
    monkeypatch.setattr('dispatcher.runtime.GitHubAdapter', lambda *a: pytest.fail('GitHub constructed'))
    with pytest.raises(SchemaError, match='admission'):
        Runtime(config)


def test_invalid_legacy_registration_is_unchanged(config, tmp_path):
    Storage.bootstrap(config.database)
    with connection(config.database) as db:
        db.execute('DROP INDEX idx_issues_state')
    before = config.database.read_bytes()
    destination = tmp_path / 'backup'
    with pytest.raises(SchemaError):
        maintain(config, 'register', destination)
    assert config.database.read_bytes() == before
    assert not destination.exists()
    assert not marker(config).exists()


def test_disabled_project_preserves_data_without_external_calls(config):
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'x'), 'p')
    disabled = replace(config, projects=(replace(config.projects[0], enabled=False),))
    from dispatcher.runtime import Runtime
    # Bare objects have no adapter methods: any attempted external call fails.
    Runtime(disabled, github=object(), git=object()).run(once=True)
    assert Storage(config.database).issue(1)['state'] == 'discovered'


def test_cli_missing_check_exit_and_no_files(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(f'dispatcher:\n  root_path: {tmp_path}\n  workspace: workspace\n  database: state/db\nprojects: []\n')
    before = tree(tmp_path)
    monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(config_file), 'db', 'check'])
    from dispatcher.__main__ import main
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1
    assert 'FAIL' in capsys.readouterr().err
    assert tree(tmp_path) == before


@pytest.mark.parametrize('status', ['reserved', 'preparing'])
def test_restore_stopped_prelaunch_history(config, tmp_path, status):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'x'), 'p')
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='preparing' WHERE issue_id=1")
        db.execute('INSERT INTO executions(issue_id,status,started_at) VALUES(1,?,?)', (status, 'now'))
    artifact = config.workspace / 'retained-result'
    artifact.write_text('unpublished')
    for _ in range(2):
        assert 'BLOCKED' in maintain(config, 'restore', snapshot)
        assert check(config)[3] == 1
        assert artifact.read_text() == 'unpublished'
    archives = list(tmp_path.glob('db.before-restore-*'))
    assert len(archives) == 2
    assert any(_archived_status(p) == status for p in archives)


def _archived_status(path):
    with inspection(path) as db:
        row = db.execute('SELECT status FROM executions').fetchone()
        return row[0] if row else None


@pytest.mark.parametrize('status', ['preparing', 'executing', 'publishing'])
def test_restore_unknown_launched_executor_preserves_db(config, tmp_path, status):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'x'), 'p')
    with store.transaction() as db:
        db.execute("INSERT INTO executions(issue_id,status,started_at,codex_started_at) VALUES(1,?,'now','now')", (status,))
    before = config.database.read_bytes()
    with pytest.raises(SchemaError, match='stop'):
        maintain(config, 'restore', snapshot)
    assert config.database.read_bytes() == before
    assert not list(tmp_path.glob('db.before-restore-*'))


def test_restore_dangling_control_socket_refused(config, tmp_path):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    (config.workspace / '.control.sock').symlink_to(tmp_path / 'absent')
    before = config.database.read_bytes()
    with pytest.raises(SchemaError, match='socket'):
        maintain(config, 'restore', snapshot)
    assert config.database.read_bytes() == before


def test_restore_inherited_worker_lock(config, tmp_path):
    import subprocess
    import sys
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    with InstanceLock(config) as lock:
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                 stdin=subprocess.PIPE, pass_fds=lock.fds)
    try:
        before = config.database.read_bytes()
        with pytest.raises(AlreadyRunning):
            maintain(config, 'restore', snapshot)
        assert config.database.read_bytes() == before
    finally:
        child.communicate(timeout=5)


def assert_no_sidecars(path):
    assert not Path(str(path) + '-wal').exists()
    assert not Path(str(path) + '-shm').exists()


def test_offline_wal_backup_restore_without_checkpoint(config, tmp_path):
    maintain(config, 'bootstrap')
    with connection(config.database) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
    assert_no_sidecars(config.database)
    for number in range(2):
        snapshot = tmp_path / f'snapshot-{number}'
        assert maintain(config, 'backup', snapshot) == 'PASS backup verified'
        assert_no_sidecars(config.database)
        assert_no_sidecars(snapshot)
        before = tree(tmp_path)
        # check() must not write into the backup directory either.
        check(config, path=snapshot)
        assert tree(tmp_path) == before
        contents = snapshot.read_bytes()
        assert 'BLOCKED' in maintain(config, 'restore', snapshot)
        assert snapshot.read_bytes() == contents
        assert_no_sidecars(snapshot)
        assert_no_sidecars(config.database)
        assert check(config)[3] == 1
        for archive in tmp_path.glob('db.before-restore-*'):
            assert_no_sidecars(archive)
            with inspection(archive) as db:
                validate(db)


def test_readonly_wal_close_reproduction(tmp_path):
    """A closed connection can leave sidecars; writable SQLite close owns cleanup."""
    path = tmp_path / 'wal-db'
    with connection(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE example(value)')
    assert_no_sidecars(path)
    with connection(path.as_uri() + '?mode=ro', uri=True) as src:
        with connection(':memory:') as dst:
            src.backup(dst)
    assert Path(str(path) + '-wal').stat().st_size == 0
    assert Path(str(path) + '-shm').exists()
    with connection(path.as_uri() + '?mode=rw', uri=True) as db:
        db.execute('SELECT * FROM example').fetchall()
    assert_no_sidecars(path)


def test_online_backup_preserves_wal_and_uncommitted_transaction(config, tmp_path):
    maintain(config, 'bootstrap')
    with connection(config.database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO debug_operations(created_at,level,component,operation,result) VALUES('now','api','test','committed','ok')")
        writer.commit()
        writer.execute("UPDATE debug_operations SET operation='uncommitted'")
        sidecars = {suffix: Path(str(config.database) + suffix)
                    for suffix in ('-wal', '-shm')}
        wal_before = sidecars['-wal'].read_bytes()
        inodes = {suffix: p.stat().st_ino for suffix, p in sidecars.items()}
        snapshot = tmp_path / 'online'
        maintain(config, 'backup', snapshot)
        assert sidecars['-wal'].read_bytes() == wal_before
        assert {suffix: p.stat().st_ino for suffix, p in sidecars.items()} == inodes
        with inspection(snapshot) as db:
            validate(db)
            assert db.execute('SELECT operation FROM debug_operations').fetchall() == [('committed',)]
        writer.commit()
        assert writer.execute('SELECT operation FROM debug_operations').fetchall() == [('uncommitted',)]
    assert_no_sidecars(config.database)


def test_restore_backup_with_committed_wal_is_untouched(config, tmp_path):
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    with connection(snapshot) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO debug_operations(created_at,level,component,operation,result) VALUES('now','api','test','in-wal','ok')")
        writer.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.glob('snapshot*')}
        maintain(config, 'restore', snapshot)
        assert {p.name: p.read_bytes() for p in tmp_path.glob('snapshot*')} == before
        with inspection(config.database) as db:
            assert db.execute('SELECT operation FROM debug_operations').fetchall() == [('in-wal',)]
        assert_no_sidecars(config.database)


@pytest.mark.parametrize('after_copy', [False, True])
def test_restore_staging_failure_preserves_sources(config, tmp_path, monkeypatch, after_copy):
    import dispatcher.db_guard as guard
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    before = config.database.read_bytes(), snapshot.read_bytes()
    original = guard.backup

    def failing_backup(source, destination, **kwargs):
        if kwargs.get('private_source'):
            if after_copy:
                original(source, destination, **kwargs)
            raise OSError('staging failure')
        return original(source, destination, **kwargs)

    monkeypatch.setattr(guard, 'backup', failing_backup)
    with pytest.raises(OSError, match='staging failure'):
        maintain(config, 'restore', snapshot)
    assert (config.database.read_bytes(), snapshot.read_bytes()) == before
    assert not list(tmp_path.glob('.restore-*'))
    assert list(tmp_path.glob('db.before-restore-*'))
    assert_no_sidecars(config.database)
    assert_no_sidecars(snapshot)


def test_cli_wal_backup_restore_separate_processes(config, tmp_path):
    import subprocess
    import sys
    maintain(config, 'bootstrap')
    with connection(config.database) as db:
        db.execute('PRAGMA journal_mode=WAL')
    settings = tmp_path / 'config.yaml'
    settings.write_text(f'dispatcher:\n  root_path: {tmp_path}\n  workspace: {config.workspace}\n  database: {config.database}\nprojects: []\n')
    snapshot = tmp_path / 'cli-snapshot'
    for command in ('backup', 'restore'):
        result = subprocess.run(
            [sys.executable, '-m', 'dispatcher', '--config', str(settings),
             'db', command, str(snapshot)], capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'PASS' in result.stdout
        assert_no_sidecars(config.database)
        assert_no_sidecars(snapshot)
    assert check(config)[3] == 1


def test_restore_sidecar_appearing_during_archive_is_preserved(config, tmp_path, monkeypatch):
    import dispatcher.db_guard as guard
    maintain(config, 'bootstrap')
    snapshot = tmp_path / 'snapshot'
    maintain(config, 'backup', snapshot)
    before = config.database.read_bytes()
    original = guard.backup
    sidecar = Path(str(config.database) + '-wal')

    def archive_with_new_sidecar(source, destination, **kwargs):
        original(source, destination, **kwargs)
        sidecar.touch()

    monkeypatch.setattr(guard, 'backup', archive_with_new_sidecar)
    with pytest.raises(SchemaError, match='WAL/SHM'):
        maintain(config, 'restore', snapshot)
    assert sidecar.exists()
    assert config.database.read_bytes() == before
    assert not list(tmp_path.glob('.restore-*'))
