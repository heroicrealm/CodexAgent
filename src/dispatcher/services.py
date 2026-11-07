"""State, groups, polling and scheduling. External calls never hold SQL locks."""
import sqlite3
from uuid import uuid4
from .config import Config, Project
from .github import GitHubError, POLL_ID
from .input import parse_input, snapshot_hash
from .models import (ACTIVE, ApprovedSnapshot, Eligibility, Execution, ExecutionStatus,
                     InputComment, ObservationBatch, IssueObservation, PRObservation, State, SyncResult, utc_now)

TERMINAL = {State.DONE, State.CANCELLED}
FROZEN = {State.PREPARING, State.RUNNING, State.MANUAL_RUNNING, State.REVIEW,
          State.REJECTED, State.ERROR, State.NEED_FIX, State.DONE, State.CANCELLED}
ALLOWED = {
    State.DISCOVERED: {State.CANCELLED},
    State.AWAITING_CHILDREN: {State.PENDING, State.CANCELLED},
    State.PENDING: {State.AWAITING_CHILDREN, State.PREPARING, State.CANCELLED},
    State.PREPARING: {State.RUNNING, State.MANUAL_RUNNING, State.ERROR},
    State.RUNNING: {State.REVIEW, State.ERROR},
    State.MANUAL_RUNNING: {State.REVIEW},
    State.REVIEW: {State.REJECTED, State.DONE},
    State.REJECTED: {State.NEED_FIX, State.CANCELLED},
    State.ERROR: {State.NEED_FIX, State.DONE, State.CANCELLED},
    State.NEED_FIX: {State.CANCELLED},
}


class GroupService:
    def __init__(self, storage, logger=None):
        self.storage, self.logger = storage, logger

    def evaluate_db(self, db, issue_id, base_branch=None):
        row = db.execute('SELECT * FROM issues WHERE issue_id=?', (issue_id,)).fetchone()
        if not row or row['is_virtual']:
            return Eligibility(False, ('virtual-or-missing',))
        root = db.execute('SELECT * FROM issues WHERE issue_id=?', (row['execution_group_id'],)).fetchone()
        base = base_branch or root['base_branch']
        reasons = []
        children = db.execute('SELECT i.* FROM issue_relations r JOIN issues i ON i.issue_id=r.child_issue_id '
                              'WHERE r.parent_issue_id=? AND i.is_virtual=0', (issue_id,)).fetchall()
        for child in children:
            prs = db.execute('SELECT * FROM pull_requests WHERE issue_id=?', (child['issue_id'],)).fetchall()
            if not child['is_ready'] and any(pr['status'] == 'open' for pr in prs):
                reasons.append('child-ready-removed-with-open-pr')
            if any(pr['status'] == 'merged' and base and pr['base_branch'] != base for pr in prs):
                reasons.append('child-merge-target-conflict')
            # Registration and the tracked relation define the dependency;
            # is_ready only records the currently observed GitHub label.
            if child['state'] not in TERMINAL:
                reasons.append('unfinished-child')
        return Eligibility(not reasons, tuple(sorted(set(reasons))))

    def evaluate(self, issue_id):
        with self.storage.connection() as db:
            result = self.evaluate_db(db, issue_id)
        if self.logger:
            self.logger.record('verbose', 'GroupService', 'evaluate', {'issue_id': issue_id},
                               'eligible' if result.eligible else 'blocked')
        return result



