"""Dispatcher-owned Git clones and conservative, done-only cleanup."""
from pathlib import Path
import os
import shutil
import shlex
import subprocess


class TaskError(RuntimeError):
    def __init__(self, code, category='DISPATCHER'):
        super().__init__(code)
        self.code, self.category = code, category


def safe_path(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or path == root:
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
        base = remote if exists else f'refs/remotes/origin/{actual_default}'
        # Never reset/reuse a pre-existing local branch or another task's history.
        self.git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
        worktree.chmod(0o700)
        return worktree, self.git(worktree, 'rev-parse', 'HEAD').stdout.strip()

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

    def push(self, project, worktree, branch, expected_origin=None):
        safe_path(self.config.workspace, worktree)
        if expected_origin is not None and self.git(worktree, 'remote', 'get-url', 'origin').stdout.strip() != expected_origin:
            raise TaskError('origin_changed', 'GIT')
        self.validate_branch(branch)
        if branch in (project.base_branch, self.default_branch(project)):
            raise TaskError('default_branch_protected', 'GIT')
        self.git(worktree, 'push', 'origin', f'HEAD:refs/heads/{branch}')


class TaskWorkspaceCleanup:
    """Next stage calls cleanup only after the persisted acceptance decision."""
    def __init__(self, manager):
        self.manager = manager

    def cleanup(self, project, row):
        try:
            return self._cleanup(project, row)
        except (TaskError, OSError):
            from dispatcher.logging import event
            event(self.manager.config.logs / 'dispatcher.jsonl', 'cleanup_failed', level='WARN',
                  project=project.id, task=row['number'] if type(row['number']) is int else None,
                  reason='cleanup_preserved_artifacts')
            raise

    def _cleanup(self, project, row):
        if row['state'] != 'done':
            raise TaskError('cleanup_requires_done')
        repo, task, worktree = self.manager.paths(project, row['number'])
        # Check the entire tree before deleting anything, including attachments.
        if task.exists():
            for path in task.rglob('*'):
                safe_path(self.manager.config.workspace, path)
        if worktree.exists():
            if self.manager.git(worktree, 'status', '--porcelain').stdout.strip():
                raise TaskError('dirty_worktree', 'GIT')
            self.manager.git(repo, 'worktree', 'remove', str(worktree))
        attachments = safe_path(self.manager.config.workspace, task / 'attachments')
        if attachments.exists():
            shutil.rmtree(attachments)
        if task.exists():
            task.rmdir()
