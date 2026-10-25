"""Additive migration, CAS branch claims and cross-connection exclusion."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from dispatcher.storage import Store, migrations
from test_stage602 import PROJECT, item


def seed(path):
    store = Store(path)
    store.apply(PROJECT, [item(), item(id=102, number=2)])
    return store


def save(store, number=1, **changes):
    row = store.get(PROJECT.repository, number)
    args = dict(expected_version=row['version'], expected_iteration_id=row['current_iteration_id'],
                expected=store.branches(PROJECT.repository, number), base_branch='main',
                work_branch='shared', branch_source='explicit', branch_status='resolved')
    args.update(changes)
    return store.save_branches(PROJECT.repository, number, **args)


@pytest.mark.parametrize('marker', [602, 604])
def test_additive_preserves_every_table_and_reopens(tmp_path, marker):
    path = tmp_path / 'db'
    store = seed(path)
    db = store.db
    store.confirm(PROJECT.repository, 1, 1)
    iteration = store.get(PROJECT.repository, 1)['current_iteration_id']
    db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,iteration_id) VALUES('a','owner/repo',1,1,'historical','running','now',?)", (iteration,))
    db.execute("UPDATE tasks SET state='review' WHERE number=1")
    db.execute("INSERT INTO task_pull_requests VALUES(301,101,11,2,'historical','main','sha','open',NULL)")
    db.execute("INSERT INTO iteration_pr_history VALUES(1,?,301,'sha','now')", (iteration,))
    db.execute("INSERT INTO pr_resolutions VALUES(301,?,'done','{}',NULL,NULL,NULL)", (iteration,))
    db.execute("INSERT INTO result_outbox VALUES('a','body',0,NULL)")
    db.execute("INSERT INTO publication_operations VALUES('op',?,'intent','pending',0,'now')", (iteration,))
    db.commit()
    # Reconstruct the exact pre-605 schema using SQLite DROP COLUMN.
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE '%branch_claim_%' OR name='task_branch_reactivate')").fetchall():
        db.execute('DROP TRIGGER ' + row[0])
    db.execute('DROP INDEX task_work_branch')
    for name in migrations.BRANCH_COLUMNS:
        db.execute('ALTER TABLE tasks DROP COLUMN ' + name)
    db.execute('DELETE FROM schema_migrations WHERE version IN (602,604,605)')
    db.execute('INSERT INTO schema_migrations VALUES(?)', (marker,))
    db.commit()
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
    before = {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables}
    store.close()
    for _ in range(2):
        store = Store(path)
        for table, rows in before.items():
            actual = [tuple(r) for r in store.db.execute('SELECT * FROM ' + table)]
            assert ([r[:-6] for r in actual] if table == 'tasks' else actual) == rows
        assert store.branches('OWNER/REPO', 1) == dict(base_branch=None, work_branch=None,
            branch_source=None, branch_status='unresolved', branch_error=None, branch_prepared_at=None)
        assert store.db.execute('SELECT 1 FROM schema_migrations WHERE version=605').fetchone()
        migrations.validate(store.db)
        store.close()
    assert not list(tmp_path.glob('*.bak'))


def test_rollback_prevents_opening(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    store = seed(path)
    store.db.execute('DELETE FROM schema_migrations WHERE version=605')
    store.db.execute('ALTER TABLE tasks DROP COLUMN branch_error')
    store.db.commit()
    store.close()
    def fail(db):
        assert 'branch_error' in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        raise migrations.MigrationError('injected failure')
    monkeypatch.setattr(migrations, 'validate', fail)
    with pytest.raises(migrations.MigrationError):
        Store(path)
    with sqlite3.connect(path) as db:
        assert 'branch_error' not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        assert not db.execute('SELECT 1 FROM schema_migrations WHERE version=605').fetchone()
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2


def test_parallel_claims_and_repository_scope(tmp_path):
    path = tmp_path / 'db'
    seed(path).close()
    def claim(number):
        store = Store(path)
        try:
            return save(store, number)
        except sqlite3.IntegrityError:
            return False
        finally:
            store.close()
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, [1, 2])) == [False, True]
    store = Store(path)
    store.apply(replace(PROJECT, repository='other/repo'), [item(id=201, repository_id=22)])
    row = store.get('other/repo', 1)
    assert store.save_branches('other/repo', 1, expected_version=1, expected_iteration_id=None,
        expected=store.branches('other/repo', 1), work_branch='shared')
    store.close()


@pytest.mark.parametrize('state', ['running', 'review', 'done', 'cancelled'])
def test_active_parameters_frozen(tmp_path, state):
    store = seed(tmp_path / 'db')
    assert save(store)
    before = store.branches(PROJECT.repository, 1)
    store.db.execute('UPDATE tasks SET state=? WHERE number=1', (state,))
    store.db.commit()
    assert not save(store, work_branch='other')
    assert store.branches(PROJECT.repository, 1) == before
    store.close()


def test_legacy_attempt_claims_and_reactivation(tmp_path):
    store = seed(tmp_path / 'db')
    db = store.db
    db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at) VALUES('a','owner/repo',1,1,'shared','review','now')")
    db.execute("UPDATE tasks SET state='review' WHERE number=1")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        save(store, 2)
    db.execute("UPDATE tasks SET state='done' WHERE number=1")
    db.commit()
    assert save(store, 2)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE attempts SET state='running' WHERE attempt_id='a'")
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE tasks SET state='pending' WHERE number=1")
    db.rollback()
    store.close()


def test_cas_audit_and_validation(tmp_path):
    store = seed(tmp_path / 'db')
    expected = store.branches(PROJECT.repository, 1)
    assert save(store)
    assert not save(store, expected=expected, work_branch='stale')
    assert not save(store, expected_version=2)
    assert not save(store, expected_iteration_id=99)
    assert save(store)
    assert store.db.execute("SELECT count(*) FROM history WHERE event='branches_updated'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        save(store, work_branch='../bad')
    with pytest.raises(sqlite3.IntegrityError):
        save(store, branch_source='unknown')
    store.close()


def test_admission_respects_claim_and_preserves_attempt_branch(tmp_path):
    from dispatcher.execution_store import ExecutionStore
    store = seed(tmp_path / 'db')
    assert save(store)
    assert store.confirm(PROJECT.repository, 1, 1)
    row = store.get(PROJECT.repository, 1)
    token = store.guard(PROJECT.repository, 1)['observation_token']
    execution = ExecutionStore(store)
    assert execution.reserve(row, 'different', 2, token) is None
    attempt = execution.reserve(row, 'shared', 2, token)
    assert attempt
    assert not save(store, work_branch='changed')
    assert execution.get(attempt)['branch'] == 'shared'
    store.close()


def test_state_reactivation_and_attempt_insert_cannot_bypass_claim(tmp_path):
    store = seed(tmp_path / 'db')
    assert save(store)
    db = store.db
    db.execute("UPDATE tasks SET state='done' WHERE number=1")
    db.commit()
    assert save(store, 2)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE tasks SET state='pending' WHERE number=1")
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at) VALUES('x','owner/repo',1,1,'shared','running','now')")
    db.rollback()
    store.close()


def test_integrity_failure_rolls_back_marker_and_stops_startup(tmp_path):
    path = tmp_path / 'db'
    seed(path).close()
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM schema_migrations WHERE version=605')
        db.execute("INSERT INTO result_outbox VALUES('missing-attempt','body',0,NULL)")
    with pytest.raises(migrations.MigrationError):
        Store(path)
    with sqlite3.connect(path) as db:
        assert not db.execute('SELECT 1 FROM schema_migrations WHERE version=605').fetchone()
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
