"""Noninteractive executor. Lifecycle alone owns child polling and reaping."""
import json
import subprocess
import time
from dispatcher.workspace import TaskError, private_file, register_artifacts


class CodexExecutor:
    def __init__(self, config, lifecycle):
        self.config, self.lifecycle = config, lifecycle
        self.checked = False

    def check(self):
        if self.checked:
            return
        try:
            version = subprocess.run([self.config.executable, '--version'], capture_output=True, text=True, timeout=15)
            help_ = subprocess.run([self.config.executable, 'exec', '--help'], capture_output=True, text=True, timeout=15)
            if version.returncode or help_.returncode or '--dangerously-bypass-approvals-and-sandbox' not in help_.stdout:
                raise ValueError
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise TaskError('unsupported_codex_cli', 'CODEX') from None
        self.checked = True

    def start(self, issue, branch, paths, worktree, task, attempt, before_exec=None):
        self.check()
        prompt = ('Implement this approved GitHub Issue. Work on branch ' + branch + '.\n'
                  'Run available tests and create a Git commit. Do not push, change GitHub Issues, create PRs or merge.\n'
                  'Only if the approved body explicitly contains No-op allowed: may you leave HEAD unchanged; '
                  'then explain in your final message using NO_OP: <reason>.\n'
                  'Use the following local attachments (outside the repository):\n' + json.dumps(paths) +
                  '\nApproved title:\n' + issue['title'] + '\nApproved body:\n' + issue['body'])
        # Keep Issue text out of argv and the shared structured log.
        register_artifacts(self.config.workspace, task, f'{attempt}.prompt', f'{attempt}.codex.log')
        prompt_path = task / f'{attempt}.prompt'
        with private_file(prompt_path) as stream:
            stream.write(prompt)
        output = task / f'{attempt}.codex.log'
        with open(prompt_path) as stdin, private_file(output) as stdout:
            child = self.lifecycle.submit([self.config.executable, 'exec',
                '--dangerously-bypass-approvals-and-sandbox', '--json', '-'],
                cwd=worktree, stdin=stdin, stdout=stdout, stderr=subprocess.STDOUT, before_exec=before_exec)
        return dict(child=child, deadline=time.monotonic() + self.config.timeout_seconds, output=output)

    @staticmethod
    def quota_exhausted(path):
        # Only structured, explicit exhaustion codes, never substring matching
        # generic network errors, agent prose or HTTP 429.
        try:
            with open(path) as stream:
                for line in stream:
                    if len(line) > 1048576:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    error = item.get('error') if item.get('type') in ('error', 'turn.failed') else None
                    if isinstance(error, dict) and error.get('code') in ('usage_limit_reached', 'quota_exhausted'):
                        return True
        except OSError:
            pass
        return False

    @staticmethod
    def confirmed_noop(issue, path):
        import re
        if not re.search(r'^No-op allowed:[ \t]*\S.+$', issue['body'], re.MULTILINE):
            return False
        try:
            with open(path) as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict) or not isinstance(item.get('item', {}), dict):
                        continue
                    message = item.get('item', {})
                    if (item.get('type') == 'item.completed' and message.get('type') == 'agent_message'
                            and re.search(r'^NO_OP:[ \t]*\S.+$', message.get('text', ''), re.MULTILINE)):
                        return True
        except OSError:
            pass
        return False
