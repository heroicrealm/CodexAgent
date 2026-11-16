"""V2.5.1 transport and foreground contracts without network credentials."""
from concurrent.futures import Future
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from dispatcher.config import Config, Project
from dispatcher.git import Git, GitError, git_environment, network_args
from dispatcher.projects import validate_remote
from dispatcher.runtime import Runtime
from test_execution_cleanup import environment, FakeRunner, gitcmd
from test_services import approve


@pytest.fixture(autouse=True)
def source_for_subprocesses(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(Path('src').resolve()))


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    # Isolate URL rewrites, auth and trace settings from the developer machine.
    for key in tuple(os.environ):
        if key.startswith(('GIT_', 'GH_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    path = tmp_path / 'checkout'
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    gitcmd(path, 'remote', 'add', 'origin', 'https://github.com/owner/repo.git')
    config = Config(tmp_path / 'workspace', tmp_path / 'db',
                    (Project('p', 'owner/repo', checkout=path),))
    return path, config, Git(config)


@pytest.mark.parametrize('url', [
    'git@github.com:owner/repo.git', 'ssh://git@github.com/owner/repo.git',
    'https://github.com/other/repo.git', 'https://user:secret@github.com/owner/repo.git',
    'https://github.com.evil/owner/repo.git', '/tmp/local.git',
])
@pytest.mark.parametrize('push', [False, True])
def test_invalid_fetch_or_push_refused_without_mutation(checkout, url, push, monkeypatch):
    path, config, adapter = checkout
    gitcmd(path, 'remote', 'set-url', *(['--push'] if push else []), 'origin', url)
    before = (path / '.git/config').read_bytes()
    actual = adapter.command
    def command(args, operation):
        assert operation not in {'fetch', 'push', 'ls-remote'}, 'network must not run'
        return actual(args, operation)
    monkeypatch.setattr(adapter, 'command', command)
    with pytest.raises(GitError, match='HTTPS') as error:
        adapter.fetch(config.projects[0])
    assert 'secret' not in str(error.value)
    assert (path / '.git/config').read_bytes() == before


def test_multiple_urls_and_rewrites_fail_closed(checkout):
    path, _, adapter = checkout
    adapter.verify_remote(path)
    gitcmd(path, 'remote', 'set-url', '--add', '--push', 'origin', 'https://github.com/owner/repo.git')
    gitcmd(path, 'remote', 'set-url', '--add', '--push', 'origin', 'https://github.com/other/repo.git')
    with pytest.raises(GitError):
        adapter.verify_remote(path)
    gitcmd(path, 'config', '--unset-all', 'remote.origin.pushurl')
    gitcmd(path, 'config', 'url.https://github.com/other/.insteadOf', 'https://github.com/owner/')
    with pytest.raises(GitError, match='rewrites'):
        adapter.verify_remote(path)


def test_fetch_push_and_lsremote_use_hardened_transport(checkout, monkeypatch):
    path, config, adapter = checkout
    actual = subprocess.run
    calls = []
    def run(args, **kwargs):
        if any(op in args for op in ('fetch', 'push', 'ls-remote')):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, '', '')
        return actual(args, **kwargs)
    monkeypatch.setattr('dispatcher.git.subprocess.run', run)
    adapter.fetch(config.projects[0])
    adapter.run(path, 'push', 'origin', 'HEAD:refs/heads/result')
    adapter.run(path, 'ls-remote', 'origin')
    assert len(calls) == 3
    for args, kwargs in calls:
        assert 'origin' in args and 'http.followRedirects=false' in args
        assert kwargs['env']['GIT_ALLOW_PROTOCOL'] == 'https'
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert kwargs['stdin'] == subprocess.DEVNULL


def test_clone_ignores_gh_ssh_preference(checkout, monkeypatch, tmp_path):
    _, config, adapter = checkout
    project = Project('new', 'owner/new', checkout=tmp_path / 'new')
    actual = subprocess.run
    calls = []
    monkeypatch.setenv('GH_CONFIG_DIR', str(tmp_path / 'gh'))
    (tmp_path / 'gh').mkdir()
    (tmp_path / 'gh/config.yml').write_text('git_protocol: ssh\n')
    def run(args, **kwargs):
        if 'clone' in args:
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, '', 'auth unavailable')
        return actual(args, **kwargs)
    monkeypatch.setattr('dispatcher.git.subprocess.run', run)
    with pytest.raises(GitError, match='clone'):
        adapter.checkout(project)
    assert len(calls) == 1
    assert calls[0][-2:] == ['https://github.com/owner/new.git', str(project.checkout)]
    assert not any(arg.startswith(('ssh:', 'git@')) for arg in calls[0])


