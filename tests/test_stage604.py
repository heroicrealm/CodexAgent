"""PR decisions, recoverable side effects, history and explicit new iterations."""
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from test_stage602 import store, PROJECT, SHA, item, pr, move, evidence, OBSERVER
from test_stage603 import publication, run
from test_stage04 import env, git, issue
from dispatcher.github import GitHubError
from dispatcher.pr_monitor import PullRequestMonitor
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError


@pytest.fixture
def monitored(publication):
    run(publication)
    publisher, execution, attempt, api, git_manager, pull = publication
    api.reset_mock(side_effect=True)
    api.response_date = '2026-10-24T00:20:00Z'
    pull = deepcopy(pull)
    pull.update(state='closed', merged=False, closed_at='2026-10-24T00:15:00Z', merged_at=None)
    remote_issue = dict(id=101, state='open', state_reason=None, closed_at=None)
    comments, writes = [], []
    def request(endpoint):
        return deepcopy(pull if '/pulls/' in endpoint else remote_issue)
    def pages(endpoint):
        if endpoint.endswith('/timeline'):
            return [dict(event='closed', created_at=pull['closed_at'], actor=dict(login='reviewer'))]
        return deepcopy(comments)
    def write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        if method == 'PATCH':
            remote_issue.update(payload, closed_at='2026-10-24T00:21:00Z')
            return deepcopy(remote_issue)
        comment = dict(id=901 + len(comments), **payload)
        comments.append(comment)
        return deepcopy(comment)
    api.request.side_effect, api.pages.side_effect, api.write.side_effect = request, pages, write
    return execution.store, api, pull, remote_issue, comments, writes, attempt


def poll(monitored, project=PROJECT):
    return PullRequestMonitor(monitored[0], monitored[1]).poll(project)


def merged(monitored):
    monitored[2].update(merged=True, merged_at='2026-10-24T00:15:00Z',
                        merged_by=dict(login='merger'), merge_commit_sha='c'*40)


def test_open_no_decision(monitored):
    monitored[2].update(state='open')
    assert poll(monitored) == []
    assert monitored[0].get(PROJECT.repository, 1)['state'] == 'review'
    assert not monitored[4] and not monitored[5]


def test_merge_closes_before_comment_and_is_idempotent(monitored):
    merged(monitored)
    assert poll(monitored) == []
    store, api, pull, remote, comments, writes, _ = monitored
    assert store.get(PROJECT.repository, 1)['state'] == 'done'
    assert remote['state_reason'] == 'completed'
    assert [w[1] for w in writes] == ['PATCH', 'POST']
    body = comments[0]['body']
    for value in ('merger', pull['merged_at'], remote['closed_at'], '/pull/2', 'Status: done'):
        assert value in body
    assert poll(monitored) == []
    assert len(comments) == 1 and len(writes) == 2
    saved = store.db.execute('SELECT * FROM pr_resolutions').fetchone()
    assert json.loads(saved['metadata'])['merge_commit_sha'] == 'c'*40


@pytest.mark.parametrize('actor', ['known', 'missing', 'unavailable'])
def test_rejected_actor_and_time(monitored, actor):
    store, api, pull, _, comments, writes, _ = monitored
    pages = api.pages.side_effect
    if actor != 'known':
        def changed(endpoint):
            if endpoint.endswith('/timeline'):
                if actor == 'unavailable': raise GitHubError()
                return []
            return pages(endpoint)
        api.pages.side_effect = changed
    assert poll(monitored) == []
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert ('reviewer' if actor == 'known' else 'Не удалось определить') in comments[0]['body']
    assert pull['closed_at'] in comments[0]['body']
    assert all(method != 'PATCH' for _, method, _ in writes)
    # Reopening a resolved PR never launches or reverses a decision.
    pull['state'] = 'open'
    assert poll(monitored) == []
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert len(comments) == 1


