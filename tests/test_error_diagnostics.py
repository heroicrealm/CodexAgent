import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dispatcher.error_diagnostics import OperationError, original, public_field
from dispatcher.execution import ProcessRunner
from dispatcher.git import GitError
from dispatcher.github import GhTransport, GitHubError
from dispatcher.storage import Storage
from test_execution_cleanup import environment, start, gitcmd

SECRET = 'ghp_SYNTHETIC_DIAGNOSTIC_SECRET'
RAW = f'Authorization: Bearer {SECRET}\nhttps://user:password@example.invalid\n/home/operator/private/file\n'


def error_body(api):
    return next(c['body'] for c in api.comments[1] if 'ERROR:v2' in c['body'])


def test_missing_base_and_issue_number(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, yaml='dispatcher:\n  base_branch: missing\n')
    executor.execute(eid)
    row = store.rows('SELECT * FROM error_log')[0]
    assert row['error_code'] == 'base_branch_not_found'
    assert 'GitError' in row['error_message'] and 'Traceback' in row['error_message']
    body = error_body(api)
    assert 'base_branch_not_found' in body and 'Base branch: missing' in body
    assert 'Operation: git.prepare' in body
    assert store.group_branches(1)[1] == 'codex/task-0001'
    assert store.issue(1)['state'] == 'error' and not runner.calls and not api.prs


@pytest.mark.parametrize('operation', ['fetch', 'clone', 'worktree', 'push'])
def test_git_captured_output_and_safe_report(environment, monkeypatch, operation):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    def failure(*args, **kwargs):
        return subprocess.CompletedProcess(args, 17, RAW + 'stdout', RAW + 'stderr')
    with monkeypatch.context() as patch:
        patch.setattr('dispatcher.git.subprocess.run', failure)
        with pytest.raises(GitError) as caught:
            git.run(config.projects[0].checkout, operation)
    exc = caught.value
    assert exc.returncode == 17 and exc.stderr == RAW + 'stderr'
    def prepare(_):
        raise exc
    monkeypatch.setattr(executor, 'prepare', prepare)
    executor.execute(eid)
    data = store.rows('SELECT * FROM error_log')[0]['error_message']
    assert RAW in json.loads(data)['exceptions'][0]['stderr']
    assert SECRET in data and 'Traceback' in data
    assert f'git_{operation}_failed' in error_body(api)
    assert SECRET not in str(api.comments) and '/home/operator' not in str(api.comments)
    assert not runner.calls and not api.prs


