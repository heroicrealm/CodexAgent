"""Local Stage A storage checks; no Publisher or GitHub service is exercised."""
from dataclasses import replace
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from dispatcher.models import ApprovedSnapshot, IssueObservation, State
from dispatcher.storage import SchemaError, Storage


@pytest.fixture
def store(tmp_path):
    storage = Storage(tmp_path / 'v2.sqlite')
    storage.register_issue(IssueObservation(1, 'owner/new', 1, 'Observed title'), 'p')
    return storage


@pytest.fixture
def snapshot():
    return ApprovedSnapshot('Approved title', 'Approved issue body', 'run_codex: false\n',
                            'snapshot-hash', 123, 'INPUT\n```yaml\nrun_codex: false\n```')


def test_comment_schema_and_old_intermediate_schema_rejected(store):
    with store.connection() as db:
        fields = {r['name']: r['type'] for r in db.execute('PRAGMA table_info(issues)')}
        assert {name: fields[name] for name in ('input_comment_id', 'approved_input',
                'output_comment_id', 'output_body')} == {
                    'input_comment_id': 'INTEGER', 'approved_input': 'TEXT',
                    'output_comment_id': 'INTEGER', 'output_body': 'TEXT'}
        assert db.execute('SELECT version FROM schema_migrations').fetchone()[0] == 1
        db.execute('ALTER TABLE issues DROP COLUMN approved_input')
    before = store.path.read_bytes()
    with pytest.raises(SchemaError):
        Storage(store.path)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('target', [State.PENDING, State.AWAITING_CHILDREN])
def test_atomic_approval_and_immutable_input(store, snapshot, target):
    assert store.commit_approval(1, snapshot, target)
    row = store.issue(1)
    assert row['state'] == target
    assert row['approved_title'] == snapshot.title
    assert row['approved_body'] == snapshot.body
    assert row['approved_hash'] == snapshot.approved_hash
    assert row['input_comment_id'] == 123
    assert row['approved_input'] == snapshot.approved_input
    assert row['parameters_yaml'] == snapshot.parameters_yaml
    assert row['approved_at'].endswith('+00:00')
    assert row['is_approved'] == 0  # observed label flag, not local approval
    store.register_issue(IssueObservation(1, 'owner/new', 1, 'Edited issue'), 'p')
    assert not store.commit_approval(1, replace(snapshot, approved_input='Edited INPUT'), target)
    assert dict(Storage(store.path).issue(1)) == dict(row)
    with store.connection() as db:
        history = db.execute('SELECT old_state,new_state,reason FROM state_history WHERE issue_id=1 ORDER BY id').fetchall()
        assert [tuple(r) for r in history] == [(None, 'discovered', 'registration'),
                                              ('discovered', target, 'approval')]


def test_approval_without_input(store):
    snap = ApprovedSnapshot('Title', 'Body', None, 'hash')
    assert store.commit_approval(1, snap, State.PENDING)
    row = store.issue(1)
    assert row['input_comment_id'] is None
    assert row['approved_input'] is None
    assert row['parameters_yaml'] is None


@pytest.mark.parametrize('changes', [
    {'input_comment_id': None}, {'approved_input': None}, {'input_comment_id': 0},
    {'input_comment_id': True}, {'parameters_yaml': 'run_codex: ['},
    {'parameters_yaml': '- list'}, {'parameters_yaml': ''},
    {'approved_hash': ''}, {'body': None},
])
def test_incomplete_or_malformed_snapshot_rejected(store, snapshot, changes):
    before = dict(store.issue(1))
    with pytest.raises(ValueError):
        store.commit_approval(1, replace(snapshot, **changes), State.PENDING)
    assert dict(store.issue(1)) == before
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 1


def test_approval_rollback_on_snapshot_write_failure(store, snapshot):
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_snapshot BEFORE UPDATE OF approved_input ON issues "
                   "BEGIN SELECT RAISE(ABORT,'snapshot failed'); END")
    before = dict(store.issue(1))
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_approval(1, snapshot, State.PENDING)
    assert dict(store.issue(1)) == before
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 1


