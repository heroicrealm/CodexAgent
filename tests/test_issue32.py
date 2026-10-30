"""Same-branch iterations: durable publication and real Git safety boundaries."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_stage602 import store, PROJECT, SHA, move, pr
from test_stage603 import publication, run, operation
from test_stage604 import monitored, poll
from test_stage04 import env, git, issue
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import GitHubError
from dispatcher.pr_publication import PullRequestPublisher
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError


@pytest.fixture
def repeat(monitored, publication):
    store, api, pull, *_ = monitored
    assert poll(monitored) == []
    assert store.decide(PROJECT.repository, 1, 1, 'approve', expected_state='rejected') == 'applied'
    assert store.save_branches(PROJECT.repository, 1, expected_version=1,
        expected_iteration_id=store.get(PROJECT.repository, 1)['current_iteration_id'],
        expected=store.branches(PROJECT.repository, 1), base_branch='main', work_branch='feature',
        branch_source='explicit', branch_status='resolved')
    execution = ExecutionStore(store)
    attempt = execution.reserve(store.get(PROJECT.repository, 1), 'feature', 1,
                                store.guard(PROJECT.repository, 1)['observation_token'])
    execution.update(attempt, worktree='/tmp/worktree', initial_sha='b'*40, origin_url='origin',
                     codex_started=1, exit_code=0)
    manager = Mock()
    manager.publication_commit.return_value = SHA
    manager.git.return_value.stdout = SHA
    api.reset_mock(side_effect=True)
    api.repository_identity.return_value = 11
    api.response_date = '2026-10-24T00:30:00Z'
    pull['head']['sha'] = SHA
    api.request.side_effect = lambda endpoint: deepcopy(pull)
    def reopen(endpoint, method, payload):
        assert endpoint.endswith('/pulls/2') and method == 'PATCH' and payload == {'state': 'open'}
        pull.update(payload)
        return deepcopy(pull)
    api.write.side_effect = reopen
    publisher = PullRequestPublisher(execution, api, manager)
    publisher.intent(attempt)
    return publisher, execution, attempt, api, manager, pull


@pytest.mark.parametrize('state', ['open', 'closed'])
def test_reuses_saved_pr_and_is_idempotent(repeat, state):
    repeat[5]['state'] = state
    run(repeat)
    assert repeat[1].get(repeat[2])['state'] == 'review'
    assert repeat[3].write.call_count == (state == 'closed')
    repeat[0].flush([PROJECT])
    assert repeat[4].push.call_count == 1
    assert repeat[1].db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
    assert repeat[1].db.execute('SELECT count(*) FROM task_iterations').fetchone()[0] == 2


@pytest.mark.parametrize('mutation,diagnostic', [('merged','pr_merged'), ('head','pr_identity_conflict'),
    ('base','pr_identity_conflict'), ('id','pr_identity_conflict'), ('repo','pr_identity_conflict')])
def test_invalid_saved_pr_never_creates_replacement(repeat, mutation, diagnostic):
    pull = repeat[5]
    if mutation == 'merged': pull.update(merged=True)
    if mutation == 'head': pull['head']['ref'] = 'foreign'
    if mutation == 'base': pull['base']['ref'] = 'foreign'
    if mutation == 'id': pull['id'] = 999
    if mutation == 'repo': pull['head']['repo']['id'] = 999
    run(repeat)
    assert repeat[1].get(repeat[2])['state'] == 'running'
    assert repeat[1].get(repeat[2])['recovery_diagnostic'] == diagnostic
    repeat[3].write.assert_not_called()
    repeat[3].pages.assert_not_called()


@pytest.mark.parametrize('lost_response', [False, True])
def test_reopen_failure_or_timeout_recovers_without_execution(repeat, lost_response):
    def write(*args):
        if lost_response:
            repeat[5]['state'] = 'open'
        raise GitHubError()
    repeat[3].write.side_effect = write
    run(repeat)
    if lost_response:
        assert repeat[1].get(repeat[2])['state'] == 'review'
    else:
        row = repeat[1].get(repeat[2])
        assert row['pushed'] == 1 and row['state'] == 'running'
        assert row['recovery_diagnostic'] == 'reopen_failed'
        repeat[5]['state'] = 'open'  # Operator repairs permissions/state.
        execution = repeat[1]
        path = execution.db.execute('PRAGMA database_list').fetchone()[2]
        restored = Store(path)
        try:
            PullRequestPublisher(ExecutionStore(restored), repeat[3], repeat[4]).flush([PROJECT])
            assert restored.get(PROJECT.repository, 1)['state'] == 'review'
            assert restored.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2
        finally:
            restored.close()
    assert repeat[4].push.call_count == 1
    assert all(call.args[1] == 'PATCH' for call in repeat[3].write.call_args_list)


def test_remote_changed_blocks_push_even_when_fast_forward_possible(repeat):
    repeat[4].git.return_value.stdout = 'c'*40
    run(repeat)
    repeat[4].push.assert_not_called()
    repeat[3].write.assert_not_called()
    assert repeat[1].get(repeat[2])['recovery_diagnostic'] == 'remote_head_changed'
    assert repeat[1].store.get(PROJECT.repository, 1)['state'] == 'running'


@pytest.fixture
def git_repeat(env):
    config, project, _, seed, remote = env
    manager = GitWorkspaceManager(config)
    worktree, initial = manager.prepare(project, issue(project), 'feature')
    git(worktree, 'push', 'origin', 'HEAD:refs/heads/feature')
    return manager, project, dict(issue(project), repeat_branch=True, previous_attempt='a'*32), worktree, seed, remote


def test_real_git_repeat_uses_current_remote_and_archives_local(git_repeat):
    manager, project, task, old, seed, remote = git_repeat
    (seed / 'manual').write_text('human work')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Manual work')
    git(seed, 'push', str(remote), 'HEAD:refs/heads/feature')
    new, sha = manager.prepare(project, task, 'feature')
    assert sha == git(seed, 'rev-parse', 'HEAD')
    assert (new / 'manual').read_text() == 'human work'
    archive = new.parent / ('previous-' + 'a'*32)
    assert archive.exists() and not (archive / 'manual').exists()
    assert git(new, 'status', '--porcelain') == ''


@pytest.mark.parametrize('failure,code', [('dirty','retry_workspace_conflict'),
    ('fetch','fetch_failed'), ('missing','remote_branch_missing'), ('occupied','retry_workspace_conflict')])
def test_real_git_repeat_safe_failures(git_repeat, failure, code):
    manager, project, task, old, seed, remote = git_repeat
    if failure == 'dirty': (old / 'unsaved').write_text('preserve me')
    if failure == 'fetch': git(old, 'remote', 'set-url', 'origin', str(remote / 'missing'))
    if failure == 'missing': git(seed, 'push', str(remote), ':refs/heads/feature')
    if failure == 'occupied':
        other = old.parent / 'foreign'
        git(old, 'worktree', 'move', str(old), str(other))
    with pytest.raises(TaskError, match=code):
        manager.prepare(project, task, 'feature')
    if failure == 'dirty': assert (old / 'unsaved').read_text() == 'preserve me'


def test_same_pr_second_rejection_retains_both_resolutions(repeat):
    from dispatcher.pr_monitor import PullRequestMonitor
    run(repeat)
    execution, api, pull = repeat[1], repeat[3], repeat[5]
    pull.update(state='closed', closed_at='2026-10-24T00:35:00Z', merged=False)
    api.response_date = '2026-10-24T00:40:00Z'
    comments = []
    api.pages.side_effect = lambda endpoint: deepcopy(comments) if endpoint.endswith('/comments') else []
    def comment(endpoint, method, payload):
        assert method == 'POST' and endpoint.endswith('/comments')
        comments.append(dict(id=999, **payload))
        return comments[-1]
    api.write.side_effect = comment
    monitor = PullRequestMonitor(execution.store, api)
    assert monitor.poll(PROJECT) == []
    assert execution.store.get(PROJECT.repository, 1)['state'] == 'rejected'
    assert monitor.poll(PROJECT) == []
    assert len(comments) == 1
    rows = execution.db.execute('SELECT * FROM pr_resolutions ORDER BY iteration_id').fetchall()
    assert len(rows) == 2 and rows[0]['iteration_id'] != rows[1]['iteration_id']
    assert rows[0]['comment_id'] != rows[1]['comment_id']


def test_resolution_migration_preserves_data_and_is_atomic(monitored):
    from dispatcher.storage.migrations import upgrade606
    db = monitored[0].db
    assert poll(monitored) == []
    before = [tuple(row) for row in db.execute('SELECT * FROM pr_resolutions')]
    with db:
        db.execute('ALTER TABLE pr_resolutions RENAME TO saved_resolutions')
        db.execute('''CREATE TABLE pr_resolutions(pr_id INTEGER PRIMARY KEY REFERENCES task_pull_requests(pr_id),
            iteration_id INTEGER NOT NULL REFERENCES task_iterations(iteration_id), target TEXT NOT NULL,
            metadata TEXT NOT NULL, issue_closed_at TEXT, comment_id INTEGER, error TEXT)''')
        db.execute('INSERT INTO pr_resolutions SELECT * FROM saved_resolutions')
        db.execute('DROP TABLE saved_resolutions')
        db.execute('DELETE FROM schema_migrations WHERE version=606')
    with pytest.raises(RuntimeError):
        with db:
            db.execute('BEGIN IMMEDIATE')
            upgrade606(db)
            raise RuntimeError('simulated migration failure')
    assert [r[1] for r in db.execute('PRAGMA table_info(pr_resolutions)') if r[5]] == ['pr_id']
    assert not db.execute('SELECT 1 FROM schema_migrations WHERE version=606').fetchone()
    with db:
        db.execute('BEGIN IMMEDIATE')
        upgrade606(db)
    with db:
        db.execute('BEGIN IMMEDIATE')
        upgrade606(db)
    assert [tuple(row) for row in db.execute('SELECT * FROM pr_resolutions')] == before
    assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_timeout_read_after_reopen_resumes_from_github(repeat):
    original = repeat[3].request.side_effect
    reads = 0
    def read(endpoint):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise GitHubError()
        return original(endpoint)
    repeat[3].request.side_effect = read
    run(repeat)
    assert repeat[5]['state'] == 'open'
    assert repeat[1].get(repeat[2])['state'] == 'running'
    PullRequestPublisher(repeat[1], repeat[3], repeat[4]).flush([PROJECT])
    assert repeat[1].get(repeat[2])['state'] == 'review'
    assert repeat[3].write.call_count == 1
    assert repeat[4].push.call_count == 1
    operation = repeat[1].db.execute("SELECT * FROM publication_operations WHERE operation_key LIKE 'reopen:%'").fetchone()
    assert operation['operation_key'] == f'reopen:{repeat[1].get(repeat[2])["iteration_id"]}:301'
    assert operation['outcome'] == 'completed'


def test_lost_push_response_is_reconciled_without_new_codex(repeat):
    repeat[4].push.side_effect = TaskError('git_operation_failed', 'GIT')
    run(repeat)
    assert repeat[1].get(repeat[2])['pushed'] == 0
    repeat[4].push.side_effect = None
    PullRequestPublisher(repeat[1], repeat[3], repeat[4]).flush([PROJECT])
    assert repeat[1].get(repeat[2])['state'] == 'review'
    assert repeat[1].db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2


@pytest.mark.parametrize('failure', ['merged', 'identity', 'dirty', 'fetch', 'missing', 'claim'])
def test_scheduler_blocks_agent_on_unsafe_repeat(env, failure):
    from test_stage04 import adapter, register, finish
    from dispatcher.scheduler import Dispatcher
    from dispatcher.runtime import Lifecycle
    from dispatcher.queue import decide_cli
    from dispatcher.pr_monitor import PullRequestMonitor
    config, project, _, seed, remote = env
    api = adapter([issue(project)])
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        finish(dispatcher)
        first = dispatcher.store.db.execute('SELECT * FROM attempts').fetchone()
        api._pr.update(state='closed', merged_at=None, closed_at='2026-10-24T00:15:00Z')
        api.response_date = '2026-10-24T00:20:00Z'
        assert PullRequestMonitor(dispatcher.store, api).poll(project) == []
        assert decide_cli(dispatcher.store, api, project, 1, 'approve') == ('applied', True)
        if failure == 'merged': api._pr['merged'] = True
        if failure == 'identity': api._pr['id'] = 999
        if failure == 'dirty': (Path(first['worktree']) / 'unsaved').write_text('keep')
        if failure == 'missing': git(seed, 'push', str(remote), ':refs/heads/' + first['branch'])
        if failure == 'fetch':
            original_git = dispatcher.git.git
            def command(cwd, *args, **kwargs):
                if args[0] == 'fetch': raise TaskError('git_operation_failed', 'GIT')
                return original_git(cwd, *args, **kwargs)
            dispatcher.git.git = command
        if failure == 'claim':
            other = issue(project, id=2, number=2)
            dispatcher.store.apply(project, [issue(project), other])
            api.fetch.return_value.append(other)  # Authoritative read still includes the claimant.
            # Another Issue's historical attempt already owns the same branch.
            with dispatcher.store.db:
                dispatcher.store.db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at) VALUES('foreign','owner/repo',2,1,?,'failed','now')", (first['branch'],))
        dispatcher.executor.start = Mock(side_effect=AssertionError('Codex must not start'))
        dispatcher.admit()
        dispatcher.executor.start.assert_not_called()
        assert dispatcher.store.get(project.repository, 1)['state'] != 'review'
        if failure == 'dirty': assert (Path(first['worktree']) / 'unsaved').read_text() == 'keep'
    finally:
        dispatcher.close()
