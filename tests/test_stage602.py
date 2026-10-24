"""Stage 6.2 contracts: temporary SQLite and verified-response fixtures only."""
import fcntl
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

import pytest

from dispatcher.config import Project
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import Adapter, GitHubError
from dispatcher.storage import Store
from dispatcher.storage.transitions import Authority, Evidence


OWNER = Authority('alice', ('alice',))
OBSERVER = replace(OWNER, role='observer')
PROJECT = Project('one', True, 'owner/repo', 'main', 'ready', ('alice',))
SHA = 'a' * 40


def item(**changes):
    value = dict(id=101, repository_id=11, number=1, title='Task', body='Requirements',
                 author='alice', target_branch='feature', eligible=True,
                 labels=['ready'], state='open', server_observed_at='2026-10-24T00:00:00Z')
    value.update(changes)
    return value


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'tasks.sqlite3')
    value.apply(PROJECT, [item()])
    yield value
    value.close()


def evidence(store, **changes):
    row = store.get('owner/repo', 1)
    value = dict(repository_id=11, issue_id=101, issue_number=1,
                 observed_at='2026-10-24T00:03:00Z', event_at='2026-10-24T00:02:00Z',
                 state_observed_at='2026-10-24T00:01:00Z', content_hash=row['content_hash'], iteration_id=row['current_iteration_id'], expected_state=row['state'])
    value.update(changes)
    return Evidence(**value)


def move(store, target, key, **changes):
    row = store.get('owner/repo', 1)
    args = dict(repository='owner/repo', number=1, expected_state=row['state'], version=row['version'],
                iteration_id=row['current_iteration_id'], target=target, event_key=key,
                authority=OBSERVER if target in ('review', 'done', 'rejected') and row['state'] != 'discovered' else OWNER,
                evidence=evidence(store))
    args.update(changes)
    return store.transition(**args)


def pr(store, **changes):
    return evidence(store, pr_id=301, pr_number=2, head='feature', base='main', head_sha=SHA,
                    linked_issue_id=101, **({'pr_state': 'open', 'merged': False} | changes))


def running(store):
    assert move(store, 'pending', 'approve:1') == 'applied'
    execution = ExecutionStore(store)
    row = store.get('owner/repo', 1)
    attempt = execution.reserve(row, 'feature', 2, store.guard('owner/repo', 1)['observation_token'])
    assert attempt
    return execution, attempt


def review(store):
    execution, attempt = running(store)
    assert move(store, 'review', 'pr:open', evidence=pr(store)) == 'applied'
    # Finish the technical attempt without using its legacy state transition.
    execution.finish(attempt, 'review')
    return execution, attempt


def test_new_schema_and_foreign_keys(store):
    assert store.db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO publication_operations VALUES('bad',999,'intent','pending',0,'now')")
    store.db.rollback()
    assert not list(__import__('pathlib').Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent.glob('*.bak'))


def test_approve_idempotency_and_iteration(store):
    row = store.get('owner/repo', 1)
    args = dict(repository='owner/repo', number=1, expected_state='discovered', version=1, iteration_id=None,
                target='pending', event_key='event', authority=OWNER, evidence=evidence(store))
    assert store.transition(**args) == 'applied'
    assert store.transition(**args) == 'already_applied'
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 1
    assert store.get('owner/repo', 1)['current_iteration_id']
    assert move(store, 'pending', 'different') == 'invalid_transition'
    assert row['version'] == store.get('owner/repo', 1)['version']


@pytest.mark.parametrize('change,outcome', [
    ({'authority': replace(OWNER, actor='mallory')}, 'unauthorized'),
    ({'authority': replace(OWNER, human=False)}, 'unauthorized'),
    ({'version': 2}, 'stale_version'),
    ({'iteration_id': 9}, 'stale_version'),
    ({'evidence': None}, 'missing_evidence'),
    ({'event_key': ''}, 'missing_evidence'),
])
def test_guards(store, change, outcome):
    assert move(store, 'pending', 'approve', **change) == outcome
    assert store.get('owner/repo', 1)['state'] == 'discovered'
    assert not store.db.execute('SELECT * FROM decisions').fetchall()


