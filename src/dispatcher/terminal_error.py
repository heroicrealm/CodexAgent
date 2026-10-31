"""Terminal decisions are independent of publication and GitHub delivery.

Called under the publication worker's flock. No network operation holds a SQLite
transaction. Only a verified merged canonical PR proves reopen impossible;
HTTP errors alone never select this path.
"""
from pathlib import Path
import re

from dispatcher.storage import now
from dispatcher.workspace import TaskError, GitWorkspaceManager
from dispatcher.github import GitHubError


def canonical_merged(pr, prior, task, attempt, project):
    if (not isinstance(pr, dict) or pr.get('id') != prior['pr_id']
            or pr.get('number') != prior['pr_number']
            or prior['issue_id'] != task['issue_id']
            or prior['repository_id'] != task['repository_id']
            or prior['head'] != attempt['branch'] or prior['base'] != project.base_branch
            or pr.get('head', {}).get('ref') != prior['head']
            or pr.get('base', {}).get('ref') != prior['base']
            or pr.get('head', {}).get('repo', {}).get('id') != task['repository_id']
            or pr.get('base', {}).get('repo', {}).get('id') != task['repository_id']):
        raise TaskError('pr_identity_conflict', 'GITHUB')
    from dispatcher.pr_monitor import utc
    if pr.get('merged') is True and pr.get('state') == 'closed' and pr.get('merged_at'):
        utc(pr['merged_at'])
        return True
    return False


