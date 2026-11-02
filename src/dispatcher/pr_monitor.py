"""PR decisions and restartable Issue delivery in the existing poll loop."""
import fcntl
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from dispatcher.github import GitHubError
from dispatcher.storage.transitions import Authority, Evidence, rest_id


def utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError('Expected UTC timestamp')
    return value


class PullRequestMonitor:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter

    def poll(self, project):
        db = self.store.db
        discovery_errors = self.discover_manual(project)
        rows = db.execute('''SELECT DISTINCT p.*,h.iteration_id,t.number,t.repository,t.state AS task_state
            FROM task_pull_requests p JOIN iteration_pr_history h USING(pr_id)
            JOIN tasks t ON t.issue_id=p.issue_id
            LEFT JOIN pr_resolutions r ON r.pr_id=p.pr_id AND r.iteration_id=h.iteration_id
            JOIN task_iterations i ON i.iteration_id=h.iteration_id
            LEFT JOIN publication_operations legacy ON legacy.operation_key='resolution:'||p.pr_id
            LEFT JOIN publication_operations o ON o.operation_key='resolution:'||p.pr_id||
                CASE WHEN i.iteration_number=1 OR legacy.iteration_id=h.iteration_id
                THEN '' ELSE ':'||h.iteration_id END
            WHERE t.repository=? AND t.state!='review_on_error' AND
            ((t.state='review' AND t.current_iteration_id=h.iteration_id)
             OR (r.pr_id IS NOT NULL AND (o.outcome IS NULL OR o.outcome!='completed')))''',
            (project.repository.casefold(),)).fetchall()
        errors = discovery_errors
        for linked in rows:
            database = db.execute('PRAGMA database_list').fetchone()[2]
            with (Path(database).parent / f'pr-resolution-{linked["pr_id"]}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                key = 'resolution:' + str(linked['pr_id'])
                legacy = db.execute('SELECT iteration_id FROM publication_operations WHERE operation_key=?',
                                    (key,)).fetchone()
                number = db.execute('SELECT iteration_number FROM task_iterations WHERE iteration_id=?',
                                    (linked['iteration_id'],)).fetchone()[0]
                if number > 1 and (legacy is None or legacy['iteration_id'] != linked['iteration_id']):
                    key += ':' + str(linked['iteration_id'])
                try:
                    self.resolve(project, linked, key)
                except (GitHubError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OSError):
                    self.store.publication(key, linked['iteration_id'], 'retry', 'failed', retry=True)
                    with db:
                        db.execute('UPDATE pr_resolutions SET error=? WHERE pr_id=? AND iteration_id=?',
                                   ('github_or_storage_unavailable', linked['pr_id'], linked['iteration_id']))
                    errors.append(linked['number'])
        return errors

    def discover_manual(self, project):
        """Bind human PRs before normal resolution, including already merged PRs."""
        from dispatcher.container_monitor import ContainerMonitor
        from dispatcher.workspace import TaskError
        errors = []
        rows = self.store.db.execute("""SELECT * FROM tasks t WHERE repository=? AND state='review'
            AND issue_type='issue' AND run_codex=0 AND NOT EXISTS
            (SELECT 1 FROM task_pull_requests p WHERE p.issue_id=t.issue_id)""", (project.repository.casefold(),)).fetchall()
        for row in rows:
            try:
                if self.store.stop_requested(row):
                    continue
                claim = self.store.db.execute("SELECT payload FROM container_operations WHERE container_issue_id=? AND iteration_id=? AND kind='branch' AND phase='confirmed' AND outcome='success'",
                    (row['issue_id'], row['current_iteration_id'])).fetchone()
                if not claim or json.loads(claim['payload']).get('branch') != row['work_branch']:
                    raise TaskError('pr_identity_conflict')
                evidence, _ = ContainerMonitor(self.store, self.adapter).prs(project, row)
                if not evidence:
                    continue
                pr = evidence[0]
                import re
                if not rest_id(pr['id']) or not rest_id(pr['number']) or not re.fullmatch(r'[0-9a-f]{40,64}', pr['head_sha']):
                    raise TaskError('pr_identity_conflict')
                if pr['base'] != row['base_branch'] and not pr['merged']:
                    raise TaskError('pr_identity_conflict')
                with self.store.db as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.store.get(project.repository, row['number'])
                    if (current['state'] != 'review' or current['current_iteration_id'] != row['current_iteration_id']
                            or self.store.stop_requested(current)):
                        continue
                    foreign = db.execute('SELECT * FROM task_pull_requests WHERE pr_id=? OR (repository_id=? AND pr_number=?) OR issue_id=?',
                        (pr['id'], row['repository_id'], pr['number'], row['issue_id'])).fetchall()
                    if foreign:
                        raise TaskError('pr_identity_conflict')
                    db.execute('INSERT INTO task_pull_requests VALUES(?,?,?,?,?,?,?,?,?)',
                        (pr['id'], row['issue_id'], row['repository_id'], pr['number'], pr['head'],
                         row['base_branch'], pr['head_sha'], pr['state'], pr['merged_at']))
                    from dispatcher.storage import now
                    db.execute('INSERT INTO iteration_pr_history(iteration_id,pr_id,head_sha,created_at) VALUES(?,?,?,?)',
                        (row['current_iteration_id'], pr['id'], pr['head_sha'], now()))
            except (GitHubError, TaskError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
                errors.append(row['number'])
        return errors

    def resolve(self, project, linked, key):
        db, api = self.store.db, self.adapter
        saved = db.execute('SELECT * FROM pr_resolutions WHERE pr_id=? AND iteration_id=?', (linked['pr_id'], linked['iteration_id'])).fetchone()
        task = self.store.get(project.repository, linked['number'])
        if self.store.stop_requested(task) or task['state'] == 'review_on_error':
            return
        if task['state'] == 'review' and task['current_iteration_id'] == linked['iteration_id']:
            pr = api.request(f'repos/{project.repository}/pulls/{linked["pr_number"]}')
            observed = utc(api.response_date)
            if (pr['id'] != linked['pr_id'] or pr['number'] != linked['pr_number']
                    or pr['head']['repo']['id'] != linked['repository_id']
                    or pr['base']['repo']['id'] != linked['repository_id']
                    or pr['head']['ref'] != linked['head']
                    or not isinstance(pr['base']['ref'], str) or not pr['base']['ref']
                    or type(pr['merged']) is not bool):
                raise GitHubError(kind='invalid_response')
            if pr['state'] == 'open' and pr['merged'] is False:
                return
            if pr['state'] != 'closed':
                raise GitHubError(kind='invalid_response')
            closed = utc(pr['closed_at'])
            target = 'done' if pr['merged'] else 'rejected'
            actor = None
            if target == 'done':
                merged_at = utc(pr['merged_at'])
                if (datetime.fromisoformat(merged_at.replace('Z', '+00:00')) > datetime.fromisoformat(closed.replace('Z', '+00:00'))
                        or not isinstance(pr.get('merge_commit_sha'), str) or not pr['merge_commit_sha']):
                    raise GitHubError(kind='invalid_response')
                actor = (pr.get('merged_by') or {}).get('login')
            else:
                try:
                    events = api.pages(f'repos/{project.repository}/issues/{linked["pr_number"]}/timeline')
                    matches = [e for e in events if e.get('event') == 'closed' and e.get('created_at') == closed]
                    if matches:
                        actor = (matches[-1].get('actor') or {}).get('login')
                except GitHubError:
                    pass  # Actor is optional; never substitute the PR author.
            metadata = dict(expected_base=linked['base'],
                            merged_base=pr['base']['ref'] if target == 'done' else None,
                            pr_url=f'https://github.com/{project.repository}/pull/{linked["pr_number"]}',
                            pr_number=linked['pr_number'], actor=actor or 'Не удалось определить',
                            closed_at=closed, merged_at=pr.get('merged_at'),
                            merge_commit_sha=pr.get('merge_commit_sha'), head_sha=pr['head']['sha'],
                            merged_by=actor if target == 'done' else None,
                            closed_by=actor if target == 'rejected' else None,
                            reason='closed_unmerged' if target == 'rejected' else None)
            guard = self.store.guard(project.repository, task['number'])
            fence = guard['server_at'] if guard else None
            if fence is None:
                transition = db.execute("SELECT evidence FROM task_transitions WHERE issue_id=? AND to_state='review' AND iteration_id=? ORDER BY rowid DESC LIMIT 1",
                                        (task['issue_id'], linked['iteration_id'])).fetchone()
                fence = json.loads(transition['evidence']).get('observed_at') if transition else None
            if datetime.fromisoformat(closed.replace('Z', '+00:00')) > datetime.fromisoformat(observed.replace('Z', '+00:00')):
                raise ValueError('Future PR decision')
            proof = Evidence(task['repository_id'], task['issue_id'], task['number'], observed, observed,
                fence, task['content_hash'], linked['iteration_id'], 'review', available=True,
                pr_id=linked['pr_id'], pr_number=linked['pr_number'], head=linked['head'], base=linked['base'],
                head_sha=pr['head']['sha'], pr_state='closed', merged=pr['merged'],
                merged_at=pr.get('merged_at'), linked_issue_id=task['issue_id'],
                merged_base=pr['base']['ref'] if target == 'done' else None)
            # Intent precedes the decision; a crash on either side remains selectable.
            self.store.publication(key, linked['iteration_id'])
            with db:
                db.execute('''INSERT INTO pr_resolutions(pr_id,iteration_id,target,metadata) VALUES(?,?,?,?)
                    ON CONFLICT(pr_id,iteration_id) DO UPDATE SET target=excluded.target,metadata=excluded.metadata''',
                    (linked['pr_id'], linked['iteration_id'], target, json.dumps(metadata)))
            result = self.store.transition(repository=project.repository, number=task['number'],
                expected_state='review', version=task['version'], iteration_id=linked['iteration_id'],
                target=target, event_key=key, authority=Authority('github-pr-observer', ('github-pr-observer',), role='observer'), evidence=proof)
            if result not in ('applied', 'already_applied'):
                raise GitHubError(kind='invalid_response')
            saved = db.execute('SELECT * FROM pr_resolutions WHERE pr_id=? AND iteration_id=?', (linked['pr_id'], linked['iteration_id'])).fetchone()
        if saved is None:
            return
        # Only deliver an intent whose guarded decision actually committed.
        if not db.execute('SELECT 1 FROM task_transitions WHERE event_key=? AND to_state=?',
                          (key, saved['target'])).fetchone():
            raise GitHubError(kind='invalid_response')
        metadata = json.loads(saved['metadata'])
        endpoint = f'repos/{project.repository}/issues/{linked["number"]}'
        issue_closed_at = saved['issue_closed_at']
        if saved['target'] == 'done' and not issue_closed_at:
            issue = api.request(endpoint)
            if issue['id'] != linked['issue_id']:
                raise GitHubError(kind='invalid_response')
            if issue['state'] != 'closed' or issue.get('state_reason') != 'completed':
                if self.store.stop_requested(self.store.get(project.repository, linked['number'])):
                    return
                api.write(endpoint, 'PATCH', dict(state='closed', state_reason='completed'))
                issue = api.request(endpoint)
            if issue['id'] != linked['issue_id'] or issue['state'] != 'closed' or issue.get('state_reason') != 'completed':
                raise GitHubError(kind='invalid_response')
            issue_closed_at = utc(issue['closed_at'])
            with db:
                db.execute('UPDATE pr_resolutions SET issue_closed_at=? WHERE pr_id=? AND iteration_id=?',
                           (issue_closed_at, linked['pr_id'], linked['iteration_id']))
        marker = f'<!-- codex-resolution:{linked["repository_id"]}:{linked["issue_id"]}:{linked["iteration_id"]}:{linked["pr_id"]} -->'
        if not saved['comment_id']:
            comments = api.pages(endpoint + '/comments')
            existing = next((c for c in comments if marker in (c.get('body') or '')), None)
            if existing is None:
                body = (f'PR #{metadata["pr_number"]}: {metadata["pr_url"]}\nStatus: {saved["target"]}\n'
                        f'{"merged_by" if saved["target"] == "done" else "closed_by"}: {metadata["actor"]}\nclosed_at: {metadata["closed_at"]}\n')
                if saved['target'] == 'done':
                    body += f'expected_base: {metadata.get("expected_base", linked["base"])}\nmerged_base: {metadata.get("merged_base", linked["base"])}\n'
                    body += f'merged_at: {metadata["merged_at"]}\nIssue closed_at: {issue_closed_at}\n'
                body += marker
                if self.store.stop_requested(self.store.get(project.repository, linked['number'])):
                    return
                existing = api.write(endpoint + '/comments', 'POST', dict(body=body))
            if not rest_id(existing.get('id')):
                raise GitHubError(kind='invalid_response')
            with db:
                db.execute('UPDATE pr_resolutions SET comment_id=?,error=NULL WHERE pr_id=? AND iteration_id=?',
                           (existing['id'], linked['pr_id'], linked['iteration_id']))
        api.reconcile(project, self.store.get(project.repository, linked['number']), store=self.store)
        with db:
            db.execute('UPDATE pr_resolutions SET error=NULL WHERE pr_id=? AND iteration_id=?', (linked['pr_id'], linked['iteration_id']))
        self.store.publication(key, linked['iteration_id'], 'delivered', 'completed')
