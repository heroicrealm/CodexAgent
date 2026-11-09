from dataclasses import replace
from pathlib import Path
import subprocess
import threading
import time
import pytest
from dispatcher.cleanup import Cleanup
from dispatcher.config import Config, Project
from dispatcher.diagnostics import DiagnosticLogger
from dispatcher.execution import Executor, ProcessRunner, Publisher
from dispatcher.git import Git
from dispatcher.github import GitHubAdapter, GitHubError
from dispatcher.locking import AlreadyRunning, InstanceLock
from dispatcher.models import State
from dispatcher.runtime import Runtime
from dispatcher.services import GroupService, PollService, Scheduler, StateService
from dispatcher.storage import Storage
from test_services import FakeAPI, approve, issue


def gitcmd(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def environment(tmp_path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], capture_output=True, check=True)
    checkout = tmp_path / 'V2 checkout with spaces'
    subprocess.run(['git', 'clone', str(remote), str(checkout)], capture_output=True, check=True)
    gitcmd(checkout, 'config', 'user.email', 'v2-test@example.invalid')
    gitcmd(checkout, 'config', 'user.name', 'V2 Test')
    gitcmd(checkout, 'checkout', '-b', 'main')
    (checkout / 'base.txt').write_text('base\n')
    gitcmd(checkout, 'add', '.')
    gitcmd(checkout, 'commit', '-m', 'initial')
    gitcmd(checkout, 'push', '-u', 'origin', 'main')
    config = Config(tmp_path / 'V2 workspace with spaces', tmp_path / 'db',
                    (Project('p', 'owner/new', checkout=checkout),), poll_interval=0.01)
    from dispatcher.db_guard import maintain
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    api = FakeAPI()
    gh = GitHubAdapter(config, DiagnosticLogger(store), api, retries=0)
    git = Git(config)
    groups = GroupService(store)
    state = StateService(store, groups, config)
    publisher = Publisher(config, store, gh, git, state)
    poll = PollService(config, store, gh, state, publisher, gh.logger)
    return config, store, api, gh, git, groups, state, publisher, poll


class FakeRunner:
    def __init__(self, store, *, code=0, change=True):
        self.store, self.code, self.change = store, code, change
        self.cancelled = threading.Event()
        self.calls = 0
        self.pass_fds = ()

    def run(self, path, prompt):
        self.calls += 1
        assert self.store.rows('SELECT * FROM executions WHERE codex_started_at IS NOT NULL')
        assert 'Body 1' in prompt
        if self.change:
            (path / 'result.txt').write_text('result\n')
        return self.code, 'Agent final result'

    def stop(self):
        self.cancelled.set()


def start(env, yaml='dispatcher: {}\n', runner=None):
    config, store, api, gh, git, groups, state, pub, poll = env
    approve(api, yaml=yaml)
    poll.poll_project(config.projects[0])
    execution = Scheduler(store, groups, state).reserve(1)
    assert execution
    runner = runner or FakeRunner(store)
    executor = Executor(config, store, git, state, pub, runner)
    return executor, execution.execution_id, runner


def test_execution_publication_and_no_second_run(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'review'
    assert runner.calls == 1
    execution = store.execution(execution_id)
    assert execution['codex_started_at'] and execution['push_completed'] and execution['pr_published']
    assert 'Agent final result' in store.issue(1)['output_body']
    assert gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'codex/task-0001')
    with pytest.raises(ValueError):
        executor.run_once(execution_id)
    assert len(api.prs) == 1
    pub.publish(execution_id)
    assert len(api.prs) == 1 and runner.calls == 1
    Cleanup(config, store, git).known()
    assert not Path(execution['worktree_path']).exists()


def test_manual_has_no_codex_worktree_or_running_label(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment, 'dispatcher:\n  run_codex: false\n')
    executor.execute(execution_id)
    assert runner.calls == 0
    assert store.issue(1)['state'] == 'manual-running'
    assert store.execution(execution_id)['worktree_path'] is None
    names = {v['name'] for v in api.issues[1]['labels']}
    assert 'dispatcher:manual-running' in names and 'dispatcher:running' not in names
    assert gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'manual/task-0001')
    api.prs[600] = dict(id=600, number=600, state='open', merged_at=None,
                       head={'ref': 'manual/task-0001', 'repo': {'full_name': 'owner/new'}}, base={'ref': 'main'})
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'review'


