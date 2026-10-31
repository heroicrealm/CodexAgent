"""Mandatory result preservation under the cleanup generation reservation.

The cleanup job is the durable retry intent. No PR or Codex effects are allowed.
The caller holds the per-Issue cleanup flock and a running cleanup reservation;
reattach is blocked until that job completes. No SQLite writer spans Git I/O.
"""
from dispatcher.storage import now
from dispatcher.workspace import TaskError, TaskWorkspaceCleanup


def publish(service, project, row):
    db, manager = service.store.db, service.manager
    stop = db.execute('''SELECT * FROM stop_requests WHERE issue_id=? AND version=?
        AND iteration_id IS ? ORDER BY request_id DESC LIMIT 1''',
        (row['issue_id'], row['version'], row['current_iteration_id'])).fetchone()
    if not stop or not stop['active'] or not stop['finalized_at'] or stop['process_status'] != 'confirmed':
        raise TaskError('detach_publication_unconfirmed')
    with db:
        db.execute('INSERT OR IGNORE INTO detach_publications(request_id) VALUES(?)', (stop['request_id'],))
    attempt = db.execute('SELECT * FROM attempts WHERE attempt_id=?', (stop['attempt_id'],)).fetchone()
    repo, _, worktree = manager.paths(project, row['number'])
    try:
        if not attempt or not attempt['initial_sha']:
            if worktree.exists() or str(worktree) in TaskWorkspaceCleanup(manager).registered(repo):
                raise TaskError('detach_identity_conflict')
            with db:
                db.execute("UPDATE detach_publications SET state='confirmed',no_diff=1,error=NULL,confirmed_at=? WHERE request_id=?",
                           (now(), stop['request_id']))
            return
        if (attempt['iteration_id'] != stop['iteration_id'] or attempt['version'] != stop['version'] or
                attempt['branch'] != row['work_branch'] or not attempt['origin_url'] or
                attempt['worktree'] != str(worktree)):
            raise TaskError('detach_identity_conflict')
        if db.execute('SELECT 1 FROM attempts WHERE repository=? AND branch=? AND number!=?',
                      (row['repository'], attempt['branch'], row['number'])).fetchone():
            raise TaskError('detach_branch_ownership_conflict', 'GIT')
        confirmed = db.execute('SELECT * FROM detach_publications WHERE request_id=?', (stop['request_id'],)).fetchone()
        if not worktree.exists():
            # Recovery after deletion, before cleanup completion. Never infer
            # publication from pushed alone (push response can be ambiguous).
            if confirmed['state'] != 'confirmed' or not confirmed['sha']:
                raise TaskError('detach_publication_unconfirmed')
            return
        TaskWorkspaceCleanup(manager).verify_metadata(repo, [worktree])
        if manager.git(worktree, 'remote', 'get-url', 'origin').stdout.strip() != attempt['origin_url']:
            raise TaskError('origin_changed', 'GIT')
        no_diff = not bool(manager.git(worktree, 'status', '--porcelain').stdout.strip())
        sha = manager.publication_commit(worktree, attempt['branch'], attempt['initial_sha'], project.base_branch, salvage=True)
        with db:
            db.execute("UPDATE detach_publications SET state='pending',sha=?,no_diff=COALESCE(no_diff,?) WHERE request_id=?",
                       (sha, int(no_diff), stop['request_id']))
        manager.push(project, worktree, attempt['branch'], attempt['origin_url'])
        remote = manager.git(worktree, 'ls-remote', 'origin', 'refs/heads/' + attempt['branch']).stdout.split()
        if len(remote) != 2 or remote[0] != sha or remote[1] != 'refs/heads/' + attempt['branch']:
            raise TaskError('detach_remote_unconfirmed', 'GIT')
        with db:
            db.execute("UPDATE detach_publications SET state='confirmed',remote_sha=?,error=NULL,confirmed_at=? WHERE request_id=?",
                       (sha, now(), stop['request_id']))
    except (TaskError, OSError) as exc:
        with db:
            db.execute("UPDATE detach_publications SET state='pending',error=? WHERE request_id=?",
                       (exc.code if isinstance(exc, TaskError) else 'publication_unavailable', stop['request_id']))
        raise
