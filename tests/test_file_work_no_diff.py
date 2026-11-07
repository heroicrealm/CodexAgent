import json
from pathlib import Path

import pytest

from dispatcher.error_diagnostics import OperationError
from test_execution_cleanup import environment, start, FakeRunner, gitcmd


@pytest.mark.parametrize('mode', ['stdout', 'created_deleted', 'staged_deleted'])
def test_no_changes_has_no_push_or_false_result(environment, monkeypatch, mode):
    config, store, api, gh, git, groups, state, pub, poll = environment
    runner = FakeRunner(store, change=False)
    executor, eid, _ = start(environment, runner=runner)
    original_run = runner.run
    def run(path, prompt):
        if mode != 'stdout':
            (path / 'temporary.java').write_text('temporary')
            if mode == 'staged_deleted':
                gitcmd(path, 'add', '.')
            (path / 'temporary.java').unlink()
        code, _ = original_run(path, prompt)
        return code, 'ghp_SYNTHETIC_PRIVATE_STDOUT'
    runner.run = run
    def forbidden(*args):
        pytest.fail('No push or salvage for empty result')
    monkeypatch.setattr(git, 'commit_push', forbidden)
    executor.execute(eid)
    execution = store.execution(eid)
    assert execution['error_code'] == 'no_changes_for_pr'
    assert execution['result_commit'] is None and not execution['push_completed']
    assert store.issue(1)['state'] == 'error' and not api.prs
    assert not gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'codex/task-0001')
    body = store.issue(1)['output_body']
    assert 'Result commit' not in body and 'Push confirmed: False' in body
    errors = store.rows("SELECT * FROM error_log WHERE error_code='no_changes_for_pr'")
    diagnostic = json.loads(errors[0]['error_message'])
    result_file = config.workspace / 'results' / f'{eid}.txt'
    assert diagnostic['context']['result_file'] == str(result_file)
    assert result_file.read_text() == 'ghp_SYNTHETIC_PRIVATE_STDOUT'
    assert 'ghp_SYNTHETIC_PRIVATE_STDOUT' not in str(api.comments)
    assert 'Operation: git.diff' in str(api.comments)
    assert 'Codex completed successfully but made no file changes' in str(api.comments)
    before = len(api.calls)
    executor.recover()
    pub.reconcile(config.projects[0])
    pub.phase_output(1)
    assert len(api.calls) == before and runner.calls == 1
    with pytest.raises(ValueError, match='consumed'):
        executor.run_once(eid)


def test_prompt_preserves_snapshot_and_user_priority(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    snapshot = dict(store.issue(1))
    original_run = runner.run
    def run(path, prompt):
        assert 'Work directly in the provided project working directory' in prompt
        assert 'Explicit user instructions' in prompt
        assert 'do not invent an arbitrary filename or language' in prompt
        assert 'only explanation' in prompt and 'Do not run git commit' in prompt
        assert prompt.endswith(snapshot['approved_title'] + '\n\n' + snapshot['approved_body'] + '\n\n' + snapshot['approved_input'])
        return original_run(path, prompt)
    runner.run = run
    executor.execute(eid)
    for key in ('approved_title', 'approved_body', 'approved_input'):
        assert store.issue(1)[key] == snapshot[key]


@pytest.mark.parametrize('mode', ['unstaged', 'staged', 'untracked', 'committed'])
def test_file_changes_publish(environment, mode):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, runner=FakeRunner(store, change=False))
    executor.prepare(eid)
    path = Path(store.execution(eid)['worktree_path'])
    target = path / ('new.java' if mode == 'untracked' else 'base.txt')
    target.write_text('changed')
    if mode in ('staged', 'committed'):
        gitcmd(path, 'add', '.')
    if mode == 'committed':
        gitcmd(path, 'commit', '-m', 'local result')
    executor.run_once(eid)
    pub.publish(eid)
    execution = store.execution(eid)
    assert execution['result_commit'] != execution['initial_commit']
    assert execution['push_completed'] and store.issue(1)['state'] == 'review'
    assert len(api.prs) == 1