def test_uncertain_pr_creation_reconciles_without_second_codex(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    original = gh.transport
    failed = False
    def transport(method, endpoint, payload):
        nonlocal failed
        value = original(method, endpoint, payload)
        if method == 'POST' and endpoint.endswith('/pulls') and not failed:
            failed = True
            raise GitHubError(503)  # server accepted, client did not receive response
        return value
    gh.transport = transport
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'error'
    assert runner.calls == 1 and len(api.prs) == 1
    pub.publish(execution_id)
    assert store.issue(1)['state'] == 'review'
    assert len(api.prs) == 1 and runner.calls == 1


def test_no_diff_is_error_and_report_sent_once(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    runner = FakeRunner(store, change=False)
    executor, execution_id, _ = start(environment, runner=runner)
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'error'
    assert not api.prs
    assert store.issue(1)['error_report_sent'] == 1
    count = len(api.comments[1])
    pub.send_final_error_report(1)
    assert len(api.comments[1]) == count


def test_error_report_retry_and_no_monitoring(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    api.fail = lambda method, endpoint, payload: method == 'POST' and payload and 'ERROR:v2' in payload.get('body', '')
    runner.code = 1
    executor.execute(execution_id)
    assert store.issue(1)['error_report_sent'] == 0
    api.fail = None
    assert pub.send_final_error_report(1)
    api.comments[1] = [c for c in api.comments[1] if 'ERROR:v2' not in c['body']]
    before = len(api.calls)
    assert not pub.send_final_error_report(1)
    assert len(api.calls) == before


def test_recovery_after_marker_never_repeats_codex(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    executor.prepare(execution_id)
    store.mark_codex_started(execution_id)
    executor.recover()
    assert runner.calls == 0
    assert store.issue(1)['state'] == 'error'
    assert store.execution(execution_id)['codex_started_at']
    with pytest.raises(ValueError):
        executor.run_once(execution_id)


def test_recovery_existing_pr_restores_review_without_execution(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    executor.prepare(execution_id)
    store.mark_codex_started(execution_id)
    api.prs[600] = dict(id=600, number=600, state='open', merged_at=None,
                       head={'ref': 'codex/task-0001', 'repo': {'full_name': 'owner/new'}}, base={'ref': 'main'})
    executor.recover()
    assert store.issue(1)['state'] == 'review'
    assert runner.calls == 0
    assert len(store.rows('SELECT * FROM pull_requests')) == 1


def test_recovery_dirty_result_is_retained_on_push_failure(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    executor.prepare(execution_id)
    path = Path(store.execution(execution_id)['worktree_path'])
    (path / 'unpublished.txt').write_text('only copy')
    def failed(*_):
        raise RuntimeError('simulated push failure')
    monkeypatch.setattr(git, 'commit_push', failed)
    executor.recover()
    Cleanup(config, store, git).known()
    assert (path / 'unpublished.txt').read_text() == 'only copy'
    assert store.issue(1)['state'] == 'error'


def test_orphan_dry_run_apply_and_registered_protection(environment):
    config, store, api, gh, git, *_ = environment
    project = config.projects[0]
    path, _ = git.prepare(project, 900, 'main', 'unused')
    cleanup = Cleanup(config, store, git)
    before = gitcmd(project.checkout, 'show-ref')
    dry = cleanup.orphans()
    assert next(r for r in dry if r.path == str(path)).safe
    assert path.exists()
    assert gitcmd(project.checkout, 'show-ref') == before
    applied = cleanup.orphans(apply=True)
    assert next(r for r in applied if r.path == str(path)).removed
    assert not path.exists()
    assert not any(r.removed for r in cleanup.orphans(apply=True))
    executor, execution_id, _ = start(environment)
    executor.prepare(execution_id)
    registered = Path(store.execution(execution_id)['worktree_path'])
    result = next(r for r in cleanup.orphans(apply=True) if r.path == str(registered))
    assert not result.removed and result.reason == 'referenced-by-sqlite'


@pytest.mark.parametrize('kind', ['dirty', 'unpublished', 'foreign', 'outside'])
def test_unsafe_orphans_never_removed(environment, kind):
    config, store, api, gh, git, *_ = environment
    project = config.projects[0]
    if kind in ('foreign', 'outside'):
        path = git.area(project) / 'user-worktree' if kind == 'foreign' else config.workspace / 'user-worktree'
        path.parent.mkdir(parents=True, exist_ok=True)
        git.run(project.checkout, 'worktree', 'add', '--detach', str(path), 'main')
    else:
        path, _ = git.prepare(project, 900, 'main', 'unused')
        (path / 'private.txt').write_text('private')
        if kind == 'unpublished':
            git.run(path, 'add', '.')
            git.run(path, 'commit', '-m', 'unpublished')
    results = Cleanup(config, store, git).orphans(apply=True)
    assert not next(r for r in results if r.path == str(path)).removed
    assert path.exists()


def test_cleanup_locks_and_invalid_database(environment):
    config, store, api, gh, git, *_ = environment
    cleanup = Cleanup(config, store, git)
    with InstanceLock(config):
        with pytest.raises(AlreadyRunning):
            cleanup.orphans(apply=True)
    with store.connection() as db:
        db.execute('UPDATE schema_migrations SET version=99')
    with pytest.raises(Exception):
        cleanup.orphans(apply=True)


def test_runtime_polls_while_worker_runs_and_graceful_stop(environment):
    config, store, api, gh, git, *_ = environment
    approve(api)
    entered = threading.Event()
    release = threading.Event()
    class SlowRunner(FakeRunner):
        def run(self, path, prompt):
            entered.set()
            assert release.wait(5)
            return super().run(path, prompt)
    runner = SlowRunner(store)
    runtime = Runtime(config, github=gh, git=git, runner=runner)
    def on_poll():
        if entered.is_set():
            runtime.stop()
            release.set()
    api.on_poll = on_poll
    runtime.run()
    assert api.polls >= 2 and runner.calls == 1
    assert store.issue(1)['state'] == 'review'


def test_process_runner_force_stop(tmp_path):
    import sys
    runner = ProcessRunner((sys.executable, '-c', 'import time; time.sleep(60)'))
    result = []
    worker = threading.Thread(target=lambda: result.append(runner.run(tmp_path, '')))
    worker.start()
    deadline = time.monotonic() + 3
    while runner._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.stop()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result[0][0] != 0


def test_non_fast_forward_retains_unpublished_result(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    class RacingRunner(FakeRunner):
        def run(self, path, prompt):
            checkout = config.projects[0].checkout
            (checkout / 'competitor.txt').write_text('competing user result')
            gitcmd(checkout, 'add', '.')
            gitcmd(checkout, 'commit', '-m', 'competitor')
            gitcmd(checkout, 'push', 'origin', 'HEAD:refs/heads/codex/task-0001')
            return super().run(path, prompt)
    runner = RacingRunner(store)
    executor, execution_id, _ = start(environment, runner=runner)
    executor.execute(execution_id)
    execution = store.execution(execution_id)
    assert store.issue(1)['state'] == 'error'
    assert not execution['push_completed']
    path = Path(execution['worktree_path'])
    Cleanup(config, store, git).known()
    assert (path / 'result.txt').read_text() == 'result\n'
    assert not api.prs


def test_group_cancellation_deletes_only_published_managed_branch(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, _ = start(environment)
    executor.execute(execution_id)
    pr = next(iter(api.prs.values()))
    pr['state'] = 'closed'
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'rejected'
    state.transition(1, State.REJECTED, State.CANCELLED, 'cancel')
    Cleanup(config, store, git).known()
    assert not gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'codex/task-0001')
    assert gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'main')


def test_failed_collision_never_deletes_user_branch(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    gitcmd(config.projects[0].checkout, 'push', 'origin', 'main:refs/heads/existing-user-branch')
    executor, execution_id, runner = start(environment, 'dispatcher:\n  branch: existing-user-branch\n')
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'error'
    assert runner.calls == 0
    state.transition(1, State.ERROR, State.CANCELLED, 'cancel')
    Cleanup(config, store, git).known()
    assert gitcmd(config.projects[0].checkout, 'ls-remote', '--heads', 'origin', 'existing-user-branch')


def test_symlink_escape_and_ignored_content_retained(environment):
    config, store, api, gh, git, *_ = environment
    project = config.projects[0]
    path, _ = git.prepare(project, 900, 'main', 'unused')
    # Exclude via per-worktree info/exclude in the common Git dir.
    common = Path(gitcmd(project.checkout, 'rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = project.checkout / common
    (common / 'info' / 'exclude').write_text('ignored-secret\n')
    (path / 'ignored-secret').write_text('only local copy')
    assert not git.safe_worktree(project, path)[0]
    alias = git.area(project) / 'e-901-00000000000000000000000000000000'
    alias.symlink_to(project.checkout, target_is_directory=True)
    assert not git.safe_worktree(project, alias)[0]
    Cleanup(config, store, git).orphans(apply=True)
    assert path.exists() and alias.exists()


def test_cleanup_failure_does_not_change_terminal_state(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, _ = start(environment)
    executor.execute(execution_id)
    pr = next(iter(api.prs.values()))
    pr['state'] = 'closed'
    pr['merged_at'] = '2026-11-07T10:00:00+00:00'
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'done'
    monkeypatch.setattr(git, 'remove_worktree', lambda *_: (_ for _ in ()).throw(PermissionError()))
    Cleanup(config, store, git).known()
    assert store.issue(1)['state'] == 'done'
    assert store.rows("SELECT * FROM error_log WHERE error_code='cleanup_failed'")
    assert Path(store.execution(execution_id)['worktree_path']).exists()


def test_inherited_lock_blocks_recovery_after_coordinator_crash(tmp_path):
    """Real processes, without Codex or network: the supervisor owns the lease."""
    import os
    import sys
    config = Config(tmp_path / 'work', tmp_path / 'db', ())
    script = '''
import sys,threading,time
from pathlib import Path
from dispatcher.config import Config
from dispatcher.locking import InstanceLock
from dispatcher.execution import ProcessRunner
config=Config(Path(sys.argv[1]),Path(sys.argv[2]),())
with InstanceLock(config) as lease:
    runner=ProcessRunner((sys.executable,'-c','import time; time.sleep(2)'),lease.fds)
    worker=threading.Thread(target=lambda: runner.run(Path(sys.argv[1]),''))
    worker.start()
    while runner._process is None:
        time.sleep(.01)
    print(runner._process.pid,flush=True)
    worker.join()
'''
    env = {**os.environ, 'PYTHONPATH': str(Path('src').resolve())}
    parent = subprocess.Popen([sys.executable, '-c', script, str(config.workspace), str(config.database)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    supervisor_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait(timeout=3)
    try:
        with pytest.raises(AlreadyRunning):
            with InstanceLock(config):
                pass
        deadline = time.monotonic() + 5
        while True:
            try:
                with InstanceLock(config):
                    break
            except AlreadyRunning:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
    finally:
        try:
            import signal
            os.killpg(supervisor_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_configured_multiple_project_orphans_do_not_mix(environment):
    config, store, api, gh, git, *_ = environment
    first = config.projects[0]
    second = replace(first, id='q', repository='owner/other')
    config = replace(config, projects=(first, second))
    git = Git(config)
    p_path, _ = git.prepare(first, 901, 'main', 'unused')
    q_path, _ = git.prepare(second, 902, 'main', 'unused')
    cleanup = Cleanup(config, store, git)
    cleanup.orphans(apply=True, project_id='p')
    assert not p_path.exists() and q_path.exists()


def test_cli_orphan_dryrun_and_apply(environment):
    import os
    import sys
    import yaml
    config, store, api, gh, git, *_ = environment
    path, _ = git.prepare(config.projects[0], 999, 'main', 'unused')
    conf_path = config.database.parent / 'config.yaml'
    conf_path.write_text(yaml.safe_dump({'dispatcher': {'workspace': str(config.workspace), 'database': str(config.database)},
        'projects': [{'id': 'p', 'repository': 'owner/new', 'checkout': str(config.projects[0].checkout)}]}))
    args = [sys.executable, '-m', 'dispatcher.tools.worktree_cleanup', '--config', str(conf_path)]
    env = {**os.environ, 'PYTHONPATH': str(Path('src').resolve())}
    dry = subprocess.run(args, capture_output=True, text=True, check=True, env=env)
    assert 'safe=1' in dry.stdout and 'removed=0' in dry.stdout and path.exists()
    applied = subprocess.run([*args, '--apply'], capture_output=True, text=True, check=True, env=env)
    assert 'removed=1' in applied.stdout and not path.exists()


def test_manual_pr_merged_between_polls_is_not_missed(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment, 'dispatcher:\n  run_codex: false\n')
    executor.execute(execution_id)
    api.prs[600] = dict(id=600, number=600, state='closed', merged_at='2026-11-07T10:00:00+00:00',
                       head={'ref': 'manual/task-0001', 'repo': {'full_name': 'owner/new'}}, base={'ref': 'main'})
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'done'
    assert runner.calls == 0


def test_correction_uses_current_branch_but_creates_new_pr(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    executor.execute(execution_id)
    first_pr = next(iter(api.prs.values()))
    first_pr['state'] = 'closed'
    poll.poll_project(config.projects[0])
    state.transition(1, State.REJECTED, State.NEED_FIX, 'permission')
    api.issues[2] = issue(2)
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    second = Scheduler(store, groups, state).reserve(2)
    assert second
    class CorrectionRunner(FakeRunner):
        def run(self, path, prompt):
            self.calls += 1
            assert (path / 'result.txt').read_text() == 'result\n'
            (path / 'correction.txt').write_text('correction\n')
            return 0, 'Correction result'
    correction_runner = CorrectionRunner(store)
    second_executor = Executor(config, store, git, state, pub, correction_runner)
    second_executor.execute(second.execution_id)
    assert store.issue(2)['state'] == 'review'
    assert len(api.prs) == 2
    assert store.group_branches(2) == store.group_branches(1)
    assert correction_runner.calls == 1


def test_runtime_control_force_stops_process(environment):
    import sys
    import socket
    config, store, api, gh, git, *_ = environment
    approve(api)
    config = replace(config, codex_command=(sys.executable, '-c', 'import time; time.sleep(60)'))
    runtime = Runtime(config, github=gh, git=git)
    replies = []
    def stop_from_client():
        deadline = time.monotonic() + 5
        while runtime.runner._process is None and time.monotonic() < deadline:
            time.sleep(.01)
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(str(config.workspace.resolve() / '.control.sock'))
            client.sendall(b'force')
            replies.append(client.recv(32))
    client = threading.Thread(target=stop_from_client)
    client.start()
    runtime.run()
    client.join(timeout=5)
    assert not client.is_alive() and replies == [b'stopping']
    assert store.issue(1)['state'] == 'error'
    executions = store.rows('SELECT * FROM executions')
    assert len(executions) == 1 and executions[0]['codex_started_at']
    assert executions[0]['exit_code'] != 0


def test_removed_output_not_monitored_or_recreated(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, runner = start(environment)
    pub.reconcile(config.projects[0])  # confirm the scheduled preparing phase first
    removed_id = store.issue(1)['output_comment_id']
    api.comments[1] = [c for c in api.comments[1] if c['id'] != removed_id]
    before = len(api.calls)
    # Unchanged state has no comment check at all.
    pub.reconcile(config.projects[0])
    assert len(api.calls) == before
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'review'
    assert not store.execution(execution_id)['comment_published']
    assert not any(c['id'] == removed_id for c in api.comments[1])
    attempts = sum('/issues/comments/' in c[1] for c in api.calls)
    pub.reconcile(config.projects[0])
    pub.reconcile(config.projects[0])
    assert sum('/issues/comments/' in c[1] for c in api.calls) == attempts
    assert not [c for c in api.calls[before:] if c[0] == 'POST' and '/comments' in c[1]]


def test_terminal_cleanup_preserves_new_remote_user_commits(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, execution_id, _ = start(environment)
    executor.execute(execution_id)
    checkout = config.projects[0].checkout
    # User commits after the last confirmed PR observation are unknown results.
    gitcmd(checkout, 'fetch', 'origin')
    gitcmd(checkout, 'checkout', '-b', 'user-follow-up', 'origin/codex/task-0001')
    (checkout / 'new-user-result.txt').write_text('new result')
    gitcmd(checkout, 'add', '.')
    gitcmd(checkout, 'commit', '-m', 'user follow-up')
    gitcmd(checkout, 'push', 'origin', 'HEAD:refs/heads/codex/task-0001')
    pr = next(iter(api.prs.values()))
    pr['state'] = 'closed'
    poll.poll_project(config.projects[0])
    state.transition(1, State.REJECTED, State.CANCELLED, 'cancel')
    Cleanup(config, store, git).known()
    assert gitcmd(checkout, 'ls-remote', '--heads', 'origin', 'codex/task-0001')
    assert store.rows("SELECT * FROM error_log WHERE error_code='branch_cleanup_failed'")


@pytest.mark.parametrize('run_codex,prefix', [(True, 'codex'), (False, 'manual')])
def test_auto_branch_collision_preserves_remote(environment, run_codex, prefix):
    config, store, api, gh, git, groups, state, pub, poll = environment
    api.issues[10] = api.issues.pop(1)
    api.issues[10]['number'] = 10
    branch = f'{prefix}/task-0010'
    checkout = config.projects[0].checkout
    gitcmd(checkout, 'push', 'origin', f'main:refs/heads/{branch}')
    before = gitcmd(checkout, 'ls-remote', '--heads', 'origin', branch)
    approve(api, 10, f'dispatcher:\n  run_codex: {str(run_codex).lower()}\n')
    poll.poll_project(config.projects[0])
    execution_id = Scheduler(store, groups, state).reserve(1).execution_id
    runner = FakeRunner(store)
    executor = Executor(config, store, git, state, pub, runner)
    assert store.group_branches(1) == ('main', branch)
    executor.execute(execution_id)
    assert store.issue(1)['state'] == 'error'
    assert runner.calls == 0
    assert not store.execution(execution_id)['push_completed']
    assert not api.prs
    state.transition(1, State.ERROR, State.CANCELLED, 'cancel')
    Cleanup(config, store, git).known()
    assert gitcmd(checkout, 'ls-remote', '--heads', 'origin', branch) == before