def test_concurrent_approvals_keep_one_complete_snapshot(store, snapshot):
    alternatives = [snapshot, replace(snapshot, title='Other', input_comment_id=124,
                                      approved_input='Other INPUT', approved_hash='other-hash')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(lambda snap: store.commit_approval(1, snap, State.PENDING), alternatives))
    assert sum(accepted) == 1
    winner = alternatives[accepted.index(True)]
    row = store.issue(1)
    assert (row['approved_title'], row['input_comment_id'], row['approved_input'], row['approved_hash']) == (
        winner.title, winner.input_comment_id, winner.approved_input, winner.approved_hash)


def test_approval_missing_virtual_wrong_state_and_invalid_target(store, snapshot):
    assert not store.commit_approval(99, snapshot, State.PENDING)
    store.register_child(IssueObservation(2, 'owner/new', 2, 'Child'),
                         IssueObservation(3, 'owner/new', 3, 'Parent'), 'p')
    assert not store.commit_approval(3, snapshot, State.PENDING)
    with pytest.raises(ValueError):
        store.commit_approval(1, snapshot, State.RUNNING)
    store.transition(1, State.DISCOVERED, State.ERROR, 'storage test')
    assert not store.commit_approval(1, snapshot, State.PENDING)


def test_output_receipts_creation_update_reopen_and_failure(store):
    assert store.issue(1)['output_comment_id'] is None
    store.record_confirmed_output(1, 900, 'Published OUTPUT')
    row = store.issue(1)
    assert (row['output_comment_id'], row['output_body']) == (900, 'Published OUTPUT')
    store.record_confirmed_output(1, 900, 'Updated OUTPUT')
    store.record_confirmed_output(1, 900, 'Updated OUTPUT')
    assert Storage(store.path).issue(1)['output_body'] == 'Updated OUTPUT'
    # A rejected publication has no receipt and never calls this storage method.
    # Storage write failure also retains the previous confirmed pair.
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_output BEFORE UPDATE OF output_body ON issues "
                   "BEGIN SELECT RAISE(ABORT,'output failed'); END")
    before = dict(store.issue(1))
    with pytest.raises(sqlite3.IntegrityError):
        store.record_confirmed_output(1, 901, 'Uncommitted OUTPUT')
    assert dict(store.issue(1)) == before
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM state_history').fetchone()[0] == 1


@pytest.mark.parametrize('comment_id,body', [(0, 'body'), (-1, 'body'), (True, 'body'),
                                            (123, None), (None, 'body')])
def test_output_rejects_incomplete_receipt(store, comment_id, body):
    with pytest.raises(ValueError):
        store.record_confirmed_output(1, comment_id, body)
    assert store.issue(1)['output_comment_id'] is None
    assert store.issue(1)['output_body'] is None


def test_output_requires_real_issue(store):
    with pytest.raises(ValueError):
        store.record_confirmed_output(99, 900, 'body')
    store.register_child(IssueObservation(2, 'owner/new', 2, 'Child'),
                         IssueObservation(3, 'owner/new', 3, 'Parent'), 'p')
    with pytest.raises(ValueError):
        store.record_confirmed_output(3, 900, 'body')


def test_conflicting_group_approval_rolls_back_snapshot(store, snapshot):
    store.transition(1, State.DISCOVERED, State.NEED_FIX, 'storage test')
    store.register_issue(IssueObservation(2, 'owner/new', 2, 'Correction'), 'p', 1)
    store.register_issue(IssueObservation(3, 'owner/new', 3, 'Other correction'), 'p', 1)
    assert store.commit_approval(2, snapshot, State.PENDING)
    before = dict(store.issue(3))
    with pytest.raises(ValueError, match='active issue'):
        store.commit_approval(3, snapshot, State.PENDING)
    assert dict(store.issue(3)) == before


def test_output_and_input_are_independent(store, snapshot):
    store.record_confirmed_output(1, 900, 'Awaiting approval')
    assert store.commit_approval(1, snapshot, State.PENDING)
    store.record_confirmed_output(1, 900, 'Approved')
    row = store.issue(1)
    assert row['approved_input'] == snapshot.approved_input
    assert row['input_comment_id'] == snapshot.input_comment_id
    assert row['output_body'] == 'Approved'
    assert row['output_comment_id'] == 900