def test_helper_does_not_store_tokens_or_ask_for_input(checkout, tmp_path, monkeypatch):
    path, _, _ = checkout
    sentinel = tmp_path / 'unexpected-helper'
    helper = f'!echo called > "{sentinel}"'
    gitcmd(path, 'config', 'credential.helper', helper)
    gitcmd(path, 'config', 'credential.https://github.com.helper', helper)
    gitcmd(path, 'config', 'credential.https://github.com/owner/repo.git.helper', helper)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    gh = bindir / 'gh'
    gh.write_text('#!/bin/sh\nexit 1\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    askpass = tmp_path / 'askpass'
    askpass.write_text(f'#!/bin/sh\ntouch "{sentinel}"\n')
    askpass.chmod(0o755)
    monkeypatch.setenv('GIT_ASKPASS', str(askpass))
    for action in ('fill', 'approve', 'reject'):
        result = subprocess.run(network_args(['git', '-C', str(path), 'credential', action]),
            input='protocol=https\nhost=github.com\npath=owner/repo.git\nusername=user\n\n',
            capture_output=True, text=True, env=git_environment(), timeout=5)
        if action == 'fill':
            assert result.returncode != 0
    assert not sentinel.exists()


def test_project_admission_refuses_rewrites():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    with pytest.raises(ValueError, match='rewrites'):
        validate_remote('owner/repo', lambda *a: {'full_name': 'owner/repo'}, run)
    assert len(calls) == 1 and 'ls-remote' not in calls[0]


def test_signal_inside_admission_lock_cannot_deadlock():
    script = '''
import os, signal, threading
from dispatcher.runtime import Runtime
runtime = Runtime.__new__(Runtime)
runtime._gate = threading.Lock()
runtime.stopping = False
signal.signal(signal.SIGTERM, runtime._signal_stop)
with runtime._gate:
    os.kill(os.getpid(), signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGTERM)
assert runtime.stopping
'''
    subprocess.run([sys.executable, '-c', script], check=True, timeout=5)


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGINT])
def test_real_signals_drain_worker_and_publish(environment, sig):
    config, store, api, gh, git, *_ = environment
    approve(api)
    entered, release = threading.Event(), threading.Event()
    class SlowRunner(FakeRunner):
        def run(self, path, prompt):
            entered.set()
            assert release.wait(5)
            assert not self.cancelled.is_set()
            return super().run(path, prompt)
    runner = SlowRunner(store)
    runtime = Runtime(config, github=gh, git=git, runner=runner)
    previous = signal.getsignal(sig)
    def sender():
        if entered.wait(5):
            os.kill(os.getpid(), sig)
            time.sleep(0.15)
            os.kill(os.getpid(), sig)
            time.sleep(0.15)
            release.set()
    sender_thread = threading.Thread(target=sender)
    sender_thread.start()
    try:
        runtime.run()
    finally:
        release.set()
        sender_thread.join(timeout=5)
    assert signal.getsignal(sig) == previous
    assert runner.calls == 1 and not runner.cancelled.is_set()
    assert store.issue(1)['state'] == 'review'
    assert not (config.workspace / '.control.sock').exists()


def test_once_observes_completed_worker_exception(environment, monkeypatch):
    config, _, api, gh, git, *_ = environment
    approve(api)
    runtime = Runtime(config, github=gh, git=git)
    class CompletedPool:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def submit(self, *args):
            result = Future()
            result.set_exception(RuntimeError('worker failed'))
            return result
    monkeypatch.setattr('dispatcher.runtime.ThreadPoolExecutor', CompletedPool)
    with pytest.raises(RuntimeError, match='worker failed'):
        runtime.run(once=True)


