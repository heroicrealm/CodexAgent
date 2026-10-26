"""Terminal reopen decisions, publication recovery and immutable delivery."""
from copy import deepcopy
from unittest.mock import Mock
import sqlite3

import pytest

from test_stage602 import store, PROJECT, SHA, item, move, pr
from test_stage603 import publication, run
from test_stage04 import env
from dispatcher.execution_store import ExecutionStore
from dispatcher.pr_publication import PullRequestPublisher
from dispatcher.storage import Store
from dispatcher.github import GitHubError
from dispatcher.workspace import TaskError
from dispatcher.cleanup import CleanupService
from dispatcher.pr_monitor import PullRequestMonitor


@pytest.fixture
def repeated(publication):
    run(publication)
    publisher, execution, old, api, git, pull = publication
    s = execution.store
    assert move(s, 'rejected', 'close', evidence=pr(s, pr_state='closed', merged=False,
        observed_at='2026-10-24T00:20:00Z', event_at='2026-10-24T00:19:00Z',
        state_observed_at='2026-10-24T00:10:00Z')) == 'applied'
    assert move(s, 'pending', 'approve:2', evidence=pr(s,
        observed_at='2026-10-24T00:30:00Z', event_at='2026-10-24T00:29:00Z',
        state_observed_at='2026-10-24T00:20:00Z')) == 'applied'
    s.apply(PROJECT, [item()])
    row = s.get(PROJECT.repository, 1)
    attempt = execution.reserve(row, 'feature', 2, s.guard(PROJECT.repository, 1)['observation_token'])
    assert attempt
    execution.update(attempt, worktree='/tmp/worktree', initial_sha=SHA, origin_url='origin', codex_started=1, exit_code=0)
    publisher.intent(attempt)
    api.reset_mock(side_effect=True)
    api.repository_identity.return_value = 11
    pull.update(state='closed', merged=True, merged_at='2026-10-24T00:31:00Z')
    api.request.return_value = pull
    comments = []
    api.pages.side_effect = lambda endpoint: deepcopy(comments)
    def write(endpoint, method, payload):
        assert endpoint.endswith('/comments') and method == 'POST'
        value = dict(id=901 + len(comments), **payload)
        comments.append(value)
        return deepcopy(value)
    api.write.side_effect = write
    git.reset_mock()
    return publisher, execution, attempt, api, git, pull, comments


def error(repeated):
    return repeated[1].db.execute('SELECT * FROM terminal_errors').fetchone()


def test_terminal_and_idempotent(repeated):
    p, e, attempt, api, git, pull, comments = repeated
    p.publish(PROJECT, e.get(attempt))
    assert e.store.get(PROJECT.repository, 1)['state'] == 'review_on_error'
    assert e.get(attempt)['state'] == 'review_on_error'
    assert error(repeated)['publication'] == 'confirmed'
    assert error(repeated)['delivered'] == 1
    body = comments[0]['body']
    for text in ('canonical_pr_merged', 'feature', SHA, '/pull/2', 'confirmed', 'Manual resolution'):
        assert text in body
    p.flush([PROJECT]); p.flush([PROJECT])
    assert len(comments) == 1
    assert git.push.call_count == 1
    assert e.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
    api.request.reset_mock()
    assert PullRequestMonitor(e.store, api).poll(PROJECT) == []
    api.request.assert_not_called()