class StateService:
    def __init__(self, storage, groups, config: Config, logger=None):
        self.storage, self.groups, self.config, self.logger = storage, groups, config, logger

    def refresh_db(self, db):
        rows = db.execute("SELECT issue_id,state FROM issues WHERE state IN ('pending','awaiting-children') "
                          'AND approved_at IS NOT NULL').fetchall()
        for row in rows:
            target = State.PENDING if self.groups.evaluate_db(db, row['issue_id']).eligible else State.AWAITING_CHILDREN
            self.storage._transition(db, row['issue_id'], State(row['state']), target, 'children-recheck')


    def diagnose(self, component, operation, context, result='ok'):
        if self.logger:
            self.logger.record('verbose', component, operation, context, result)

    def transition(self, issue_id, expected_state, target_state, reason):
        changed = self._apply_transition(issue_id, expected_state, target_state, reason)
        self.diagnose('StateService', 'transition', {'issue_id': issue_id}, 'ok' if changed else 'unchanged')
        return changed

    def _apply_transition(self, issue_id, expected_state, target_state, reason):
        if target_state not in ALLOWED.get(expected_state, set()):
            raise ValueError('Forbidden state transition')
        with self.storage.transaction() as db:
            if target_state in TERMINAL:
                return self._finish_group(db, issue_id, expected_state, target_state, reason)
            return self.storage._transition(db, issue_id, expected_state, target_state, reason)

    def _finish_group(self, db, issue_id, expected, target, reason):
        row = db.execute('SELECT * FROM issues WHERE issue_id=?', (issue_id,)).fetchone()
        if not row or row['state'] != expected:
            return False
        members = db.execute('SELECT * FROM issues WHERE execution_group_id=?', (row['execution_group_id'],)).fetchall()
        if any(m['state'] in (State.PREPARING, State.RUNNING, State.MANUAL_RUNNING) for m in members):
            raise ValueError('Group is executing; terminal command is forbidden')
        for member in members:
            self.storage._transition(db, member['issue_id'], State(member['state']), target, reason)
        self.refresh_db(db)
        return True

    def approve(self, project, issue, comment, source=None):
        row = self.storage.issue(issue.issue_id)
        if not row or row['is_virtual'] or row['approved_at'] is not None:
            return False
        params, raw = parse_input(comment.body if comment else None)
        correction = None
        branches = None
        if params.correction_for is not None:
            if not source or source.repository != issue.repository or source.issue_number != params.correction_for:
                raise ValueError('Correction source was not fully resolved')
            correction = source.issue_id
        else:
            base = params.base_branch or project.base_branch
            from .input import branch_name
            branch_name(base)
            branches = (base, params.branch or f"{'codex' if params.run_codex else 'manual'}/task-{issue.issue_number:04d}")
            if branches[0] == branches[1]:
                raise ValueError('Work branch must differ from base branch')
        snapshot = ApprovedSnapshot(issue.title, issue.body, raw,
            snapshot_hash(issue.title, issue.body, comment.body if comment else None),
            comment.id if comment else None, comment.body if comment else None)
        changed = self.storage.commit_approval(issue.issue_id, snapshot, State.PENDING,
            correction_of=correction, branches=branches,
            readiness=lambda db: self.groups.evaluate_db(db, issue.issue_id,
                branches[0] if branches else None).eligible)
        self.diagnose('StateService', 'approve', {'issue_id': issue.issue_id}, 'ok' if changed else 'unchanged')
        return changed

    def apply_observation(self, project, batch):
        labels = self._labels(project)
        registered = 0
        with self.storage.transaction() as db:
            before = db.execute("SELECT count(*) FROM state_history").fetchone()[0]
            for issue in batch.issues:
                old = db.execute('SELECT * FROM issues WHERE issue_id=?', (issue.issue_id,)).fetchone()
                if labels['codex-ready'] in issue.labels:
                    if not old or old['is_virtual']:
                        self.storage._register(db, issue, project.id, None)
                        registered += 1
                if db.execute('SELECT 1 FROM issues WHERE issue_id=?', (issue.issue_id,)).fetchone():
                    db.execute('UPDATE issues SET github_state=?,is_ready=?,is_approved=?,title=?,last_seen_at=?,updated_at=? WHERE issue_id=?',
                        (issue.github_state, int(labels['codex-ready'] in issue.labels), int(labels['approve'] in issue.labels),
                         issue.title, utc_now(), utc_now(), issue.issue_id))
            for child_id, parent in batch.parents.items():
                child = db.execute('SELECT * FROM issues WHERE issue_id=?', (child_id,)).fetchone()
                if not child or child['is_virtual']:
                    continue
                # Existing parent relations may change only before their execution.
                db.execute("DELETE FROM issue_relations WHERE child_issue_id=? AND parent_issue_id IN "
                    "(SELECT issue_id FROM issues WHERE state IS NULL OR state IN ('discovered','pending','awaiting-children'))",
                    (child_id,))
                if parent:
                    existing = db.execute('SELECT * FROM issues WHERE issue_id=?', (parent.issue_id,)).fetchone()
                    if not existing:
                        now = utc_now()
                        db.execute('INSERT INTO issues(issue_id,repository,issue_number,project_id,is_virtual,title,created_at,updated_at) '
                                   'VALUES (?,?,?,?,1,?,?,?)', (parent.issue_id, parent.repository, parent.issue_number, project.id, parent.title, now, now))
                        existing = db.execute('SELECT * FROM issues WHERE issue_id=?', (parent.issue_id,)).fetchone()
                    if parent.repository != child['repository']:
                        raise ValueError('Cross-repository parent is unsupported')
                    if existing['state'] not in FROZEN:
                        db.execute('INSERT OR IGNORE INTO issue_relations VALUES (?,?)', (parent.issue_id, child_id))
            for parent_id, children in batch.children.items():
                parent = db.execute('SELECT * FROM issues WHERE issue_id=?', (parent_id,)).fetchone()
                if not parent or parent['state'] in FROZEN:
                    continue
                db.execute('DELETE FROM issue_relations WHERE parent_issue_id=?', (parent_id,))
                for child in children:
                    tracked = db.execute('SELECT * FROM issues WHERE issue_id=? AND is_virtual=0', (child.issue_id,)).fetchone()
                    if tracked:
                        db.execute('INSERT OR IGNORE INTO issue_relations VALUES (?,?)', (parent_id, child.issue_id))
            self.refresh_db(db)
            transitioned = db.execute("SELECT count(*) FROM state_history").fetchone()[0] - before - registered
        self.diagnose("StateService", "apply_observation", {"poll_id": batch.poll_id, "repository": project.repository})
        return SyncResult(registered, transitioned)

    def _labels(self, project):
        return self.config.effective_labels(project)

    def apply_pr(self, pr, *, recover=False):
        with self.storage.transaction() as db:
            issue = db.execute('SELECT * FROM issues WHERE issue_id=?', (pr.issue_id,)).fetchone()
            root = db.execute('SELECT * FROM issues WHERE issue_id=?', (issue['execution_group_id'],)).fetchone() if issue else None
            if not issue or issue['repository'] != pr.repository or root['work_branch'] != pr.head_branch:
                raise ValueError('PR does not match execution group')
            existing = db.execute('SELECT issue_id FROM pull_requests WHERE pr_id=?', (pr.pr_id,)).fetchone()
            if existing and existing['issue_id'] != pr.issue_id:
                raise ValueError('PR already belongs to another issue')
            db.execute('INSERT INTO pull_requests VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(pr_id) DO UPDATE SET '
                       'head_branch=excluded.head_branch,base_branch=excluded.base_branch,status=excluded.status,'
                       'merged_at=excluded.merged_at,updated_at=excluded.updated_at',
                (pr.pr_id, pr.issue_id, pr.repository, pr.pr_number, pr.head_branch, pr.base_branch, pr.status, pr.merged_at, utc_now()))
            if pr.head_commit:
                db.execute('UPDATE executions SET result_commit=? WHERE issue_id=? AND push_completed=0', (pr.head_commit, pr.issue_id))
            current = State(issue['state'])
            if (current in (State.MANUAL_RUNNING, State.RUNNING) or
                    (recover and current in (State.PREPARING, State.ERROR))):
                self.storage._transition(db, pr.issue_id, current, State.REVIEW, 'pr-found')
                current = State.REVIEW
            if current == State.REVIEW:
                if pr.status == 'merged':
                    self._finish_group(db, pr.issue_id, current, State.DONE, 'pr-merged')
                elif pr.status == 'closed':
                    self.storage._transition(db, pr.issue_id, current, State.REJECTED, 'pr-closed')
            self.refresh_db(db)


