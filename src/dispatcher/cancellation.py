"""Durable delivery of verified cancellation decisions, including after restart."""
import fcntl
from pathlib import Path

from dispatcher.github import GitHubError
from dispatcher.workspace import TaskError


class CancellationDelivery:
    def __init__(self, config, store, adapter):
        self.config, self.store, self.adapter = config, store, adapter

    def poll(self, project):
        db = self.store.db
        rows = db.execute('''SELECT t.*,c.issue_closed,c.cleaned FROM cancellations c
            JOIN tasks t USING(issue_id) WHERE t.repository=? AND t.state='cancelled'
            AND c.issue_closed=0''', (project.repository.casefold(),)).fetchall()
        errors = []
        for row in rows:
            if self.store.stop_requested(row):
                continue
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
                            if self.store.stop_requested(self.store.get(project.repository, row['number'])):
                                continue
                            self.adapter.write(endpoint, 'PATCH', dict(state='closed', state_reason='not_planned'))
                            issue = self.adapter.request(endpoint)
                        if (issue['id'] != row['issue_id'] or issue['state'] != 'closed'
                                or issue.get('state_reason') != 'not_planned'):
                            raise GitHubError()
                        with db:
                            db.execute('UPDATE cancellations SET issue_closed=1 WHERE issue_id=?', (row['issue_id'],))
                    with db:
                        db.execute('UPDATE cancellations SET error=NULL WHERE issue_id=?', (row['issue_id'],))
                except (GitHubError, TaskError, OSError, KeyError, TypeError, ValueError):
                    with db:
                        db.execute("UPDATE cancellations SET error='delivery_failed' WHERE issue_id=?", (row['issue_id'],))
                    errors.append(row['number'])
        return errors

    def cleanup(self, project, row):
        # Compatibility entry point; automatic deletion belongs to the durable worker.
        from dispatcher.cleanup import CleanupService
        CleanupService(self.config, self.store).cleanup(project, row)