@pytest.mark.parametrize('failure', ['push', 'remote', 'comment', 'label', 'lost_comment'])
def test_restart_only_retries_publication(repeated, failure):
    p, e, attempt, api, git, pull, comments = repeated
    if failure == 'push': git.push.side_effect = TaskError('git_operation_failed', 'GIT')
    if failure == 'remote': git.git.return_value.stdout = 'c' * 40
    if failure == 'comment': api.pages.side_effect = GitHubError()
    if failure == 'label': api.reconcile.side_effect = GitHubError()
    if failure == 'lost_comment':
        write = api.write.side_effect
        def lost(*args):
            write(*args)
            raise GitHubError(kind='timeout')
        api.write.side_effect = lost
    p.publish(PROJECT, e.get(attempt))
    assert e.store.get(PROJECT.repository, 1)['state'] == 'review_on_error'
    assert error(repeated)['publication'] == ('pending' if failure in ('push','remote') else 'confirmed')
    assert not e.db.execute('SELECT * FROM cleanup_jobs').fetchall()
    filename = e.db.execute('PRAGMA database_list').fetchone()[2]
    git.push.side_effect = None
    git.git.return_value.stdout = SHA
    api.pages.side_effect = lambda endpoint: deepcopy(comments)
    api.write.side_effect = lambda endpoint, method, payload: comments.append(dict(id=902, **payload)) or comments[-1]
    api.reconcile.side_effect = None
    restarted = Store(filename)
    try:
        recovered = PullRequestPublisher(ExecutionStore(restarted), api, git)
        recovered.flush([PROJECT]); recovered.flush([PROJECT])
        assert restarted.get(PROJECT.repository, 1)['state'] == 'review_on_error'
        assert restarted.db.execute('SELECT publication FROM terminal_errors').fetchone()[0] == 'confirmed'
        assert len(comments) == 1
        assert restarted.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2
    finally:
        restarted.close()


@pytest.mark.parametrize('status', [None, 403, 404, 422, 500])
def test_http_alone_never_terminal(repeated, status):
    p,e,attempt,api,git,pull,comments = repeated
    pull.update(merged=False, merged_at=None)
    api.write.side_effect = GitHubError(status=status)
    p.publish(PROJECT, e.get(attempt))
    assert e.store.get(PROJECT.repository, 1)['state'] == 'running'
    assert error(repeated) is None
    assert not comments
    assert not e.db.execute('SELECT * FROM cleanup_jobs').fetchall()


def test_identity_mismatch_cannot_terminal(repeated):
    p,e,attempt,api,git,pull,comments = repeated
    pull['head']['repo']['id'] = 22
    p.publish(PROJECT, e.get(attempt))
    assert error(repeated) is None
    git.push.assert_not_called()


def test_terminal_guards_and_relabels(repeated):
    p,e,attempt,api,git,pull,comments = repeated
    p.publish(PROJECT, e.get(attempt))
    s = e.store
    row = s.get(PROJECT.repository, 1)
    assert not s.confirm(PROJECT.repository, 1, row['version'])
    assert not s.reset_confirmation(PROJECT.repository, 1, row['version'])
    assert s.decide(PROJECT.repository, 1, row['version'], 'approve') == 'conflict'
    s.apply(PROJECT, [])
    s.apply(PROJECT, [item(body='edited', labels=['ready','approve'])])
    assert s.get(PROJECT.repository, 1)['state'] == 'review_on_error'
    assert move(s, 'done', 'fake-merge') == 'invalid_transition'
    with pytest.raises(sqlite3.IntegrityError, match='Terminal'):
        with s.db:
            s.db.execute("UPDATE tasks SET state='discovered' WHERE issue_id=101")
    assert e.reserve(row, 'feature', 2, s.guard(PROJECT.repository, 1)['observation_token']) is None


def test_cleanup_requires_publication(repeated, tmp_path):
    p,e,attempt,api,git,pull,comments = repeated
    git.push.side_effect = TaskError('git_operation_failed', 'GIT')
    p.publish(PROJECT, e.get(attempt))
    # Reconcile is independent of delivery and never enqueues an unsafe job.
    config = Mock(workspace=tmp_path)
    service = CleanupService(config, e.store)
    service.reconcile()
    assert not e.db.execute('SELECT * FROM cleanup_jobs').fetchall()
    git.push.side_effect = None
    p.flush([PROJECT])
    service.reconcile()
    assert e.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'pending'


def test_label_contract():
    from dataclasses import replace
    from dispatcher.config import Label
    assert PROJECT.label('review_on_error').name == 'dispatcher:review-on-error'
    assert PROJECT.label('review_on_error').description
    assert replace(PROJECT, labels={'review_on_error': Label('custom', '123456')}).label('review_on_error').name == 'custom'