def test_early_runtime_error_is_stderr_and_nonzero(environment, monkeypatch, capsys):
    from dispatcher.__main__ import main
    config = environment[0]
    monkeypatch.setattr('dispatcher.__main__.load_config', lambda *a: config)
    def fail(*a):
        raise RuntimeError('token=never-print-this')
    monkeypatch.setattr('dispatcher.__main__.Runtime', fail)
    monkeypatch.setattr(sys, 'argv', ['codex-dispatcher', 'run', '--once'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert 'FAIL runtime' in output.err
    assert 'never-print-this' not in output.err + output.out


def test_documented_migration_preserves_data_and_refuses_foreign_origin(tmp_path):
    import yaml
    from dispatcher.db_guard import maintain
    path = tmp_path / 'workspace' / 'repos' / 'p'
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-b', 'main', str(path)], check=True, capture_output=True)
    gitcmd(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
           'commit', '--allow-empty', '-m', 'initial')
    gitcmd(path, 'remote', 'add', 'origin', 'git@github.com:owner/repo.git')
    config = Config(tmp_path / 'workspace', tmp_path / 'db', (Project('p', 'owner/repo'),))
    maintain(config, 'bootstrap')
    worktree = config.workspace / 'worktrees/p/e-1-' / ('a' * 32)
    gitcmd(path, 'worktree', 'add', '-b', 'saved', str(worktree))
    conf = tmp_path / 'config.yaml'
    conf.write_text(yaml.safe_dump({'dispatcher': {'root_path': str(tmp_path),
        'workspace': 'workspace', 'database': 'db'},
        'projects': [{'id': 'p', 'repository': 'owner/repo'}]}))
    document = Path('docs/v2.5.1-https.md').read_text()
    script = document.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    state = {p: p.read_bytes() for p in (config.database, config.workspace / '.dispatcher-instance')}
    refs = gitcmd(path, 'show-ref')
    trees = gitcmd(path, 'worktree', 'list', '--porcelain')
    def migrate():
        return subprocess.run([sys.executable, '-', str(conf), 'p'], input=script,
                              text=True, capture_output=True, timeout=10)
    gitcmd(path, 'config', 'remote.origin.pushurl', 'git@github.com:other/repo.git')
    result = migrate()
    assert result.returncode != 0 and 'identity is unproven' in result.stderr
    assert not (path / '.git/config.pre-https').exists()
    gitcmd(path, 'config', '--unset', 'remote.origin.pushurl')
    assert migrate().returncode == 0
    assert gitcmd(path, 'remote', 'get-url', 'origin') == 'https://github.com/owner/repo.git'
    assert gitcmd(path, 'remote', 'get-url', '--push', 'origin') == 'https://github.com/owner/repo.git'
    assert gitcmd(path, 'show-ref') == refs
    assert gitcmd(path, 'worktree', 'list', '--porcelain') == trees
    assert all(p.read_bytes() == contents for p, contents in state.items())
    assert (path / '.git/config.pre-https').stat().st_mode & 0o777 == 0o600


def test_network_diagnostics_redact_credentials_and_disable_trace(checkout, monkeypatch):
    path, config, adapter = checkout
    secret = 'ghp_SYNTHETIC_TOKEN'
    raw = f'Authorization: Bearer {secret}\nhttps://user:password@github.com/owner/repo.git'
    actual = subprocess.run
    def run(args, **kwargs):
        assert 'GIT_TRACE_CURL' not in kwargs['env']
        if 'fetch' in args:
            return subprocess.CompletedProcess(args, 1, raw, raw)
        return actual(args, **kwargs)
    monkeypatch.setenv('GIT_TRACE_CURL', str(path / 'trace'))
    monkeypatch.setattr('dispatcher.git.subprocess.run', run)
    with pytest.raises(GitError) as error:
        adapter.fetch(config.projects[0])
    from dispatcher.error_diagnostics import original
    diagnostic = original(error.value, 'test')
    assert secret not in diagnostic and 'password' not in diagnostic
    assert '[redacted]' in diagnostic
    assert not (path / 'trace').exists()


def test_commit_push_refuses_worktree_of_another_project(checkout):
    path, _, adapter = checkout
    other = Project('other', 'owner/other')
    with pytest.raises(GitError, match='identity'):
        adapter.commit_push(other, path, 'result')


def test_run_with_xdg_config_does_not_bootstrap_missing_database(tmp_path):
    import yaml
    root = tmp_path / 'installation with spaces'
    root.mkdir()
    conf = tmp_path / 'xdg' / 'codex-dispatcher' / 'config.yaml'
    conf.parent.mkdir(parents=True)
    conf.write_text(yaml.safe_dump({'dispatcher': {'root_path': str(root),
        'workspace': 'workspace', 'database': 'runtime/dispatcher.sqlite3'}, 'projects': []}))
    elsewhere = tmp_path / 'other-cwd'
    elsewhere.mkdir()
    env = {**os.environ, 'XDG_CONFIG_HOME': str(conf.parent.parent)}
    result = subprocess.run([sys.executable, '-m', 'dispatcher', 'run', '--once'],
        env=env, cwd=elsewhere, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'FAIL' in result.stderr
    assert list(root.iterdir()) == []
    assert list(elsewhere.iterdir()) == []


def test_force_stop_preserves_unrelated_process(tmp_path):
    from dispatcher.execution import ProcessRunner
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 start_new_session=True)
    runner = ProcessRunner((sys.executable, '-c', 'import time; time.sleep(30)'))
    result = []
    worker = threading.Thread(target=lambda: result.append(runner.run(tmp_path, '')))
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while runner._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner._process is not None
        runner.stop()
        worker.join(timeout=6)
        assert not worker.is_alive() and result[0][0] != 0
        assert unrelated.poll() is None
    finally:
        runner.stop()
        worker.join(timeout=6)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_early_config_error_does_not_echo_yaml_secrets(tmp_path):
    config = tmp_path / 'invalid.yaml'
    config.write_text('token: ghp_PRIVATE_CONFIG_TOKEN\nbroken: [')
    result = subprocess.run([sys.executable, '-m', 'dispatcher', '--config', str(config),
                             'run', '--once'], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and 'FAIL configuration' in result.stderr
    assert 'ghp_PRIVATE_CONFIG_TOKEN' not in result.stderr + result.stdout