class PollService:
    def __init__(self, config, storage, github, state, publisher, logger):
        self.config, self.storage, self.github = config, storage, github
        self.state, self.publisher, self.logger = state, publisher, logger

    def collect(self, project):
        poll_id = POLL_ID.get() or 'poll-' + uuid4().hex
        issues = self.github.issues(project, poll_id)
        labels = self.config.effective_labels(project)
        known = {r['issue_id']: r for r in self.storage.rows('SELECT * FROM issues WHERE project_id=?', (project.id,))}
        batch = ObservationBatch(poll_id, issues)
        for issue in issues:
            old = known.get(issue.issue_id)
            if not old and labels['codex-ready'] not in issue.labels:
                continue
            batch.parents[issue.issue_id] = self.github.parent(project, issue.issue_number, poll_id)
            if not old or old['state'] not in FROZEN:
                batch.children[issue.issue_id] = self.github.children(project, issue.issue_number, poll_id)
            if labels['approve'] in issue.labels and (not old or old['approved_at'] is None):
                fresh = self.github.issue(project, issue.issue_number, poll_id)
                # A changed command/readiness context must not approve a stale observation.
                if labels['approve'] not in fresh.labels or (not old and labels['codex-ready'] not in fresh.labels):
                    raise ValueError('Approval context changed while reading')
                try:
                    comment = self.github.input_comment(project, issue.issue_number, poll_id)
                except ValueError as exc:
                    batch.approval_errors[issue.issue_id] = exc
                    continue
                batch.approvals[issue.issue_id] = (fresh, comment)
                try:
                    params, _ = parse_input(comment.body if comment else None)
                except ValueError:
                    continue  # StateService reports invalid INPUT without discarding registrations.
                if params.correction_for:
                    try:
                        batch.corrections[issue.issue_id] = self.github.issue(project, params.correction_for, poll_id)
                    except GitHubError as exc:
                        if exc.status != 404:
                            raise
                    except ValueError as exc:
                        batch.approval_errors[issue.issue_id] = exc
        prs = []
        for row in self.storage.rows("SELECT p.* FROM pull_requests p JOIN issues i ON i.issue_id=p.issue_id "
                                     "WHERE i.project_id=? AND p.status='open'", (project.id,)):
            prs.append(self.github.pr(project, row['pr_number'], row['issue_id'], poll_id))
        for row in known.values():
            if row['state'] == State.MANUAL_RUNNING:
                pr = self.publisher.find_pr(row, poll_id)
                if pr:
                    prs.append(pr)
        batch.prs = tuple(prs)
        return batch

    def poll_project(self, project):
        token = POLL_ID.set('poll-' + uuid4().hex)
        try:
            return self._poll_project(project)
        finally:
            POLL_ID.reset(token)

    def _poll_project(self, project):
        batch = self.collect(project)  # no mutations before complete reads
        result = self.state.apply_observation(project, batch)
        labels = self.config.effective_labels(project)
        for pr in batch.prs:
            self.state.apply_pr(pr)
        for observation in sorted(batch.issues, key=lambda item: (labels['approve'] in item.labels, item.issue_id)):
            row = self.storage.issue(observation.issue_id)
            if not row or row['is_virtual']:
                continue
            commands = {key for key in ('approve', 'need-fix', 'cancelled', 'done') if labels[key] in observation.labels}
            # Terminal status labels are not incoming commands after completion.
            if row['state'] in TERMINAL:
                commands.clear()
                if observation.github_state == 'open':
                    self.publisher._closed.discard(row['issue_id'])
            if row['state'] == State.NEED_FIX:
                commands.discard('need-fix')
            if len(commands) > 1:
                self.publisher.approval_error(project, row, 'Conflicting commands; correct the labels')
                if 'approve' in commands:
                    self.github.remove_label(project, row['issue_number'], labels['approve'], batch.poll_id)
                continue
            try:
                if 'approve' in commands:
                    if observation.issue_id in batch.approval_errors:
                        raise batch.approval_errors[observation.issue_id]
                    if observation.issue_id in batch.approvals:
                        fresh, comment = batch.approvals[observation.issue_id]
                        self.state.approve(project, fresh, comment, batch.corrections.get(observation.issue_id))
                    self.github.remove_label(project, row['issue_number'], labels['approve'], batch.poll_id)
                elif commands:
                    command = next(iter(commands))
                    self.state.transition(row['issue_id'], State(row['state']), State(command), 'user-command')
            except ValueError as exc:
                # Keep the original exception private; publish a controlled summary.
                self.publisher.approval_error(project, row, exc)
                if 'approve' in commands:
                    self.github.remove_label(project, row['issue_number'], labels['approve'], batch.poll_id)
            current = self.storage.issue(row['issue_id'])
            self.github.sync_labels(project, row['issue_number'], current['state'], observation.labels, batch.poll_id)
        self.publisher.reconcile(project)
        self.logger.record('verbose', 'PollService', 'poll', {'poll_id': batch.poll_id, 'repository': project.repository}, 'ok')
        return result