@pytest.mark.parametrize('body', [
    '```codex-config\nbase_branch: [main]\n```',
    '```codex-config\nbranch_name: x\nbranch_name: y\n```',
    '```codex-config\nother: x\n```',
    '```codex-config\nbranch_name: !!python/object:os.system {}\n```',
    '```codex-config\nbranch_name: x\n```\n```codex-config\nbranch_name: y\n```',
    '```codex-config\nbranch_name: [\n```',
])
def test_correctable_config_is_not_terminal(store, body):
    from dispatcher import preparation_error as prep
    store.apply(PROJECT, [item(body=body)])
    assert store.confirm(PROJECT.repository, 1, 2)
    row = store.get(PROJECT.repository, 1)
    with pytest.raises(TaskError) as exc:
        prep.parameters(body)
    prep.record(store, row, exc.value.code)
    assert store.get(PROJECT.repository, 1)['state'] == 'discovered'
    assert not store.confirm(PROJECT.repository, 1, 2)
    assert not store.db.execute('SELECT * FROM terminal_errors').fetchall()
    api = Mock()
    comments = []
    api.pages.side_effect = lambda endpoint: deepcopy(comments)
    def lost(endpoint, method, payload):
        comments.append(dict(id=1, **payload))
        raise GitHubError()
    api.write.side_effect = lost
    prep.flush(store, api, [PROJECT]); prep.flush(store, api, [PROJECT])
    assert len(comments) == 1
    store.apply(PROJECT, [item(body='fixed')])
    assert store.confirm(PROJECT.repository, 1, 3)


def test_real_git_publication_failure_restart_cleanup(env):
    from test_stage04 import adapter, register, finish, issue, git
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    config, project, control, seed, remote = env
    api = adapter([issue(project)])
    d = Dispatcher(config, Lifecycle(), api)
    try:
        register(d, project, [issue(project)])
        d.admit(); finish(d)
        api._pr.update(state='closed', closed_at='2026-10-24T00:15:00Z', merged_at=None)
        api.response_date = '2026-10-24T00:20:00Z'
        assert PullRequestMonitor(d.store, api).poll(project) == []
        assert d.store.decide(project.repository, 1, 1, 'approve', expected_state='rejected') == 'applied'
        control.write_text('{"exec":"dirty"}')
        d.admit()
        assert d.active
        api._pr.update(merged=True, merged_at='2026-10-24T00:31:00Z')
        d.git.push = Mock(side_effect=TaskError('git_operation_failed', 'GIT'))
        finish(d)
        row = d.store.get(project.repository, 1)
        assert row['state'] == 'review_on_error'
        repo, task, tree = d.git.paths(project, 1)
        sha = git(tree, 'rev-parse', 'HEAD')
        assert tree.exists()
        service = CleanupService(config, d.store)
        service.tick()
        assert tree.exists() and not d.store.db.execute('SELECT * FROM cleanup_jobs').fetchall()
        count = d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0]
    finally:
        d.close()
    d = Dispatcher(config, Lifecycle(), api)
    try:
        d.pr_publisher.flush([project])
        assert git(remote, 'rev-parse', row['work_branch']) == sha
        d.admit()
        assert not d.active
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == count
        CleanupService(config, d.store).tick()
        assert not task.exists()
        assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'completed'
        assert d.store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
    finally:
        d.close()


def test_concurrent_publishers_and_project_isolation(repeated):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    p,e,attempt,api,git,pull,comments = repeated
    second = replace(PROJECT, id='two', repository='owner/second')
    e.store.apply(second, [item(id=202, repository_id=22)])
    filename = e.db.execute('PRAGMA database_list').fetchone()[2]
    def worker():
        s = Store(filename)
        try:
            PullRequestPublisher(ExecutionStore(s), api, git).flush([PROJECT, second])
        finally:
            s.close()
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: worker(), range(2)))
    p.flush([PROJECT, second])
    assert len(comments) == 1
    assert git.push.call_count == 1
    assert e.store.get(second.repository, 1)['state'] == 'discovered'
    assert e.store.get(PROJECT.repository, 1)['state'] == 'review_on_error'


