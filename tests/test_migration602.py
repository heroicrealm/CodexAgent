"""Issue #20: process-bounded migration of populated legacy SQLite databases."""
import fcntl
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dispatcher.storage import Store
from dispatcher.storage import migrations


# A timeout kills and reaps the child, including when SQLite's backup retry loop
# never returns. Also assert ordering explicitly: lock behavior varies by SQLite.
MIGRATE = r'''
import sqlite3
import sys
from pathlib import Path
from dispatcher.config import Project
from dispatcher.storage import Store

original_connect = sqlite3.connect
connections = []
class Connection(sqlite3.Connection):
    def backup(self, target, **kwargs):
        assert not any(connection.in_transaction for connection in connections), 'write transaction held during backup'
        return super().backup(target, **kwargs)
def connect(*args, **kwargs):
    connection = original_connect(*args, factory=Connection, **kwargs)
    connections.append(connection)
    return connection
sqlite3.connect = connect
path = Path(sys.argv[1])
store = Store(path)
assert store.db.execute('SELECT 1 FROM schema_migrations WHERE version=602').fetchone()
for table in ('tasks', 'history', 'attempts', 'result_outbox'):
    assert store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
project = Project('new', True, 'owner/repo', 'main', 'ready', ('alice',))
store.apply(project, [dict(id=5001, repository_id=11, number=1, title='New task',
    body='New requirements', author='alice', target_branch='main', eligible=True)])
store.close()
store = Store(path)
assert store.get('owner/repo', 1)['issue_id'] == 5001
assert len(list(path.parent.glob('*.bak'))) == 1
store.close()
'''


def run_child(code, path):
    env = dict(os.environ)
    # Keep this helper runnable both under scripts/test.sh and plain pytest.
    env['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1] / 'src'), env.get('PYTHONPATH', '')))
    return subprocess.run([sys.executable, '-c', code, str(path)], env=env,
                          capture_output=True, text=True, timeout=10)


def populated_legacy(path, journal_mode):
    db = sqlite3.connect(path)
    assert db.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0].casefold() == journal_mode.casefold()
    db.execute('PRAGMA wal_autocheckpoint=0')
    db.executescript('''
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(1);
        INSERT INTO schema_migrations VALUES(2);
        INSERT INTO schema_migrations VALUES(3);
        CREATE TABLE tasks(
            repository TEXT NOT NULL, number INTEGER NOT NULL, issue_id INTEGER NOT NULL,
            project_id TEXT NOT NULL, state TEXT NOT NULL, available INTEGER NOT NULL,
            version INTEGER NOT NULL, content_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
            observed TEXT NOT NULL, approved_version INTEGER, created_at TEXT NOT NULL,
            synced_at TEXT NOT NULL, PRIMARY KEY(repository,number));
        CREATE TABLE history(
            id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
            version INTEGER NOT NULL, event TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(repository,number) REFERENCES tasks(repository,number));
        CREATE TABLE attempts(
            attempt_id TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
            version INTEGER NOT NULL, branch TEXT NOT NULL, state TEXT NOT NULL,
            started_at TEXT NOT NULL, result_sha TEXT, UNIQUE(repository,number,version));
        CREATE TABLE result_outbox(
            attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), body TEXT NOT NULL,
            published INTEGER NOT NULL DEFAULT 0, comment_id TEXT);
        INSERT INTO tasks VALUES('owner/repo',1,101,'old','review',1,1,'hash','snapshot','observed',1,'created','synced');
        INSERT INTO history VALUES(1,'owner/repo',1,1,'review','legacy snapshot','created');
        INSERT INTO attempts VALUES('old-attempt','owner/repo',1,1,'feature','review','started','old-sha');
        INSERT INTO result_outbox VALUES('old-attempt','legacy result',0,NULL);
    ''')
    return db


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_populated_legacy_migration_completes_with_timeout(tmp_path, journal_mode):
    path = tmp_path / 'tasks.sqlite3'
    db = populated_legacy(path, journal_mode)
    try:
        expected = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                    for table in ('tasks', 'history', 'attempts', 'result_outbox', 'schema_migrations')}
        if journal_mode == 'WAL':
            # Keep the writer open so committed legacy data actually remains in
            # WAL while the separate migration process performs its backup.
            assert Path(str(path) + '-wal').stat().st_size > 0
        result = run_child(MIGRATE, path)
        assert result.returncode == 0, result.stderr
        backups = list(tmp_path.glob('*.bak'))
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not backup.execute('PRAGMA foreign_key_check').fetchall()
            for table, rows in expected.items():
                assert backup.execute(f'SELECT * FROM {table}').fetchall() == rows
        assert db.execute('SELECT issue_id FROM tasks').fetchone()[0] == 5001
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM history').fetchone()[0] == 1  # new discovery only
    finally:
        db.close()


def test_concurrent_process_cannot_migrate_under_runtime_lock(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    db = populated_legacy(path, 'WAL')
    try:
        with (tmp_path / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = run_child('''
import sys
from dispatcher.storage import Store
from dispatcher.storage.migrations import MigrationBusy
try:
    Store(sys.argv[1])
except MigrationBusy:
    pass
else:
    raise AssertionError('Migration bypassed runtime lock')
''', path)
            assert result.returncode == 0, result.stderr
        assert db.execute('SELECT issue_id FROM tasks').fetchone()[0] == 101
        assert not list(tmp_path.glob('*.bak'))
    finally:
        db.close()


def test_stale_opener_rechecks_schema_after_other_migrator(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite3'
    db = populated_legacy(path, 'WAL')
    real_flock = fcntl.flock
    calls = []
    def competing_migrator(fd, operation):
        # Complete a real independent migration after the outer opener's first
        # schema read, but before it obtains the workspace lock.
        calls.append(operation)
        result = run_child(MIGRATE, path)
        assert result.returncode == 0, result.stderr
        real_flock(fd, operation)
    monkeypatch.setattr(migrations.fcntl, 'flock', competing_migrator)
    try:
        store = Store(path)
        assert store.get('owner/repo', 1)['issue_id'] == 5001
        store.close()
        assert len(calls) == 1
        assert len(list(tmp_path.glob('*.bak'))) == 1
    finally:
        db.close()


def test_backup_verification_failure_does_not_start_reset(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite3'
    db = populated_legacy(path, 'WAL')
    connect = sqlite3.connect
    class InvalidBackup(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'PRAGMA integrity_check':
                return super().execute("SELECT 'injected integrity failure'")
            return super().execute(sql, *args, **kwargs)
    def checked_connect(database, *args, **kwargs):
        if str(database).endswith('.bak'):
            kwargs['factory'] = InvalidBackup
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(migrations.sqlite3, 'connect', checked_connect)
    try:
        with pytest.raises(migrations.MigrationError):
            Store(path)
        assert db.execute('SELECT issue_id FROM tasks').fetchone()[0] == 101
        assert db.execute('SELECT count(*) FROM history').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM schema_migrations WHERE version=602').fetchone()
        assert len(list(tmp_path.glob('*.bak'))) == 1
    finally:
        db.close()