@pytest.mark.parametrize('change,outcome', [
    ({'available': False}, 'missing_evidence'),
    ({'event_at': '2026-10-24T00:01:00Z'}, 'missing_evidence'),
    ({'event_at': '2026-10-24T00:04:00Z'}, 'missing_evidence'),
    ({'observed_at': 'invalid'}, 'missing_evidence'),
    ({'repository_id': 22}, 'conflict'),
    ({'issue_id': 999}, 'conflict'),
    ({'issue_id': True}, 'identity_unresolved'),
    ({'content_hash': 'changed'}, 'stale_version'),
])
def test_evidence_guards(store, change, outcome):
    assert move(store, 'pending', 'approve', evidence=evidence(store, **change)) == outcome


def test_same_version_new_iteration_new_pr_and_sha_history(store):
    review(store)
    assert move(store, 'rejected', 'pr:closed', evidence=pr(store, pr_state='closed')) == 'applied'
    first = store.get('owner/repo', 1)['current_iteration_id']
    fresh = evidence(store, observed_at='2026-10-24T00:06:00Z', event_at='2026-10-24T00:05:00Z',
                     state_observed_at='2026-10-24T00:04:00Z')
    assert move(store, 'pending', 'approve:2', evidence=fresh) == 'applied'
    row = store.get('owner/repo', 1)
    assert row['version'] == 1 and row['current_iteration_id'] != first
    assert [r[0] for r in store.db.execute('SELECT iteration_number FROM task_iterations ORDER BY iteration_number')] == [1, 2]
    execution = ExecutionStore(store)
    attempt = execution.reserve(row, 'feature-2', 1, store.guard('owner/repo', 1)['observation_token'])
    assert attempt
    assert move(store, 'review', 'pr:reopened', evidence=pr(store)) == 'conflict'
    assert move(store, 'review', 'pr:new', evidence=replace(pr(store), pr_id=302, pr_number=3, head='feature-2')) == 'applied'
    assert store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM iteration_pr_history').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2


def test_rejected_requires_new_server_event(store):
    review(store)
    assert move(store, 'rejected', 'closed', evidence=pr(store, pr_state='closed')) == 'applied'
    assert move(store, 'pending', 'old-approve') == 'missing_evidence'


@pytest.mark.parametrize('target,proof,expected', [
    ('done', dict(pr_state='open', merged=False), 'missing_evidence'),
    ('done', dict(pr_state='closed', merged=False), 'missing_evidence'),
    ('done', dict(pr_state='closed', merged=True, merged_at='2026-10-24T00:02:00Z'), 'applied'),
    ('rejected', dict(pr_state='closed', merged=False), 'applied'),
    ('rejected', dict(pr_state='closed', merged=True, merged_at='2026-10-24T00:02:00Z'), 'missing_evidence'),
])
def test_merge_and_reject_evidence(store, target, proof, expected):
    review(store)
    assert move(store, target, 'result', evidence=pr(store, **proof)) == expected


def test_unverified_push_never_proves_pr(store):
    execution, attempt = running(store)
    execution.update(attempt, pushed=1, result_sha=SHA)
    assert move(store, 'review', 'push') == 'missing_evidence'
    assert store.get('owner/repo', 1)['state'] == 'running'


@pytest.mark.parametrize('terminal', ['done', 'cancelled'])
def test_terminal_states(store, terminal):
    review(store)
    if terminal == 'done':
        assert move(store, terminal, 'merged', evidence=pr(store, pr_state='closed', merged=True, merged_at='2026-10-24T00:02:00Z')) == 'applied'
    else:
        assert move(store, 'rejected', 'closed', evidence=pr(store, pr_state='closed')) == 'applied'
        assert move(store, terminal, 'cancel', evidence=evidence(store, observed_at='2026-10-24T00:06:00Z',
                    event_at='2026-10-24T00:05:00Z', state_observed_at='2026-10-24T00:04:00Z')) == 'applied'
    store.apply(PROJECT, [])
    store.apply(PROJECT, [item(body='reopened')])
    assert store.get('owner/repo', 1)['state'] == terminal
    assert move(store, 'pending', 'another') == 'invalid_transition'
    assert store.decide('owner/repo', 1, 1, 'approve') == 'conflict'