@pytest.mark.parametrize('failure', ['close', 'close_lost', 'read_closed', 'comment', 'comment_lost', 'label'])
def test_recovery_across_restart(monitored, failure):
    merged(monitored)
    store, api, pull, remote, comments, writes, _ = monitored
    write, request = api.write.side_effect, api.request.side_effect
    def failed_write(endpoint, method, payload):
        if failure == 'close' and method == 'PATCH': raise GitHubError()
        if failure == 'comment' and method == 'POST': raise GitHubError()
        result = write(endpoint, method, payload)
        if (failure == 'close_lost' and method == 'PATCH') or (failure == 'comment_lost' and method == 'POST'):
            raise GitHubError()
        return result
    api.write.side_effect = failed_write
    if failure == 'read_closed':
        def failed_read(endpoint):
            if '/issues/' in endpoint and remote['state'] == 'closed': raise GitHubError()
            return request(endpoint)
        api.request.side_effect = failed_read
    if failure == 'label': api.reconcile.side_effect = GitHubError()
    assert poll(monitored) == [1]
    assert store.get(PROJECT.repository, 1)['state'] == 'done'
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    reopened = Store(path)
    api.write.side_effect, api.request.side_effect, api.reconcile.side_effect = write, request, None
    try:
        assert PullRequestMonitor(reopened, api).poll(PROJECT) == []
        assert PullRequestMonitor(reopened, api).poll(PROJECT) == []
        assert len(comments) == 1
        assert sum(w[1] == 'PATCH' for w in writes) == 1
        assert reopened.db.execute("SELECT outcome FROM publication_operations WHERE operation_key='resolution:301'").fetchone()[0] == 'completed'
    finally:
        reopened.close()


@pytest.mark.parametrize('mutation', ['404', 'id', 'repo', 'future', 'bad_time', 'sha'])
def test_invalid_evidence_preserves_review(monitored, mutation):
    store, api, pull, _, comments, writes, _ = monitored
    if mutation == '404': api.request.side_effect = GitHubError(status=404)
    if mutation == 'id': pull['id'] = 999
    if mutation == 'repo': pull['head']['repo']['id'] = 12
    if mutation == 'future': pull['closed_at'] = '2027-01-01T00:00:00Z'
    if mutation == 'bad_time': pull['closed_at'] = 'yesterday'
    if mutation == 'sha': pull['head']['sha'] = 'bad'
    assert poll(monitored) == [1]
    assert store.get(PROJECT.repository, 1)['state'] == 'review'
    assert not comments and not writes


def test_closed_issue_still_observes_merge(monitored):
    merged(monitored)
    store = monitored[0]
    store.apply(PROJECT, [item(eligible=False, state='closed')])
    assert store.get(PROJECT.repository, 1)['available'] == 0
    assert poll(monitored) == []
    assert store.get(PROJECT.repository, 1)['state'] == 'done'


def test_explicit_retry_without_edit_new_iteration_new_pr(monitored):
    store, api, pull, _, comments, writes, attempt = monitored
    assert poll(monitored) == []
    prior_iteration = store.get(PROJECT.repository, 1)['current_iteration_id']
    store.apply(PROJECT, [item()])
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert store.decide(PROJECT.repository, 1, 1, 'approve', expected_state='rejected') == 'applied'
    task = store.get(PROJECT.repository, 1)
    assert task['current_iteration_id'] != prior_iteration
    from dispatcher.execution_store import ExecutionStore
    execution = ExecutionStore(store)
    next_attempt = execution.reserve(task, 'feature-2', 1, store.guard(PROJECT.repository, 1)['observation_token'])
    assert next_attempt != attempt
    assert move(store, 'review', 'new-pr', evidence=replace(pr(store), pr_id=302, pr_number=3, head='feature-2')) == 'applied'
    assert len(store.db.execute('SELECT * FROM task_pull_requests').fetchall()) == 2
    assert {r['pr_id'] for r in store.db.execute('SELECT * FROM iteration_pr_history')} == {301, 302}
    assert poll(monitored) == [1]  # API still serves old PR: identity conflict.
    assert store.get(PROJECT.repository, 1)['state'] == 'review'


