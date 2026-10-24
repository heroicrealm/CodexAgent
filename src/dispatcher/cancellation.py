"""Durable delivery of verified cancellation decisions, including after restart."""
import fcntl
from pathlib import Path

from dispatcher.github import GitHubError
from dispatcher.workspace import GitWorkspaceManager, TaskWorkspaceCleanup, TaskError


class CancellationDelivery:
    def __init__(self, config, store, adapter):
        self.config, self.store, self.adapter = config, store, adapter

    def poll(self, project):
        db = self.store.db
        rows = db.execute('''SELECT t.*,c.issue_closed,c.cleaned FROM cancellations c
            JOIN tasks t USING(issue_id) WHERE t.repository=? AND t.state='cancelled'
            AND (c.issue_closed=0 OR c.cleaned=0)''', (project.repository.casefold(),)).fetchall()
        errors = []
        for row in rows:
            database = Path(db.execute('PRAGMA database_list').fetchone()[2])
            with (database.parent / f'cancel-{row["issue_id"]}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # Re-read after acquiring the lock: another poll may have delivered it.
                saved = db.execute('SELECT * FROM cancellations WHERE issue_id=?', (row['issue_id'],)).fetchone()
                try:
                    if not saved['issue_closed']:
                        endpoint = f'repos/{project.repository}/issues/{row["number"]}'
                        issue = self.adapter.request(endpoint)
                        if issue['id'] != row['issue_id']:
                            raise GitHubError()
                        if issue['state'] != 'closed' or issue.get('state_reason') != 'not_planned':
                            self.adapter.write(endpoint, 'PATCH', dict(state='closed', state_reason='not_planned'))
                            issue = self.adapter.request(endpoint)
                        if (issue['id'] != row['issue_id'] or issue['state'] != 'closed'
                                or issue.get('state_reason') != 'not_planned'):
                            raise GitHubError()
                        with db:
                            db.execute('UPDATE cancellations SET issue_closed=1 WHERE issue_id=?', (row['issue_id'],))
                    if not saved['cleaned']:
                        self.cleanup(project, row)
                        with db:
                            db.execute('UPDATE cancellations SET cleaned=1 WHERE issue_id=?', (row['issue_id'],))
                    with db:
                        db.execute('UPDATE cancellations SET error=NULL WHERE issue_id=?', (row['issue_id'],))
                except (GitHubError, TaskError, OSError, KeyError, TypeError, ValueError):
                    with db:
                        db.execute("UPDATE cancellations SET error='delivery_failed' WHERE issue_id=?", (row['issue_id'],))
                    errors.append(row['number'])
        return errors

    def cleanup(self, project, row):
        manager = GitWorkspaceManager(self.config)
        repo, task, worktree = manager.paths(project, row['number'])
        if not task.exists():
            return
        owned = self.store.db.execute('''SELECT DISTINCT a.attempt_id,a.branch,a.result_sha FROM attempts a
            JOIN task_iterations i ON i.iteration_id=a.iteration_id
            JOIN iteration_pr_history h ON h.iteration_id=i.iteration_id
            JOIN task_pull_requests p ON p.pr_id=h.pr_id
            WHERE i.issue_id=? AND a.branch=p.head AND a.result_sha=p.head_sha''',
            (row['issue_id'],)).fetchall()
        archived = []
        candidates = [(worktree, None)] + [(task / ('previous-' + a['attempt_id']), a['attempt_id']) for a in owned]
        for path, attempt in candidates:
            if not path.exists():
                continue
            # Reject symlinks before running git against any task-local path.
            from dispatcher.workspace import safe_path
            safe_path(self.config.workspace, path)
            branch = manager.git(path, 'symbolic-ref', '--short', 'HEAD').stdout.strip()
            sha = manager.git(path, 'rev-parse', 'HEAD').stdout.strip()
            common = manager.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            expected = manager.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            if common != expected or not any(a['branch'] == branch and a['result_sha'] == sha
                                            and (attempt is None or attempt == a['attempt_id']) for a in owned):
                raise TaskError('cleanup_ownership_unproven')
            if attempt:
                archived.append(path.name)
        # Branches are preserved: legacy attempts have no proof that the remote
        # ref was created by the dispatcher, rather than supplied by a human.
        TaskWorkspaceCleanup(manager).cleanup(project, dict(row, owned_worktrees=archived,
            owned_attachments=['attachments-' + a['attempt_id'] for a in owned]))
