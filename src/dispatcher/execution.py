"""Execution and publication; retry publication independently from Codex."""
from datetime import datetime
from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import tempfile
from .error_diagnostics import OperationError, original, public_error, public_field, private_directory, private_file
from .input import ERROR_MARKER, OUTPUT_MARKER, parse_input
from .github import GitHubError
from .output import confirmed_sha, public_result
from .models import Execution, ExecutionStatus, ExecutionResult, PublishResult, State, utc_now


FILE_WORK_INSTRUCTION = """Dispatcher instructions:
Work directly in the provided project working directory. For repository implementation
or code changes, create or edit the appropriate files; do not only print source code
in your final message. Briefly describe what you changed. Explicit user instructions
about the work take priority: do not invent an arbitrary filename or language when
ambiguous, and do not create files for tasks explicitly requesting only explanation,
analysis, or text output without changes. Repository changes imply editing real files
without requiring the words 'into file'. Do not run git commit, push, branch operations,
or create PRs; the dispatcher manages these operations.
"""


class ProcessRunner:
    def __init__(self, command, pass_fds=()):
        self.command, self.pass_fds = command, pass_fds
        self.cancelled = threading.Event()
        self._process = None
        self._lock = threading.Lock()

    def stop(self):
        self.cancelled.set()
        with self._lock:
            if self._process and self._process.poll() is None:
                try:
                    os.killpg(self._process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def run(self, path, prompt):
        self.stderr = None
        # TemporaryFile is unlinked, mode 0600, and cannot fill a pipe buffer.
        with tempfile.TemporaryFile(mode='w+b') as stderr_file, tempfile.TemporaryFile(mode='w+b') as startup:
            os.fchmod(stderr_file.fileno(), 0o600)
            try:
                result = self._run(path, prompt, stderr_file, startup)
                if result[0] or self.cancelled.is_set():
                    stderr_file.seek(0)
                    self.stderr = stderr_file.read().decode('utf-8', errors='replace')
                startup.seek(0)
                if startup.read() == b'failed':
                    raise OperationError('Codex could not be started', code='codex_start_failed',
                        operation='codex.start', returncode=result[0], stdout=result[1], stderr=self.stderr)
                return result
            except OperationError:
                raise
            except Exception as exc:
                stderr_file.seek(0)
                raise OperationError('Codex process failed', code='codex_start_failed',
                    operation='codex.start', stderr=stderr_file.read().decode('utf-8', errors='replace')) from exc

    def _run(self, path, prompt, stderr_file, startup):
        if self.cancelled.is_set():
            raise RuntimeError('Execution interrupted')
        with self._lock:
            if self.cancelled.is_set():
                raise RuntimeError('Execution interrupted')
            # The supervisor holds inherited locks even if Codex closes unknown
            # descriptors. If the coordinator crashes, a new runtime cannot recover
            # while this process (and therefore the old Codex) is still alive.
            supervisor = (
                "import os,signal,subprocess,sys\n"
                "signal.signal(signal.SIGTERM, lambda *_: None)\n"
                "try:\n"
                " child=subprocess.Popen(sys.argv[2:])\n"
                "except Exception:\n"
                " os.write(int(sys.argv[1]), b'failed')\n"
                " raise\n"
                "sys.exit(child.wait())\n"
            )
            process = subprocess.Popen((sys.executable, '-c', supervisor, str(startup.fileno()), *self.command),
                cwd=path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, text=True,
                start_new_session=True, pass_fds=(*self.pass_fds, startup.fileno()))
            self._process = process
        try:
            pending_input = prompt
            while True:
                try:
                    output, _ = process.communicate(input=pending_input, timeout=0.2)
                    return process.returncode, output
                except subprocess.TimeoutExpired:
                    pending_input = None
                    if self.cancelled.is_set():
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            output, _ = process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            output, _ = process.communicate()
                        return process.returncode, output
        finally:
            with self._lock:
                self._process = None


class Publisher:
    def __init__(self, config, storage, github, git, state):
        self.config, self.storage, self.github, self.git, self.state = config, storage, github, git, state
        self._closed = set()
        self._output_lock = threading.RLock()
        self._unavailable_outputs = set()

    def project(self, issue):
        return next(p for p in self.config.projects if p.id == issue['project_id'])

    def output(self, issue_id, body):
        with self._output_lock:
            issue = self.storage.issue(issue_id)
            if issue['output_body'] == OUTPUT_MARKER + '\n' + body:
                return True  # local confirmation, no GET or monitoring of comments
            if issue['output_comment_id'] in self._unavailable_outputs:
                return False
            try:
                comment_id, confirmed_body = self.github.output(self.project(issue), issue, body)
            except GitHubError as exc:
                if exc.status != 404 or not issue['output_comment_id']:
                    raise
                self._unavailable_outputs.add(issue['output_comment_id'])
                self.storage.record_error(issue_id, 'output_missing', 'output',
                    'Confirmed OUTPUT is unavailable; it is not monitored or recreated')
                return False
            self.storage.record_confirmed_output(issue_id, comment_id, confirmed_body)
            return True

    def phase_output(self, issue_id):
        """Derive pending publication from existing SQLite facts; never read OUTPUT."""
        with self._output_lock:
            issue = self.storage.issue(issue_id)
            _, branch = self.storage.group_branches(issue_id)
            parts = [f"State: `{issue['state']}`. Group branch: `{public_field(branch)}`."]
            prs = self.storage.rows('SELECT * FROM pull_requests WHERE issue_id=? ORDER BY pr_id DESC', (issue_id,))
            if prs:
                pr = prs[0]
                parts.append(f"PR [#{pr['pr_number']}](https://github.com/{issue['repository']}/pull/{pr['pr_number']}) "
                             f"({pr['status']}) → `{pr['base_branch']}`.")
            executions = self.storage.rows('SELECT * FROM executions WHERE issue_id=?', (issue_id,))
            if executions:
                execution = executions[0]
                sha = confirmed_sha(execution)
                if sha:
                    parts.append(f"Result commit: [{sha}](https://github.com/{issue['repository']}/commit/{sha}).")
                parts.append(f"Started: {execution['started_at']}. Finished: {execution['finished_at']}.")
                if execution['codex_started_at'] and execution['codex_finished_at']:
                    duration = (datetime.fromisoformat(execution['codex_finished_at']) -
                                datetime.fromisoformat(execution['codex_started_at'])).total_seconds()
                    parts.append(f"Codex duration: {duration:.1f}s; exit code: {execution['exit_code']}.")
                parts.append(f"Push confirmed: {bool(execution['push_completed'])}.")
                result_file = self.config.workspace / 'results' / f"{execution['execution_id']}.txt"
                if result_file.is_file() and execution['status'] != 'failed':
                    parts.append(public_result(result_file.read_text(), execution, issue['repository'],
                        lambda sha, relative: self.git.committed_file(self.project(issue),
                            execution['worktree_path'], sha, relative))[:20000])
            confirmed = self.output(issue_id, '\n\n'.join(parts))
            if executions:
                self.storage.update_execution(executions[0]['execution_id'], comment_published=int(confirmed))

    def approval_error(self, project, issue, message):
        # Not an execution failure: discovered remains discovered for another approve.
        exc = message if isinstance(message, Exception) else ValueError(message)
        self.storage.record_error(issue['issue_id'], 'approval_rejected', 'approval', original(exc, 'approval.validate'))
        self.output(issue['issue_id'], 'Approval rejected; inspect local SQLite error_log for details.')

    def find_pr(self, issue, poll_id=None):
        project = self.project(issue)
        known = self.storage.rows('SELECT * FROM pull_requests WHERE issue_id=? ORDER BY pr_id DESC', (issue['issue_id'],))
        if known:
            return self.github.pr(project, known[0]['pr_number'], issue['issue_id'], poll_id)
        executions = self.storage.rows('SELECT started_at FROM executions WHERE issue_id=?', (issue['issue_id'],))
        excluded = frozenset(row['pr_id'] for row in self.storage.rows(
            'SELECT p.pr_id FROM pull_requests p JOIN issues i ON i.issue_id=p.issue_id '
            'WHERE i.execution_group_id=? AND p.issue_id<>?', (issue['execution_group_id'], issue['issue_id'])))
        return self.github.find_pr(project, self.storage.group_branches(issue['issue_id'])[1], issue['issue_id'],
                                   poll_id, since=executions[0]['started_at'] if executions else None, excluded=excluded)

    def publish(self, execution_id):
        execution = self.storage.execution(execution_id)
        issue = self.storage.issue(execution['issue_id'])
        project = self.project(issue)
        if issue['state'] not in (State.RUNNING, State.ERROR, State.REVIEW, State.MANUAL_RUNNING):
            raise ValueError('Publication is not allowed in this state')
        base, branch = self.storage.group_branches(issue['issue_id'])
        self.storage.update_execution(execution_id, status='publishing')
        has_diff = None
        if execution['worktree_path'] and Path(execution['worktree_path']).exists():
            path = Path(execution['worktree_path'])
            has_diff = self.git.has_diff(project, path, base)
            if not has_diff and not confirmed_sha(execution):
                raise OperationError(
                    'Codex completed successfully but made no file changes. PR was not created.',
                    code='no_changes_for_pr', operation='git.diff')
            if has_diff:
                commit = self.git.commit_push(project, path, branch)
                self.storage.update_execution(execution_id, result_commit=commit, push_completed=1)
        elif not execution['push_completed']:
            raise RuntimeError('No published result or surviving worktree')
        # Always reconcile before POST, including retry after an uncertain response.
        pr = self.find_pr(issue)
        if not pr:
            if has_diff is None:
                raise RuntimeError('No surviving worktree to compare for a new PR')
            if has_diff is False:
                raise OperationError(
                    'Codex completed successfully but made no file changes. PR was not created.',
                    code='no_changes_for_pr', operation='git.diff')
            if not execution['codex_finished_at'] or execution['exit_code'] != 0:
                raise RuntimeError('No completed successful Codex result for a new PR')
            pr = self.github.create_pr(project, issue, branch, base)
        self.state.apply_pr(pr, recover=True)
        self.storage.update_execution(execution_id, pr_published=1, status='completed', finished_at=utc_now())
        self.phase_output(issue['issue_id'])
        self.github.sync_labels(project, issue['issue_number'], self.storage.issue(issue['issue_id'])['state'])
        self.state.diagnose('Publisher', 'publish', {'issue_id': issue['issue_id']})
        return PublishResult(True, pr.pr_id)

    def send_final_error_report(self, issue_id):
        with self._output_lock:
            return self._send_final_error_report(issue_id)

    def _send_final_error_report(self, issue_id):
        issue = self.storage.issue(issue_id)
        if issue['error_report_sent']:
            return False
        errors = self.storage.rows('SELECT id,execution_id,error_stage,error_code FROM error_log '
                                   'WHERE issue_id=? ORDER BY id', (issue_id,))
        attempts = [r for r in errors if r['error_code'] in
                    ('error_report_attempt', 'error_report_rejected', 'error_report_uncertain')]
        if attempts and attempts[-1]['error_code'] != 'error_report_rejected':
            return False  # ambiguous POST/crash: never risk an automatic duplicate
        executions = self.storage.rows('SELECT * FROM executions WHERE issue_id=?', (issue_id,))
        execution_id = executions[0]['execution_id'] if executions else None
        primary = next((r for r in errors if r['execution_id'] == execution_id
                        and r['error_stage'] not in ('salvage', 'error_report', 'output', 'recovery_sync')), errors[0] if errors else None)
        if primary is None:
            return False
        text = ERROR_MARKER + '\n' + public_error(primary, issue, self.storage.group_branches(issue_id)[0])
        secondary = sum(r['error_stage'] in ('salvage', 'error_report')
                        and r['error_code'] != 'error_report_attempt' for r in errors)
        if secondary:
            text += f'\nSecondary diagnostic records: {secondary} (local only).'
        text = text[:4000]
        self.storage.record_error(issue_id, 'error_report_attempt', 'error_report',
                                  'ERROR POST pending confirmation', execution_id)
        try:
            self.github.comment(self.project(issue), issue['issue_number'], text)
        except Exception as exc:
            rejected = isinstance(exc, GitHubError) and exc.status is not None
            self.storage.record_error(issue_id, 'error_report_rejected' if rejected else 'error_report_uncertain',
                                      'error_report', original(exc, 'publisher.error_report'), execution_id)
            raise
        with self.storage.transaction() as db:
            db.execute('UPDATE issues SET error_report_sent=1 WHERE issue_id=?', (issue_id,))
        return True

    def fail(self, execution_id, code='execution_failed', exc=None, stage=None):
        execution = self.storage.execution(execution_id)
        issue = self.storage.issue(execution['issue_id'])
        stage = stage or code.split('_')[0]
        exc = exc or OperationError('Execution interrupted; original exception unavailable')
        diagnostic_code = getattr(exc, 'code', None) or code
        self.storage.record_error(issue['issue_id'], diagnostic_code, stage,
            original(exc, getattr(exc, 'operation', None) or 'executor.' + stage,
                     {'result_file': str(self.config.workspace / 'results' / f'{execution_id}.txt')}), execution_id)
        # Best effort to save dirty work, even when the main pipeline failed.
        if diagnostic_code != 'no_changes_for_pr' and execution['worktree_path'] and Path(execution['worktree_path']).exists():
            try:
                if self.git.has_unpublished_changes(Path(execution['worktree_path']),
                        execution['result_commit'] if execution['push_completed'] else execution['initial_commit']):
                    commit = self.git.commit_push(self.project(issue), Path(execution['worktree_path']),
                                                  self.storage.group_branches(issue['issue_id'])[1])
                    self.storage.update_execution(execution_id, result_commit=commit, push_completed=1)
            except Exception as secondary:
                self.storage.record_error(issue['issue_id'], 'salvage_failed', 'salvage',
                    original(secondary, 'publisher.salvage'), execution_id)
        self.storage.update_execution(execution_id, status='failed', error_code=diagnostic_code, finished_at=utc_now())
        if issue['state'] in (State.PREPARING, State.RUNNING):
            self.state.transition(issue['issue_id'], State(issue['state']), State.ERROR, diagnostic_code)
        try:
            self.phase_output(issue['issue_id'])
            self.send_final_error_report(issue['issue_id'])
            self.github.sync_labels(self.project(issue), issue['issue_number'], self.storage.issue(issue['issue_id'])['state'])
        except Exception as secondary:
            self.storage.record_error(issue['issue_id'], 'error_report_failed', 'error_report',
                original(secondary, 'publisher.error_report'), execution_id)

    def reconcile(self, project):
        for issue in self.storage.rows('SELECT * FROM issues WHERE project_id=? AND is_virtual=0', (project.id,)):
            if issue['approved_at']:
                try:
                    self.phase_output(issue['issue_id'])
                except Exception as exc:
                    self.storage.record_error(issue['issue_id'], 'output_sync_failed', 'output',
                                              original(exc, 'publisher.output'))
            if issue['state'] in (State.DONE, State.CANCELLED) and issue['issue_id'] not in self._closed:
                self.github.sync_labels(project, issue['issue_number'], issue['state'])
                self.github.close_issue(project, issue['issue_number'], issue['state'])
                self._closed.add(issue['issue_id'])
            elif issue['state'] == State.ERROR:
                self.send_final_error_report(issue['issue_id'])


class Executor:
    def __init__(self, config, storage, git, state, publisher, runner=None):
        self.config, self.storage, self.git, self.state, self.publisher = config, storage, git, state, publisher
        self.runner = runner or ProcessRunner(config.codex_command)

    def prepare(self, execution_id):
        execution = self.storage.execution(execution_id)
        issue = self.storage.issue(execution['issue_id'])
        if self.runner.cancelled.is_set():
            raise RuntimeError('Execution interrupted')
        if issue['state'] != State.PREPARING or execution['codex_started_at']:
            raise ValueError('Execution cannot be prepared again')
        project = self.publisher.project(issue)
        base, branch = self.storage.group_branches(issue['issue_id'])
        params, _ = parse_input(issue['approved_input'])
        checkout = self.git.fetch(project)
        if not issue['correction_of_issue_id'] and not execution['push_completed'] and self.git.remote_ref(checkout, branch):
            raise OperationError('New group branch already exists; choose another branch',
                                 code='work_branch_exists', operation='git.prepare')
        self.storage.update_execution(execution_id, status='preparing', branch_name=branch)
        if not params.run_codex:
            commit = self.git.publish_manual(project, base, branch)
            self.storage.update_execution(execution_id, initial_commit=commit, result_commit=commit,
                                          push_completed=1, status='completed', finished_at=utc_now())
            self.state.transition(issue['issue_id'], State.PREPARING, State.MANUAL_RUNNING, 'manual-branch-published')
            self.publisher.phase_output(issue['issue_id'])
            self.publisher.github.sync_labels(project, issue['issue_number'], State.MANUAL_RUNNING)
        elif not execution['worktree_path']:
            path, commit = self.git.prepare(project, execution_id, base, branch)
            self.storage.update_execution(execution_id, worktree_path=str(path), initial_commit=commit)
        self.state.diagnose('Executor', 'prepare', {'issue_id': issue['issue_id']})
        prepared = self.storage.execution(execution_id)
        return Execution(prepared['execution_id'], prepared['issue_id'], ExecutionStatus(prepared['status']), prepared['codex_started_at'])

    def run_once(self, execution_id):
        execution = self.storage.execution(execution_id)
        issue = self.storage.issue(execution['issue_id'])
        if execution['codex_started_at']:
            raise ValueError('Codex attempt already consumed')
        if issue['state'] != State.PREPARING or not execution['worktree_path']:
            raise ValueError('Execution is not prepared')
        if self.runner.cancelled.is_set():
            raise RuntimeError('Execution interrupted')
        if not self.state.transition(issue['issue_id'], State.PREPARING, State.RUNNING, 'codex-start'):
            raise ValueError('Execution state changed before Codex start')
        self.publisher.github.sync_labels(self.publisher.project(issue), issue['issue_number'], State.RUNNING)
        if not self.storage.mark_codex_started(execution_id):
            raise ValueError('Codex attempt already consumed')
        code, result = self.runner.run(Path(execution['worktree_path']),
            FILE_WORK_INSTRUCTION + '\n' + issue['approved_title'] + '\n\n' + issue['approved_body'] + '\n\n' + (issue['approved_input'] or ''))
        self.storage.update_execution(execution_id, codex_finished_at=utc_now(), exit_code=code)
        result_dir = self.config.workspace / 'results'
        private_directory(result_dir)
        result_path = result_dir / f'{execution_id}.txt'
        private_file(result_path)
        result_path.write_text(result)
        if self.runner.cancelled.is_set() and not code:
            raise OperationError('Codex execution interrupted', code='codex_interrupted', operation='codex.run',
                                 returncode=code, stdout=result, stderr=getattr(self.runner, 'stderr', None))
        if code:
            raise OperationError('Codex process failed', code='codex_exit_nonzero', operation='codex.run',
                                 returncode=code, stdout=result, stderr=getattr(self.runner, 'stderr', None))
        self.state.diagnose('Executor', 'run_once', {'issue_id': issue['issue_id']})
        return ExecutionResult(execution_id, code)

    def execute(self, execution_id):
        stage = "preparation"
        try:
            self.prepare(execution_id)
            issue = self.storage.issue(self.storage.execution(execution_id)['issue_id'])
            if issue['state'] == State.MANUAL_RUNNING:
                return
            stage = "codex"
            self.run_once(execution_id)
            stage = "publication"
            if self.runner.cancelled.is_set():
                raise RuntimeError("Execution interrupted before publication")
            self.publisher.publish(execution_id)
        except Exception as exc:
            self.publisher.fail(execution_id, stage + "_failed", exc, stage)

    def recover(self):
        # Runtime lock must be held: no old Codex process may remain alive.
        for execution in self.storage.rows("SELECT e.* FROM executions e JOIN issues i ON i.issue_id=e.issue_id "
                                            "WHERE i.state IN ('preparing','running')"):
            issue = self.storage.issue(execution['issue_id'])
            project = self.publisher.project(issue)
            branch = self.storage.group_branches(issue['issue_id'])[1]
            try:
                pr = self.publisher.find_pr(issue)
                if pr:
                    self.state.apply_pr(pr, recover=True)
                    self.storage.update_execution(execution['execution_id'], pr_published=1, push_completed=1,
                                                  status='completed', finished_at=utc_now())
                    self.publisher.phase_output(issue['issue_id'])
                    continue
            except Exception as exc:
                # A failed reconciliation is not proof of absence, but Codex is never retried.
                self.storage.record_error(issue['issue_id'], 'recovery_sync_failed', 'recovery_sync',
                    original(exc, 'publisher.recover'), execution['execution_id'])
            self.publisher.fail(execution['execution_id'], 'interrupted_execution')
