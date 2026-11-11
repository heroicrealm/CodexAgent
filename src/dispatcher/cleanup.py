"""Known-resource recovery and explicit orphan maintenance, without schema changes."""
from dataclasses import dataclass
from pathlib import Path
from .locking import InstanceLock
from .storage import Storage


@dataclass(frozen=True)
class CleanupResult:
    path: str
    safe: bool
    reason: str
    removed: bool = False


class Cleanup:
    def __init__(self, config, storage, git):
        self.config, self.storage, self.git = config, storage, git
        self._removed_branches = set()

    def referenced(self):
        return {Path(r['worktree_path']).resolve() for r in self.storage.rows(
            'SELECT worktree_path FROM executions WHERE worktree_path IS NOT NULL')}

    def orphan_check(self, project, path):
        if Path(path).resolve() in self.referenced():
            return False, 'referenced-by-sqlite'
        if self.storage.rows('SELECT 1 FROM recovery_quarantine LIMIT 1'):
            return False, 'recovery-quarantine'
        if self.storage.rows("SELECT 1 FROM issues WHERE state IN ('preparing','running')"):
            return False, 'unfinished-execution'
        return self.git.safe_worktree(project, path)

    def orphans(self, *, apply=False, project_id=None):
        # Validate before any mutation or Git operation. A missing DB is not evidence
        # of an orphan: maintenance must not silently initialize one.
        if not self.config.database.is_file() or self.config.database.stat().st_size == 0:
            raise ValueError('Existing V2 SQLite required for maintenance')
        results = []
        with InstanceLock(self.config):
            if apply:
                from .db_guard import check
                check(self.config, execution=True)
            Storage(self.config.database)
            for project in self.config.projects:
                if not project.enabled:
                    continue
                if project_id and project.id != project_id:
                    continue
                try:
                    entries = self.git.worktrees(project)
                except Exception:
                    results.append(CleanupResult(str(project.checkout or self.config.workspace / 'repos' / project.id),
                                                 False, 'project-inspection-failed'))
                    continue
                for entry in entries:
                    path = entry['worktree']
                    try:
                        # Dry-run does not fetch or alter Git refs.
                        safe, reason = self.orphan_check(project, path) if apply else self._dry_check(project, path)
                        removed = False
                        if apply and safe:
                            safe, reason = self.orphan_check(project, path)
                            if safe:
                                self.git.remove_worktree(project, path)
                                removed = True
                        results.append(CleanupResult(path, safe, reason, removed))
                    except Exception:
                        results.append(CleanupResult(path, False, 'inspection-or-removal-failed'))
        return results

    def _dry_check(self, project, path):
        if Path(path).resolve() in self.referenced():
            return False, 'referenced-by-sqlite'
        if self.storage.rows('SELECT 1 FROM recovery_quarantine LIMIT 1'):
            return False, 'recovery-quarantine'
        if self.storage.rows("SELECT 1 FROM issues WHERE state IN ('preparing','running')"):
            return False, 'unfinished-execution'
        return self.git.safe_worktree(project, path, refresh=False)

    def known(self):
        """Caller holds the runtime lock. Never scan unknown directories."""
        for project in self.config.projects:
            if not project.enabled:
                continue
            rows = self.storage.rows('SELECT e.*,i.state,i.execution_group_id FROM executions e '
                'JOIN issues i ON i.issue_id=e.issue_id WHERE lower(i.repository)=?', (project.repository.lower(),))
            blocked = set()
            for row in rows:
                if self.storage.blocked(row['issue_id']):
                    blocked.add(row['execution_group_id'])
                    continue
                if row['worktree_path'] and Path(row['worktree_path']).exists():
                    try:
                        if row['state'] in ('preparing', 'running') or not row['push_completed']:
                            blocked.add(row['execution_group_id'])
                            continue
                        safe, _ = self.git.safe_worktree(project, row['worktree_path'])
                        if safe:
                            self.git.remove_worktree(project, row['worktree_path'])
                        else:
                            blocked.add(row['execution_group_id'])
                    except Exception:
                        blocked.add(row['execution_group_id'])
                        self.storage.record_error(row['issue_id'], 'cleanup_failed', 'cleanup', 'Worktree retained for a safe retry', row['execution_id'])
                elif row['worktree_path'] and not row['push_completed']:
                    blocked.add(row['execution_group_id'])
            for root in self.storage.rows("SELECT * FROM issues WHERE lower(repository)=? AND execution_group_id=issue_id "
                                           "AND state IN ('done','cancelled') AND work_branch IS NOT NULL", (project.repository.lower(),)):
                if self.storage.blocked(root['issue_id']):
                    continue
                if (project.id, root['issue_id']) in self._removed_branches:
                    continue
                if root['work_branch'] == root['base_branch'] or root['issue_id'] in blocked:
                    continue
                if not self.storage.rows('SELECT 1 FROM executions e JOIN issues i ON i.issue_id=e.issue_id '
                                         'WHERE i.execution_group_id=? AND e.push_completed=1', (root['issue_id'],)):
                    continue  # Never delete a colliding/pre-existing user branch after failed preparation.
                if self.storage.rows("SELECT 1 FROM issues WHERE execution_group_id=? AND state NOT IN ('done','cancelled')", (root['issue_id'],)):
                    continue
                try:
                    # Delete group remote branch only after all local results are safe.
                    expected = {row['result_commit'] for row in rows
                                if row['execution_group_id'] == root['issue_id'] and row['result_commit']}
                    self.git.delete_remote_branch(project, root['work_branch'], expected)
                    self._removed_branches.add((project.id, root['issue_id']))
                except Exception:
                    self.storage.record_error(root['issue_id'], 'branch_cleanup_failed', 'cleanup', 'Group branch retained for retry')
