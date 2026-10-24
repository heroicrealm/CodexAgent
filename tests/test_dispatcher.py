import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from dispatcher.config import ConfigError, load
from dispatcher.doctor import diagnose
from dispatcher.runtime import Lifecycle, identity, prepare, running


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.yaml'
        self.yaml = '''paths:
  workspace: ./work
  logs: ./logs
projects:
  - id: first
    enabled: true
    repository: owner/repo
    base_branch: main
    task_label: task
  - id: second
    enabled: false
    repository: other/repo
    base_branch: develop
    task_label: ready
'''
        self.path.write_text(self.yaml)
        from dispatcher.initialization import save_result
        configured = load(self.path)
        save_result(configured, configured.projects[0], True)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'dispatcher', '--config', str(self.path), *args], capture_output=True, text=True, timeout=10)

    def test_configuration(self):
        config = load(self.path)
        self.assertEqual(config.workspace, self.root / 'work')
        self.assertEqual(len(config.projects), 2)
        self.assertEqual(config.max_parallel_tasks, 1)
        invalid = [self.yaml.replace('second', 'first'), self.yaml.replace('enabled: true', 'enabled: 1'),
                   self.yaml.replace('owner/repo', 'invalid'), self.yaml.replace('./work', '/mnt/c/work'),
                   self.yaml.replace('id: first', 'id: ../escape'),
                   self.yaml + 'dispatcher:\n  max_parallel_tasks: 0\n',
                   self.yaml + 'dispatcher:\n  poll_interval_seconds: true\n',
                   self.yaml + 'dispatcher:\n  shutdown_policy: cancel\n',
                   self.yaml + 'github:\n  auth_method: token\n', 'projects: []', '[]', 'projects: [']
        for content in invalid:
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ConfigError):
                    load(self.path)

    def test_lifecycle_cli_and_stale_state(self):
        config = load(self.path)
        root = prepare(config)
        (root / 'state.json').write_text(json.dumps({'pid': os.getpid(), 'identity': identity(os.getpid())}))
        self.assertIsNone(running(root))  # live PID without held lock is stale too
        self.assertIn('stopped', self.cli('status').stdout)
        try:
            starts = [subprocess.Popen([sys.executable, '-m', 'dispatcher', '--config', str(self.path), 'start'], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
            for child in starts:
                _, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, err)
            first = running(root)
            self.assertIsNotNone(first)
            self.assertIn('already running', self.cli('start').stdout)
            self.assertEqual(first, running(root))
            self.assertEqual(self.cli('stop').returncode, 0)
            self.assertIsNone(running(root))
            self.assertEqual(self.cli('stop').returncode, 0)
            self.assertEqual(self.cli('start').returncode, 0)
            self.assertEqual(self.cli('stop', '--force').returncode, 0)
            self.assertFalse((root / 'state.json').exists())
            records = [json.loads(line) for line in (config.logs / 'dispatcher.jsonl').read_text().splitlines()]
            self.assertIn('stopped_force', [r['event'] for r in records])
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        finally:
            self.cli('stop', '--force')

    def test_signals_and_crash_recovery(self):
        root = prepare(load(self.path))
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGKILL):
                self.assertEqual(self.cli('start').returncode, 0)
                state = running(root)
                os.kill(state['pid'], sig)
                deadline = time.monotonic() + 5
                while running(root) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(running(root))
            self.assertEqual(self.cli('start').returncode, 0)
            self.assertIsNotNone(running(root))
        finally:
            self.cli('stop', '--force')

    def test_parallel_limit(self):
        lifecycle = Lifecycle(max_parallel_tasks=1)
        try:
            lifecycle.submit([sys.executable, '-c', 'import time; time.sleep(60)'])
            with self.assertRaisesRegex(RuntimeError, 'Parallel'):
                lifecycle.submit(['unused'])
        finally:
            lifecycle.cancel()

    def test_drain_waits_and_rejects_admission(self):
        lifecycle = Lifecycle()
        child = lifecycle.submit([sys.executable, '-c', 'import time; time.sleep(.3)'])
        lifecycle.request_stop()
        with self.assertRaises(RuntimeError):
            lifecycle.submit(['unused'])
        started = time.monotonic()
        lifecycle.drain()
        self.assertGreater(time.monotonic() - started, .2)
        self.assertEqual(child.returncode, 0)

    def test_force_interrupts_drain_and_descendants(self):
        lifecycle = Lifecycle()
        marker = self.root / 'pid'
        code = ('import subprocess,time,pathlib; '
                'p=subprocess.Popen(["sleep","60"]); '
                f'pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)')
        child = lifecycle.submit([sys.executable, '-c', code])
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            descendant = int(marker.read_text())
            lifecycle.request_stop()
            timer = threading.Timer(.1, lambda: lifecycle.request_stop(True))
            timer.start()
            lifecycle.drain()
            timer.join()
            self.assertIsNotNone(child.poll())
            self.assertIsNone(identity(descendant))
        finally:
            lifecycle.cancel()

    def test_doctor_stubs_and_no_secret_output(self):
        config = load(self.path)
        run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout='SECRET'))
        checks = diagnose(config, which=lambda _: '/stub', run=run)
        self.assertTrue(any('repository read access cannot be verified' in message for _, message in checks))
        run.assert_any_call(['gh', 'auth', 'status'], capture_output=True, timeout=15)
        for binary in ('git', 'gh', 'codex'):
            run.assert_any_call([binary, '--version'], capture_output=True, timeout=15)
        self.assertNotIn('SECRET', str(checks))
        self.assertTrue(any(level == 'FAIL' for level, _ in diagnose(config, which=lambda _: None)))
        run.return_value = subprocess.CompletedProcess([], 1)
        self.assertIn(('FAIL', 'GitHub authentication: run gh auth login'), diagnose(config, which=lambda _: '/stub', run=run))
        with unittest.mock.patch('dispatcher.doctor.os.access', return_value=False):
            self.assertIn(('FAIL', 'Directories: check ownership and permissions'), diagnose(config, which=lambda _: None))

    def test_grace_timeout_validation(self):
        self.assertEqual(load(self.path).force_grace_timeout_seconds, 5)
        for value in ('0', '-1', 'true', '1.5', 'null'):
            self.path.write_text(self.yaml + f'dispatcher:\n  force_grace_timeout_seconds: {value}\n')
            with self.assertRaises(ConfigError):
                load(self.path)

    def test_version_failures(self):
        for error in (OSError('SECRET'), subprocess.TimeoutExpired('SECRET', 15), None):
            with self.subTest(error=error):
                def run(argv, **kwargs):
                    if argv[-1] == '--version':
                        if error:
                            raise error
                        return subprocess.CompletedProcess(argv, 42, stdout='SECRET')
                    return subprocess.CompletedProcess(argv, 0)
                checks = diagnose(load(self.path), which=lambda _: '/stub', run=run, environ={})
                for binary in ('git', 'gh', 'codex'):
                    self.assertTrue(any(level == 'FAIL' and message.startswith(binary + ': version')
                                        for level, message in checks))
                self.assertNotIn('SECRET', str(checks))

    def test_ssh_agent(self):
        config = load(self.path)
        clone = config.workspace / 'first' / '.git'
        clone.mkdir(parents=True)
        for remote, required in (('git@github.com:owner/repo', True),
                                 ('ssh://git@github.com/owner/repo', True),
                                 ('https://github.com/owner/repo', False)):
            for outcome in (0, 1, 2, OSError('SECRET'), subprocess.TimeoutExpired('ssh-add', 15)):
                def run(argv, **kwargs):
                    if argv[-1] == '-v':
                        return subprocess.CompletedProcess(argv, 0, stdout=f'origin {remote} (fetch)')
                    if argv == ['ssh-add', '-l']:
                        if isinstance(outcome, Exception):
                            raise outcome
                        return subprocess.CompletedProcess(argv, outcome, stdout='SECRET')
                    return subprocess.CompletedProcess(argv, 0)
                with self.subTest(remote=remote, outcome=outcome):
                    checks = diagnose(config, which=lambda _: '/stub', run=run,
                                      environ={'SSH_AUTH_SOCK': '/stub/socket'})
                    agent = [level for level, msg in checks if msg.startswith('SSH Agent:')]
                    self.assertEqual(agent, ['PASS' if outcome == 0 else 'FAIL' if required else 'WARN'])
                    self.assertNotIn('SECRET', str(checks))
            checks = diagnose(config, which=lambda _: '/stub', run=run, environ={})
            self.assertIn('FAIL' if required else 'PASS',
                          [level for level, msg in checks if msg.startswith('SSH Agent:')])
        checks = diagnose(config, which=lambda name: None if name == 'ssh-add' else '/stub',
                          run=run, environ={'SSH_AUTH_SOCK': '/stub/socket'})
        self.assertIn(('WARN', 'SSH Agent: install OpenSSH client and run ssh-add locally'), checks)

    def test_transport_and_auth_errors(self):
        config = load(self.path)
        (config.workspace / 'first' / '.git').mkdir(parents=True)
        for outcome in (OSError('SECRET'), subprocess.TimeoutExpired('git', 15), 1):
            def run(argv, **kwargs):
                if argv[-1] == '-v' or argv == ['gh', 'auth', 'status']:
                    if isinstance(outcome, Exception):
                        raise outcome
                    return subprocess.CompletedProcess(argv, outcome, stdout='SECRET')
                return subprocess.CompletedProcess(argv, 0)
            checks = diagnose(config, which=lambda _: '/stub', run=run, environ={})
            self.assertTrue(any(level == 'WARN' and msg.startswith('Git transport:') for level, msg in checks))
            self.assertIn(('FAIL', 'GitHub authentication: run gh auth login'), checks)
            self.assertNotIn('SECRET', str(checks))

    def test_group_survives_exited_leader(self):
        lifecycle = Lifecycle(force_grace_timeout_seconds=1)
        marker = self.root / 'grandchild'
        # The leader exits before a TERM-ignoring grandchild. Its unreaped PID
        # must keep the group identity reserved throughout the grace period.
        grandchild = ('import signal,time,pathlib,os; '
                      'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
        middle = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{grandchild!r}])'
        leader = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{middle!r}])'
        child = lifecycle.submit([sys.executable, '-c', leader])
        unrelated = subprocess.Popen(['sleep', '60'], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            deadline = time.monotonic() + 5
            while identity(child.pid) is not None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(identity(child.pid))
            lifecycle.request_stop()
            timer = threading.Timer(.1, lambda: lifecycle.request_stop(True))
            timer.start()
            started = time.monotonic()
            lifecycle.drain()
            timer.join()
            self.assertGreaterEqual(time.monotonic() - started, 1)
            self.assertIsNone(identity(int(marker.read_text())))
            self.assertIsNone(unrelated.poll())
            lifecycle.cancel()  # repeated cancellation sends no more signals
        finally:
            lifecycle.cancel()
            unrelated.terminate()
            unrelated.wait()

    def test_graceful_group_waits_for_descendant(self):
        lifecycle = Lifecycle()
        code = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(.4)"])'
        lifecycle.submit([sys.executable, '-c', code])
        lifecycle.request_stop()
        started = time.monotonic()
        lifecycle.drain()
        self.assertGreater(time.monotonic() - started, .3)
        self.assertEqual(lifecycle.children, [])

    def test_force_allows_term_cleanup(self):
        lifecycle = Lifecycle(force_grace_timeout_seconds=2)
        ready, cleaned = self.root / 'ready', self.root / 'cleaned'
        code = ("import signal,time,pathlib; "
                f"signal.signal(signal.SIGTERM, lambda *_: (time.sleep(.2), pathlib.Path({str(cleaned)!r}).touch(), exit(0))); "
                f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
        child = lifecycle.submit([sys.executable, '-c', code])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            lifecycle.request_stop(True)
            with unittest.mock.patch('dispatcher.runtime.os.killpg', wraps=os.killpg) as killpg:
                lifecycle.drain()
                self.assertEqual([call.args[1] for call in killpg.call_args_list], [signal.SIGTERM])
            self.assertTrue(cleaned.exists())
            self.assertEqual(child.returncode, 0)
        finally:
            lifecycle.cancel()

    def test_reaped_leader_is_not_signalled(self):
        lifecycle = Lifecycle()
        child = lifecycle.submit([sys.executable, '-c', 'pass'])
        child.wait()
        with unittest.mock.patch('dispatcher.runtime.os.killpg') as killpg:
            with self.assertRaisesRegex(RuntimeError, 'reaped outside'):
                lifecycle.cancel()
            killpg.assert_not_called()


if __name__ == '__main__':
    unittest.main()