def test_primary_survives_salvage_and_recovery(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    runner.code = 19
    runner.stderr = RAW
    def salvage(*args):
        raise RuntimeError('secondary ' + RAW)
    monkeypatch.setattr(git, 'commit_push', salvage)
    executor.execute(eid)
    rows = store.rows('SELECT * FROM error_log ORDER BY id')
    assert rows[0]['error_code'] == 'codex_exit_nonzero'
    assert rows[1]['error_stage'] == 'salvage'
    assert SECRET in rows[0]['error_message'] and SECRET in rows[1]['error_message']
    assert 'codex_exit_nonzero' in error_body(api) and 'Secondary' in error_body(api)
    assert 'Agent final result' not in store.issue(1)['output_body']
    assert (config.workspace / 'results' / f'{eid}.txt').read_text() == 'Agent final result'
    before = len(api.comments[1])
    executor.recover()
    pub.send_final_error_report(1)
    assert len(api.comments[1]) == before and runner.calls == 1
    assert SECRET not in str(api.comments)


def test_large_stderr_local_original(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    runner = ProcessRunner((sys.executable, '-c',
        "import sys; sys.stdin.read(); sys.stderr.write('x'*3000000 + " + repr(RAW) + "); print('stdout-result'); sys.exit(9)"))
    executor, eid, _ = start(environment, runner=runner)
    executor.execute(eid)
    data = json.loads(store.rows('SELECT * FROM error_log ORDER BY id')[0]['error_message'])
    assert data['exceptions'][0]['stderr'] == 'x' * 3000000 + RAW
    assert data['exceptions'][0]['returncode'] == 9
    assert (config.workspace / 'results' / f'{eid}.txt').read_text() == 'stdout-result\n'
    assert SECRET not in str(api.comments)
    with pytest.raises(ValueError):
        executor.run_once(eid)


def test_start_failure_and_chain(tmp_path):
    runner = ProcessRunner(('/nonexistent/synthetic-codex',))
    with pytest.raises(OperationError) as caught:
        runner.run(tmp_path, 'test')
    assert caught.value.code == 'codex_start_failed'
    assert 'FileNotFoundError' in caught.value.stderr
    try:
        try:
            raise ValueError(RAW)
        except ValueError as exc:
            raise OperationError('outer') from exc
    except OperationError as exc:
        data = json.loads(original(exc, 'codex.start'))
    assert RAW == data['exceptions'][1]['message']
    assert SECRET in data['traceback']


def test_gh_original_status_and_no_payload_publication(monkeypatch):
    monkeypatch.setattr('dispatcher.github.subprocess.run', lambda *a, **k:
        subprocess.CompletedProcess(a, 1, RAW, RAW + ' HTTP 503'))
    with pytest.raises(GitHubError) as caught:
        GhTransport()('POST', 'endpoint', {'body': 'private input'})
    assert caught.value.status == 503
    assert caught.value.stdout == RAW and caught.value.returncode == 1
    assert caught.value.stderr == RAW + ' HTTP 503'


def test_private_database_sidecars_and_parent(tmp_path):
    parent = tmp_path / 'owned' / 'diagnostics'
    store = Storage(parent / 'state.sqlite')
    assert parent.stat().st_mode & 0o777 == 0o700
    assert parent.parent.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    with store.connection():
        for suffix in ('-wal', '-shm'):
            assert Path(str(store.path) + suffix).stat().st_mode & 0o777 == 0o600
    os.chmod(store.path, 0o666)
    Storage(store.path)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('value', [SECRET, 'sk-synthetic', '/home/operator/private', 'https://user:pass@host', 'a\nAuthorization: secret'])
def test_public_fields_withheld(value):
    assert public_field(value) == '[withheld]'


def test_uncertain_error_post_never_duplicates(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    runner.code = 1
    transport = gh.transport
    def uncertain(method, endpoint, payload):
        result = transport(method, endpoint, payload)
        if method == 'POST' and payload and 'ERROR:v2' in payload.get('body', ''):
            raise GitHubError(stderr=RAW)
        return result
    gh.transport = uncertain
    executor.execute(eid)
    assert store.issue(1)['error_report_sent'] == 0
    assert SECRET in str([dict(r) for r in store.rows('SELECT * FROM error_log')])
    gh.transport = transport
    assert not pub.send_final_error_report(1)
    executor.recover()
    pub.reconcile(config.projects[0])
    assert len([c for c in api.comments[1] if 'ERROR:v2' in c['body']]) == 1
    assert runner.calls == 1


def test_cli_and_verbose_do_not_show_original(environment, monkeypatch, capsys):
    from dispatcher import __main__ as cli
    from dispatcher.diagnostics import DiagnosticLogger
    from types import SimpleNamespace
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    runner.code, runner.stderr = 2, RAW
    executor.execute(eid)
    logger = DiagnosticLogger(store, 'verbose')
    logger.record('verbose', 'Executor', 'fail', {'issue_id': 1, 'message': RAW, 'stderr': RAW}, 'failed')
    monkeypatch.setattr(cli, 'load_config', lambda _: config)
    monkeypatch.setattr(cli, 'Runtime', lambda _: SimpleNamespace(storage=store))
    for command in ('status', 'queue'):
        monkeypatch.setattr(sys, 'argv', ['dispatcher', '--config', 'unused', command])
        cli.main()
        output = capsys.readouterr()
        assert SECRET not in output.out + output.err and 'Traceback' not in output.out
        assert 'error_message' not in output.out
    assert SECRET not in str([dict(r) for r in store.rows('SELECT * FROM debug_operations')])


def test_no_stderr_before_process_launch(tmp_path, monkeypatch):
    def failed(*a, **k):
        raise OSError(RAW)
    monkeypatch.setattr('dispatcher.execution.subprocess.Popen', failed)
    with pytest.raises(OperationError) as caught:
        ProcessRunner(('codex',)).run(tmp_path, 'prompt')
    assert caught.value.code == 'codex_start_failed'
    assert caught.value.stderr == ''  # capture was created, but process never wrote
    assert RAW == caught.value.__cause__.args[0]


def test_git_timeout_retains_received_output(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('git fetch', 120, output=RAW, stderr=RAW)
    monkeypatch.setattr('dispatcher.git.subprocess.run', timeout)
    with pytest.raises(GitError) as caught:
        git.fetch(config.projects[0])
    assert caught.value.stderr == RAW and caught.value.stdout == RAW
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


def test_poll_failure_without_issue_is_private(environment, monkeypatch, capsys):
    from dispatcher.runtime import Runtime
    config, store, api, gh, git, groups, state, pub, poll = environment
    runtime = Runtime(config, github=gh, git=git)
    def failed(*args):
        raise RuntimeError(RAW)
    monkeypatch.setattr(runtime.poll, 'poll_project', failed)
    runtime.run(once=True)
    assert not store.rows('SELECT * FROM issues')
    diagnostic = config.workspace / 'diagnostics' / 'poll-errors.jsonl'
    assert SECRET in diagnostic.read_text()
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert diagnostic.parent.stat().st_mode & 0o777 == 0o700
    assert SECRET not in ''.join(capsys.readouterr())


def test_origin_main_missing_issue_number_11(environment):
    from test_services import approve, issue
    from dispatcher.services import Scheduler
    from dispatcher.execution import Executor
    from test_execution_cleanup import FakeRunner
    config, store, api, gh, git, groups, state, pub, poll = environment
    api.issues = {11: {**issue(1), 'number': 11}}
    approve(api, number=11)
    poll.poll_project(config.projects[0])
    eid = Scheduler(store, groups, state).reserve(1).execution_id
    gitcmd(config.projects[0].checkout, 'push', 'origin', '--delete', 'main')
    runner = FakeRunner(store)
    Executor(config, store, git, state, pub, runner).execute(eid)
    assert store.group_branches(1) == ('main', 'codex/task-0011')
    body = next(c['body'] for c in api.comments[11] if 'ERROR:v2' in c['body'])
    assert 'base_branch_not_found' in body and 'Base branch: main' in body
    assert 'Base branch not found' in store.rows('SELECT * FROM error_log')[0]['error_message']
    assert not runner.calls and not api.prs


def test_clone_failure_retains_original(environment, monkeypatch, tmp_path):
    from dataclasses import replace
    config, store, api, gh, git, groups, state, pub, poll = environment
    monkeypatch.setattr('dispatcher.git.subprocess.run', lambda *a, **k:
        subprocess.CompletedProcess(a, 42, RAW, RAW))
    with pytest.raises(GitError) as caught:
        git.checkout(replace(config.projects[0], checkout=tmp_path / 'missing-clone'))
    assert caught.value.code == 'git_clone_failed'
    assert caught.value.stdout == RAW and caught.value.stderr == RAW
    assert caught.value.returncode == 42


def test_stderr_temporary_file_mode_and_cancel(tmp_path, monkeypatch):
    import tempfile
    import threading
    import time
    real_temp = tempfile.TemporaryFile
    modes = []
    def temporary(*a, **k):
        stream = real_temp(*a, **k)
        modes.append(os.fstat(stream.fileno()).st_mode & 0o777)
        return stream
    monkeypatch.setattr('dispatcher.execution.tempfile.TemporaryFile', temporary)
    ready = tmp_path / 'ready'
    runner = ProcessRunner((sys.executable, '-c',
        "import sys,time,pathlib; sys.stdin.read(); sys.stderr.write(" + repr(RAW) +
        "); sys.stderr.flush(); pathlib.Path('ready').touch(); time.sleep(60)"))
    result = []
    thread = threading.Thread(target=lambda: result.append(runner.run(tmp_path, 'prompt')))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
    finally:
        runner.stop()
        thread.join(timeout=6)
    assert not thread.is_alive() and result[0][0] != 0
    assert runner.stderr == RAW and modes == [0o600, 0o600]


def test_existing_directory_permissions_warn_without_changing_parent(tmp_path):
    parent = tmp_path / 'shared'
    parent.mkdir(mode=0o755)
    with pytest.warns(RuntimeWarning, match='parent directory'):
        store = Storage(parent / 'state.sqlite')
    assert parent.stat().st_mode & 0o777 == 0o755
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_malformed_gh_response_is_private(monkeypatch):
    monkeypatch.setattr('dispatcher.github.subprocess.run', lambda *a, **k:
        subprocess.CompletedProcess(a, 0, RAW, RAW))
    with pytest.raises(GitHubError) as caught:
        GhTransport()('POST', 'endpoint', None)
    assert caught.value.stdout == RAW and caught.value.stderr == RAW
    assert SECRET not in str(caught.value)


def test_unknown_error_uses_template(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    def failed(*a):
        raise ValueError(RAW)
    monkeypatch.setattr(executor, 'prepare', failed)
    executor.execute(eid)
    assert SECRET in store.rows('SELECT * FROM error_log')[0]['error_message']
    body = error_body(api)
    assert 'execution_failed' in body and 'executor.preparation' in body
    assert SECRET not in str(api.comments) and '/home/operator' not in str(api.comments)


def test_approval_original_stays_local(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    try:
        raise ValueError(RAW)
    except ValueError as exc:
        pub.approval_error(config.projects[0], store.issue(1), exc)
    row = store.rows("SELECT * FROM error_log WHERE error_stage='approval'")[0]
    assert SECRET in row['error_message'] and 'Traceback' in row['error_message']
    assert SECRET not in str(api.comments)
