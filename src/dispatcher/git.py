"""Conservative Git/worktree operations. No force push, rebase or force removal."""
from pathlib import Path
import re
import subprocess
from uuid import uuid4
from .input import branch_name
from .config import is_v1_path


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, config):
        self.config = config

    def run(self, directory, *args, check=True):
        result = subprocess.run(['git', '-C', str(directory), *args], capture_output=True, text=True, timeout=120)
        if check and result.returncode:
            # Never expose raw stderr (may contain credentials or task data).
            raise GitError(f'Git {args[0]} failed')
        return result.stdout.strip() if check else result

    def checkout(self, project, *, create=True):
        path = (project.checkout or self.config.workspace / 'repos' / project.id).resolve()
        if is_v1_path(path):
            raise GitError('V1 checkout is forbidden')
        if not path.exists():
            if not create:
                raise GitError("V2 checkout is missing")
            path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(['git', 'clone', f'git@github.com:{project.repository}.git', str(path)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise GitError('Git clone failed')
        self.run(path, 'rev-parse', '--git-dir')
        return path

    def area(self, project):
        root = self.config.workspace.resolve()
        area = (root / 'worktrees' / project.id).resolve()
        if not area.is_relative_to(root) or is_v1_path(area):
            raise GitError('Managed worktree area escapes workspace')
        return area

    def fetch(self, project):
        path = self.checkout(project)
        self.run(path, 'fetch', 'origin', '+refs/heads/*:refs/remotes/origin/*')
        return path

    def remote_ref(self, checkout, branch):
        branch_name(branch)
        ref = 'refs/remotes/origin/' + branch
        return ref if self.run(checkout, 'show-ref', '--verify', '--quiet', ref, check=False).returncode == 0 else None

    def prepare(self, project, execution_id, base, work):
        checkout = self.fetch(project)
        start = self.remote_ref(checkout, work) or self.remote_ref(checkout, base)
        if not start:
            raise GitError('Base branch not found')
        suffix = f'{execution_id}-{uuid4().hex}'
        path = self.area(project) / ('e-' + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(checkout, 'worktree', 'add', '-b', 'dispatcher-v2/' + suffix, str(path), start)
        return path, self.run(path, 'rev-parse', 'HEAD')

    def publish_manual(self, project, base, work):
        checkout = self.fetch(project)
        ref = self.remote_ref(checkout, work) or self.remote_ref(checkout, base)
        if not ref:
            raise GitError('Base branch not found')
        self.run(checkout, 'push', 'origin', f'{ref}:refs/heads/{branch_name(work)}')
        return self.run(checkout, 'rev-parse', ref)

    def commit_push(self, project, path, work):
        self.run(path, 'add', '--all')
        if self.run(path, 'diff', '--cached', '--quiet', check=False).returncode != 0:
            self.run(path, 'commit', '-m', 'Dispatcher task result')
        commit = self.run(path, 'rev-parse', 'HEAD')
        self.run(path, 'push', 'origin', f'HEAD:refs/heads/{branch_name(work)}')
        return commit

    def committed_file(self, project, worktree, sha, relative):
        """Check a literal tree entry locally, even after worktree cleanup."""
        from .output import SHA
        if not SHA.fullmatch(sha):
            return False
        try:
            path = Path(worktree) if worktree and Path(worktree).is_dir() else self.checkout(project, create=False)
            result = self.run(path, 'ls-tree', '-z', sha, '--', ':(literal)' + relative, check=False)
            if result.returncode:
                return False
            for entry in result.stdout.split('\0'):
                metadata, sep, name = entry.partition('\t')
                if sep and name == relative and metadata.split()[:2] in (
                        ['100644', 'blob'], ['100755', 'blob']):
                    return True
        except (GitError, OSError, subprocess.SubprocessError):
            pass
        return False

    def has_diff(self, project, path, base):
        self.fetch(project)
        return self.run(path, 'diff', '--quiet', f'origin/{branch_name(base)}...HEAD', check=False).returncode == 1

    def worktrees(self, project):
        raw = self.run(self.checkout(project, create=False), 'worktree', 'list', '--porcelain', '-z')
        entries, current = [], {}
        for item in raw.split('\0'):
            if not item:
                if current:
                    entries.append(current)
                    current = {}
            else:
                key, _, value = item.partition(' ')
                current[key] = value
        if current:
            entries.append(current)
        return entries

    def safe_worktree(self, project, path, *, refresh=True):
        """Prove ownership, cleanliness and commit reachability from the remote."""
        path = Path(path)
        canonical = path.resolve()
        area = self.area(project)
        if not canonical.is_relative_to(area) or canonical == area or path.is_symlink():
            return False, 'outside-managed-area'
        if not re.fullmatch(r'e-\d+-[0-9a-f]{32}', canonical.name):
            return False, 'unidentified-resource'
        entries = self.worktrees(project)
        entry = next((v for v in entries if Path(v['worktree']).resolve() == canonical), None)
        if not entry or entry is entries[0] or 'locked' in entry or 'prunable' in entry:
            return False, 'not-removable-worktree'
        expected = 'refs/heads/dispatcher-v2/' + canonical.name[2:]
        if entry.get('branch') != expected:
            return False, 'not-owned-branch'
        if self.run(canonical, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
            return False, 'dirty'
        if refresh:
            self.fetch(project)
        head = self.run(canonical, 'rev-parse', 'HEAD')
        # Current server refs, not stale tracking refs, prove a surviving remote copy.
        advertised = self.run(self.checkout(project, create=False), 'ls-remote', '--heads', 'origin')
        tips = [line.split()[0] for line in advertised.splitlines()]
        if not any(self.run(canonical, 'merge-base', '--is-ancestor', head, tip, check=False).returncode == 0 for tip in tips):
            return False, 'unpublished-commits'
        return True, 'safe'

    def remove_worktree(self, project, path):
        self.run(self.checkout(project), 'worktree', 'remove', str(Path(path).resolve()))

    def delete_remote_branch(self, project, branch, expected_commits):
        checkout = self.checkout(project, create=False)
        branch_name(branch)
        if branch == project.base_branch:
            raise GitError('Refusing to delete project base branch')
        advertised = self.run(checkout, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch)
        if not advertised:
            return
        tip = advertised.split()[0]
        if tip not in expected_commits:
            raise GitError('Branch changed since the confirmed result; retained')
        # Conditional deletion refuses a concurrent change to the confirmed tip.
        self.run(checkout, 'push', 'origin', f'--force-with-lease=refs/heads/{branch}:{tip}', '--delete', branch)
