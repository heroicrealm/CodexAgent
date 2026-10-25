"""Issue 27: authenticated first retry/cancellation and durable side effects."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_stage604 import monitored, poll
from test_stage603 import publication
from test_stage04 import env
from test_stage602 import store, PROJECT, item
from test_stage03 import event_proof
from dispatcher.queue import commands
from dispatcher.storage import Store
from dispatcher.github import GitHubError
from dispatcher.cancellation import CancellationDelivery
from dispatcher.workspace import TaskError


def command(monitored, action='approve', *, event_id=80, actor='alice', changed=False,
            when='2026-10-24T00:22:00Z', extra=None, unavailable=False):
    db, api, *_ = monitored
    before = {1: dict(db.get(PROJECT.repository, 1))}
    names = ['ready', PROJECT.label('cancelled' if action == 'cancel' else action).name]
    if extra:
        names.append(PROJECT.label(extra).name)
    raw = item(labels=names, server_observed_at='2026-10-24T00:23:00Z')
    if changed:
        raw['body'] = 'changed requirements'
    observation = {}
    db.apply(PROJECT, [raw], observation=observation)
    event, proof = event_proof(PROJECT, db, 'cancelled' if action == 'cancel' else action, actor=actor, event_id=event_id)
    proof['databaseId'] = 101
    proof['labels']['nodes'] = [dict(name=n) for n in names]
    event['created_at'] = when
    api.response_date = '2026-10-24T00:24:00Z'
    api.command_events.side_effect = GitHubError() if unavailable else None
    api.command_events.return_value = [event], proof
    return commands(db, api, PROJECT, [raw], before, observation)


def rejected(monitored):
    assert poll(monitored) == []
    assert monitored[0].get(PROJECT.repository, 1)['state'] == 'rejected'


def test_first_correct_retry_and_repeat(monitored):
    rejected(monitored)
    assert command(monitored)[0]['outcome'] == 'applied'
    db = monitored[0]
    assert db.get(PROJECT.repository, 1)['state'] == 'pending'
    assert command(monitored)[0]['outcome'] == 'duplicate'
    assert [r[0] for r in db.db.execute('SELECT to_state FROM task_transitions ORDER BY rowid')][-2:] == ['rejected', 'pending']
    assert db.db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 2


@pytest.mark.parametrize('action,case,reason', [
    (action, case, reason) for action in ('approve', 'cancel')
    for case, reason in [('old', 'ambiguous_event_time'), ('edit', 'stale_version'),
                         ('untrusted', 'untrusted_actor')]
] + [('cancel', 'future', 'ambiguous_event_time')])
def test_invalid_commands_preserve_rejected(monitored, case, reason, action):
    rejected(monitored)
    kwargs = dict(changed=case == 'edit', actor='stranger' if case == 'untrusted' else 'alice')
    if case == 'old': kwargs['when'] = '2026-10-24T00:01:00Z'
    if case == 'future': kwargs['when'] = '2027-01-01T00:00:00Z'
    assert command(monitored, action, **kwargs)[0]['reason'] == reason
    assert monitored[0].get(PROJECT.repository, 1)['state'] == 'rejected'


def test_unknown_remove_reapply_requires_observed_absence(monitored):
    rejected(monitored)
    assert command(monitored, unavailable=True)[0]['reason'] == 'proof_unavailable'
    assert command(monitored, event_id=81)[0]['outcome'] == 'quarantined'
    db = monitored[0]
    db.apply(PROJECT, [item(server_observed_at='2026-10-24T00:25:00Z')])
    commands(db, monitored[1], PROJECT, [item(server_observed_at='2026-10-24T00:25:00Z')], {})
    assert command(monitored, event_id=82, when='2026-10-24T00:26:00Z')[0]['outcome'] == 'applied'


@pytest.mark.parametrize('other', ['approve', 'reject'])
def test_cancel_conflicting_labels_never_decides(monitored, other):
    rejected(monitored)
    receipts = command(monitored, 'cancel', extra=other)
    assert all(r['reason'] == 'conflicting_labels' for r in receipts)
    assert monitored[0].get(PROJECT.repository, 1)['state'] == 'rejected'


def test_cancel_audit_and_terminal(monitored):
    rejected(monitored)
    assert command(monitored, 'cancel')[0]['outcome'] == 'applied'
    db = monitored[0]
    audit = db.db.execute("SELECT * FROM task_transitions WHERE to_state='cancelled'").fetchone()
    assert audit['actor'] == 'alice'
    assert json.loads(audit['evidence'])['event']['id'] == 80
    history = db.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0]
    for raw in (item(eligible=False, state='closed'), item(body='reopened and edited')):
        db.apply(PROJECT, [raw])
        assert db.decide(PROJECT.repository, 1, 1, 'approve') == 'conflict'
        assert db.get(PROJECT.repository, 1)['state'] == 'cancelled'
    assert db.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == history


@pytest.mark.parametrize('failure', ['close', 'close_lost', 'cleanup', 'none'])
def test_delivery_retry_restart_no_double_cleanup(monitored, tmp_path, failure):
    rejected(monitored)
    assert command(monitored, 'cancel')[0]['outcome'] == 'applied'
    db, api, _, remote, _, writes, _ = monitored
    config = SimpleNamespace(workspace=tmp_path / 'workspace', logs=tmp_path / 'logs')
    delivery = CancellationDelivery(config, db, api)
    write = api.write.side_effect
    def failed(endpoint, method, payload):
        if failure == 'close': raise GitHubError()
        result = write(endpoint, method, payload)
        if failure == 'close_lost': raise GitHubError()
        return result
    api.write.side_effect = failed
    delivery.cleanup = Mock(side_effect=TaskError('dirty_worktree') if failure == 'cleanup' else None)
    assert delivery.poll(PROJECT) == ([1] if failure in ('close', 'close_lost') else [])
    delivery.cleanup.assert_not_called()
    path = db.db.execute('PRAGMA database_list').fetchone()[2]
    reopened = Store(path)
    try:
        api.write.side_effect = write
        retry = CancellationDelivery(config, reopened, api)
        retry.cleanup = Mock()
        assert retry.poll(PROJECT) == retry.poll(PROJECT) == []
        retry.cleanup.assert_not_called()
        assert remote['state'] == 'closed' and remote['state_reason'] == 'not_planned'
        assert sum(w[1] == 'PATCH' for w in writes) == 1
        assert reopened.db.execute('SELECT issue_closed,cleaned,error FROM cancellations').fetchone()[:] == (1, 0, None)
    finally:
        reopened.close()


@pytest.mark.parametrize('case', ['clean', 'archive', 'dirty', 'foreign', 'branch', 'sha', 'symlink'])
def test_real_git_cleanup_requires_ownership_and_preserves_data(env, case):
    from test_stage04 import adapter, register, finish, issue, git
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    from dispatcher.pr_monitor import PullRequestMonitor
    config, project, *_ = env
    api = adapter([issue(project)])
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        finish(dispatcher)
        api._pr.update(state='closed', closed_at='2026-10-24T00:15:00Z', merged_at=None)
        api.response_date = '2026-10-24T00:20:00Z'
        assert PullRequestMonitor(dispatcher.store, api).poll(project) == []
        assert dispatcher.store.decide(project.repository, 1, 1, 'cancel', expected_state='rejected') == 'applied'
        row = dispatcher.store.get(project.repository, 1)
        repo, task, worktree = dispatcher.git.paths(project, 1)
        if case == 'archive':
            attempt = dispatcher.store.db.execute('SELECT attempt_id FROM attempts').fetchone()[0]
            git(repo, 'worktree', 'move', str(worktree), str(task / ('previous-' + attempt)))
            attachments = task / ('attachments-' + attempt)
            attachments.mkdir()
            (attachments / '001-test.txt').write_text('owned attachment')
            (attachments / 'manifest.json').write_text(json.dumps([dict(local_path=str(task / 'attachments' / '001-test.txt'))]))
        if case == 'dirty': (worktree / 'human.txt').write_text('preserve')
        if case == 'foreign': (task / 'human.txt').write_text('preserve')
        if case == 'branch': git(worktree, 'switch', '-c', 'human-branch')
        if case == 'sha':
            (worktree / 'human.txt').write_text('preserve')
            git(worktree, 'add', '.')
            git(worktree, 'commit', '-m', 'Human changes')
        if case == 'symlink': (task / 'foreign').symlink_to(config.source)
        delivery = CancellationDelivery(config, dispatcher.store, api)
        history = [tuple(r) for r in dispatcher.store.db.execute('SELECT * FROM attempts')]
        if case in ('clean', 'archive'):
            delivery.cleanup(project, row)
            delivery.cleanup(project, row)
            assert not task.exists()
        else:
            with pytest.raises(TaskError): delivery.cleanup(project, row)
            assert task.exists() and worktree.exists()
        assert history == [tuple(r) for r in dispatcher.store.db.execute('SELECT * FROM attempts')]
    finally:
        dispatcher.close()


def test_stale_receipt_stays_duplicate_then_new_event_approves_current_version(monitored):
    rejected(monitored)
    assert command(monitored, changed=True)[0]['reason'] == 'stale_version'
    # The same label event cannot become consent merely because a poll now knows v2.
    assert command(monitored, changed=True)[0]['outcome'] == 'duplicate'
    assert command(monitored, changed=True, event_id=81, when='2026-10-24T00:26:00Z')[0]['outcome'] == 'applied'
    assert monitored[0].get(PROJECT.repository, 1)['approved_version'] == 2


@pytest.mark.parametrize('action', ['approve', 'cancel'])
@pytest.mark.parametrize('mutation,reason', [('bot', 'untrusted_actor'), ('missing', 'no_event_proof'),
                                           ('identity', 'eligibility_changed')])
def test_label_presence_is_not_authorization(monitored, action, mutation, reason):
    rejected(monitored)
    db, api, *_ = monitored
    original = api.command_events
    # Interpose on the proof returned by command(), retaining the real verifier.
    def proof(*args):
        event, data = original.return_value
        if mutation == 'missing': return [], data
        if mutation == 'bot': event[0]['actor']['type'] = 'Bot'
        if mutation == 'identity': data['databaseId'] = 999
        return event, data
    # command() resets side_effect, so patch the call to commands locally.
    from unittest.mock import patch
    real_commands = commands
    def verify(*args):
        api.command_events.side_effect = proof
        return real_commands(*args)
    with patch(__name__ + '.commands', side_effect=verify):
        assert command(monitored, action)[0]['reason'] == reason
    assert db.get(PROJECT.repository, 1)['state'] == 'rejected'


def test_concurrent_cancel_delivery_closes_once(monitored, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    rejected(monitored)
    assert command(monitored, 'cancel')[0]['outcome'] == 'applied'
    db, api, *_ = monitored
    config = SimpleNamespace(workspace=tmp_path / 'workspace', logs=tmp_path / 'logs')
    path = db.db.execute('PRAGMA database_list').fetchone()[2]
    entered, release = Event(), Event()
    cleanup = Mock()
    original_request = api.request.side_effect
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original_request(*args)
    api.request.side_effect = paused
    def worker():
        connection = Store(path)
        try:
            delivery = CancellationDelivery(config, connection, api)
            delivery.cleanup = cleanup
            return delivery.poll(PROJECT)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        contender = CancellationDelivery(config, db, api)
        contender.cleanup = cleanup
        assert contender.poll(PROJECT) == []
        release.set()
        assert future.result(timeout=5) == []
        assert contender.poll(PROJECT) == []
    cleanup.assert_not_called()
    assert db.db.execute("SELECT issue_closed FROM cancellations").fetchone()[0] == 1


def test_sync_poll_delivers_cancel_and_reconciles_cancelled(monitored, tmp_path, monkeypatch):
    from dispatcher.github.sync import SyncService
    rejected(monitored)
    db, api, _, remote, *_ = monitored
    event, proof = event_proof(PROJECT, db, 'cancelled', event_id=81)
    event['created_at'] = '2026-10-24T00:22:00Z'
    proof['databaseId'] = 101
    api.command_events.return_value = [event], proof
    api.response_date = '2026-10-24T00:24:00Z'
    api.fetch.return_value = [item(labels=['ready', PROJECT.label('cancelled').name],
                                  server_observed_at='2026-10-24T00:23:00Z')]
    config = SimpleNamespace(projects=[PROJECT], workspace=tmp_path / 'workspace', logs=tmp_path / 'logs')
    monkeypatch.setattr('dispatcher.github.sync.ready', lambda project: True)
    sync = SyncService(config, db, api)
    receipt = sync.poll()
    assert receipt['errors'] == []
    assert receipt['commands'][0]['action'] == 'cancel'
    assert receipt['commands'][0]['outcome'] == 'applied'
    assert remote['state_reason'] == 'not_planned'
    assert api.reconcile.call_args.args[1]['state'] == 'cancelled'
    assert sync.poll()['commands'] == []
    assert db.get(PROJECT.repository, 1)['state'] == 'cancelled'