def test_merge_during_reopen_reconciles_before_terminal(repeated):
    p,e,attempt,api,git,pull,comments = repeated
    pull.update(merged=False, merged_at=None)
    def write(endpoint, method, payload):
        assert method == 'PATCH'
        pull.update(merged=True, merged_at='2026-10-24T00:31:00Z')
        raise GitHubError(kind='timeout')
    api.write.side_effect = write
    p.publish(PROJECT, e.get(attempt))
    assert e.store.get(PROJECT.repository, 1)['state'] == 'review_on_error'
    api.write.side_effect = lambda endpoint, method, payload: comments.append(dict(id=902, **payload)) or comments[-1]
    p.flush([PROJECT])
    assert error(repeated)['publication'] == 'confirmed'
    assert len(comments) == 1


def test_network_outside_transactions(repeated):
    p,e,attempt,api,git,pull,comments = repeated
    for target, name in ((api,'request'),(api,'pages'),(api,'write'),(api,'reconcile'),
                         (git,'publication_commit'),(git,'push'),(git,'git')):
        method = getattr(target, name)
        original, result = method.side_effect, method.return_value
        def checked(*args, original=original, result=result, **kwargs):
            assert not e.db.in_transaction
            return original(*args, **kwargs) if original else result
        method.side_effect = checked
    p.publish(PROJECT, e.get(attempt))
    assert error(repeated)['delivered'] == 1


@pytest.mark.parametrize('problem', ['base', 'foreign', 'yaml'])
def test_preparation_failure_returns_discovered(env, problem):
    from test_stage04 import adapter, register, issue, git
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    config,project,control,seed,remote = env
    body = 'requirements'
    if problem == 'base': body = '```codex-config\nbase_branch: missing\n```'
    if problem == 'foreign':
        git(seed, 'branch', 'foreign')
        git(seed, 'push', str(remote), 'foreign')
        body = '```codex-config\nbranch_name: foreign\n```'
    if problem == 'yaml': body = '```codex-config\nbranch_name: [\n```'
    api = adapter([issue(project, body=body)])
    d = Dispatcher(config, Lifecycle(), api)
    try:
        register(d, project, api.fetch.return_value)
        d.admit()
        assert not d.active
        assert d.store.get(project.repository, 1)['state'] == 'discovered'
        assert not d.git.paths(project, 1)[2].exists()
        assert not d.store.db.execute('SELECT * FROM terminal_errors').fetchall()
        assert not d.store.db.execute('SELECT * FROM attempts WHERE codex_started=1').fetchall()
        assert not d.store.confirm(project.repository, 1, 1)
    finally:
        d.close()


def test_atomic_terminal_intent_rollback(repeated, monkeypatch):
    from dispatcher.terminal_error import TerminalError
    p,e,attempt,api,git,pull,comments = repeated
    prior = e.db.execute('SELECT * FROM task_pull_requests').fetchone()
    def fail(*args, **kwargs):
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(e.store, '_record_transition', fail)
    with pytest.raises(RuntimeError):
        TerminalError(p).record(e.get(attempt), prior)
    assert e.store.get(PROJECT.repository, 1)['state'] == 'running'
    assert error(repeated) is None
    assert not e.db.in_transaction


def test_terminal_preparation_stops_before_new_codex(env):
    from test_stage04 import adapter, register, finish, issue
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    config,project,*_ = env
    api = adapter([issue(project)])
    d = Dispatcher(config, Lifecycle(), api)
    try:
        register(d, project, [issue(project)])
        d.admit(); finish(d)
        api._pr.update(state='closed', closed_at='2026-10-24T00:15:00Z', merged_at=None)
        api.response_date = '2026-10-24T00:20:00Z'
        assert PullRequestMonitor(d.store, api).poll(project) == []
        assert d.store.decide(project.repository, 1, 1, 'approve', expected_state='rejected') == 'applied'
        api._pr.update(merged=True, merged_at='2026-10-24T00:31:00Z')
        d.admit()
        assert not d.active
        assert d.store.get(project.repository, 1)['state'] == 'review_on_error'
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        CleanupService(config, d.store).tick()
        assert not d.git.paths(project, 1)[1].exists()
    finally:
        d.close()
