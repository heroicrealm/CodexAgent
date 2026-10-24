"""Restartable PR publication. Network calls never hold a SQLite transaction."""
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from dispatcher.codex_summary import final_summary
from dispatcher.github import GitHubError
from dispatcher.storage.transitions import Authority, Evidence, rest_id
from dispatcher.workspace import TaskError


class PullRequestPublisher:
    def __init__(self, execution, adapter, git):
        self.execution, self.store = execution, execution.store
        self.adapter, self.git = adapter, git

    def intent(self, attempt):
        row = self.execution.get(attempt)
        self.store.publication('pr:' + attempt, row['iteration_id'])

    def pending(self, attempt):
        return self.store.db.execute('SELECT 1 FROM publication_operations WHERE operation_key=?',
                                     ('pr:' + attempt,)).fetchone() is not None

    def flush(self, projects):
        rows = self.store.db.execute("""SELECT a.* FROM attempts a JOIN publication_operations p
            ON p.operation_key='pr:'||a.attempt_id WHERE p.phase!='terminal'
            AND (p.outcome!='completed' OR a.state='running')""").fetchall()
        for row in rows:
            project = next((p for p in projects if p.repository.casefold() == row['repository']), None)
            if project is not None:
                self.publish(project, row)

    def publish(self, project, attempt):
        # A separate advisory lock serializes publishers across connections and
        # processes without holding SQLite's writer lock during network calls.
        database = self.store.db.execute('PRAGMA database_list').fetchone()[2]
        name = hashlib.sha256(attempt['attempt_id'].encode()).hexdigest()
        with (Path(database).parent / ('publication-' + name + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            operation = self.store.db.execute('SELECT * FROM publication_operations WHERE operation_key=?',
                                             ('pr:' + attempt['attempt_id'],)).fetchone()
            if operation and (operation['outcome'] == 'completed' or operation['phase'] == 'terminal'):
                return
            self._publish(project, self.execution.get(attempt['attempt_id']))

    def _publish(self, project, attempt):
        key, iteration = 'pr:' + attempt['attempt_id'], attempt['iteration_id']
        phase = 'verify'
        def progress(value, outcome='pending'):
            self.store.publication(key, iteration, value, outcome)
        try:
            # Persist once, including fallback, before any Git/HTTP publication.
            # The operation flock serializes selection across publisher processes.
            saved = self.store.db.execute(
                'SELECT body FROM attempt_summaries WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()
            if saved is None:
                summary = f'implementation for Issue #{attempt["number"]}.'
                if attempt['exit_code'] == 0 and attempt['codex_started'] and attempt['worktree']:
                    summary = final_summary(Path(attempt['worktree']).parent /
                        (attempt['attempt_id'] + '.codex.log'), summary)
                with self.store.db:
                    self.store.db.execute('INSERT INTO attempt_summaries VALUES(?,?)',
                                          (attempt['attempt_id'], summary))
            else:
                summary = saved['body']
            progress('verify')
            task = self.store.get(attempt['repository'], attempt['number'])
            if task['version'] != attempt['version'] or task['current_iteration_id'] != iteration:
                raise TaskError('publication_conflict', 'GITHUB')
            if not all(rest_id(task[k]) for k in ('repository_id', 'issue_id')):
                raise GitHubError(kind='invalid_response')
            if self.adapter.repository_identity(project) != task['repository_id']:
                raise TaskError('publication_conflict', 'GITHUB')
            worktree = Path(attempt['worktree'])
            sha = self.git.publication_commit(worktree, attempt['branch'], attempt['initial_sha'], project.base_branch)
            self.execution.update(attempt['attempt_id'], result_sha=sha)
            phase = 'push'
            progress(phase)
            self.git.push(project, worktree, attempt['branch'], attempt['origin_url'])
            remote = self.git.git(worktree, 'ls-remote', 'origin', 'refs/heads/' + attempt['branch']).stdout.split()
            if not remote or remote[0] != sha:
                raise GitHubError(kind='invalid_response')
            self.execution.update(attempt['attempt_id'], pushed=1, remote_sha=sha)
            phase = 'pr'
            progress(phase)
            repo = project.repository
            marker = f'<!-- codex-pr:{task["repository_id"]}:{task["issue_id"]}:{iteration} -->'
            def find():
                candidates = self.adapter.pages(f'repos/{repo}/pulls?' + urlencode(dict(
                    state='all', head=repo.split('/')[0] + ':' + attempt['branch'], base=project.base_branch)))
                if not isinstance(candidates, list) or any(not isinstance(p, dict) for p in candidates):
                    raise GitHubError(kind='invalid_response')
                if len(candidates) > 1:
                    raise TaskError('publication_conflict', 'GITHUB')
                return candidates[0] if candidates else None
            found = find()
            if found is None:
                # Repeat authoritative search immediately before the non-idempotent POST.
                found = find()
                if found is None:
                    issue = json.loads(task['snapshot'])
                    body = (f'https://github.com/{repo}/issues/{task["number"]}\nRepository: {repo}\n'
                            f'Iteration: {iteration}\nBranch: {attempt["branch"]}\nSHA: {sha}\n'
                            f'Summary: {summary}\n{marker}')
                    try:
                        found = self.adapter.write(f'repos/{repo}/pulls', 'POST', dict(
                            title=f'[Issue #{task["number"]}] {issue["title"]}', body=body,
                            head=attempt['branch'], base=project.base_branch, draft=False))
                    except GitHubError:
                        found = find()
                        if found is None:
                            raise
            if not isinstance(found, dict) or not rest_id(found.get('number')):
                raise GitHubError(kind='invalid_response')
            pr = self.adapter.request(f'repos/{repo}/pulls/{found["number"]}')
            if not isinstance(pr, dict):
                raise GitHubError(kind='invalid_response')
            stamp = self.adapter.response_date
            linked = self.store.db.execute('SELECT * FROM task_pull_requests WHERE pr_id=?', (pr.get('id'),)).fetchone()
            owned = marker in (pr.get('body') or '') or (linked and linked['pr_id'] == pr.get('id'))
            if (not owned or not rest_id(pr.get('id')) or pr.get('number') != found['number'] or pr.get('state') != 'open'
                    or pr.get('draft') is not False or pr.get('merged') is not False
                    or pr.get('head', {}).get('ref') != attempt['branch']
                    or pr.get('head', {}).get('sha') != sha
                    or pr.get('head', {}).get('repo', {}).get('id') != task['repository_id']
                    or pr.get('base', {}).get('ref') != project.base_branch
                    or pr.get('base', {}).get('repo', {}).get('id') != task['repository_id']
                    or type(pr.get('changed_files')) is not int or pr['changed_files'] <= 0):
                raise TaskError('publication_conflict', 'GITHUB')
            guard = self.store.guard(project.repository, task['number'])
            fence = guard['server_at'] or json.loads(task['snapshot']).get('server_observed_at')
            proof = Evidence(task['repository_id'], task['issue_id'], task['number'], stamp, stamp,
                fence, task['content_hash'], iteration, 'running', available=bool(task['available']), pr_id=pr['id'], pr_number=pr['number'],
                head=attempt['branch'], base=project.base_branch, head_sha=sha, pr_state='open',
                merged=False, linked_issue_id=task['issue_id'])
            result = self.store.transition(repository=project.repository, number=task['number'],
                expected_state='running', version=attempt['version'], iteration_id=iteration,
                target='review', event_key=key, authority=Authority('publisher', ('publisher',), role='observer'), evidence=proof)
            if result in ('missing_evidence', 'identity_unresolved'):
                raise GitHubError(kind='invalid_response')
            if result not in ('applied', 'already_applied'):
                raise TaskError('publication_conflict', 'GITHUB')
            self.execution.finish(attempt['attempt_id'], 'review')
            phase = 'label'
            progress(phase)
            self.adapter.reconcile(project, self.store.get(project.repository, task['number']))
            progress('label', 'completed')
        except TaskError as exc:
            self.execution.update(attempt['attempt_id'], recovery_diagnostic=exc.code)
            if exc.code in ('no_diff', 'publication_conflict'):
                self.execution.finish(attempt['attempt_id'], 'failed', exc.code, exc.category)
                progress('terminal', 'failed')
            else:
                self.store.publication(key, iteration, phase, 'failed', retry=True)
        except (GitHubError, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            self.execution.update(attempt['attempt_id'], recovery_diagnostic='publication_unavailable')
            self.store.publication(key, iteration, phase, 'failed', retry=True)
