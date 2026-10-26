"""PR publication contracts with REST fixtures and a real temporary SQLite."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_stage602 import store, running, PROJECT, SHA
from test_stage04 import env
from dispatcher.github import GitHubError
from dispatcher.pr_publication import PullRequestPublisher
from dispatcher.workspace import TaskError


@pytest.fixture
def publication(store):
    execution, attempt = running(store)
    execution.update(attempt, worktree='/tmp/worktree', initial_sha='b'*40, origin_url='origin')
    api, git = Mock(), Mock()
    api.control_issue.return_value = dict(id=101, number=1, labels=[dict(name=PROJECT.task_label)])
    api.repository_identity.return_value = 11
    api.response_date = '2026-10-24T00:10:00Z'
    git.publication_commit.return_value = SHA
    git.git.return_value.stdout = SHA + '\trefs/heads/feature\n'
    marker = f'<!-- codex-pr:11:101:{execution.get(attempt)["iteration_id"]} -->'
    pr = dict(id=301, number=2, state='open', draft=False, merged=False, changed_files=1, body=marker,
              head=dict(ref='feature', sha=SHA, repo=dict(id=11)), base=dict(ref='main', repo=dict(id=11)))
    api.pages.return_value = []
    api.write.return_value = pr
    api.request.return_value = pr
    publisher = PullRequestPublisher(execution, api, git)
    publisher.intent(attempt)
    return publisher, execution, attempt, api, git, pr


def run(fixture):
    publisher, execution, attempt, *_ = fixture
    publisher.publish(PROJECT, execution.get(attempt))


def operation(fixture):
    return fixture[1].db.execute("SELECT * FROM publication_operations WHERE operation_key LIKE 'pr:%'").fetchone()


def test_happy_path_and_repeated_poll(publication):
    run(publication)
    publisher, execution, attempt, api, git, pr = publication
    assert execution.get(attempt)['state'] == 'review'
    assert operation(publication)['outcome'] == 'completed'
    assert execution.db.execute('SELECT pr_id FROM task_pull_requests').fetchone()[0] == 301
    assert execution.db.execute('SELECT count(*) FROM iteration_pr_history').fetchone()[0] == 1
    publisher.flush([PROJECT])
    assert api.write.call_count == 1
    assert api.reconcile.call_count == 1
    assert git.push.call_count == 1


def test_existing_pr(publication):
    publication[3].pages.return_value = [publication[5]]
    run(publication)
    assert publication[1].get(publication[2])['state'] == 'review'
    publication[3].write.assert_not_called()


@pytest.mark.parametrize('mutation', ['foreign', 'closed', 'head', 'base', 'repo', 'sha', 'empty', 'id', 'draft'])
def test_unacceptable_pr(publication, mutation):
    pr = deepcopy(publication[5])
    if mutation == 'foreign': pr['body'] = 'unrelated'
    if mutation == 'closed': pr['state'] = 'closed'
    if mutation == 'head': pr['head']['ref'] = 'other'
    if mutation == 'base': pr['base']['ref'] = 'other'
    if mutation == 'repo': pr['head']['repo']['id'] = 12
    if mutation == 'sha': pr['head']['sha'] = 'c'*40
    if mutation == 'empty': pr['changed_files'] = 0
    if mutation == 'id': pr['id'] = 'node-id'
    if mutation == 'draft': pr['draft'] = True
    publication[3].pages.return_value = [pr]
    publication[3].request.return_value = pr
    run(publication)
    assert publication[1].get(publication[2])['state'] == 'failed'
    assert operation(publication)['phase'] == 'terminal'
    publication[3].write.assert_not_called()
    assert not publication[1].db.execute('SELECT * FROM task_pull_requests').fetchall()


def test_no_diff_never_pushes_or_retries_codex(publication):
    publication[4].publication_commit.side_effect = TaskError('no_diff', 'GIT')
    run(publication)
    publisher, execution, attempt, api, git, _ = publication
    assert execution.get(attempt)['error_code'] == 'no_diff'
    publisher.flush([PROJECT])
    git.push.assert_not_called()
    api.write.assert_not_called()
    assert execution.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['push', 'remote', 'create', 'read', 'label'])
def test_failure_resumes_without_agent(publication, failure):
    publisher, execution, attempt, api, git, pr = publication
    if failure == 'push': git.push.side_effect = TaskError('git_operation_failed', 'GIT')
    if failure == 'remote': git.git.return_value.stdout = 'c'*40
    if failure == 'create': api.write.side_effect = GitHubError()
    if failure == 'read': api.request.side_effect = GitHubError()
    if failure == 'label': api.reconcile.side_effect = GitHubError()
    run(publication)
    assert operation(publication)['outcome'] == 'failed'
    assert execution.get(attempt)['state'] == ('review' if failure == 'label' else 'running')
    git.push.side_effect = api.write.side_effect = api.request.side_effect = api.reconcile.side_effect = None
    git.git.return_value.stdout = SHA
    api.pages.return_value = [pr] if failure in ('read', 'label') else []
    # A fresh publisher reconstructs progress exclusively from SQLite/GitHub.
    PullRequestPublisher(execution, api, git).flush([PROJECT])
    assert execution.get(attempt)['state'] == 'review'
    assert operation(publication)['outcome'] == 'completed'
    assert execution.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    assert execution.db.execute('SELECT count(*) FROM iteration_pr_history').fetchone()[0] == 1


def test_lost_create_response(publication):
    api = publication[3]
    api.pages.side_effect = [[], [], [publication[5]]]
    api.write.side_effect = GitHubError()
    run(publication)
    assert publication[1].get(publication[2])['state'] == 'review'
    assert api.write.call_count == 1


def test_unresolved_ids_remain_recoverable(publication):
    execution = publication[1]
    with execution.db:
        execution.db.execute('UPDATE tasks SET repository_id=NULL')
    run(publication)
    publication[4].push.assert_not_called()
    assert execution.get(publication[2])['state'] == 'running'
    assert operation(publication)['outcome'] == 'failed'


def test_review_bypass_forbidden(publication):
    with pytest.raises(RuntimeError, match='verified PR'):
        publication[1].finish(publication[2], 'review')
    assert publication[1].get(publication[2])['state'] == 'running'


def test_partial_transition_rolls_back_then_reconciles(publication):
    execution, api = publication[1], publication[3]
    execution.db.execute("CREATE TRIGGER fail_pr BEFORE INSERT ON iteration_pr_history BEGIN SELECT RAISE(ABORT,'test'); END")
    run(publication)
    assert not execution.db.execute('SELECT * FROM task_pull_requests').fetchall()
    assert execution.get(publication[2])['state'] == 'running'
    execution.db.execute('DROP TRIGGER fail_pr')
    api.pages.return_value = [publication[5]]
    run(publication)
    assert execution.get(publication[2])['state'] == 'review'
    assert api.write.call_count == 1


def test_concurrent_publishers_do_not_duplicate(publication):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from dispatcher.storage import Store
    from dispatcher.execution_store import ExecutionStore
    publisher, execution, attempt, api, git, _ = publication
    entered, release = Event(), Event()
    path = execution.db.execute('PRAGMA database_list').fetchone()[2]
    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
    git.push.side_effect = pause
    def worker():
        connection = Store(path)
        try:
            other = PullRequestPublisher(ExecutionStore(connection), api, git)
            other.flush([PROJECT])
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        publisher.flush([PROJECT])
        # Main connection can write while the worker is in a network operation.
        execution.update(attempt, recovery_diagnostic='concurrent_check')
        release.set()
        future.result(timeout=5)
    assert api.write.call_count == 1
    assert execution.get(attempt)['state'] == 'review'


def test_restart_after_transition_before_finish(publication, monkeypatch):
    publisher, execution, attempt, api, git, pr = publication
    finish = execution.finish
    monkeypatch.setattr(execution, 'finish', Mock(side_effect=OSError()))
    run(publication)
    assert execution.get(attempt)['state'] == 'running'
    assert execution.store.get(PROJECT.repository, 1)['state'] == 'review'
    monkeypatch.setattr(execution, 'finish', finish)
    api.pages.return_value = [pr]
    publisher.flush([PROJECT])
    assert execution.get(attempt)['state'] == 'review'
    assert execution.db.execute('SELECT count(*) FROM iteration_pr_history').fetchone()[0] == 1
    assert api.write.call_count == 1


def test_published_sha_must_be_confirmed(publication):
    publication[4].git.return_value.stdout = 'c'*40
    run(publication)
    publication[3].write.assert_not_called()
    assert publication[1].get(publication[2])['state'] == 'running'


@pytest.mark.parametrize('mode', ['dirty', 'committed'])
def test_git_commits_and_configured_base(env, mode):
    from dataclasses import replace
    from test_stage04 import git, issue
    from dispatcher.workspace import GitWorkspaceManager
    config, project, _, seed, remote = env
    project = replace(project, base_branch='release')
    git(seed, 'branch', 'release')
    git(seed, 'push', str(remote), 'release')
    manager = GitWorkspaceManager(config)
    worktree, initial = manager.prepare(project, issue(project), 'feature')
    (worktree / 'change').write_text('task implementation')
    if mode == 'committed':
        git(worktree, 'add', '.')
        git(worktree, 'commit', '-m', 'Agent commit')
        # Existing branch commits still count even if Codex added no new commit.
        initial = git(worktree, 'rev-parse', 'HEAD')
    sha = manager.publication_commit(worktree, 'feature', initial, project.base_branch)
    assert git(worktree, 'status', '--porcelain') == ''
    assert sha == git(worktree, 'rev-parse', 'HEAD')
    assert git(worktree, 'rev-list', '--count', 'refs/remotes/origin/release..HEAD') == '1'


def test_scheduler_restart_preserves_publication(env):
    from test_stage04 import adapter, issue, register, finish
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    config, project, *_ = env
    api = adapter([issue(project)])
    api.request.side_effect = GitHubError()
    dispatcher = Dispatcher(config, Lifecycle(), api)
    register(dispatcher, project, [issue(project)])
    dispatcher.admit()
    finish(dispatcher)
    row = dispatcher.store.db.execute('SELECT * FROM attempts').fetchone()
    assert row['state'] == 'running' and row['pushed'] == 1
    dispatcher.close()
    restarted = Dispatcher(config, Lifecycle(), api)
    try:
        assert restarted.execution.get(row['attempt_id'])['state'] == 'running'
        api.pages.return_value = [api._pr]
        api.request.side_effect = None
        api.request.return_value = api._pr
        api._pr['head']['sha'] = row['result_sha']
        restarted.pr_publisher.flush([project])
        assert restarted.execution.get(row['attempt_id'])['state'] == 'review'
        assert restarted.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert api.write.call_count == 1
    finally:
        restarted.close()


def test_same_issue_number_in_two_projects(publication):
    from dataclasses import replace
    from test_stage602 import item
    publisher, execution, attempt, api, git, first_pr = publication
    other = replace(PROJECT, id='two', repository='other/repo')
    execution.store.apply(other, [item(id=102, repository_id=12)])
    assert execution.store.confirm(other.repository, 1, 1)
    row = execution.store.get(other.repository, 1)
    second = execution.reserve(row, 'feature', 2, execution.store.guard(other.repository, 1)['observation_token'])
    execution.update(second, worktree='/tmp/second', initial_sha='b'*40, origin_url='origin')
    publisher.intent(second)
    second_pr = deepcopy(first_pr)
    second_pr.update(id=302, body=f'<!-- codex-pr:12:102:{execution.get(second)["iteration_id"]} -->')
    second_pr['head']['repo']['id'] = second_pr['base']['repo']['id'] = 12
    api.repository_identity.side_effect = lambda p: 11 if p.repository == PROJECT.repository else 12
    api.write.side_effect = lambda endpoint, *args: first_pr if endpoint.startswith('repos/owner/') else second_pr
    api.request.side_effect = lambda endpoint: first_pr if endpoint.startswith('repos/owner/') else second_pr
    api.control_issue.side_effect = lambda project, number: dict(id=101 if project.repository == PROJECT.repository else 102, number=number, labels=[dict(name=project.task_label)])
    publisher.flush([PROJECT, other])
    assert execution.get(attempt)['state'] == execution.get(second)['state'] == 'review'
    assert {(r['issue_id'], r['repository_id'], r['pr_id']) for r in execution.db.execute('SELECT * FROM task_pull_requests')} == {(101, 11, 301), (102, 12, 302)}


def test_no_network_inside_sqlite_transaction(publication):
    publisher, execution, attempt, api, git, pr = publication
    def checked(value):
        def call(*args, **kwargs):
            assert not execution.db.in_transaction
            return value
        return call
    api.pages.side_effect = checked([])
    api.write.side_effect = checked(pr)
    api.request.side_effect = checked(pr)
    git.push.side_effect = checked(None)
    run(publication)
    assert execution.get(attempt)['state'] == 'review'


def test_pr_search_pagination_preserves_filters():
    from dispatcher.github import Adapter
    api = Adapter()
    api.request = Mock(side_effect=[[dict(number=n) for n in range(100)], []])
    endpoint = 'repos/owner/repo/pulls?state=all&head=owner%3Afeature&base=release'
    assert len(api.pages(endpoint)) == 100
    assert api.request.call_args_list[0].args[0] == endpoint + '&per_page=100&page=1'
    assert api.request.call_args_list[1].args[0] == endpoint + '&per_page=100&page=2'
