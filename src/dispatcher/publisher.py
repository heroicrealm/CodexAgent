"""Durable final comments with retry reconciliation after uncertain delivery."""
import re
from datetime import datetime, timezone
from dispatcher.github import GitHubError
from dispatcher.storage import now


def marker(attempt):
    return f'<!-- codex-dispatcher-attempt:{attempt} -->'


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def comment_body(row):
    # Only locally generated fields; never issue text, stderr or exception messages.
    category = row['category'] if row['category'] in ('CODEX','GIT','ATTACHMENT','DISPATCHER','GITHUB') else 'DISPATCHER'
    success = row['state'] == 'review'
    lines = [f'[{category}] ' + ('Task completed successfully.' if success else 'Task did not complete.')]
    branch = row['branch']
    # Prevent injecting Markdown, secrets or links from untrusted branch names.
    if (re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', branch)
            and not re.search(r'(?:gh[pousr]_|github_pat_|sk-|AKIA)', branch)):
        lines.append(f'Branch: {branch}')
    else:
        lines.append('Branch: [redacted]')
    sha = row['result_sha']
    if sha and re.fullmatch('[0-9a-f]{40,64}', sha):
        lines.append(f'Commit: {sha}')
    lines.append('Status: ' + ('Awaiting review.' if success else row['state']))
    if not success:
        # Codes are selected by the dispatcher, never derived from raw output.
        from dispatcher.scheduler import SAFE_CODES
        lines.append('Reason: ' + (row['error_code'] if row['error_code'] in SAFE_CODES else 'internal_error'))
        if not row['codex_started']:
            lines.append('Codex was not started.')
    duration = int(max(0, row['duration']))
    lines.extend([f'Started: {utc(row["started_at"])}', f'Finished: {utc(row["finished_at"])}',
                  f'Duration: {duration // 3600:02}:{duration // 60 % 60:02}:{duration % 60:02}',
                  marker(row['attempt_id'])])
    return '\n'.join(lines)


class TaskResultPublisher:
    def __init__(self, execution, adapter):
        self.execution, self.adapter = execution, adapter

    def flush(self):
        db = self.execution.db
        # Serialize publishers, including the reconciliation read and write. A
        # network failure leaves the immutable body pending for the next tick.
        for pending in db.execute('SELECT attempt_id FROM result_outbox WHERE published=0').fetchall():
            operation_key = 'comment:' + pending[0]
            with db:
                db.execute("UPDATE publication_operations SET retry_count=retry_count+1,updated_at=? WHERE operation_key=? AND outcome='failed'",
                           (now(), operation_key))
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    out = db.execute('SELECT * FROM result_outbox WHERE attempt_id=?', (pending[0],)).fetchone()
                    if out['published']:
                        continue
                    row = self.execution.get(out['attempt_id'])
                    endpoint = f'repos/{row["repository"]}/issues/{row["number"]}/comments'
                    comments = self.adapter.pages(endpoint)
                    if any(not isinstance(c, dict) or not isinstance(c.get('body', ''), str) for c in comments):
                        raise GitHubError(kind='invalid_response')
                    found = next((c for c in comments if marker(out['attempt_id']) in c.get('body', '')), None)
                    if found is None:
                        found = self.adapter.write(endpoint, 'POST', dict(body=out['body']))
                    if not isinstance(found, dict) or not found.get('id'):
                        raise GitHubError(kind='invalid_response')
                    db.execute('UPDATE result_outbox SET published=1,comment_id=? WHERE attempt_id=?',
                               (str(found['id']), out['attempt_id']))
                    db.execute("UPDATE publication_operations SET phase='comment',outcome='completed',updated_at=? WHERE operation_key=?",
                               (now(), operation_key))
            except GitHubError:
                with db:
                    db.execute("UPDATE publication_operations SET phase='comment',outcome='failed',updated_at=? WHERE operation_key=? AND outcome!='completed'",
                               (now(), operation_key))
                continue