class TerminalError:
    def __init__(self, publisher):
        self.worker = publisher
        self.store, self.execution = publisher.store, publisher.execution

    def saved(self, attempt):
        return self.store.db.execute('SELECT * FROM terminal_errors WHERE attempt_id=?',
                                     (attempt['attempt_id'],)).fetchone()

    def record(self, attempt, prior, *, preparation=None):
        with self.store.db as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.store.get(attempt['repository'], attempt['number'])
            if self.saved(attempt):
                return
            expected = preparation if preparation is not None else attempt
            expected_iteration = expected['current_iteration_id'] if preparation is not None else attempt['iteration_id']
            if (task['state'] != ('pending' if preparation is not None else 'running') or task['version'] != expected['version']
                    or task['current_iteration_id'] != expected_iteration):
                raise TaskError('publication_conflict', 'GITHUB')
            db.execute('''INSERT INTO terminal_errors(issue_id,attempt_id,iteration_id,version,
                reason,pr_number,pr_id,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                (task['issue_id'], attempt['attempt_id'], expected_iteration, task['version'],
                 'canonical_pr_merged', prior['pr_number'], prior['pr_id'], now()))
            if not self.store._set_state(task, 'review_on_error', None):
                raise TaskError('publication_conflict', 'GITHUB')
            self.store._record_transition(task, 'review_on_error', 'terminal:' + attempt['attempt_id'],
                'publisher', dict(reason='canonical_pr_merged', pr_id=prior['pr_id']))
            self.store._history(task['repository'], task['number'], task['version'],
                                'review_on_error', task['snapshot'], now())

        if isinstance(self.worker.git, GitWorkspaceManager):
            from dispatcher.logging import event
            config = self.worker.git.config
            config.logs.mkdir(parents=True, exist_ok=True)
            event(config.logs / 'dispatcher.jsonl', 'review_on_error', project=task['project_id'],
                  task=task['number'], reason='canonical_pr_merged', attempt=attempt['attempt_id'])

    def drain(self, project, attempt):
        from dispatcher.detachment import fence
        saved = self.saved(attempt)
        guarded_attempt = dict(attempt, iteration_id=saved['iteration_id'], version=saved['version'])
        fence(self.store, self.worker.adapter, project, guarded_attempt)
        task = self.store.get(attempt['repository'], attempt['number'])
        if (task['state'] != 'review_on_error' or task['version'] != saved['version']
                or task['current_iteration_id'] != saved['iteration_id']
                or self.worker.adapter.repository_identity(project) != task['repository_id']):
            raise TaskError('publication_conflict', 'GITHUB')
        if saved['publication'] != 'confirmed':
            git, path = self.worker.git, Path(attempt['worktree'])
            # This path deliberately ignores PR diff: even a no-diff result must
            # be preserved. Git verifies branch, ancestry and configured origin.
            fence(self.store, self.worker.adapter, project, guarded_attempt)
            sha = git.publication_commit(path, attempt['branch'], attempt['initial_sha'],
                                         project.base_branch, salvage=True,
                                         before_commit=lambda: fence(self.store, self.worker.adapter, project, guarded_attempt))
            self.execution.update(attempt['attempt_id'], result_sha=sha)
            remote = git.git(path, 'ls-remote', 'origin', 'refs/heads/' + attempt['branch']).stdout.split()
            if not remote or remote[0] not in (attempt['initial_sha'], sha):
                raise TaskError('remote_head_changed', 'GIT')
            fence(self.store, self.worker.adapter, project, guarded_attempt)
            git.push(project, path, attempt['branch'], attempt['origin_url'],
                     before_push=lambda: fence(self.store, self.worker.adapter, project, guarded_attempt))
            remote = git.git(path, 'ls-remote', 'origin', 'refs/heads/' + attempt['branch']).stdout.split()
            if not remote or remote[0] != sha:
                raise TaskError('publication_unavailable', 'GIT')
            branch = attempt['branch']
            # Reuse the normal result comment's branch redaction policy.
            if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', branch)
                    or re.search(r'(?:gh[pousr]_|github_pat_|sk-|AKIA)', branch)):
                branch = '[redacted]'
            body = (f'Status: review_on_error\nReason: canonical_pr_merged (reopen impossible)\n'
                    f'Branch: {branch}\nCommit: {sha}\n'
                    f'Canonical PR: https://github.com/{project.repository}/pull/{saved["pr_number"]}\n'
                    'Publication: confirmed by remote SHA.\nManual resolution in GitHub is required.\n'
                    f'<!-- codex-terminal:{saved["issue_id"]}:{saved["iteration_id"]} -->')
            with self.store.db as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE attempts SET state=CASE WHEN state='running' THEN 'review_on_error' ELSE state END,pushed=1,remote_sha=?,finished_at=COALESCE(finished_at,?),recovery_diagnostic=NULL WHERE attempt_id=?",
                           (sha, now(), attempt['attempt_id']))
                db.execute("UPDATE terminal_errors SET publication='confirmed',diagnostic=NULL,body=? WHERE issue_id=?",
                           (body, saved['issue_id']))
        self.deliver(project, attempt)
        self.store.publication('pr:' + attempt['attempt_id'], attempt['iteration_id'], 'terminal', 'completed')

    def deliver(self, project, attempt):
        from dispatcher.detachment import fence
        saved = self.saved(attempt)
        guarded_attempt = dict(attempt, iteration_id=saved['iteration_id'], version=saved['version'])
        fence(self.store, self.worker.adapter, project, guarded_attempt)
        if saved['delivered']:
            return
        endpoint = f'repos/{project.repository}/issues/{attempt["number"]}/comments'
        marker = f'<!-- codex-terminal:{saved["issue_id"]}:{saved["iteration_id"]} -->'
        comments = self.worker.adapter.pages(endpoint)
        if not isinstance(comments, list) or any(not isinstance(c, dict) or not isinstance(c.get('body', ''), str) for c in comments):
            raise GitHubError(kind='invalid_response')
        found = next((c for c in comments if marker in c.get('body', '')), None)
        if found is None:
            fence(self.store, self.worker.adapter, project, guarded_attempt)
            found = self.worker.adapter.write(endpoint, 'POST', dict(body=saved['body']))
        if not isinstance(found, dict) or type(found.get('id')) is not int:
            raise GitHubError(kind='invalid_response')
        self.worker.adapter.reconcile(project, self.store.get(attempt['repository'], attempt['number']), store=self.store)
        with self.store.db as db:
            db.execute('UPDATE terminal_errors SET delivered=1,comment_id=?,diagnostic=NULL WHERE issue_id=?',
                       (str(found['id']), saved['issue_id']))
