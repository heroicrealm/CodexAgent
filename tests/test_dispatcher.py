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
        self.assertTrue(all(level == 'PASS' for level, _ in checks))
        run.assert_called_once_with(['gh', 'auth', 'status'], capture_output=True, timeout=15)
        self.assertNotIn('SECRET', str(checks))
        self.assertTrue(any(level == 'FAIL' for level, _ in diagnose(config, which=lambda _: None)))
        run.return_value = subprocess.CompletedProcess([], 1)
        self.assertIn(('FAIL', 'GitHub authentication: run gh auth login'), diagnose(config, which=lambda _: '/stub', run=run))
        with unittest.mock.patch('dispatcher.doctor.prepare', side_effect=PermissionError):
            self.assertIn(('FAIL', 'Directories: check ownership and permissions'), diagnose(config, which=lambda _: None))


if __name__ == '__main__':
    unittest.main()