class Scheduler:
    def __init__(self, storage, groups, state, logger=None):
        self.storage, self.groups, self.state, self.logger = storage, groups, state, logger

    def reserve(self, issue_id):
        result = self._reserve(issue_id)
        if self.logger:
            self.logger.record('verbose', 'Scheduler', 'reserve', {'issue_id': issue_id},
                               'reserved' if result else 'unavailable')
        return result

    def _reserve(self, issue_id):
        with self.storage.transaction() as db:
            # One process slot; manual-running waits for a human without occupying it.
            if db.execute("SELECT 1 FROM issues WHERE state IN ('preparing','running')").fetchone():
                return None
            self.state.refresh_db(db)
            row = db.execute('SELECT * FROM issues WHERE issue_id=?', (issue_id,)).fetchone()
            if not row or row['state'] != State.PENDING or not row['approved_at']:
                return None
            if not self.groups.evaluate_db(db, issue_id).eligible:
                return None
            if db.execute('SELECT 1 FROM executions WHERE issue_id=?', (issue_id,)).fetchone():
                return None
            if any(r['state'] in ACTIVE for r in db.execute(
                    'SELECT state FROM issues WHERE execution_group_id=? AND issue_id<>?',
                    (row['execution_group_id'], issue_id))):
                return None
            if not self.storage._transition(db, issue_id, State.PENDING, State.PREPARING, 'scheduler'):
                return None
            cursor = db.execute("INSERT INTO executions(issue_id,status,started_at) VALUES (?,'reserved',?)", (issue_id, utc_now()))
            return Execution(cursor.lastrowid, issue_id, ExecutionStatus.RESERVED)

    def next(self):
        for row in self.storage.rows("SELECT issue_id FROM issues WHERE state='pending' ORDER BY issue_id"):
            execution = self.reserve(row['issue_id'])
            if execution:
                return execution
        return None
