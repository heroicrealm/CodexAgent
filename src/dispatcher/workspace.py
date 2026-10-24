"""Dispatcher-owned Git clones and conservative, done-only cleanup."""
from pathlib import Path
import os
import json
import shlex
import subprocess


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
    def __init__(self, config):
        self.config = config

    def git(self, cwd, *args, check=True):
        try:
            environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
            environment.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes')
            helper = '!' + shlex.quote(self.config.github_executable) + ' auth git-credential'
            result = subprocess.run(['git', '-c', 'credential.helper=', '-c',
                                     'credential.helper=' + helper, '-C', str(cwd), *args], capture_output=True,
                                    text=True, timeout=120, env=environment, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise TaskError('git_unavailable', 'GIT') from None
        if check and result.returncode:
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
        repo, _, _ = self.paths(project, 1)
        if repo.exists():
            output = self.git(repo, 'ls-remote', '--symref', 'origin', 'HEAD').stdout
            for line in output.splitlines():
                if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
                    return line.split('\t')[0].removeprefix('ref: refs/heads/')
            raise TaskError('default_branch_unavailable', 'GIT')
        try:
            import json
            response = subprocess.run([self.config.github_executable, 'repo', 'view', project.repository,
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
        from dispatcher.github import valid_branch
        if not valid_branch(branch):
            raise TaskError('invalid_branch', 'GIT')
        result = subprocess.run(['git', 'check-ref-format', '--branch', branch], capture_output=True, timeout=10)
        if result.returncode:
            raise TaskError('invalid_branch', 'GIT')

    def prepare(self, project, issue, branch):
        repo, task, worktree = self.paths(project, issue['number'])
        private_dir(self.config.workspace, task)
        if issue.get('retry_sha'):
            self.retry_source(project, issue['number'], issue['retry_sha'])
            if worktree.exists():
                if self.git(worktree, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
                    raise TaskError('retry_workspace_conflict', 'GIT')
                preserved = safe_path(self.config.workspace, task / ('previous-' + issue['previous_attempt']))
                self.git(repo, 'worktree', 'move', str(worktree), str(preserved))
            attachments = safe_path(self.config.workspace, task / 'attachments')
            if attachments.exists():
                archived = safe_path(self.config.workspace, task / ('attachments-' + issue['previous_attempt']))
                if archived.exists():
                    raise TaskError('retry_workspace_conflict', 'GIT')
                attachments.rename(archived)
        if worktree.exists():
            raise TaskError('worktree_already_exists', 'GIT')
        if not repo.exists():
            # gh honors its configured SSH/HTTPS protocol and existing auth.
            try:
                response = subprocess.run([self.config.github_executable, 'repo', 'clone', project.repository,
                                           str(repo), '--', '--no-checkout'], capture_output=True,
                                          timeout=120, stdin=subprocess.DEVNULL,
                                          env=dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0'))
                if response.returncode:
                    raise ValueError
            except (ValueError, OSError, subprocess.TimeoutExpired):
                raise TaskError('clone_url_unavailable', 'GIT') from None
        repo.chmod(0o700)
        self.git(repo, 'fetch', 'origin')
        actual_default = self.default_branch(project)
        if branch == actual_default:
            raise TaskError('default_branch_protected', 'GIT')
        remote = f'refs/remotes/origin/{branch}'
        exists = self.git(repo, 'show-ref', '--verify', remote, check=False).returncode == 0
        base = issue.get('retry_sha') or (remote if exists else f'refs/remotes/origin/{project.base_branch}')
        if issue.get('retry_sha') and (exists or self.git(repo, 'show-ref', '--verify', 'refs/heads/' + branch, check=False).returncode == 0):
            raise TaskError('retry_workspace_conflict', 'GIT')
        # Never reset/reuse a pre-existing local branch or another task's history.
        self.git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
        worktree.chmod(0o700)
        return worktree, self.git(worktree, 'rev-parse', 'HEAD').stdout.strip()

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

    def publication_commit(self, worktree, branch, initial_sha, base):
        safe_path(self.config.workspace, worktree)
        if self.git(worktree, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != branch:
            raise TaskError('unexpected_branch', 'GIT')
        if self.git(worktree, 'status', '--porcelain').stdout.strip():
            self.git(worktree, 'add', '--all')
            self.git(worktree, 'commit', '-m', 'Implement task')
        sha = self.verify(worktree, branch, initial_sha, noop=True)
        self.validate_branch(base)
        self.git(worktree, 'fetch', 'origin', f'+refs/heads/{base}:refs/remotes/origin/{base}')
        diff = self.git(worktree, 'diff', '--quiet', f'refs/remotes/origin/{base}...{sha}', check=False)
        if diff.returncode == 0:
            raise TaskError('no_diff', 'GIT')
        if diff.returncode != 1:
            raise TaskError('git_operation_failed', 'GIT')
        return sha

    def push(self, project, worktree, branch, expected_origin=None):
        safe_path(self.config.workspace, worktree)
        if expected_origin is not None and self.git(worktree, 'remote', 'get-url', 'origin').stdout.strip() != expected_origin:
            raise TaskError('origin_changed', 'GIT')
        self.validate_branch(branch)
        if branch in (project.base_branch, self.default_branch(project)):
            raise TaskError('default_branch_protected', 'GIT')
        result = self.git(worktree, 'push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
        if result.returncode:
            rejected = any(token in result.stderr.lower() for token in ('non-fast-forward', 'fetch first'))
            raise TaskError('non_fast_forward' if rejected else 'git_operation_failed', 'GIT')


class TaskWorkspaceCleanup:
    """Next stage calls cleanup only after the persisted acceptance decision."""
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
        if row['state'] != 'done':
            raise TaskError('cleanup_requires_done')
        repo, task, worktree = self.manager.paths(project, row['number'])
        root = self.manager.config.workspace
        if not task.exists():
            return
        # Preflight everything before removing the worktree or any artifacts.
        for path in task.rglob('*'):
            safe_path(root, path)
        registry = task / '.dispatcher-artifacts.jsonl'
        owned = set()
        if registry.exists():
            try:
                owned = {json.loads(line) for line in registry.read_text().splitlines()}
            except (ValueError, TypeError):
                raise TaskError('cleanup_invalid_registry') from None
            for name in owned:
                if (not isinstance(name, str) or Path(name).name != name
                        or name in ('', '.', '..', 'worktree', 'attachments', registry.name)):
                    raise TaskError('cleanup_invalid_registry')
                safe_path(root, task / name)
        # Compatibility with pre-registry attempts: only the supplied task attempt.
        attempt = dict(row).get('attempt_id')
        if attempt is not None:
            import re
            if not isinstance(attempt, str) or not re.fullmatch(r'[0-9a-f]{32}', attempt):
                raise TaskError('cleanup_invalid_attempt')
            owned.update((f'{attempt}.prompt', f'{attempt}.codex.log'))
        for path in task.iterdir():
            if path.name not in owned | {'worktree', 'attachments', registry.name}:
                raise TaskError('cleanup_unexpected_file')
            if path.name in owned | {registry.name} and not path.is_file():
                raise TaskError('cleanup_unexpected_file')
        attachments = safe_path(root, task / 'attachments')
        attachment_files = set()
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
                        path = safe_path(root, local)
                        if path.parent != attachments or not path.name.startswith(f'{index:03d}-'):
                            raise ValueError
                        attachment_files.add(path)
                except (ValueError, TypeError, AttributeError):
                    raise TaskError('cleanup_invalid_manifest') from None
            for path in attachments.iterdir():
                if path not in attachment_files | {manifest} or not path.is_file():
                    raise TaskError('cleanup_unexpected_file')
        if worktree.exists():
            if self.manager.git(worktree, 'status', '--porcelain', '--untracked-files=all', '--ignored').stdout.strip():
                raise TaskError('dirty_worktree', 'GIT')
            self.manager.git(repo, 'worktree', 'remove', str(worktree))
        for path in attachment_files:
            path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        if attachments.exists():
            attachments.rmdir()
        for name in owned:
            (task / name).unlink(missing_ok=True)
        registry.unlink(missing_ok=True)
        task.rmdir()
