import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from dispatcher.storage import Storage, SchemaError
from dispatcher.models import IssueObservation, State, PRObservation, PRStatus


@pytest.fixture
def store(tmp_path):
    return Storage(tmp_path / 'v2.sqlite')


def obs(id=1, repo='owner/new', number=1):
    return IssueObservation(id, repo, number, 'title')


def test_schema_and_reopen(store):
    store.register_issue(obs(), 'p')
    with store.connection() as db:
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0] == 8
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'").fetchone()[0] == 11
        assert db.execute('SELECT version FROM schema_migrations').fetchone()[0] == 1
    assert Storage(store.path).issue(1)['title'] == 'title'
    assert store.issue(1)['created_at'].endswith('+00:00')


@pytest.mark.parametrize('damage', ['old', 'version', 'index', 'check', 'trigger', 'corrupt', 'fk'])
def test_reject_without_mutation(tmp_path, damage):
    path = tmp_path / 'db'
    if damage == 'corrupt':
        path.write_bytes(b'not sqlite')
    elif damage == 'old':
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE legacy (id)')
    else:
        s = Storage(path)
        with s.connection() as db:
            if damage == 'version':
                db.execute('UPDATE schema_migrations SET version=2')
            elif damage == 'index':
                db.execute('DROP INDEX idx_issues_state')
            elif damage == 'trigger':
                db.execute('CREATE TRIGGER extra AFTER INSERT ON issues BEGIN SELECT 1; END')
            elif damage == 'check':
                db.execute('PRAGMA writable_schema=ON')
                db.execute("UPDATE sqlite_master SET sql=replace(sql,'issue_number > 0','issue_number >= 0') WHERE name='issues'")
            elif damage == 'fk':
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('INSERT INTO issue_relations VALUES (123,456)')
        # Close the last connection before recording on-disk state.
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        Storage(path)
    assert path.read_bytes() == before


def test_virtual_materialization_and_idempotency(store):
    store.register_child(obs(2, number=2), obs(), 'p')
    store.register_child(obs(2, number=2), obs(), 'p')
    assert store.issue(1)['is_virtual'] == 1
    assert store.reserve(1) is None
    store.register_issue(obs(), 'p')
    store.register_issue(obs(), 'p')
    assert store.issue(1)['execution_group_id'] == 1
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM issue_relations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 2


def test_registration_rollback_and_fk(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.register_child(obs(), obs(), 'p')
    assert store.issue(1) is None
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute('INSERT INTO issue_relations VALUES (100,200)')
    store.register_issue(obs(), 'p')
    store.register_issue(obs(2, 'another/repo'), 'q')
    with pytest.raises(sqlite3.IntegrityError):
        store.register_issue(obs(3), 'p')


def test_cas_reservation_and_marker(store):
    store.register_issue(obs(), 'p')
    assert store.transition(1, State.DISCOVERED, State.PENDING, 'test caller approval')
    assert not store.transition(1, State.DISCOVERED, State.PENDING, 'duplicate')
    def reserve(_):
        return store.reserve(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    execution = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    assert store.issue(1)['state'] == State.PREPARING
    assert store.mark_codex_started(execution.execution_id)
    assert not Storage(store.path).mark_codex_started(execution.execution_id)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 3


def test_reservation_rollback(store):
    store.register_issue(obs(), 'p')
    store.transition(1, State.DISCOVERED, State.PENDING, 'test')
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail BEFORE INSERT ON executions BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.reserve(1)
    assert store.issue(1)['state'] == State.PENDING
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 2


def test_corrections_branch_and_active_group(store):
    store.register_issue(obs(), 'p')
    with pytest.raises(ValueError):
        store.register_issue(obs(2, number=2), 'p', 1)
    store.transition(1, State.DISCOVERED, State.NEED_FIX, 'test primitive')
    store.register_issue(obs(2, number=2), 'p', 1)
    store.register_issue(obs(3, number=3), 'p', 1)
    assert store.issue(3)['execution_group_id'] == 1
    assert store.issue(3)['correction_of_issue_id'] == 1
    store.transition(2, State.DISCOVERED, State.PENDING, 'test')
    with pytest.raises(ValueError):
        store.transition(3, State.DISCOVERED, State.PENDING, 'test')
    store.transition(1, State.NEED_FIX, State.DONE, 'test primitive')
    with pytest.raises(ValueError):
        store.register_issue(obs(4, number=4), 'p', 1)


def test_pr_and_errors(store):
    store.register_issue(obs(), 'p')
    pr = PRObservation(100, 1, 'owner/new', 1, 'work', 'base', PRStatus.OPEN)
    store.record_pr(pr)
    store.record_pr(pr)
    store.record_error(1, 'prepare_failed', 'prepare', 'Safe summary')
    with pytest.raises(ValueError):
        store.record_error(1, 'error', 'prepare', 'summary', 999)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM pull_requests').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM error_log').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 1


def test_group_origin_and_branches(store):
    store.register_issue(obs(), 'p')
    store.set_group_branches(1, 'base', 'work')
    store.transition(1, State.DISCOVERED, State.NEED_FIX, 'test')
    store.register_issue(obs(2, number=2), 'p', 1)
    # A correction may reference any previous issue, not only the need-fix issue.
    store.register_issue(obs(3, number=3), 'p', 2)
    assert store.issue(3)['correction_of_issue_id'] == 2
    assert store.group_branches(3) == ('base', 'work')
    store.set_group_branches(3, 'base', 'work')
    with pytest.raises(ValueError):
        store.set_group_branches(2, 'different', 'work')
    with pytest.raises(ValueError):
        store.register_issue(obs(4, 'other/repo', 4), 'p', 1)


def test_transaction_context_rolls_back(store):
    with pytest.raises(RuntimeError), store.transaction() as db:
        db.execute("INSERT INTO schema_migrations VALUES (2,'test')")
        raise RuntimeError('rollback')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 1
