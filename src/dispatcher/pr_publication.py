"""Restartable PR publication. Network calls never hold a SQLite transaction."""
import fcntl
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from dispatcher.codex_summary import final_summary
from dispatcher.detachment import fence
from dispatcher.github import GitHubError
from dispatcher.storage import now
from dispatcher.storage.transitions import Authority, Evidence, rest_id
from dispatcher.workspace import TaskError
from dispatcher.terminal_error import TerminalError, canonical_merged


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
            AND (p.outcome!='completed' OR a.state='running')
            UNION SELECT a.* FROM attempts a JOIN terminal_errors e USING(attempt_id)
            WHERE e.delivered=0 OR e.publication!='confirmed'""").fetchall()
        for row in rows:
            project = next((p for p in projects if p.repository.casefold() == row['repository']), None)
            if project is not None:
                self.publish(project, row)

    def publish(self, project, attempt):
        # A separate advisory lock serializes publishers across connections and
        # processes without holding SQLite's writer lock during network calls.
        database = self.store.db.execute('PRAGMA database_list').fetchone()[2]
        name = hashlib.sha256(f'{attempt["repository"]}:{attempt["number"]}'.encode()).hexdigest()
        with (Path(database).parent / ('publication-' + name + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            operation = self.store.db.execute('SELECT * FROM publication_operations WHERE operation_key=?',
                                             ('pr:' + attempt['attempt_id'],)).fetchone()
            terminal = TerminalError(self).saved(attempt)
            if operation and not terminal and (operation['outcome'] == 'completed' or operation['phase'] == 'terminal'):
                return
            if hasattr(self.git, 'operation_attempt'):
                self.git.operation_attempt = attempt['attempt_id']
            try:
                self._publish(project, self.execution.get(attempt['attempt_id']))
            finally:
                if hasattr(self.git, 'operation_attempt'):
                    self.git.operation_attempt = None

    def _publish(self, project, attempt):
        task = self.store.get(attempt['repository'], attempt['number'])
        if task['base_branch']:
            project = replace(project, base_branch=task['base_branch'])
        key, iteration = 'pr:' + attempt['attempt_id'], attempt['iteration_id']
        phase, repeated, reuse_key = 'verify', False, None
        def progress(value, outcome='pending'):
            self.store.publication(key, iteration, value, outcome)
        terminal = TerminalError(self)
        try:
            if terminal.saved(attempt):
                terminal.drain(project, attempt)
                return
            fence(self.store, self.adapter, project, attempt)
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
            prior = self.store.db.execute("""SELECT p.* FROM task_pull_requests p
                JOIN iteration_pr_history h USING(pr_id) JOIN task_iterations i USING(iteration_id)
                WHERE p.issue_id=? AND p.repository_id=? AND i.iteration_id!=?
                ORDER BY i.iteration_number DESC LIMIT 1""",
                (task['issue_id'], task['repository_id'], iteration)).fetchone()
            repeated = self.store.db.execute('SELECT iteration_number FROM task_iterations WHERE iteration_id=?',
                                             (iteration,)).fetchone()[0] > 1
            if self.store.db.execute('SELECT 1 FROM stop_requests WHERE issue_id=? AND active=0', (task['issue_id'],)).fetchone():
                repeated = prior is not None
            if repeated and prior is None:
                raise TaskError('pr_identity_conflict', 'GITHUB')
            if task['state'] not in ('running', 'review'):
                raise TaskError('publication_conflict', 'GITHUB')
            if repeated:
                observed = self.adapter.request(f'repos/{project.repository}/pulls/{prior["pr_number"]}')
                if canonical_merged(observed, prior, task, attempt, project):
                    terminal.record(attempt, prior)
                    terminal.drain(project, attempt)
                    return
            worktree = Path(attempt['worktree'])
            fence(self.store, self.adapter, project, attempt)
            sha = self.git.publication_commit(worktree, attempt['branch'], attempt['initial_sha'], project.base_branch,
                before_commit=lambda: fence(self.store, self.adapter, project, attempt))
            self.execution.update(attempt['attempt_id'], result_sha=sha)
            phase = 'push'
            progress(phase)
            if repeated:
                remote_before = self.git.git(worktree, 'ls-remote', 'origin',
                                            'refs/heads/' + attempt['branch']).stdout.split()
                # A lost push response may already have installed our persisted result.
                if not remote_before:
                    raise TaskError('remote_branch_missing', 'GIT')
                if remote_before[0] not in (attempt['initial_sha'], sha):
                    raise TaskError('remote_head_changed', 'GIT')
            if not attempt['pushed']:
                fence(self.store, self.adapter, project, attempt)
                self.git.push(project, worktree, attempt['branch'], attempt['origin_url'],
                    before_push=lambda: fence(self.store, self.adapter, project, attempt))
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
            if repeated:
                reuse_key = f'reopen:{iteration}:{prior["pr_id"]}'
                self.store.publication(reuse_key, iteration, 'verify')
                endpoint = f'repos/{repo}/pulls/{prior["pr_number"]}'
                found = observed
                def validate_prior(pr):
                    if (not isinstance(pr, dict) or pr.get('id') != prior['pr_id']
                            or pr.get('number') != prior['pr_number']
                            or prior['head'] != attempt['branch'] or prior['base'] != project.base_branch
                            or pr.get('head', {}).get('ref') != prior['head']
                            or pr.get('base', {}).get('ref') != prior['base']
                            or pr.get('head', {}).get('repo', {}).get('id') != task['repository_id']
                            or pr.get('base', {}).get('repo', {}).get('id') != task['repository_id']):
                        raise TaskError('pr_identity_conflict', 'GITHUB')
                    if canonical_merged(pr, prior, task, attempt, project):
                        terminal.record(attempt, prior)
                        raise TaskError('pr_merged', 'GITHUB')
                    if pr.get('merged') is not False or pr.get('merged_at'):
                        raise TaskError('pr_merged', 'GITHUB')
                validate_prior(found)
                if found.get('state') == 'closed':
                    phase = 'reopen:' + str(prior['pr_id'])
                    progress(phase)
                    self.store.publication(reuse_key, iteration, 'reopen')
                    try:
                        fence(self.store, self.adapter, project, attempt)
                        self.adapter.write(endpoint, 'PATCH', dict(state='open'))
                    except GitHubError:
                        pass  # Reconcile an unknown result before deciding to retry.
                    found = self.adapter.request(endpoint)
                    validate_prior(found)
                if found.get('state') != 'open':
                    raise TaskError('reopen_failed', 'GITHUB')
            else:
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
                        fence(self.store, self.adapter, project, attempt)
                        found = self.adapter.write(f'repos/{repo}/pulls', 'POST', dict(
                            title=f'[Issue #{task["number"]}] {issue["title"]}', body=body,
                            head=attempt['branch'], base=project.base_branch, draft=False))
                    except GitHubError:
                        found = find()
                        if found is None:
                            raise
            if not isinstance(found, dict) or not rest_id(found.get('number')):
                raise GitHubError(kind='invalid_response')
            with self.store.db as db:
                db.execute('INSERT OR REPLACE INTO attempt_publications VALUES(?,?,?,?)',
                           (attempt['attempt_id'], found.get('id'), found['number'], now()))
            pr = self.adapter.request(f'repos/{repo}/pulls/{found["number"]}')
            if repeated:
                validate_prior(pr)
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
            if reuse_key:
                self.store.publication(reuse_key, iteration, 'confirmed', 'completed')
            guard = self.store.guard(project.repository, task['number'])
            server_fence = guard['server_at'] or json.loads(task['snapshot']).get('server_observed_at')
            proof = Evidence(task['repository_id'], task['issue_id'], task['number'], stamp, stamp,
                server_fence, task['content_hash'], iteration, 'running', available=bool(task['available']), pr_id=pr['id'], pr_number=pr['number'],
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
            self.adapter.reconcile(project, self.store.get(project.repository, task['number']), store=self.store)
            progress('label', 'completed')
        except TaskError as exc:
            if exc.code == 'task_detached':
                progress('terminal', 'failed')
                return
            if terminal.saved(attempt):
                with self.store.db:
                    self.store.db.execute('UPDATE terminal_errors SET diagnostic=? WHERE attempt_id=?', (exc.code, attempt['attempt_id']))
            if reuse_key:
                self.store.publication(reuse_key, iteration, exc.code, 'failed', retry=True)
            self.execution.update(attempt['attempt_id'], recovery_diagnostic=exc.code)
            if exc.code == 'no_diff' or (exc.code == 'publication_conflict' and not repeated):
                self.execution.finish(attempt['attempt_id'], 'failed', exc.code, exc.category)
                progress('terminal', 'failed')
            else:
                self.store.publication(key, iteration, phase, 'failed', retry=True)
        except (GitHubError, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            if terminal.saved(attempt):
                with self.store.db:
                    self.store.db.execute("UPDATE terminal_errors SET diagnostic='publication_unavailable' WHERE attempt_id=?", (attempt['attempt_id'],))
            if reuse_key:
                self.store.publication(reuse_key, iteration, 'publication_unavailable', 'failed', retry=True)
            self.execution.update(attempt['attempt_id'], recovery_diagnostic='publication_unavailable')
            self.store.publication(key, iteration, phase, 'failed', retry=True)
