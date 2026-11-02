"""Integration consumption and manual publication with real Git and SQLite."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from dispatcher.container_monitor import ContainerMonitor
from dispatcher.integration import readiness
from dispatcher.pr_monitor import PullRequestMonitor
from dispatcher.runtime import Lifecycle
from dispatcher.scheduler import Dispatcher
from dispatcher.workspace import TaskError
from test_stage04 import env, issue, adapter, register, finish, git
from test_stage6433 import raw
from test_stage6434 import monitor_setup, status


@pytest.mark.parametrize('run,mode,change', [(True, 'dirty', None), (True, 'noop', None), (False, 'fail', None), (True, 'dirty', 'children'), (True, 'dirty', 'push'), (False, 'fail', 'push'), (True, 'dirty', 'finish'), (False, 'fail', 'finish'), (False, 'fail', 'label')])
def test_container_pipeline(env, run, mode, change):
    config, project, control, seed, remote = env
    control.write_text(json.dumps(dict(exec=mode)))
    item = issue(project, body=f'Integrate only.\n```yaml\ndispatcher: {{issue_type: container, run_codex: {str(run).lower()}}}\n```')
    api = adapter([item])
    original_request, original_write = api.request.side_effect, api.write.side_effect
    def request(endpoint):
        if '/git/ref/' in endpoint:
            return dict(ref='refs/heads/codex/issue-1', object=dict(type='commit', sha=git(remote, 'rev-parse', 'codex/issue-1')))
        if '/issues/' in endpoint:
            return raw(project, item)
        return original_request(endpoint)
    def write(endpoint, method, payload):
        if endpoint.endswith('/git/refs'):
            git(remote, 'update-ref', payload['ref'], payload['sha'])
            return dict(ref=payload['ref'])
        return original_write(endpoint, method, payload)
    api.request.side_effect, api.write.side_effect = request, write
    api.list_direct_children.return_value = []
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [item])
        dispatcher.admit()
        assert dispatcher.store.get(project.repository, 1)['state'] == 'awaiting_children'
        # A child's merged result is represented by a real diff already on head.
        git(seed, 'checkout', '-b', 'codex/issue-1')
        (seed / 'child.txt').write_text('child result')
        git(seed, 'add', '.')
        git(seed, 'commit', '-m', 'Child result')
        git(seed, 'push', str(remote), 'codex/issue-1')
        ContainerMonitor(dispatcher.store, api).poll(project)
        if change == 'push':
            def failed_push(*args, **kwargs):
                raise TaskError('git_operation_failed', 'GIT')
            dispatcher.git.push = failed_push
        if change == 'finish':
            from unittest.mock import Mock
            dispatcher.execution.finish = Mock(side_effect=OSError('simulated storage outage'))
        if change == 'label':
            from dispatcher.github import GitHubError
            api.reconcile.side_effect = GitHubError()
        dispatcher.admit()
        if change == 'children':
            api.list_direct_children.return_value = [raw(project, issue(project, id=2, number=2))]
        finish(dispatcher)
        if change in ('push', 'finish', 'label'):
            saved = dispatcher.store.db.execute('SELECT * FROM attempts').fetchone()
            assert saved['result_sha'] and Path(saved['worktree']).exists()
            assert saved['state'] == ('review' if change == 'label' else 'running')
            dispatcher.admit()
            assert dispatcher.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
            dispatcher.close()
            dispatcher = Dispatcher(config, Lifecycle(), api)
            api.reconcile.side_effect = None
            if change in ('finish', 'label'):
                api.pages.return_value = [api._pr]
            dispatcher.pr_publisher.flush(config.projects)
        task = dispatcher.store.get(project.repository, 1)
        attempt = dispatcher.store.db.execute('SELECT * FROM attempts').fetchone()
        if change == 'children':
            assert task['state'] != 'review'
            assert attempt['result_sha'] == git(remote, 'rev-parse', 'codex/issue-1')
            assert not dispatcher.store.db.execute('SELECT 1 FROM task_pull_requests').fetchone()
            dispatcher.admit()
            assert dispatcher.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
            return
        assert task['state'] == 'review', dict(attempt)
        assert attempt['codex_started'] == int(run)
        assert attempt['category'] == ('CODEX' if run else 'DISPATCHER')
        assert dispatcher.store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
        if run:
            prompt = (Path(attempt['worktree']).parent / (attempt['attempt_id'] + '.prompt')).read_text()
            assert 'Integrate only.' in prompt and 'dispatcher:' not in prompt
            assert 'do not repeat child tasks' in prompt
            assert 'Do not run Git commands' in prompt
        dispatcher.admit()
        assert dispatcher.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    finally:
        dispatcher.close()


def test_semantic_snapshot_and_claim(env):
    store, api, monitor, row, pr, head, _ = monitor_setup(env)
    project = env[1]
    monitor.poll(project)
    with store.db as db:
        db.execute("INSERT INTO container_blockers VALUES(1,'historical',NULL,'old','old',0,'now')")
    old = status(store)['generation']
    api.list_direct_children.return_value[0]['updated_at'] = '2026-10-24T00:00:01Z'
    api.list_direct_children.return_value[0]['labels'].append(dict(name='cosmetic'))
    monitor.poll(project)
    assert status(store)['generation'] == old
    proof = readiness(store, api, project, row)
    from dispatcher.execution_store import ExecutionStore
    execution = ExecutionStore(store)
    attempt = execution.reserve(row, row['work_branch'], 2, store.guard(project.repository, 1)['observation_token'], integration=proof)
    assert attempt
    assert not execution.reserve(row, row['work_branch'], 2, store.guard(project.repository, 1)['observation_token'], integration=proof)
    pr['base']['ref'] = 'wrong'
    from dispatcher.integration import publication_guard
    with pytest.raises(TaskError, match='child_snapshot_stale'):
        publication_guard(store, api, project, store.get(project.repository, 1))
    store.close()


@pytest.mark.parametrize('state', ['open', 'merged', 'closed', 'ambiguous'])
def test_manual_branch_and_discovery(env, state):
    config, project, _, _, remote = env
    item = issue(project, body='Manual task\n```yaml\ndispatcher: {run_codex: false}\n```')
    api = adapter([item])
    original_write = api.write.side_effect
    def write(endpoint, method, payload):
        if endpoint.endswith('/git/refs'):
            git(remote, 'update-ref', payload['ref'], payload['sha'])
            return dict(ref=payload['ref'])
        return original_write(endpoint, method, payload)
    api.write.side_effect = write
    api.request.side_effect = lambda endpoint: raw(project, item)
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [item])
        dispatcher.admit()
        assert dispatcher.store.get(project.repository, 1)['state'] == 'review'
        assert not dispatcher.store.db.execute('SELECT 1 FROM attempts').fetchone()
        assert not any(c.args[0].endswith('/pulls') for c in api.write.call_args_list)
        sha = git(remote, 'rev-parse', 'codex/issue-1')
        pr = dict(id=301, number=9, head=dict(ref='codex/issue-1', sha=sha, repo=dict(id=11)),
            base=dict(ref='main', repo=dict(id=11)), state='open' if state == 'open' else 'closed',
            merged=state == 'merged', merged_at='2026-10-24T00:11:00Z' if state == 'merged' else None,
            closed_at='2026-10-24T00:11:00Z', merge_commit_sha=sha)
        if state == 'merged':
            git(remote, 'update-ref', '-d', 'refs/heads/codex/issue-1')
        api.response_date = '2026-10-24T00:12:00Z'
        api.pages.side_effect = lambda endpoint: ([pr, dict(pr, id=302)] if state == 'ambiguous' else [pr]) if '/pulls?' in endpoint else []
        def request(endpoint):
            if '/pulls/' in endpoint:
                return deepcopy(pr)
            return dict(raw(project, item), state='closed', state_reason='completed', closed_at='2026-10-24T00:12:00Z')
        api.request.side_effect = request
        errors = PullRequestMonitor(dispatcher.store, api).poll(project)
        assert bool(errors) == (state == 'ambiguous')
        assert dispatcher.store.get(project.repository, 1)['state'] == dict(open='review', merged='done', closed='rejected', ambiguous='review')[state]
    finally:
        dispatcher.close()


def test_manual_container_no_diff_and_recovery(env):
    from dispatcher.integration import manual_diff
    store, parent, api, service = __import__('test_stage6433').setup(env)
    project = env[1]
    service.prepare(project, store.get(project.repository, 1))
    row = store.get(project.repository, 1)
    api.pages.return_value = []
    api.request.side_effect = lambda endpoint: dict(ref='refs/heads/feature/container', object=dict(type='commit', sha=git(env[-1], 'rev-parse', 'feature/container'))) if '/git/ref/' in endpoint else raw(project, parent)
    comments = []
    def write(endpoint, method, payload):
        value = dict(id=len(comments) + 1, body=payload['body'])
        comments.append(value)
        return value
    api.write.side_effect = write
    for _ in range(3):
        assert not manual_diff(store, api, service.git, project, row)
    assert len(comments) == 1 and 'no_diff' in comments[0]['body']
    assert store.get(project.repository, 1)['state'] == 'awaiting_children'
    assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    seed, remote = env[-2:]
    git(seed, 'checkout', '-b', 'feature/container')
    (seed / 'real.txt').write_text('real diff')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Real change')
    git(seed, 'push', str(remote), 'feature/container')
    api.request.side_effect = lambda endpoint: dict(ref='refs/heads/feature/container', object=dict(type='commit', sha=git(remote, 'rev-parse', 'feature/container'))) if '/git/ref/' in endpoint else raw(project, parent)
    assert manual_diff(store, api, service.git, project, row)
    assert not store.db.execute('SELECT 1 FROM container_blockers WHERE active=1').fetchone()
    store.close()


def test_prompt_has_only_direct_child_summary(env):
    from dispatcher.codex import CodexExecutor
    from unittest.mock import Mock
    config = env[0]
    task = config.workspace / 'prompt-task'
    task.mkdir()
    lifecycle = Mock()
    executor = CodexExecutor(config, lifecycle)
    executor.checked = True
    child = dict(issue_id=2, number=2, title='Child title', url='https://github.com/owner/repo/issues/2',
                 participating=True, prs=[dict(number=20, merged=True, base='container')], body='PRIVATE CHILD BODY')
    item = issue(env[1], body='Container body\n```yaml\ndispatcher: {issue_type: container}\n```')
    item['integration'] = dict(children=[child])
    executor.start(item, 'container', [], task, task, 'a' * 32)
    prompt = (task / ('a' * 32 + '.prompt')).read_text()
    assert 'https://github.com/owner/repo/pull/20' in prompt
    assert 'Child title' in prompt and 'PRIVATE CHILD BODY' not in prompt
    assert 'dispatcher:' not in prompt and 'Container body' in prompt
    assert '"merged": true' in prompt and '"base": "container"' in prompt


@pytest.mark.parametrize('control', ['approve', 'reject', 'emergency_stop'])
def test_child_control_labels_fail_closed(env, control):
    store, api, monitor, row, _, _, _ = monitor_setup(env)
    monitor.poll(env[1])
    api.list_direct_children.return_value[0]['labels'].append(dict(name=env[1].label(control).name))
    with pytest.raises(TaskError, match='child_snapshot_stale'):
        readiness(store, api, env[1], row)
    monitor.poll(env[1])
    assert not status(store)['ready_for_integration']
    store.close()