def test_edit_rejected_requires_fresh_explicit_confirmation(monitored):
    assert poll(monitored) == []
    store = monitored[0]
    store.apply(PROJECT, [item(body='New requirements')])
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert store.decide(PROJECT.repository, 1, 1, 'approve', expected_state='rejected') == 'conflict'
    assert store.decide(PROJECT.repository, 1, 2, 'approve', expected_state='rejected') == 'applied'


def test_done_and_cancelled_terminal(monitored):
    merged(monitored)
    assert poll(monitored) == []
    store = monitored[0]
    for state in ('done', 'cancelled'):
        with store.db:
            store.db.execute('UPDATE tasks SET state=?', (state,))
        store.apply(PROJECT, [item(body='Edited')])
        assert store.decide(PROJECT.repository, 1, 1, 'approve') == 'conflict'
        assert store.get(PROJECT.repository, 1)['state'] == state


def test_project_isolation(monitored):
    other = replace(PROJECT, id='two', repository='other/repo')
    monitored[0].apply(other, [item(id=102, repository_id=12)])
    assert poll(monitored, other) == []
    monitored[1].request.assert_not_called()
    assert poll(monitored) == []
    assert monitored[0].get(other.repository, 1)['state'] == 'discovered'


def test_retry_git_starts_at_previous_sha_and_preserves_old_worktree(env):
    config, project, _, seed, remote = env
    manager = GitWorkspaceManager(config)
    task_issue = issue(project)
    old, initial = manager.prepare(project, task_issue, 'feature')
    (old / 'change').write_text('previous result')
    git(old, 'add', '.')
    git(old, 'commit', '-m', 'First attempt')
    previous_sha = git(old, 'rev-parse', 'HEAD')
    git(old, 'push', 'origin', 'feature')
    retry = dict(task_issue, retry_sha=previous_sha, previous_attempt='a'*32)
    new, sha = manager.prepare(project, retry, 'codex/issue-1-iteration-2')
    assert sha == previous_sha
    assert git(new, 'branch', '--show-current') == 'codex/issue-1-iteration-2'
    assert git(new, 'rev-parse', 'feature') == previous_sha
    assert (new.parent / ('previous-' + 'a'*32) / 'change').read_text() == 'previous result'
    assert manager.publication_commit(new, 'codex/issue-1-iteration-2', sha, project.base_branch) == previous_sha


def test_retry_missing_sha_blocks(env):
    config, project, *_ = env
    manager = GitWorkspaceManager(config)
    for sha in (None, 'bad', 'f'*40):
        with pytest.raises(TaskError, match='retry_sha_unavailable'):
            manager.retry_source(project, 1, sha)


def test_scheduler_retry_creates_new_branch_and_pr_from_rejected_sha(env):
    from test_stage04 import adapter, register, finish
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    from dispatcher.queue import decide_cli
    config, project, *_ = env
    api = adapter([issue(project)])
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        finish(dispatcher)
        first = dispatcher.store.db.execute('SELECT * FROM attempts').fetchone()
        old_pr = deepcopy(api._pr)
        api._pr.update(state='closed', closed_at='2026-10-24T00:15:00Z', merged_at=None)
        api.response_date = '2026-10-24T00:20:00Z'
        assert PullRequestMonitor(dispatcher.store, api).poll(project) == []
        assert dispatcher.store.get(project.repository, 1)['state'] == 'rejected'
        assert decide_cli(dispatcher.store, api, project, 1, 'approve') == ('applied', True)
        original_write = api.write.side_effect
        def new_pr(endpoint, method, payload):
            result = original_write(endpoint, method, payload)
            if endpoint.endswith('/pulls'):
                result.update(id=302, number=3)
            return result
        api.write.side_effect = new_pr
        env[2].write_text(json.dumps(dict(exec='dirty')))
        dispatcher.admit()
        finish(dispatcher)
        attempts = dispatcher.store.db.execute('SELECT * FROM attempts ORDER BY rowid').fetchall()
        assert len(attempts) == 2
        second = attempts[1]
        assert second['state'] == 'review'
        assert second['initial_sha'] == first['result_sha']
        assert second['branch'] != first['branch']
        assert {r['pr_id'] for r in dispatcher.store.db.execute('SELECT * FROM task_pull_requests')} == {301, 302}
        assert old_pr['head']['ref'] == first['branch']
        assert not any(c.args[1] == 'PATCH' and '/pulls/' in c.args[0] for c in api.write.call_args_list)
    finally:
        dispatcher.close()