def test_updated_base_empty_diff_preserves_confirmed_result(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    executor.execute(eid)
    execution = store.execution(eid)
    sha = execution['result_commit']
    path = Path(execution['worktree_path'])
    gitcmd(path, 'push', 'origin', 'HEAD:refs/heads/main')
    def forbidden(*args):
        pytest.fail('No redundant push')
    monkeypatch.setattr(git, 'commit_push', forbidden)
    pub.publish(eid)  # confirmed result and existing PR still reconcile
    api.prs.clear()
    # A subsequent failed comparison must preserve the last confirmed result.
    exc = OperationError('empty', code='no_changes_for_pr', operation='git.diff')
    pub.fail(eid, exc=exc, stage='publication')
    assert store.execution(eid)['result_commit'] == sha
    assert store.execution(eid)['push_completed']
    assert f'/commit/{sha}' in store.issue(1)['output_body']


def test_git_comparison_error_is_not_empty_diff(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, runner=FakeRunner(store, change=False))
    original_run = git.run
    def run(path, *args, **kwargs):
        if args[0] == 'diff':
            import subprocess
            return subprocess.CompletedProcess([], 128, '', 'synthetic git failure')
        return original_run(path, *args, **kwargs)
    monkeypatch.setattr(git, 'run', run)
    executor.execute(eid)
    assert store.execution(eid)['error_code'] == 'git_diff_failed'
    assert not store.execution(eid)['push_completed']
    assert not store.rows("SELECT * FROM error_log WHERE error_code='no_changes_for_pr'")


@pytest.mark.parametrize('committed', [False, True])
def test_salvage_only_real_unpublished_work(environment, committed):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, runner=FakeRunner(store, change=False))
    executor.prepare(eid)
    path = Path(store.execution(eid)['worktree_path'])
    if committed:
        (path / 'saved.txt').write_text('save')
        gitcmd(path, 'add', '.')
        gitcmd(path, 'commit', '-m', 'unpublished')
    runner.code = 1
    with pytest.raises(OperationError):
        executor.run_once(eid)
    pub.fail(eid, stage='codex')
    assert bool(store.execution(eid)['push_completed']) == committed
    assert bool(store.execution(eid)['result_commit']) == committed


@pytest.mark.parametrize('failed', [False, True])
def test_correction_inherits_group_diff_without_false_salvage(environment, failed):
    from dispatcher.execution import Executor, FILE_WORK_INSTRUCTION
    from dispatcher.models import State
    from dispatcher.services import Scheduler
    from test_services import approve, issue
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    executor.execute(eid)
    sha = store.execution(eid)['result_commit']
    next(iter(api.prs.values()))['state'] = 'closed'
    poll.poll_project(config.projects[0])
    state.transition(1, State.REJECTED, State.NEED_FIX, 'permission')
    api.issues[2] = issue(2)
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    second = Scheduler(store, groups, state).reserve(2).execution_id
    class CorrectionRunner(FakeRunner):
        def run(self, path, prompt):
            self.calls += 1
            assert FILE_WORK_INSTRUCTION in prompt and 'Body 2' in prompt
            assert (path / 'result.txt').exists()
            return int(failed), 'No additional edits'
    correction = CorrectionRunner(store, change=False)
    Executor(config, store, git, state, pub, correction).execute(second)
    execution = store.execution(second)
    if failed:
        assert not execution['push_completed'] and execution['result_commit'] is None
        assert store.execution(eid)['result_commit'] == sha
        assert len(api.prs) == 1
    else:
        assert store.issue(2)['state'] == 'review' and len(api.prs) == 2
        assert execution['result_commit'] == sha and execution['push_completed']
    assert correction.calls == 1


@pytest.mark.parametrize('base_only', [False, True])
def test_base_updated_before_first_publish_is_no_changes(environment, base_only):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, runner=FakeRunner(store, change=not base_only))
    executor.prepare(eid)
    executor.run_once(eid)
    path = Path(store.execution(eid)['worktree_path'])
    checkout = config.projects[0].checkout
    (checkout / ('base-only.txt' if base_only else 'result.txt')).write_text('result\n')
    gitcmd(checkout, 'add', '.')
    gitcmd(checkout, 'commit', '-m', 'base caught up')
    gitcmd(checkout, 'push', 'origin', 'main')
    before = len(api.calls)
    with pytest.raises(OperationError) as error:
        pub.publish(eid)
    assert error.value.code == 'no_changes_for_pr'
    assert len(api.calls) == before  # comparison never queries GitHub
    pub.fail(eid, exc=error.value, stage='publication')
    assert not store.execution(eid)['push_completed']
    assert store.execution(eid)['result_commit'] is None
    if not base_only:
        assert (path / 'result.txt').read_text() == 'result\n'
