"""Dispatcher-owned Git clones and conservative terminal cleanup."""
from pathlib import Path
import os
import json
import shlex
import subprocess
from dispatcher.process_wait import run as natural_run


class TaskError(RuntimeError):
    def __init__(self, code, category='DISPATCHER'):
        super().__init__(code)
        self.code, self.category = code, category


def safe_path(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if '..' in path.parts or not path.is_relative_to(root) or path == root:
        raise TaskError('unsafe_path')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise TaskError('unsafe_path')
    return path


def private_dir(root, path):
    path = safe_path(root, path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while current != Path(root):
        current.chmod(0o700)
        current = current.parent
    return path


def private_file(path, mode='w'):
    import os
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, mode)


def register_artifacts(root, task, *names):
    """Persist task-local ownership before creating dispatcher temporary files."""
    registry = safe_path(root, task / '.dispatcher-artifacts.jsonl')
    for name in names:
        if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
            raise TaskError('unsafe_path')
        path = safe_path(root, task / name)
        if path.exists() or name in ('worktree', 'attachments', registry.name):
            raise TaskError('artifact_already_exists')
    fd = os.open(registry, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        for name in names:
            stream.write(json.dumps(name) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


class GitWorkspaceManager:
    def __init__(self, config, lifecycle=None, execution=None):
        self.config = config
        self.operation_guard = None
        self.lifecycle, self.execution = lifecycle, execution
        self.operation_attempt = None
        self.cleanup_lock_fd = None

    def run(self, argv, **kwargs):
        """Register task helpers before exec; Lifecycle alone reaps their groups."""
        allow_stopped = kwargs.pop('_allow_stopped', False)
        if self.cleanup_lock_fd is not None:
            kwargs['pass_fds'] = (self.cleanup_lock_fd,)
        if not self.lifecycle or not self.operation_attempt:
            return natural_run(argv, **kwargs)
        import tempfile
        import time
        from dispatcher.runtime import identity
        from dispatcher.storage import now
        timeout = kwargs.pop('timeout', 120)
        text = kwargs.pop('text', False)
        kwargs.pop('capture_output', None)
        process_id = None
        def registered(child):
            nonlocal process_id
            with self.execution.db as db:
                db.execute('BEGIN IMMEDIATE')
                task_attempt = self.execution.get(self.operation_attempt)
                task = self.execution.store.get(task_attempt['repository'], task_attempt['number'])
                if self.execution.store.stop_requested(task) and not allow_stopped:
                    raise RuntimeError('Task detached before helper launch')
                process_id = db.execute('''INSERT INTO attempt_processes(attempt_id,pid,process_identity,started_at)
                    VALUES(?,?,?,?)''', (self.operation_attempt, child.pid, identity(child.pid), now())).lastrowid
        # Files avoid pipe deadlocks without communicate(), which reaps the leader.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                child = self.lifecycle.submit(argv, service=True, before_exec=registered, during_drain=True,
                    stdout=stdout, stderr=stderr, **kwargs)
            except RuntimeError:
                task_attempt = self.execution.get(self.operation_attempt)
                if self.execution.store.stop_requested(self.execution.store.get(task_attempt['repository'], task_attempt['number'])):
                    raise TaskError('task_detached', 'DISPATCHER') from None
                raise
            deadline = time.monotonic() + timeout
            expired = False
            while child in self.lifecycle._active():
                if self.lifecycle.forced:
                    self.lifecycle.cancel()
                if time.monotonic() >= deadline:
                    expired = True
                time.sleep(.01)
            with self.execution.db as db:
                db.execute('UPDATE attempt_processes SET finished_at=?,exit_code=? WHERE process_id=?',
                           (now(), child.returncode, process_id))
            if expired:
                raise subprocess.TimeoutExpired(argv, timeout)
            stdout.seek(0)
            stderr.seek(0)
            out, err = stdout.read(), stderr.read()
            return subprocess.CompletedProcess(argv, child.returncode,
                out.decode(errors='replace') if text else out, err.decode(errors='replace') if text else err)

    def git(self, cwd, *args, check=True):
        if self.operation_guard:
            self.operation_guard()
        try:
            environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
            environment.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes')
            helper = '!' + shlex.quote(self.config.github_executable) + ' auth git-credential'
            result = self.run(['git', '-c', 'credential.helper=', '-c',
                                     'credential.helper=' + helper, '-C', str(cwd), *args], capture_output=True,
                                    text=True, timeout=120, env=environment, stdin=subprocess.DEVNULL,
                                    _allow_stopped=bool(args and (args[0] in ('rev-parse','symbolic-ref','status','merge-base','diff','ls-remote','show-ref') or args[:2] == ('remote','get-url'))))
        except (OSError, subprocess.TimeoutExpired):
            raise TaskError('git_unavailable', 'GIT') from None
        if check and result.returncode:
            from dispatcher.logging import event
            # Classify stderr; never persist arbitrary Git output, URLs or argv.
            reason = 'branch_already_exists' if 'already exists' in result.stderr and args[:2] == ('worktree', 'add') else 'git_command_failed'
            self.config.logs.mkdir(parents=True, exist_ok=True)
            operations = {'worktree', 'branch', 'fetch', 'push', 'rev-parse', 'show-ref',
                          'status', 'symbolic-ref', 'merge-base', 'reflog', 'remote',
                          'commit', 'checkout', 'add', 'diff', 'ls-remote', 'cat-file'}
            operation = next((arg for arg in args if arg in operations), 'git')
            event(self.config.logs / 'dispatcher.jsonl', 'git_failed', level='WARN',
                  action=operation, reason=reason, result=result.returncode)
            raise TaskError('git_operation_failed', 'GIT')
        return result

    def paths(self, project, number):
        if type(number) is not int or number <= 0:
            raise TaskError('unsafe_path')
        root = self.config.workspace
        base = safe_path(root, root / project.id)
        task = safe_path(root, base / 'tasks' / f'issue-{number}')
        return safe_path(root, base / 'repository'), task, safe_path(root, task / 'worktree')

    def default_branch(self, project):
        if self.operation_guard:
            self.operation_guard()
        repo, _, _ = self.paths(project, 1)
        if repo.exists():
            output = self.git(repo, 'ls-remote', '--symref', 'origin', 'HEAD').stdout
            for line in output.splitlines():
                if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
                    return line.split('\t')[0].removeprefix('ref: refs/heads/')
            raise TaskError('default_branch_unavailable', 'GIT')
        try:
            import json
            response = self.run([self.config.github_executable, 'repo', 'view', project.repository,
                                       '--json', 'defaultBranchRef'], capture_output=True, text=True, timeout=30)
            value = json.loads(response.stdout)['defaultBranchRef']['name']
            if response.returncode:
                raise ValueError
            self.validate_branch(value)
            return value
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            raise TaskError('default_branch_unavailable', 'GIT') from None

    def branch(self, project, issue):
        branch = issue['target_branch']
        self.validate_branch(branch)
        return f'codex/issue-{issue["number"]}' if branch in (project.base_branch, self.default_branch(project)) else branch

    def validate_branch(self, branch):
        if self.operation_guard:
            self.operation_guard()
        from dispatcher.github import valid_branch
        if not valid_branch(branch):
            raise TaskError('invalid_branch', 'GIT')
        result = self.run(['git', 'check-ref-format', '--branch', branch], capture_output=True, timeout=10)
        if result.returncode:
            raise TaskError('invalid_branch', 'GIT')

    def prepare(self, project, issue, branch):
        repo, task, worktree = self.paths(project, issue['number'])
        private_dir(self.config.workspace, task)
        if issue.get('retry_sha') or issue.get('repeat_branch'):
            if issue.get('retry_sha'):
                self.retry_source(project, issue['number'], issue['retry_sha'])
            if worktree.exists():
                if self.git(worktree, 'status', '--porcelain', '--untracked-files=all', '--ignored').stdout.strip():
                    raise TaskError('retry_workspace_conflict', 'GIT')
                preserved = safe_path(self.config.workspace, task / ('previous-' + issue['previous_attempt']))
                register_artifacts(self.config.workspace, task, preserved.name)
                self.git(repo, 'worktree', 'move', str(worktree), str(preserved))
            attachments = safe_path(self.config.workspace, task / 'attachments')
            if attachments.exists():
                archived = safe_path(self.config.workspace, task / ('attachments-' + issue['previous_attempt']))
                if archived.exists():
                    raise TaskError('retry_workspace_conflict', 'GIT')
                register_artifacts(self.config.workspace, task, archived.name)
                attachments.rename(archived)
        if worktree.exists():
            raise TaskError('worktree_already_exists', 'GIT')
        if not repo.exists():
            if self.operation_guard:
                self.operation_guard()
            # gh honors its configured SSH/HTTPS protocol and existing auth.
            try:
                response = self.run([self.config.github_executable, 'repo', 'clone', project.repository,
                                           str(repo), '--', '--no-checkout'], capture_output=True,
                                          timeout=120, stdin=subprocess.DEVNULL,
                                          env=dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0'))
                if response.returncode:
                    raise ValueError
            except (ValueError, OSError, subprocess.TimeoutExpired):
                raise TaskError('clone_url_unavailable', 'GIT') from None
        repo.chmod(0o700)
        try:
            self.git(repo, 'fetch', '--prune', 'origin')
        except TaskError:
            raise TaskError('fetch_failed', 'GIT') from None
        actual_default = self.default_branch(project)
        if branch == actual_default:
            raise TaskError('default_branch_protected', 'GIT')
        remote = f'refs/remotes/origin/{branch}'
        exists = self.git(repo, 'show-ref', '--verify', remote, check=False).returncode == 0
        if issue.get('repeat_branch'):
            if not exists:
                raise TaskError('remote_branch_missing', 'GIT')
            sha = self.git(repo, 'rev-parse', remote).stdout.strip()
            # Detached archives preserve the old commits; never reset the old branch.
            for entry in self.git(repo, 'worktree', 'list', '--porcelain').stdout.split('\n\n'):
                if 'branch refs/heads/' + branch in entry.splitlines():
                    path = Path(entry.splitlines()[0].removeprefix('worktree '))
                    expected = task / ('previous-' + issue['previous_attempt'])
                    if path != expected:
                        raise TaskError('retry_workspace_conflict', 'GIT')
                    self.git(path, 'checkout', '--detach')
            if self.git(repo, 'show-ref', '--verify', 'refs/heads/' + branch, check=False).returncode == 0:
                # Keep the old local ref as a named archive before recreating it.
                self.git(repo, 'branch', '-m', branch, 'archive/' + issue['previous_attempt'])
            self.git(repo, 'worktree', 'add', '-b', branch, str(worktree), sha)
            worktree.chmod(0o700)
            return worktree, sha
        if issue.get('detach_attempt'):
            if self.git(repo, 'rev-parse', '--verify', f'refs/remotes/origin/{project.base_branch}^{{commit}}', check=False).returncode:
                raise TaskError('base_branch_missing', 'GIT')
            self.detach_branch(project, issue, branch, exists)
        if exists and not issue.get('retry_sha'):
            raise TaskError('branch_ownership_conflict', 'GIT')
        if self.git(repo, 'show-ref', '--verify', f'refs/remotes/origin/{project.base_branch}', check=False).returncode:
            raise TaskError('base_branch_missing', 'GIT')
        base = issue.get('retry_sha') or f'refs/remotes/origin/{project.base_branch}'
        if issue.get('retry_sha') and (exists or self.git(repo, 'show-ref', '--verify', 'refs/heads/' + branch, check=False).returncode == 0):
            raise TaskError('retry_workspace_conflict', 'GIT')
        # Never reset/reuse a pre-existing local branch or another task's history.
        self.git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
        worktree.chmod(0o700)
        return worktree, self.git(worktree, 'rev-parse', 'HEAD').stdout.strip()

    def detach_branch(self, project, issue, branch, remote_exists):
        """Archive only a proven, fully cleaned generation; never reuse its HEAD."""
        from dispatcher.detachment import live_processes
        if not self.execution:
            raise TaskError('detach_identity_conflict', 'GIT')
        db = self.execution.db
        previous = self.execution.get(issue['detach_attempt'])
        row = self.execution.store.get(project.repository, issue['number'])
        stop = db.execute('SELECT * FROM stop_requests WHERE attempt_id=? ORDER BY request_id DESC LIMIT 1',
                          (issue['detach_attempt'],)).fetchone()
        repo, task, worktree = self.paths(project, issue['number'])
        if (not previous or not row or row['project_id'] != project.id or
                previous['repository'] != project.repository.casefold() or previous['number'] != issue['number'] or
                previous['branch'] != branch or row['work_branch'] != branch or not previous['initial_sha'] or
                previous['worktree'] != str(worktree) or previous['state'] == 'running' or
                previous['iteration_id'] == row['current_iteration_id'] or not stop or stop['active'] or
                stop['issue_id'] != row['issue_id'] or stop['iteration_id'] != previous['iteration_id'] or
                not stop['finalized_at'] or stop['process_status'] != 'confirmed'):
            raise TaskError('detach_identity_conflict', 'GIT')
        cleanup = db.execute("""SELECT 1 FROM cleanup_jobs WHERE issue_id=? AND attempt_id=?
            AND generation=? AND version=? AND state='completed'""",
            (row['issue_id'], previous['attempt_id'], stop['iteration_id'], stop['version'])).fetchone()
        if (not cleanup or live_processes(self.execution.store, previous) or
                db.execute('SELECT 1 FROM attempt_processes WHERE attempt_id=? AND finished_at IS NULL',
                           (previous['attempt_id'],)).fetchone() or worktree.exists()):
            raise TaskError('detach_cleanup_incomplete', 'GIT')
        if (not previous['origin_url'] or self.git(repo, 'remote', 'get-url', 'origin').stdout.strip() != previous['origin_url'] or
                Path(self.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()) != repo / '.git'):
            raise TaskError('detach_identity_conflict', 'GIT')
        if db.execute('SELECT 1 FROM attempts WHERE repository=? AND branch=? AND number!=?',
                      (previous['repository'], branch, issue['number'])).fetchone():
            raise TaskError('detach_branch_ownership_conflict', 'GIT')
        for entry in self.git(repo, 'worktree', 'list', '--porcelain').stdout.split('\n\n'):
            if 'branch refs/heads/' + branch in entry.splitlines() or 'worktree ' + str(worktree) in entry.splitlines():
                raise TaskError('detach_active_worktree', 'GIT')
        if remote_exists:
            raise TaskError('detach_remote_branch_conflict', 'GIT')
        if db.execute('SELECT 1 FROM task_pull_requests WHERE issue_id=?', (row['issue_id'],)).fetchone() or previous['pushed']:
            raise TaskError('detach_published_result_conflict', 'GIT')
        ref = 'refs/heads/' + branch
        local = self.git(repo, 'rev-parse', '--verify', ref, check=False)
        sha = local.stdout.strip() if local.returncode == 0 else None
        intent = db.execute('SELECT * FROM detach_refs WHERE attempt_id=?', (previous['attempt_id'],)).fetchone()
        if intent is None:
            # Compatibility with already completed cleanup from older versions.
            # The recorded initial commit and clone identity must prove lineage.
            if sha:
                history = self.git(repo, 'reflog', 'show', '--format=%H%x00%gs', ref).stdout.splitlines()
                created = previous['initial_sha'] + '\0branch: Created from refs/remotes/origin/' + project.base_branch
                if (not history or history[-1] != created or
                        any('\0commit' not in entry for entry in history[:-1]) or
                        self.git(repo, 'merge-base', '--is-ancestor', previous['initial_sha'], sha, check=False).returncode):
                    raise TaskError('detach_branch_ownership_conflict', 'GIT')
            archive = 'archive/detach/' + previous['attempt_id']
            if self.git(repo, 'show-ref', '--verify', 'refs/heads/' + archive, check=False).returncode == 0:
                raise TaskError('detach_archive_conflict', 'GIT')
            with db:
                db.execute('INSERT INTO detach_refs VALUES(?,?,?,?)', (previous['attempt_id'], branch, sha, archive))
            intent = db.execute('SELECT * FROM detach_refs WHERE attempt_id=?', (previous['attempt_id'],)).fetchone()
        if intent['branch'] != branch:
            raise TaskError('detach_identity_conflict', 'GIT')
        archived = self.git(repo, 'rev-parse', '--verify', 'refs/heads/' + intent['archive'], check=False)
        archived_sha = archived.stdout.strip() if archived.returncode == 0 else None
        if archived_sha:
            if archived_sha != intent['sha'] or sha is not None:
                raise TaskError('detach_archive_conflict', 'GIT')
            return  # Rename committed before a crash; do not archive twice.
        if sha != intent['sha']:
            raise TaskError('detach_branch_ownership_conflict', 'GIT')
        if sha:
            self.git(repo, 'branch', '-m', branch, intent['archive'])

    def retry_source(self, project, number, sha):
        import re
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40,64}', sha):
            raise TaskError('retry_sha_unavailable', 'GIT')
        repo, _, _ = self.paths(project, number)
        if not repo.exists():
            raise TaskError('retry_sha_unavailable', 'GIT')
        if self.git(repo, 'cat-file', '-e', sha + '^{commit}', check=False).returncode:
            self.git(repo, 'fetch', 'origin', sha, check=False)
        if self.git(repo, 'cat-file', '-e', sha + '^{commit}', check=False).returncode:
            raise TaskError('retry_sha_unavailable', 'GIT')

    def verify(self, worktree, branch, initial_sha, noop=False):
        safe_path(self.config.workspace, worktree)
        if self.git(worktree, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != branch:
            raise TaskError('unexpected_branch', 'GIT')
        if self.git(worktree, 'status', '--porcelain').stdout.strip():
            raise TaskError('dirty_worktree', 'GIT')
        sha = self.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if sha == initial_sha and not noop:
            raise TaskError('no_commit', 'CODEX')
        if self.git(worktree, 'merge-base', '--is-ancestor', initial_sha, sha, check=False).returncode:
            raise TaskError('rewritten_history', 'GIT')
        return sha

    def publication_commit(self, worktree, branch, initial_sha, base, *, salvage=False, before_commit=None):
        safe_path(self.config.workspace, worktree)
        if self.git(worktree, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != branch:
            raise TaskError('unexpected_branch', 'GIT')
        if self.git(worktree, 'status', '--porcelain').stdout.strip():
            self.git(worktree, 'add', '--all')
            if before_commit:
                before_commit()
            self.git(worktree, 'commit', '-m', 'Implement task')
        sha = self.verify(worktree, branch, initial_sha, noop=True)
        if self.execution and self.operation_attempt:
            self.execution.update(self.operation_attempt, result_sha=sha)
        if salvage:
            return sha
        self.validate_branch(base)
        self.git(worktree, 'fetch', 'origin', f'+refs/heads/{base}:refs/remotes/origin/{base}')
        diff = self.git(worktree, 'diff', '--quiet', f'refs/remotes/origin/{base}...{sha}', check=False)
        if diff.returncode == 0:
            raise TaskError('no_diff', 'GIT')
        if diff.returncode != 1:
            raise TaskError('git_operation_failed', 'GIT')
        return sha

    def push(self, project, worktree, branch, expected_origin=None, *, before_push=None):
        safe_path(self.config.workspace, worktree)
        if expected_origin is not None and self.git(worktree, 'remote', 'get-url', 'origin').stdout.strip() != expected_origin:
            raise TaskError('origin_changed', 'GIT')
        self.validate_branch(branch)
        if branch in (project.base_branch, self.default_branch(project)):
            raise TaskError('default_branch_protected', 'GIT')
        if before_push:
            before_push()
        result = self.git(worktree, 'push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
        if result.returncode:
            rejected = any(token in result.stderr.lower() for token in ('non-fast-forward', 'fetch first'))
            raise TaskError('non_fast_forward' if rejected else 'git_operation_failed', 'GIT')
        if self.execution and self.operation_attempt:
            self.execution.update(self.operation_attempt, pushed=1)


class TaskWorkspaceCleanup:
    """Remove verified artifacts only after a persisted terminal decision."""
    def __init__(self, manager):
        self.manager = manager

    def cleanup(self, project, row):
        try:
            return self._cleanup(project, row)
        except (TaskError, OSError) as exc:
            from dispatcher.logging import event
            event(self.manager.config.logs / 'dispatcher.jsonl', 'cleanup_failed', level='WARN',
                  project=project.id, task=row['number'] if type(row['number']) is int else None,
                  reason=exc.code if isinstance(exc, TaskError) else 'cleanup_preserved_artifacts')
            raise

    def _cleanup(self, project, row):
        if row['state'] not in ('done', 'cancelled', 'detached') and not (row['state'] == 'review_on_error' and dict(row).get('publication_confirmed') is True):
            raise TaskError('cleanup_requires_done')
        repo, task, worktree = self.manager.paths(project, row['number'])
        root = self.manager.config.workspace
        # A positive /proc observation is a veto, never a completeness proof.
        for entry in Path('/proc').glob('[0-9]*/cwd'):
            try:
                if entry.resolve(strict=True).is_relative_to(task):
                    raise TaskError('cleanup_active_execution')
            except FileNotFoundError:
                pass
            except PermissionError:
                # Unrelated protected processes are outside the registry contract.
                # Registered-process uncertainty is checked by CleanupService.
                pass
        archived = dict(row).get('owned_worktrees', ())
        if any(not isinstance(name, str) or not name.startswith('previous-')
               or Path(name).name != name for name in archived):
            raise TaskError('cleanup_invalid_registry')
        archived_attachments = dict(row).get('owned_attachments', ())
        if any(not isinstance(name, str) or not name.startswith('attachments-')
               or Path(name).name != name for name in archived_attachments):
            raise TaskError('cleanup_invalid_registry')
        worktrees = [worktree] + [safe_path(root, task / name) for name in archived]
        registered = self.registered(repo)
        for registered_path in registered:
            candidate = Path(registered_path)
            if candidate.is_relative_to(task) and candidate not in worktrees:
                raise TaskError('cleanup_ownership_unproven')
        self.verify_metadata(repo, worktrees)
        if not task.exists():
            # A crash may leave Git metadata after the worktree disappeared.
            for path in worktrees:
                if str(path) in self.registered(repo):
                    self.manager.git(repo, 'worktree', 'remove', *(['--force'] if row['state'] == 'detached' else []), str(path))
            return
        # Preflight everything before removing the worktree or any artifacts.
        for path in task.rglob('*'):
            safe_path(root, path)
        registry = task / '.dispatcher-artifacts.jsonl'
        owned = set()
        if registry.exists():
            try:
                entries = [json.loads(line) for line in registry.read_text().splitlines()]
                if any(not isinstance(name, str) for name in entries):
                    raise ValueError
                owned = set(entries)
            except (ValueError, TypeError):
                raise TaskError('cleanup_invalid_registry') from None
            for name in owned:
                if (not isinstance(name, str) or Path(name).name != name
                        or name in ('', '.', '..', 'worktree', 'attachments', registry.name)):
                    raise TaskError('cleanup_invalid_registry')
                safe_path(root, task / name)
        # Archive entries are write-ahead ownership intents; SQLite history must
        # independently confirm their attempt before treating them as directories.
        archive_names = set(archived) | set(archived_attachments)
        if any(name.startswith(('previous-', 'attachments-')) and name not in archive_names for name in owned):
            raise TaskError('cleanup_ownership_unproven')
        owned -= archive_names
        for historical in dict(row).get('owned_attempts', ()):
            import re
            if not isinstance(historical, str) or not re.fullmatch('[0-9a-f]{32}', historical):
                raise TaskError('cleanup_invalid_attempt')
            owned.update((historical + '.prompt', historical + '.codex.log'))
        # Compatibility with pre-registry attempts: only the supplied task attempt.
        attempt = dict(row).get('attempt_id')
        if attempt is not None:
            import re
            if not isinstance(attempt, str) or not re.fullmatch(r'[0-9a-f]{32}', attempt):
                raise TaskError('cleanup_invalid_attempt')
            owned.update((f'{attempt}.prompt', f'{attempt}.codex.log'))
        for path in task.iterdir():
            if path.name not in owned | {'worktree', 'attachments', registry.name} | set(archived) | set(archived_attachments):
                raise TaskError('cleanup_unexpected_file')
            if path.name in owned | {registry.name} and not path.is_file():
                raise TaskError('cleanup_unexpected_file')
        attachment_files = set()
        attachment_dirs = [safe_path(root, task / name) for name in ('attachments', *archived_attachments)]
        for attachments in attachment_dirs:
            manifest = attachments / 'manifest.json'
            if attachments.exists():
                if not attachments.is_dir():
                    raise TaskError('cleanup_unexpected_file')
                if manifest.exists():
                    try:
                        entries = json.loads(manifest.read_text())
                        if not isinstance(entries, list):
                            raise ValueError
                        for index, entry in enumerate(entries, 1):
                            local = entry.get('local_path')
                            if local is None:
                                continue
                            original = safe_path(root, local)
                            path = safe_path(root, attachments / original.name)
                            if original.parent != task / 'attachments':
                                raise ValueError
                            if path.parent != attachments or not path.name.startswith(f'{index:03d}-'):
                                raise ValueError
                            attachment_files.add(path)
                    except (ValueError, TypeError, AttributeError):
                        raise TaskError('cleanup_invalid_manifest') from None
                for path in attachments.iterdir():
                    if path not in attachment_files | {manifest} or not path.is_file():
                        raise TaskError('cleanup_unexpected_file')
        for path in worktrees:
            if row['state'] != 'detached' and path.exists() and self.manager.git(path, 'status', '--porcelain', '--untracked-files=all', '--ignored').stdout.strip():
                raise TaskError('dirty_worktree', 'GIT')
        for path in worktrees:
            if path.exists() or str(path) in self.registered(repo):
                self.verify_metadata(repo, [path])
                self.manager.git(repo, 'worktree', 'remove', *(['--force'] if row['state'] == 'detached' else []), str(path))
        for path in attachment_files:
            safe_path(root, path)
            path.unlink(missing_ok=True)
        for attachments in attachment_dirs:
            safe_path(root, attachments / 'manifest.json').unlink(missing_ok=True)
            if attachments.exists():
                attachments.rmdir()
        for name in owned:
            safe_path(root, task / name).unlink(missing_ok=True)
        safe_path(root, registry).unlink(missing_ok=True)
        task.rmdir()


    def registered(self, repo):
        if not repo.exists():
            return set()
        output = self.manager.git(repo, 'worktree', 'list', '--porcelain', '-z').stdout
        return {part.removeprefix('worktree ') for part in output.split('\0') if part.startswith('worktree ')}

    def verify_metadata(self, repo, paths):
        root = self.manager.config.workspace
        safe_path(root, repo)
        safe_path(root, repo / '.git')
        registered = self.registered(repo)
        for path in paths:
            safe_path(root, path)
            if not path.exists() and str(path) not in registered:
                continue
            if str(path) not in registered:
                raise TaskError('cleanup_git_ownership_mismatch')
            common = self.manager.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            # Repository clone must have its own local metadata, never another
            # clone's gitdir or a symlink to a user repository.
            if Path(common) != repo / '.git':
                raise TaskError('cleanup_git_ownership_mismatch')
            if path.exists():
                dotgit = safe_path(root, path / '.git')
                if not dotgit.is_file():
                    raise TaskError('cleanup_git_ownership_mismatch')
                content = dotgit.read_text().strip()
                if not content.startswith('gitdir: '):
                    raise TaskError('cleanup_git_ownership_mismatch')
                metadata = safe_path(root, content.removeprefix('gitdir: '))
                if metadata.parent != repo / '.git' / 'worktrees':
                    raise TaskError('cleanup_git_ownership_mismatch')
                pointer = safe_path(root, metadata / 'gitdir')
                if pointer.read_text().strip() != str(dotgit):
                    raise TaskError('cleanup_git_ownership_mismatch')
                actual = self.manager.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                if actual != common:
                    raise TaskError('cleanup_git_ownership_mismatch')
            else:
                # Missing directory: require a reciprocal metadata registration.
                metadata_root = safe_path(root, repo / '.git' / 'worktrees')
                matches = []
                for metadata in metadata_root.iterdir():
                    pointer = safe_path(root, metadata / 'gitdir')
                    if pointer.is_file() and pointer.read_text().strip() == str(path / '.git'):
                        matches.append(metadata)
                if len(matches) != 1:
                    raise TaskError('cleanup_git_ownership_mismatch')
