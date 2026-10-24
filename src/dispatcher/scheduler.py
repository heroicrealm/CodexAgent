"""Single daemon loop combining polling, fair admission, execution and delivery."""
import json
import time
from dispatcher.attachments import IssueAttachmentManager
from dispatcher.codex import CodexExecutor
from dispatcher.codex.quota import CodexQuotaProvider
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import Adapter, GitHubError
from dispatcher.github.sync import SyncService
from dispatcher.initialization import ready
from dispatcher.logging import event
from dispatcher.publisher import TaskResultPublisher
from dispatcher.pr_publication import PullRequestPublisher
from dispatcher.runtime import identity
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError

SAFE_CODES = frozenset('''attachment_download_failed git_unavailable git_operation_failed
    retry_sha_unavailable retry_workspace_conflict no_diff non_fast_forward publication_conflict publication_unavailable origin_changed default_branch_unavailable invalid_branch default_branch_protected worktree_already_exists clone_url_unavailable
    unexpected_branch dirty_worktree no_commit rewritten_history unsupported_codex_cli
    codex_failed codex_timeout force_stop stop_requested quota_exhausted crash_recovery internal_error unsafe_path'''.split())


class Dispatcher:
    def __init__(self, config, lifecycle, adapter=None):
        self.config, self.lifecycle = config, lifecycle
        self.store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
        self.execution = ExecutionStore(self.store)
        self.adapter = adapter or Adapter(config.github_executable)
        self.sync = SyncService(config, self.store, self.adapter)
        self.git = GitWorkspaceManager(config)
        self.attachments = IssueAttachmentManager(config)
        self.executor = CodexExecutor(config, lifecycle)
        self.quota = CodexQuotaProvider(config, lifecycle, self.execution.quota)
        self.publisher = TaskResultPublisher(self.execution, self.adapter)
        self.pr_publisher = PullRequestPublisher(self.execution, self.adapter, self.git)
        self.active = {}
        self.projects = [p for p in config.projects if ready(p)]
        self.cursor = 0
        self.next_poll = 0
        self.next_publish = 0
        # The process-wide runtime lock is held by serve. Never retry execution.
        for row in self.execution.db.execute("SELECT * FROM attempts WHERE state='running'").fetchall():
            if row['exit_code'] == 0 and row['codex_started'] and not self.pr_publisher.pending(row['attempt_id']):
                self.pr_publisher.intent(row['attempt_id'])
            if self.pr_publisher.pending(row["attempt_id"]):
                continue
            self.inspect_recovery(row)
            self.execution.finish(row['attempt_id'], 'interrupted', 'crash_recovery', 'DISPATCHER')

    def inspect_recovery(self, row):
        """Read local artifacts and remote SHA; ambiguity never retries execution."""
        diagnostic = 'inspection_unavailable'
        try:
            project = next(p for p in self.config.projects if p.repository.casefold() == row['repository'])
            _, _, worktree = self.git.paths(project, row['number'])
            if not row['worktree'] or str(worktree) != row['worktree'] or not worktree.exists():
                diagnostic = 'worktree_missing'
            else:
                from dispatcher.workspace import safe_path
                safe_path(self.config.workspace, worktree)
                sha = self.git.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                dirty = bool(self.git.git(worktree, 'status', '--porcelain').stdout.strip())
                diagnostic = 'dirty_worktree' if dirty else 'local_commits_preserved'
                self.execution.update(row['attempt_id'], result_sha=sha)
                origin = self.git.git(worktree, 'remote', 'get-url', 'origin').stdout.strip()
                if row['origin_url'] and origin == row['origin_url']:
                    self.git.validate_branch(row['branch'])
                    remote = self.git.git(worktree, 'ls-remote', 'origin', f'refs/heads/{row["branch"]}')
                    values = remote.stdout.split()
                    remote_sha = values[0] if values else None
                    self.execution.update(row['attempt_id'], remote_sha=remote_sha)
                    if remote_sha == sha and not dirty:
                        diagnostic = 'remote_matches_local'
        except (StopIteration, TaskError, OSError):
            pass
        self.execution.update(row['attempt_id'], recovery_diagnostic=diagnostic)

    def admit(self):
        order = self.projects[self.cursor:] + self.projects[:self.cursor]
        for project in order:
            if self.lifecycle.stopping or len(self.active) >= self.config.max_parallel_tasks:
                return
            candidates = [r for r in self.store.queue(project.repository) if r['state'] == 'pending'
                          and r['available'] and r['version'] == r['approved_version']]
            for candidate in candidates:
                if self.lifecycle.stopping or not self.quota.check(fresh=True):
                    return
                try:
                    # Full authoritative snapshot also applies revocation/version changes
                    # under the same stage 3 synchronization model.
                    items = self.adapter.fetch(project)
                    observation = {}
                    self.store.apply(project, items, observation=observation)
                    current = observation.get(candidate['number'])
                    if (not current or current['state'] != 'pending' or not current['available']
                            or current['version'] != candidate['version']):
                        continue
                    issue = json.loads(current['snapshot'])
                    branch = self.git.branch(project, issue)
                    previous = self.store.db.execute(
                        'SELECT * FROM attempts WHERE repository=? AND number=? ORDER BY rowid DESC LIMIT 1',
                        (current['repository'], current['number'])).fetchone()
                    if previous:
                        source = self.store.db.execute("""SELECT p.head_sha FROM task_pull_requests p
                            JOIN iteration_pr_history h USING(pr_id) JOIN pr_resolutions r USING(pr_id)
                            WHERE h.iteration_id=? AND r.target='rejected' ORDER BY h.id DESC LIMIT 1""",
                            (previous['iteration_id'],)).fetchone()
                        sha = source['head_sha'] if source else previous['result_sha']
                        self.git.retry_source(project, issue['number'], sha)
                        issue['retry_sha'] = sha
                        issue['previous_attempt'] = previous['attempt_id']
                        branch = f'codex/issue-{issue["number"]}-iteration-{current["current_iteration_id"]}'
                    if self.lifecycle.stopping:
                        return
                    attempt = self.execution.reserve(current, branch, self.config.max_parallel_tasks,
                                                     current['observation_token'])
                    if not attempt:
                        continue
                    self.cursor = (self.projects.index(project) + 1) % len(self.projects)
                    started = time.monotonic()
                    try:
                        worktree, sha = self.git.prepare(project, issue, branch)
                        self.execution.update(attempt, worktree=str(worktree), initial_sha=sha)
                        if issue.get('previous_attempt'):
                            archived = worktree.parent / ('previous-' + issue['previous_attempt'])
                            if archived.exists():
                                self.execution.update(issue['previous_attempt'], worktree=str(archived))
                        _, task, _ = self.git.paths(project, issue['number'])
                        origin = self.git.git(worktree, 'remote', 'get-url', 'origin').stdout.strip()
                        self.execution.update(attempt, origin_url=origin)
                        paths = self.attachments.prepare(issue['body'], task)
                        if self.lifecycle.stopping:
                            self.execution.finish(attempt, 'interrupted', 'force_stop' if self.lifecycle.forced else 'stop_requested',
                                                  'DISPATCHER', time.monotonic() - started)
                            return
                        process = self.executor.start(issue, branch, paths, worktree, task, attempt)
                        self.execution.update(attempt, codex_started=1, pid=process['child'].pid,
                                              process_identity=identity(process['child'].pid))
                        self.active[attempt] = dict(process, project=project, worktree=worktree, origin=origin,
                                                    initial_sha=sha, branch=branch, issue=issue, started=started, failure=None)
                    except TaskError as exc:
                        self.execution.finish(attempt, 'failed', exc.code, exc.category, time.monotonic() - started)
                    except (OSError, RuntimeError):
                        self.execution.finish(attempt, 'failed', 'internal_error', 'DISPATCHER', time.monotonic() - started)
                    break
                except (GitHubError, TaskError) as exc:
                    event(self.config.logs / 'dispatcher.jsonl', 'admission_unavailable', level='WARN', project=project.id,
                          task=candidate['number'], reason=exc.code if isinstance(exc, TaskError) else 'github_unavailable')
                    break

    def collect(self):
        if self.lifecycle.forced:
            self.lifecycle.cancel()
        live = self.lifecycle._active()
        for attempt, task in list(self.active.items()):
            child = task['child']
            if child in live and time.monotonic() >= task['deadline']:
                task['failure'] = 'codex_timeout'
                self.lifecycle.cancel_child(child)
            elif child in live:
                continue
            state, code, category = 'review', None, 'CODEX'
            try:
                self.execution.update(attempt, exit_code=child.returncode)
                if self.lifecycle.forced:
                    state, code = 'interrupted', 'force_stop'
                elif task['failure']:
                    state, code = 'failed', task['failure']
                elif child.returncode:
                    if self.executor.quota_exhausted(task['output']):
                        state, code = 'interrupted', 'quota_exhausted'
                        self.quota.exhausted()
                    else:
                        state, code = 'failed', 'codex_failed'
                else:
                    self.pr_publisher.intent(attempt)
                    self.pr_publisher.publish(task['project'], self.execution.get(attempt))
                    del self.active[attempt]
                    continue
            except TaskError as exc:
                state, code, category = 'failed', exc.code, exc.category
            except (OSError, RuntimeError):
                state, code, category = 'failed', 'internal_error', 'DISPATCHER'
            if self.pr_publisher.pending(attempt):
                self.execution.update(attempt, recovery_diagnostic='publication_unavailable')
                del self.active[attempt]
                continue
            self.execution.finish(attempt, state, code, category, time.monotonic() - task['started'])
            del self.active[attempt]
            try:
                self.adapter.reconcile(task['project'], self.store.get(task['project'].repository,
                                                                      self.execution.get(attempt)['number']))
            except GitHubError:
                pass

    def run(self):
        while not self.lifecycle.stopping or self.active:
            self.collect()
            if not self.lifecycle.stopping:
                if time.monotonic() >= self.next_poll:
                    self.sync.poll()
                    self.next_poll = time.monotonic() + self.config.poll_interval_seconds
                self.quota.check()
                self.admit()
            if time.monotonic() >= self.next_publish:
                self.pr_publisher.flush(self.projects)
                self.publisher.flush()
                self.next_publish = time.monotonic() + min(30, self.config.poll_interval_seconds)
            time.sleep(.05)
        self.publisher.flush()

    def close(self):
        self.quota.close()
        self.store.close()
