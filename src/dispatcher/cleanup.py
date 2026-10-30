"""Durable terminal cleanup, independent of GitHub delivery and polling.

SQLite owns the generation reservation; Linux flock owns worker liveness. A
running row with a free lock is recoverable immediately, without a time-based
lease that could steal a slow worker's files. No DB writer is held during Git.
"""
import fcntl
import os
import re
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from dispatcher.logging import event
from dispatcher.runtime import identity, group_members
from dispatcher.storage import Store, now
from dispatcher.workspace import GitWorkspaceManager, TaskWorkspaceCleanup, TaskError, safe_path


class CleanupService:
    def __init__(self, config, store, clock=None):
        self.config, self.store = config, store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.manager = GitWorkspaceManager(config)

    def reconcile(self):
        # Filesystem ownership is checked before *any* deletion, including legacy
        # jobs. Unsafe legacy objects get a durable diagnostic rather than vanish.
        with self.store.db as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT OR IGNORE INTO cleanup_jobs(issue_id,project_id,repository,number,
                generation,version,attempt_id,created_at,updated_at)
                SELECT issue_id,project_id,repository,number,COALESCE(current_iteration_id,0),version,
                    (SELECT attempt_id FROM attempts a WHERE a.repository=t.repository AND a.number=t.number ORDER BY rowid DESC LIMIT 1),?,?
                FROM tasks t WHERE state IN ('done','cancelled','review_on_error','detached')
                AND (state!='review_on_error' OR EXISTS(SELECT 1 FROM terminal_errors e
                    WHERE e.issue_id=t.issue_id AND e.publication='confirmed'))''', (now(), now()))

    def retry(self, project, number):
        with self.store.db as db:
            db.execute('BEGIN IMMEDIATE')
            return bool(db.execute('''UPDATE cleanup_jobs SET state='pending',attempts=0,
                next_retry_at=NULL,last_error=NULL,completed_at=NULL,updated_at=?
                WHERE project_id=? AND repository=? AND number=? AND state='failed'
                AND EXISTS(SELECT 1 FROM tasks t WHERE t.issue_id=cleanup_jobs.issue_id
                    AND t.state IN ('done','cancelled','review_on_error','detached') AND t.project_id=cleanup_jobs.project_id
                    AND COALESCE(t.current_iteration_id,0)=cleanup_jobs.generation AND t.version=cleanup_jobs.version)''',
                (self.clock().isoformat(), project.id, project.repository.casefold(), number)).rowcount)

    def owned_row(self, project, row):
        repo, task, worktree = self.manager.paths(project, row['number'])
        if row['project_id'] != project.id or row['repository'] != project.repository.casefold():
            raise TaskError('cleanup_identity_mismatch')
        if row['state'] == 'review_on_error' and not self.store.db.execute(
                "SELECT 1 FROM terminal_errors WHERE issue_id=? AND publication='confirmed'",
                (row['issue_id'],)).fetchone():
            raise TaskError('cleanup_publication_unconfirmed')
        attempts = self.store.db.execute('''SELECT * FROM attempts WHERE repository=? AND number=? ORDER BY rowid''',
                                         (row['repository'], row['number'])).fetchall()
        for a in attempts:
            for helper in self.store.db.execute('SELECT * FROM attempt_processes WHERE attempt_id=? AND finished_at IS NULL', (a['attempt_id'],)):
                if group_members(helper['pid']) or identity(helper['pid']) == helper['process_identity']:
                    raise TaskError('cleanup_active_execution')
                raise TaskError('cleanup_process_unconfirmed')
            if a['pid'] and (a['exit_code'] is None or not a['process_identity'] or
                    a['recovery_diagnostic'] == 'process_identity_unconfirmed'):
                raise TaskError('cleanup_process_unconfirmed')
            if not re.fullmatch('[0-9a-f]{32}', a['attempt_id']):
                raise TaskError('cleanup_invalid_attempt')
            if a['state'] == 'running' or (a['pid'] and (group_members(a['pid']) or (a['process_identity'] and identity(a['pid']) == a['process_identity']))):
                raise TaskError('cleanup_active_execution')
        if not attempts and (task.exists() or str(worktree) in TaskWorkspaceCleanup(self.manager).registered(repo)):
            raise TaskError('cleanup_ownership_unproven')
        result = dict(row, publication_confirmed=row['state'] == 'review_on_error', owned_worktrees=['previous-' + a['attempt_id'] for a in attempts],
                      owned_attachments=['attachments-' + a['attempt_id'] for a in attempts],
                      owned_attempts=[a['attempt_id'] for a in attempts])
        candidates = [(worktree, attempts[-1] if attempts else None)]
        candidates += [(task / ('previous-' + a['attempt_id']), a) for a in attempts]
        for path, attempt in candidates:
            safe_path(self.config.workspace, path)
            git_args = ()
            cwd = path
            if not path.exists():
                if str(path) not in TaskWorkspaceCleanup(self.manager).registered(repo):
                    continue
                TaskWorkspaceCleanup(self.manager).verify_metadata(repo, [path])
                metadata_root = safe_path(self.config.workspace, repo / '.git' / 'worktrees')
                metadata = next(m for m in metadata_root.iterdir()
                                if safe_path(self.config.workspace, m / 'gitdir').read_text().strip() == str(path / '.git'))
                cwd, git_args = repo, ('--git-dir=' + str(metadata),)
            if attempt is None:
                raise TaskError('cleanup_ownership_unproven')
            sha = self.manager.git(cwd, *git_args, 'rev-parse', 'HEAD').stdout.strip()
            branch = self.manager.git(cwd, *git_args, 'symbolic-ref', '--short', 'HEAD', check=False).stdout.strip()
            expected_sha = attempt['result_sha'] or attempt['initial_sha']
            # Detached previous worktrees are intentional; current must retain its branch.
            detached_owned = (row['state'] == 'detached' and attempt['initial_sha'] and
                self.manager.git(cwd, *git_args, 'merge-base', '--is-ancestor', attempt['initial_sha'], sha, check=False).returncode == 0)
            if (sha != expected_sha and not detached_owned) or (branch != attempt['branch'] and (path == worktree or branch)):
                raise TaskError('cleanup_ownership_unproven')
            if path == worktree and attempt['worktree'] != str(worktree):
                raise TaskError('cleanup_ownership_unproven')
        return result

    def cleanup(self, project, row):
        owned = self.owned_row(project, row)
        if row['state'] == 'detached':
            attempt = self.store.db.execute(
                'SELECT * FROM attempts WHERE repository=? AND number=? ORDER BY rowid DESC LIMIT 1',
                (row['repository'], row['number'])).fetchone()
            repo, _, worktree = self.manager.paths(project, row['number'])
            # A missing worktree cannot attest to the current local branch tip.
            # Leave legacy/ref-only verification to preparation in that case.
            if attempt and attempt['initial_sha'] and worktree.exists():
                ref = self.manager.git(repo, 'rev-parse', '--verify', 'refs/heads/' + attempt['branch'], check=False)
                sha = ref.stdout.strip() if ref.returncode == 0 else None
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO detach_refs VALUES(?,?,?,?)',
                        (attempt['attempt_id'], attempt['branch'], sha, 'archive/detach/' + attempt['attempt_id']))
        TaskWorkspaceCleanup(self.manager).cleanup(project, owned)

    def tick(self, project_id=None, stop=None):
        self.reconcile()
        db = self.store.db
        jobs = db.execute("SELECT * FROM cleanup_jobs WHERE state IN ('pending','running') ORDER BY cleanup_id").fetchall()
        for job in jobs:
            if stop is not None and stop.is_set():
                break
            if project_id is not None and job["project_id"] != project_id:
                continue
            project = next((p for p in self.config.projects if p.id == job['project_id']
                            and p.repository.casefold() == job['repository']), None)
            if project is None:
                continue  # No guessing paths after project configuration changes.
            lock_path = self.config.workspace / '.runtime' / f'cleanup-{job["issue_id"]}.lock'
            safe_path(self.config.workspace, lock_path)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                stamp = self.clock()
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT * FROM cleanup_jobs WHERE cleanup_id=?', (job['cleanup_id'],)).fetchone()
                    if current['state'] not in ('pending','running'):
                        continue
                    if current['next_retry_at'] and datetime.fromisoformat(current['next_retry_at']) > stamp:
                        continue
                    row = self.store.get(job['repository'], job['number'])
                    latest = db.execute('SELECT attempt_id FROM attempts WHERE repository=? AND number=? ORDER BY rowid DESC LIMIT 1',
                                        (job['repository'], job['number'])).fetchone()
                    if (not row or row['issue_id'] != job['issue_id'] or row['project_id'] != job['project_id']
                            or row['state'] not in ('done','cancelled','review_on_error','detached') or row['version'] != job['version']
                            or (row['current_iteration_id'] or 0) != job['generation']
                            or (latest[0] if latest else None) != job['attempt_id']):
                        db.execute("UPDATE cleanup_jobs SET state='failed',last_error='cleanup_generation_changed',updated_at=? WHERE cleanup_id=?",
                                   (stamp.isoformat(), job['cleanup_id']))
                        continue
                    executions = db.execute('SELECT state,pid,process_identity FROM attempts WHERE repository=? AND number=?',
                                            (job['repository'], job['number'])).fetchall()
                    active_execution = any(a['state'] == 'running' or
                        (a['pid'] and (group_members(a['pid']) or (a['process_identity'] and identity(a['pid']) == a['process_identity'])))
                        for a in executions)
                    # Recovery resumes the same counted attempt. The OS lock proves
                    # the previous worker cannot still be deleting this task.
                    count = current['attempts'] + (current['state'] == 'pending')
                    db.execute("UPDATE cleanup_jobs SET state='running',attempts=?,started_at=?,updated_at=?,next_retry_at=NULL WHERE cleanup_id=?",
                               (count, stamp.isoformat(), stamp.isoformat(), job['cleanup_id']))
                self.log(project, job, count, 'running')
                try:
                    if active_execution:
                        raise TaskError('cleanup_active_execution')
                    self.manager.cleanup_lock_fd = fd
                    try:
                        self.cleanup(project, row)
                    finally:
                        self.manager.cleanup_lock_fd = None
                except (TaskError, OSError, ValueError, TypeError) as exc:
                    error = exc.code if isinstance(exc, TaskError) else 'cleanup_preserved_artifacts'
                    delay = (60, 300, 900)[count - 1] if count < 4 else (900 if row['state'] == 'detached' or error == 'cleanup_process_unconfirmed' else None)
                    with db:
                        db.execute('''UPDATE cleanup_jobs SET state=?,updated_at=?,next_retry_at=?,last_error=? WHERE cleanup_id=?''',
                                   ('pending' if delay else 'failed', self.clock().isoformat(),
                                    (self.clock() + timedelta(seconds=delay)).isoformat() if delay else None,
                                    error, job['cleanup_id']))
                    self.log(project, job, count, 'retry' if delay else 'failed', error)
                else:
                    with db:
                        db.execute("UPDATE cleanup_jobs SET state='completed',completed_at=?,updated_at=?,last_error=NULL,next_retry_at=NULL WHERE cleanup_id=?",
                                   (self.clock().isoformat(), self.clock().isoformat(), job['cleanup_id']))
                        db.execute('UPDATE cancellations SET cleaned=1 WHERE issue_id=?', (row['issue_id'],))
                    self.log(project, job, count, 'completed')
            finally:
                os.close(fd)

    def log(self, project, job, count, result, error=None):
        self.config.logs.mkdir(parents=True, exist_ok=True)
        event(self.config.logs / 'dispatcher.jsonl', 'cleanup', project=project.id, task=job['number'],
              cleanup_id=job['cleanup_id'], attempt=count, result=result, reason=error)


class CleanupWorker:
    def __init__(self, config):
        self.config = config
        self.stop = Event()
        self.threads = [Thread(target=self.run, args=(p.id,),
                               name='workspace-cleanup-' + p.id, daemon=True)
                        for p in config.projects]

    def start(self):
        for thread in self.threads:
            thread.start()

    def run(self, project_id):
        store = None
        try:
            while not self.stop.is_set():
                try:
                    if store is None:
                        store = Store(self.config.workspace / '.runtime' / 'tasks.sqlite3')
                    CleanupService(self.config, store).tick(project_id, self.stop)
                except Exception:
                    # Persisted running jobs are recovered on the next cycle;
                    # transient connection/migration errors also retry startup.
                    self.config.logs.mkdir(parents=True, exist_ok=True)
                    event(self.config.logs / 'dispatcher.jsonl', 'cleanup_worker_failed', level='WARN',
                          project=project_id, reason='cleanup_unavailable')
                self.stop.wait(1)
        finally:
            if store is not None:
                store.close()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()