def test_identity_conflicts_and_two_repositories(store):
    other = replace(PROJECT, id='two', repository='other/repo')
    store.apply(other, [item(id=102, repository_id=12)])
    assert len(store.queue()) == 2
    with pytest.raises(sqlite3.IntegrityError):
        store.apply(other, [item(id=101, repository_id=12)])
    with pytest.raises(sqlite3.IntegrityError):
        store.apply(PROJECT, [item(repository_id=12)])
    assert store.get('owner/repo', 1)['repository_id'] == 11


def test_unavailable_identity(store):
    with store.db:
        store.db.execute('UPDATE tasks SET repository_id=NULL')
    assert move(store, 'pending', 'a') == 'identity_unresolved'
    store.apply(PROJECT, [item()])
    assert move(store, 'pending', 'a') == 'applied'


def test_pr_wrong_identity_and_same_number(store):
    running(store)
    assert move(store, 'review', 'bad', evidence=replace(pr(store), linked_issue_id=999)) == 'missing_evidence'
    assert move(store, 'review', 'good', evidence=pr(store)) == 'applied'
    assert move(store, 'done', 'other', evidence=replace(pr(store, pr_state='closed', merged=True, merged_at='2026-10-24T00:02:00Z'), pr_id=999)) == 'conflict'


def test_publication_retry_does_not_create_attempt(store):
    execution, attempt = running(store)
    iteration = execution.get(attempt)['iteration_id']
    assert store.publication('op', iteration) == 'applied'
    assert store.publication('op', iteration, 'pr', 'failed', retry=True) == 'applied'
    assert store.publication('op', iteration, 'pr', 'completed', retry=True) == 'applied'
    assert store.publication('op', iteration, retry=True) == 'already_applied'
    assert store.db.execute('SELECT retry_count FROM publication_operations').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,iteration_id,attempt_number) VALUES('duplicate','owner/repo',1,1,'other','failed','now',?,1)", (iteration,))
    store.db.rollback()
    # Explicitly recorded technical attempts have independent ordinal numbers.
    with store.db:
        store.db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,iteration_id,attempt_number) VALUES('technical','owner/repo',1,1,'other','failed','now',?,2)", (iteration,))


def test_concurrent_approve(store):
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    proof = evidence(store)
    def approve(_):
        connection = Store(path)
        try:
            return connection.transition(repository='owner/repo', number=1, expected_state='discovered',
                version=1, iteration_id=None, target='pending', event_key='same-event', authority=OWNER, evidence=proof)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, range(2))) == ['already_applied', 'applied']
    assert store.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 1


def test_transaction_rolls_back_all_transition_writes(store):
    store.db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        move(store, 'pending', 'event')
    assert store.get('owner/repo', 1)['state'] == 'discovered'
    for table in ('task_iterations', 'task_transitions', 'decisions'):
        assert not store.db.execute(f'SELECT * FROM {table}').fetchall()


def legacy(path):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE tasks(old TEXT); INSERT INTO tasks VALUES("original");')
    db.close()


