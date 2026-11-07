"""Private originals and public, allowlisted error descriptions."""
import json
import os
from pathlib import Path
import re
import traceback

REASONS = {
    'no_changes_for_pr': 'Codex completed successfully but made no file changes. PR was not created.',
    'git_diff_failed': 'Git diff failed.',
    'base_branch_not_found': 'The remote base branch was not found.',
    'work_branch_exists': 'The work branch already exists; choose another branch.',
    'git_clone_failed': 'Git clone failed.',
    'git_fetch_failed': 'Git fetch failed.',
    'git_worktree_failed': 'Git worktree creation failed.',
    'git_push_failed': 'Git push failed.',
    'codex_exit_nonzero': 'Codex exited with a nonzero status.',
    'codex_start_failed': 'Codex could not be started.',
    'github_http_error': 'A GitHub request failed.',
}


class OperationError(RuntimeError):
    def __init__(self, message, *, code=None, operation=None, stdout=None, stderr=None, returncode=None):
        super().__init__(message)
        self.code, self.operation = code, operation
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def original(exc, operation, context=None):
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({'type': type(current).__name__, 'message': str(current),
                      **{key: getattr(current, key, None) for key in
                         ('operation', 'returncode', 'stdout', 'stderr')}})
        current = current.__cause__ or current.__context__
    return json.dumps({'operation': operation, 'context': context, 'exceptions': chain,
                       'traceback': ''.join(traceback.format_exception(exc))}, ensure_ascii=False,
                      default=lambda v: v.decode('utf-8', errors='replace') if isinstance(v, bytes) else str(v))


def private_directory(path):
    path = Path(path)
    if not path.exists():
        private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    return path


def private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)


def public_field(value, *, repository=False):
    # Conservative publication grammar, plus secret detection for branch/repo names.
    pattern = r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' if repository else r'[A-Za-z0-9][A-Za-z0-9_./-]{0,199}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return '[withheld]'
    # OpenAI's short prefix must start a token, not end a word like "task-".
    # Underscores and Git path separators are token boundaries too; do not use
    # \b here, since it treats underscores as word characters.
    if re.search(r'(?i)(gh[pousr]_|github_pat_|(?<![a-z0-9])sk-|token|password|secret|authorization)', value):
        return '[withheld]'
    if not repository:
        from .input import branch_name
        try:
            branch_name(value)
        except ValueError:
            return '[withheld]'
    return value


def public_error(row, issue, base):
    # Never read error_message here, including legacy plain-text rows.
    code = row['error_code'] if row['error_code'] in REASONS else 'execution_failed'
    stage = row['error_stage'] if row['error_stage'] in {'preparation', 'codex', 'publication', 'recovery'} else 'execution'
    operation = {'no_changes_for_pr': 'git.diff', 'base_branch_not_found': 'git.prepare', 'work_branch_exists': 'git.prepare',
                 'codex_exit_nonzero': 'codex.run', 'codex_start_failed': 'codex.start',
                 'github_http_error': 'github.request'}.get(code)
    if code.startswith('git_'):
        operation = 'git.' + code[4:-7]
    operation = operation or 'executor.' + stage
    execution_id = row['execution_id'] if type(row['execution_id']) is int else 'unavailable'
    return '\n'.join(['Dispatcher execution failed', '', f'Stage: {stage}', f'Operation: {operation}',
        f'Error code: {code}', f"Repository: {public_field(issue['repository'], repository=True)}",
        f'Base branch: {public_field(base)}', f'Execution ID: {execution_id}', '',
        'Reason: ' + REASONS.get(code, 'The operation failed; inspect local diagnostics for the original cause.'),
        'Local diagnostics: saved in SQLite error_log.', 'Local results: preserved where possible.'])