def test_poll_integration_and_failure_logging(monitored, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dispatcher.github.sync import SyncService
    monkeypatch.setattr('dispatcher.github.sync.ready', lambda p: True)
    merged(monitored)
    store, api, *_ = monitored
    api.fetch.return_value = [item()]
    config = SimpleNamespace(projects=[PROJECT], logs=tmp_path / 'logs')
    assert SyncService(config, store, api).poll()['errors'] == []
    assert store.get(PROJECT.repository, 1)['state'] == 'done'
    assert len(monitored[4]) == 1


def test_old_approve_label_cannot_restart_rejected(monitored):
    from test_stage03 import event_proof
    from dispatcher.queue import commands
    store, api, *_ = monitored
    assert poll(monitored) == []
    task_item = item(labels=['ready', PROJECT.label('approve').name])
    before = {1: dict(store.get(PROJECT.repository, 1))}
    observation = {}
    store.apply(PROJECT, [task_item], observation=observation)
    event, proof = event_proof(PROJECT, store)
    proof['databaseId'] = 101
    api.command_events.return_value = [event], proof
    result = commands(store, api, PROJECT, [task_item], before, observation)
    assert result[0]['reason'] == 'ambiguous_event_time'
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    # A new authenticated human command after rejection may approve unchanged content.
    store.clear_block(PROJECT.repository, 1, 'approve', '2026-10-24T00:21:00Z')
    event.update(id=8, created_at='2026-10-24T00:22:00Z')
    result = commands(store, api, PROJECT, [task_item], before, observation)
    assert result[0]['outcome'] == 'applied'
    assert store.get(PROJECT.repository, 1)['state'] == 'pending'


def test_migration_preserves_existing_pr_history(monitored):
    from dispatcher.storage import migrations
    store = monitored[0]
    db = store.db
    path = db.execute('PRAGMA database_list').fetchone()[2]
    expected = {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in
                ('tasks', 'attempts', 'task_pull_requests', 'task_iterations', 'iteration_pr_history', 'task_transitions')}
    # Build the prior schema's exact uniqueness constraint around real history.
    db.execute('PRAGMA foreign_keys=OFF')
    with db:
        statement = next(s for s in migrations.SCHEMA.split(';') if 'CREATE TABLE IF NOT EXISTS task_pull_requests(' in s)
        statement = statement.replace('task_pull_requests(', 'prior_pr(').replace('UNIQUE(repository_id,pr_number)', 'UNIQUE(repository_id,pr_number), UNIQUE(issue_id)')
        db.execute(statement)
        db.execute('INSERT INTO prior_pr SELECT * FROM task_pull_requests')
        db.execute('DROP TRIGGER pr_iteration_identity')
        db.execute('DROP TABLE task_pull_requests')
        db.execute('ALTER TABLE prior_pr RENAME TO task_pull_requests')
        db.execute('DELETE FROM schema_migrations WHERE version=604')
    db.execute('PRAGMA foreign_keys=ON')
    upgraded = Store(path)
    try:
        for table, records in expected.items():
            assert [tuple(r) for r in upgraded.db.execute('SELECT * FROM ' + table)] == records
        with upgraded.db:
            upgraded.db.execute("INSERT INTO task_pull_requests VALUES(302,101,11,3,'new','main',?,'open',NULL)", (SHA,))
        assert not upgraded.db.execute('PRAGMA foreign_key_check').fetchall()
        assert upgraded.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        upgraded.close()


def test_concurrent_monitors_serialize_comment_delivery(monitored):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    store, api, *_ = monitored
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    entered, release = Event(), Event()
    write = api.write.side_effect
    def paused(endpoint, method, payload):
        if method == 'POST':
            entered.set()
            assert release.wait(5)
        return write(endpoint, method, payload)
    api.write.side_effect = paused
    def worker():
        connection = Store(path)
        try:
            return PullRequestMonitor(connection, api).poll(PROJECT)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        assert poll(monitored) == []
        release.set()
        assert future.result(timeout=5) == []
    assert len(monitored[4]) == 1


def test_two_projects_same_issue_and_pr_numbers(monitored):
    from dispatcher.execution_store import ExecutionStore
    from dispatcher.storage.transitions import Authority, Evidence
    store, api, pull, *_ = monitored
    other = replace(PROJECT, id='two', repository='other/repo')
    store.apply(other, [item(id=102, repository_id=12)])
    assert store.confirm(other.repository, 1, 1)
    row = store.get(other.repository, 1)
    execution = ExecutionStore(store)
    second_attempt = execution.reserve(row, 'feature', 2, store.guard(other.repository, 1)['observation_token'])
    proof = Evidence(12, 102, 1, '2026-10-24T00:10:00Z', '2026-10-24T00:10:00Z',
                     '2026-10-24T00:00:00Z', row['content_hash'], row['current_iteration_id'], 'running',
                     pr_id=302, pr_number=2, head='feature', base='main', head_sha=SHA,
                     pr_state='open', merged=False, linked_issue_id=102)
    assert store.transition(repository=other.repository, number=1, expected_state='running', version=1,
        iteration_id=row['current_iteration_id'], target='review', event_key='other-pr',
        authority=Authority('observer', ('observer',), role='observer'), evidence=proof) == 'applied'
    execution.finish(second_attempt, 'review')
    second_pr = deepcopy(pull)
    second_pr.update(id=302, merged=True, merged_at=pull['closed_at'], merged_by=dict(login='other-merger'))
    second_pr['head']['repo']['id'] = second_pr['base']['repo']['id'] = 12
    request, pages, write = api.request.side_effect, api.pages.side_effect, api.write.side_effect
    other_comments = []
    def scoped_request(endpoint):
        if endpoint.startswith('repos/other/'):
            return second_pr if '/pulls/' in endpoint else dict(id=102, state='closed', state_reason='completed', closed_at='2026-10-24T00:16:00Z')
        return request(endpoint)
    api.request.side_effect = scoped_request
    api.pages.side_effect = lambda endpoint: other_comments if endpoint.startswith('repos/other/') else pages(endpoint)
    def scoped_write(endpoint, method, payload):
        if endpoint.startswith('repos/other/'):
            comment = dict(id=902, **payload)
            other_comments.append(comment)
            return comment
        return write(endpoint, method, payload)
    api.write.side_effect = scoped_write
    assert poll(monitored) == poll(monitored, other) == []
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert store.get(other.repository, 1)['state'] == 'done'
    assert 'other/repo/pull/2' in other_comments[0]['body']
    assert 'owner/repo/pull/2' in monitored[4][0]['body']
    assert len(other_comments) == len(monitored[4]) == 1


def test_cli_started_before_rejection_is_not_a_retry(monitored):
    from dispatcher.queue import decide_cli
    store, api, *_ = monitored
    def fetch(project):
        assert poll(monitored) == []
        return [item()]
    api.fetch.side_effect = fetch
    with pytest.raises(RuntimeError, match='conflicts'):
        decide_cli(store, api, PROJECT, 1, 'approve')
    assert store.get(PROJECT.repository, 1)['state'] == 'rejected'