def test_migration_blocked_by_daemon_lock(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    legacy(path)
    with (tmp_path / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError):
            Store(path)
    assert sqlite3.connect(path).execute('SELECT * FROM tasks').fetchone()[0] == 'original'
    assert not list(tmp_path.glob('*.bak'))


def test_failed_backup_never_clears_database(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite3'
    legacy(path)
    # Backup destination cannot be created.
    monkeypatch.setattr('dispatcher.storage.migrations.uuid.uuid4', lambda: type('ID', (), {'hex': 'fixed'})())
    (tmp_path / 'tasks.sqlite3.pre-602-fixed.bak').mkdir()
    with pytest.raises(RuntimeError):
        Store(path)
    assert sqlite3.connect(path).execute('SELECT * FROM tasks').fetchone()[0] == 'original'


def test_migration_never_clears_new_data_on_reopen(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    legacy(path)
    store = Store(path)
    store.apply(PROJECT, [item()])
    move(store, 'pending', 'approve')
    store.close()
    store = Store(path)
    assert store.get('owner/repo', 1)['state'] == 'pending'
    assert len(list(tmp_path.glob('*.bak'))) == 1
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    store.close()


@pytest.mark.parametrize('response', [dict(id=11, full_name='owner/repo'), dict(id=True, full_name='owner/repo'),
                                     dict(id=11, full_name='wrong/repo'), dict(id='node-id', full_name='owner/repo')])
def test_adapter_repository_rest_identity(response):
    adapter = Adapter()
    adapter.request = Mock(return_value=response)
    if type(response['id']) is int and response['full_name'] == 'owner/repo':
        assert adapter.repository_identity(PROJECT) == 11
    else:
        with pytest.raises(GitHubError):
            adapter.repository_identity(PROJECT)


def test_guarded_reservation_creates_one_attempt(store):
    from dispatcher.storage.transitions import Reservation
    assert move(store, 'pending', 'approval') == 'applied'
    row = store.get('owner/repo', 1)
    request = dict(repository='owner/repo', number=1, expected_state='pending', version=1,
                   iteration_id=row['current_iteration_id'], target='running', event_key='reserve',
                   authority=OWNER, evidence=evidence(store),
                   reservation=Reservation('feature', 1, store.guard('owner/repo', 1)['observation_token']))
    assert store.transition(**request) == 'applied'
    assert store.transition(**request) == 'already_applied'
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


def test_guarded_reservation_requires_current_observation(store):
    from dispatcher.storage.transitions import Reservation
    assert move(store, 'pending', 'approval') == 'applied'
    assert move(store, 'running', 'reserve', reservation=Reservation('feature', 1, 'stale')) == 'conflict'
    assert store.get('owner/repo', 1)['state'] == 'pending'
    assert not store.db.execute('SELECT * FROM attempts').fetchall()


def test_old_iteration_cannot_reserve_new_approval(store):
    execution, attempt = review(store)
    old = dict(store.get('owner/repo', 1), state='pending')
    assert move(store, 'rejected', 'closed', evidence=pr(store, pr_state='closed')) == 'applied'
    assert move(store, 'pending', 'approve:2', evidence=evidence(store,
        observed_at='2026-10-24T00:06:00Z', event_at='2026-10-24T00:05:00Z',
        state_observed_at='2026-10-24T00:04:00Z')) == 'applied'
    assert execution.reserve(old, 'feature', 1, store.guard('owner/repo', 1)['observation_token']) is None
    assert store.get('owner/repo', 1)['state'] == 'pending'


def test_failed_or_interrupted_does_not_retry_automatically(store):
    execution, attempt = running(store)
    execution.finish(attempt, 'failed', 'codex_failed')
    assert move(store, 'pending', 'retry') == 'invalid_transition'
    assert execution.reserve(store.get('owner/repo', 1), 'feature', 1,
                             store.guard('owner/repo', 1)['observation_token']) is None
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


def test_runtime_outbox_tracks_intent_failure_and_retry_without_codex(store):
    from dispatcher.publisher import TaskResultPublisher
    execution, attempt = running(store)
    execution.finish(attempt, 'failed', 'codex_failed')
    operation = store.db.execute('SELECT * FROM publication_operations').fetchone()
    assert operation['operation_key'] == 'comment:' + attempt
    assert operation['phase'] == 'intent' and operation['retry_count'] == 0
    adapter = Adapter()
    adapter.pages = Mock(return_value=[])
    adapter.write = Mock(side_effect=GitHubError())
    publisher = TaskResultPublisher(execution, adapter)
    publisher.flush()
    assert store.db.execute('SELECT outcome FROM publication_operations').fetchone()[0] == 'failed'
    adapter.write.side_effect = None
    adapter.write.return_value = {'id': 999}
    publisher.flush()
    operation = store.db.execute('SELECT * FROM publication_operations').fetchone()
    assert operation['outcome'] == 'completed' and operation['retry_count'] == 1
    publisher.flush()
    assert adapter.write.call_count == 2
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
