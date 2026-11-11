"""Offline administration: read external facts first, persist intent before effects."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .db_guard import check, inspection, quiescent, validate
from .locking import InstanceLock
from .models import State, utc_now
from .storage import SchemaError, Storage
from .services import GroupService

RETRY_COMMENT = (
    'Codex Dispatcher — Recovery retry. Предыдущее незавершённое исполнение сброшено '
    'после db restore; опубликованный результат не обнаружен; локальная работа удалена; '
    'диагностика сохранена; Issue возвращена в discovered. Для повторного выполнения '
    'проверьте постановку и заново установите dispatcher:approve'
)


class RecoveryLogger:
    """Buffer content-free API metrics; dry-run never writes debug_operations."""
    def __init__(self, config):
        self.mode, self.pending = config.debug_mode, []

    def record(self, level, component, operation, context, result):
        if self.mode == 'off' or (self.mode == 'api' and level != 'api'):
            return
        # GitHubAdapter supplies route templates and numeric metrics, never bodies.
        from .diagnostics import _CODE, _ROUTE
        if not all(isinstance(v, str) and _CODE.fullmatch(v) for v in (component, operation, result)):
            return
        safe = {}
        for key in ('duration_ms', 'http_status'):
            value = context.get(key)
            if type(value) is int and value >= 0:
                safe[key] = value
        if context.get('method') in ('GET', 'POST', 'DELETE', 'PATCH'):
            safe['method'] = context['method']
        endpoint = context.get('endpoint')
        if isinstance(endpoint, str) and _ROUTE.fullmatch(endpoint):
            safe['endpoint'] = endpoint
        self.pending.append({'created_at': utc_now(), 'level': level, 'component': component,
                             'operation': operation, 'result': result, **safe})

    def flush(self, db):
        for entry in self.pending:
            columns = list(entry)
            db.execute('INSERT INTO debug_operations(' + ','.join(columns) + ') VALUES (' +
                       ','.join('?' for _ in columns) + ')', tuple(entry.values()))
        # Clear only after the enclosing transaction commits (the caller does this).


class RecoveryStore:
    """Dedicated administrative API; never weakens Storage.connection admission."""
    def __init__(self, config):
        self.config = config

    def snapshot(self):
        with inspection(self.config.database) as db:
            validate(db)
            db.row_factory = sqlite3.Row
            return self.snapshot_db(db)

    @staticmethod
    def snapshot_db(db):
        return {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)]
                for table in ('issues', 'executions', 'pull_requests', 'issue_relations',
                              'recovery_quarantine', 'recovery_operations')}

    @contextmanager
    def transaction(self):
        # Only Recovery uses this, inside the full instance lock.
        check(self.config)
        db = sqlite3.connect(self.config.database, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            validate_tuple = validate_with_rows(db)
            if validate_tuple[1] != 2:
                raise SchemaError('Explicit db upgrade required')
            yield db
            logger = getattr(self, 'logger', None)
            if logger:
                logger.flush(db)
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise SchemaError('Recovery foreign key violation')
            db.commit()
            if logger:
                logger.pending.clear()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def quarantine(db, issue_id, reason, details):
        now = utc_now()
        db.execute('INSERT INTO recovery_quarantine VALUES (?,?,?,?,?) '
                   'ON CONFLICT(issue_id,reason_code) DO UPDATE SET details=excluded.details,updated_at=excluded.updated_at',
                   (issue_id, reason, details, now, now))

    @staticmethod
    def event(db, issue_id, action, *, session=None, operation=None, details='{}', reason=None):
        db.execute('INSERT INTO recovery_events(issue_id,session_id,operation_id,action,reason_code,details,created_at) '
                   'VALUES (?,?,?,?,?,?,?)', (issue_id, session, operation, action, reason, details, utc_now()))

    @staticmethod
    def transition(db, issue, target, operation=None):
        if issue['state'] == target:
            return
        db.execute('UPDATE issues SET state=?,updated_at=? WHERE issue_id=?', (target, utc_now(), issue['issue_id']))
        db.execute('INSERT INTO state_history(issue_id,old_state,new_state,reason,created_at) VALUES (?,?,?,?,?)',
                   (issue['issue_id'], issue['state'], target, 'recovery:' + (operation or 'reconcile'), utc_now()))


def validate_with_rows(db):
    factory = db.row_factory
    db.row_factory = None
    try:
        return validate(db)
    finally:
        db.row_factory = factory


class Recovery:
    def __init__(self, config, github, git, fault=None):
        self.config, self.github, self.git = config, github, git
        self.logger = RecoveryLogger(config)
        if hasattr(github, 'logger'):
            github.logger = self.logger
        self.store = RecoveryStore(config)
        self.store.logger = self.logger
        self.fault = fault or (lambda phase: None)

    def project(self, repository):
        return next((p for p in self.config.projects if p.repository.casefold() == repository.casefold()), None)

    def status(self):
        meta = check(self.config)
        if meta[1] != 2:
            return {'format_version': meta[1], 'recovery_required': bool(meta[3]),
                    'upgrade_required': True, 'quarantine': [], 'operations': []}
        data = self.store.snapshot()
        return {'format_version': meta[1], 'recovery_required': bool(meta[3]), 'quarantine': data['recovery_quarantine'],
                'operations': [{k: v for k, v in o.items() if k != 'details'}
                               for o in data['recovery_operations']]}

    def local(self, project):
        """No fetch, clone, staging, index refresh or other Git mutations."""
        configured = (project.checkout or self.config.workspace / 'repos' / project.id).absolute()
        if any(p.is_symlink() for p in (configured, *configured.parents)):
            raise SchemaError('Symlink checkout identity is unproven')
        checkout = self.git.checkout(project, create=False)
        origin = self.git.run(checkout, 'remote', 'get-url', 'origin')
        # Credentials and arbitrary local repositories are not identity evidence.
        repo = project.repository.casefold()
        normalized = origin.casefold().removesuffix('.git').rstrip('/')
        if normalized not in ('git@github.com:' + repo, 'https://github.com/' + repo,
                              'ssh://git@github.com/' + repo):
            raise SchemaError('Checkout origin identity is unproven')
        remote = self.git.run(checkout, 'ls-remote', 'origin')
        refs = dict(line.split()[::-1] for line in remote.splitlines())
        trees = self.git.worktrees(project)
        for tree in trees:
            path = Path(tree['worktree'])
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise SchemaError('Symlink worktree identity is unproven')
            if path.exists():
                tree['dirty'] = self.git.run(path, '--no-optional-locks', 'status', '--porcelain',
                                              '--untracked-files=all', '--ignored=matching')
        if not trees or trees[0].get('dirty'):
            raise SchemaError('Unknown checkout activity; reconciliation refused')
        local_refs = self.git.run(checkout, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
        return {'remote': refs, 'trees': trees, 'refs': local_refs}

    def collect(self):
        data = self.store.snapshot()
        result = {}
        for repository in sorted({r['repository'].casefold() for r in data['issues']} |
                                 {p.repository for p in self.config.projects if p.enabled}):
            project = self.project(repository)
            if not project or not project.enabled:
                # Persistent isolation survives later configuration changes.
                result[repository] = None
                continue
            local = self.local(project)
            known_paths = {e['worktree_path'] for e in data['executions'] if e['worktree_path']}
            for tree in local['trees'][1:]:
                if tree['worktree'] not in known_paths:
                    raise SchemaError('Unknown worktree activity; reconciliation refused')
            owned_refs = {'refs/heads/dispatcher-v2/' + Path(path).name[2:] for path in known_paths
                          if re.fullmatch(r'e-[0-9]+-[0-9a-f]{32}', Path(path).name)}
            for line in local['refs'].splitlines():
                ref = line.split()[0]
                if ref.startswith('refs/heads/dispatcher-v2/') and ref not in owned_refs:
                    raise SchemaError('Unknown internal local branch; reconciliation refused')
            observations = {}
            opened = self.github.issues(project, 'recovery')
            tracked_ids = {r['issue_id'] for r in data['issues']}
            for obs in opened:
                if obs.issue_id in tracked_ids or self.config.effective_labels(project)['codex-ready'] in obs.labels:
                    observations[obs.issue_id] = obs
            for issue in data['issues']:
                if issue['repository'].casefold() != repository:
                    continue
                obs = self.github.issue(project, issue['issue_number'], 'recovery')
                if (obs.issue_id, obs.repository.casefold(), obs.issue_number) != (
                        issue['issue_id'], repository, issue['issue_number']):
                    raise SchemaError('GitHub Issue identity mismatch')
                observations[obs.issue_id] = obs
            # One all-state PR batch per repository, including previously closed PRs.
            prs = list(self.github.pages(f'repos/{repository}/pulls?state=all'))
            for saved in data['pull_requests']:
                if saved['repository'].casefold() == repository and not any(p['id'] == saved['pr_id'] for p in prs):
                    pr = self.github.request('GET', f"repos/{repository}/pulls/{saved['pr_number']}",
                                             route='/repos/{owner}/{repo}/pulls/{number}')
                    if (pr['id'], pr['number']) != (saved['pr_id'], saved['pr_number']):
                        raise SchemaError('GitHub PR identity mismatch')
                    prs.append(pr)
            prs.sort(key=lambda p: p['id'])
            details = {}
            for issue_id, obs in sorted(observations.items()):
                children = self.github.children(project, obs.issue_number, 'recovery')
                parent = self.github.parent(project, obs.issue_number, 'recovery')
                comment = self.github.input_comment(project, obs.issue_number, 'recovery')
                details[issue_id] = {'issue': asdict(obs), 'children': sorted(c.issue_id for c in children),
                                     'parent': parent.issue_id if parent else None,
                                     'input': asdict(comment) if comment else None}
            result[repository] = {'local': local, 'issues': details, 'prs': prs}
        return data, result

    @staticmethod
    def candidates(issue, facts, branch):
        return [p for p in facts['prs'] if p['head']['ref'] == branch or
                re.search(r'(?:Dispatcher result for #|/issues/)' + str(issue['issue_number']) + r'\b',
                          p.get('body') or '')]

    def proven_pr(self, issue, data, facts):
        root = next(r for r in data['issues'] if r['issue_id'] == issue['execution_group_id'])
        others = {p['pr_id'] for p in data['pull_requests'] if p['issue_id'] != issue['issue_id']}
        matches = [p for p in self.candidates(issue, facts, root['work_branch']) if p['id'] not in others]
        if not matches:
            return None
        if len(matches) != 1:
            raise SchemaError('Multiple or conflicting PRs')
        pr = matches[0]
        known = next((p for p in data['pull_requests'] if p['pr_id'] == pr['id'] and
                      p['issue_id'] == issue['issue_id'] and p['pr_number'] == pr['number']), None)
        execution = next((e for e in data['executions'] if e['issue_id'] == issue['issue_id']), None)
        sha = execution['result_commit'] if execution else None
        body_proof = f"Dispatcher result for #{issue['issue_number']}" == (pr.get('body') or '').strip()
        if (pr['head']['ref'] != root['work_branch'] or
            (pr['head'].get('repo') or {}).get('full_name', '').casefold() != issue['repository'].casefold() or
            (pr['base'].get('repo') or {}).get('full_name', '').casefold() != issue['repository'].casefold() or
            not (known or (body_proof and sha == pr['head'].get('sha') and sha))):
            raise SchemaError('PR ownership is unproven')
        return pr

    def plan(self, data, facts):
        plan = []
        for issue in sorted(data['issues'], key=lambda r: r['issue_id']):
            reasons, target, pr = [], issue['state'], None
            external = facts[issue['repository'].casefold()]
            if external is None:
                reasons.append('state-conflict')
            elif not issue['is_virtual']:
                obs = external['issues'][issue['issue_id']]['issue']
                execution = next((e for e in data['executions'] if e['issue_id'] == issue['issue_id']), None)
                root = next(r for r in data['issues'] if r['issue_id'] == issue['execution_group_id'])
                branch = root['work_branch']
                if not branch:
                    from .input import parse_input
                    comment = external['issues'][issue['issue_id']]['input']
                    try:
                        params, _ = parse_input(comment['body'] if comment else None)
                        branch = params.branch or f"{'codex' if params.run_codex else 'manual'}/task-{issue['issue_number']:04d}"
                    except ValueError:
                        reasons.append('approval-uncertain')
                try:
                    if root['work_branch']:
                        pr = self.proven_pr(issue, data, external)
                    elif self.candidates(issue, external, branch):
                        reasons.append('pr-conflict')
                except SchemaError:
                    reasons.append('pr-conflict')
                if issue['state'] in ('preparing', 'running'):
                    reasons.append('execution-uncertain')
                if execution and issue['state'] in ('discovered', 'pending', 'awaiting-children'):
                    reasons.append('execution-uncertain')
                if execution and execution['worktree_path'] and any(
                        t['worktree'] == execution['worktree_path'] for t in external['local']['trees']):
                    reasons.append('local-worktree')
                if branch and 'refs/heads/' + branch in external['local']['remote'] and not pr:
                    if issue['state'] != 'manual-running':
                        reasons.append('branch-conflict')
                if branch and any(line.startswith('refs/heads/' + branch + ' ')
                                  for line in external['local']['refs'].splitlines()):
                    reasons.append('branch-conflict')
                project = self.project(issue['repository'])
                labels = self.config.effective_labels(project)
                if labels['approve'] in obs['labels']:
                    reasons.append('approval-uncertain')
                if issue['state'] in ('pending', 'awaiting-children'):
                    if not issue['approved_at'] or labels['codex-ready'] not in obs['labels']:
                        reasons.append('approval-uncertain')
                status_labels = {value for key, value in labels.items() if key not in ('codex-ready', 'approve')}
                expected_label = labels.get(issue['state'])
                if (set(obs['labels']) & status_labels) - {expected_label}:
                    reasons.append('state-conflict')
                if issue['approved_at']:
                    from .input import snapshot_hash
                    comment = external['issues'][issue['issue_id']]['input']
                    if snapshot_hash(obs['title'], obs['body'], comment['body'] if comment else None) != issue['approved_hash']:
                        reasons.append('approval-uncertain')
                old_children = sorted(r['child_issue_id'] for r in data['issue_relations']
                                      if r['parent_issue_id'] == issue['issue_id'])
                tracked = {r['issue_id'] for r in data['issues']}
                new_children = [c for c in external['issues'][issue['issue_id']]['children'] if c in tracked]
                if old_children != new_children:
                    reasons.append('group-dependency')
                old_parents = sorted(r['parent_issue_id'] for r in data['issue_relations']
                                     if r['child_issue_id'] == issue['issue_id'])
                observed_parent = external['issues'][issue['issue_id']]['parent']
                new_parents = [observed_parent] if observed_parent in tracked else []
                if old_parents != new_parents:
                    reasons.append('group-dependency')
                if obs['github_state'] == 'closed' and issue['state'] not in ('done', 'cancelled'):
                    if not pr:
                        reasons.append('state-conflict')
                if issue['state'] == 'review' and not pr:
                    reasons.append('pr-conflict')
                if pr and not reasons:
                    try:
                        target = self.pr_target(issue, pr)
                    except SchemaError:
                        reasons.append('state-conflict')
                if target in ('done', 'cancelled') and target != issue['state']:
                    members = [r for r in data['issues'] if r['execution_group_id'] == issue['execution_group_id']]
                    if any(m['state'] in ('preparing', 'running', 'manual-running') for m in members):
                        reasons.append('group-dependency')
                        target = issue['state']
            plan.append({'issue_id': issue['issue_id'], 'reasons': sorted(set(reasons)), 'target': target,
                         'pr': pr})
        known_ids = {r['issue_id'] for r in data['issues']}
        for repository, external in facts.items():
            if external is None:
                continue
            for issue_id, details in external['issues'].items():
                if issue_id not in known_ids:
                    # New after backup: do not trust an approval that may predate restore.
                    plan.append({'issue_id': issue_id, 'reasons': ['approval-uncertain'],
                                 'target': 'discovered', 'pr': None, 'new': details['issue']})
        return sorted(plan, key=lambda r: r['issue_id'])

    @staticmethod
    def pr_target(issue, pr):
        target = 'done' if pr.get('merged_at') else ('review' if pr['state'] == 'open' else 'rejected')
        allowed = {'review': {'review', 'done', 'rejected'}, 'running': {'review'},
                   'preparing': {'review'}, 'manual-running': {'review'}, 'error': {'review', 'done'}}
        if target != issue['state'] and target not in allowed.get(issue['state'], set()):
            raise SchemaError('PR requires an unsupported recovery transition')
        return target

    def recover(self, dry_run=False):
        try:
            return self._recover(dry_run)
        except Exception as exc:
            if not dry_run:
                try:
                    check(self.config)  # never write through an unverified workspace
                    from .error_diagnostics import private_directory
                    import os
                    area = self.config.workspace / 'diagnostics'
                    target = area / 'recovery-errors.jsonl'
                    if any(p.is_symlink() for p in (target, *target.absolute().parents)):
                        raise SchemaError('Symlink diagnostics path')
                    private_directory(area)
                    fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'a') as stream:
                        stream.write(json.dumps({'at': utc_now(), 'error_type': type(exc).__name__,
                                                 'action': 'recover', 'admission': 'unchanged',
                                                 'api_operations': self.logger.pending}) + '\n')
                except (OSError, SchemaError):
                    pass  # diagnostics must not mask the refusal
            raise

    def _recover(self, dry_run=False):
        # dry-run does not even create lock files.
        def analyze():
            meta = check(self.config)
            if meta[1] != 2:
                raise SchemaError('Explicit db upgrade required')
            quiescent(self.config)
            data, facts = self.collect()
            plan = self.plan(data, facts)
            return data, facts, plan
        if dry_run:
            return self.public_plan(analyze()[2])
        with InstanceLock(self.config):
            data, facts, plan = analyze()
            # Re-read critical external facts outside the committing transaction.
            if (data, facts) != self.collect():
                raise SchemaError('External prerequisites changed; retry reconciliation')
            session, now = uuid4().hex, utc_now()
            with self.store.transaction() as db:
                if self.store.snapshot_db(db) != data:
                    raise SchemaError('SQLite prerequisites changed before admission commit')
                db.execute('INSERT INTO recovery_sessions VALUES (?,?,?,?,?,?,?)',
                           (session, now, utc_now(), 'completed', len(plan),
                            sum(bool(p['reasons']) for p in plan),
                            json.dumps([{'issue_id': p['issue_id'], 'reasons': p['reasons'], 'target': p['target']}
                                        for p in plan], sort_keys=True)))
                for entry in plan:
                    if 'new' in entry:
                        from .models import IssueObservation
                        observation = IssueObservation(**entry['new'])
                        Storage._register(db, observation, self.project(observation.repository).id, None)
                    issue = db.execute('SELECT * FROM issues WHERE issue_id=?', (entry['issue_id'],)).fetchone()
                    for reason in entry['reasons']:
                        self.store.quarantine(db, issue['issue_id'], reason, 'External reconciliation conflict')
                    self.store.event(db, issue['issue_id'], 'reconcile', session=session,
                                     details=json.dumps({'reasons': entry['reasons']}))
                for entry in plan:
                    issue = db.execute('SELECT * FROM issues WHERE issue_id=?', (entry['issue_id'],)).fetchone()
                    if not entry['reasons'] and not Storage.blocked_db(db, issue['issue_id']):
                        self.apply_result(db, issue, entry['pr'], entry['target'])
                # Use existing dependency rules with quarantine-aware evaluation.
                groups = GroupService(None)
                rows = db.execute("SELECT * FROM issues WHERE state IN ('pending','awaiting-children')").fetchall()
                for row in rows:
                    if not Storage.blocked_db(db, row['issue_id']):
                        target = 'pending' if groups.evaluate_db(db, row['issue_id']).eligible else 'awaiting-children'
                        self.store.transition(db, row, target)
                db.execute('UPDATE dispatcher_metadata SET recovery_required=0')
            return self.public_plan(plan)

    @staticmethod
    def public_plan(plan):
        return [{'issue_id': p['issue_id'], 'reasons': p['reasons'], 'target': p['target']} for p in plan]

    def apply_result(self, db, issue, pr, target, operation=None):
        if pr:
            from .github import GitHubAdapter
            value = GitHubAdapter.pr_observation(pr, issue['issue_id'], issue['repository'])
            db.execute('INSERT INTO pull_requests VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(pr_id) DO UPDATE SET '
                       'status=excluded.status,base_branch=excluded.base_branch,merged_at=excluded.merged_at,updated_at=excluded.updated_at',
                       (value.pr_id, value.issue_id, value.repository, value.pr_number, value.head_branch,
                        value.base_branch, value.status, value.merged_at, utc_now()))
            db.execute("UPDATE executions SET result_commit=?,pr_published=1,push_completed=1,status='completed',"
                       "finished_at=coalesce(finished_at,?) WHERE issue_id=?",
                       (value.head_commit, utc_now(), issue['issue_id']))
        if target in ('done', 'cancelled') and target != issue['state']:
            members = db.execute('SELECT * FROM issues WHERE execution_group_id=?',
                                 (issue['execution_group_id'],)).fetchall()
            if any(m['state'] in ('preparing', 'running', 'manual-running') for m in members):
                raise SchemaError('Group is executing; terminal recovery forbidden')
            for member in members:
                if member['issue_id'] != issue['issue_id'] and (
                        db.execute('SELECT 1 FROM recovery_quarantine WHERE issue_id=?',
                                   (member['issue_id'],)).fetchone() or
                        db.execute("SELECT 1 FROM recovery_operations WHERE issue_id=? AND phase<>'completed'",
                                   (member['issue_id'],)).fetchone()):
                    raise SchemaError('Another group member is quarantined')
                children = db.execute('SELECT child_issue_id FROM issue_relations WHERE parent_issue_id=?',
                                      (member['issue_id'],)).fetchall()
                if any(Storage.blocked_db(db, c[0]) for c in children):
                    raise SchemaError('Quarantined child prevents terminal recovery')
            for member in members:
                self.store.transition(db, member, target, operation)
            return
        self.store.transition(db, issue, target, operation)

    def command(self, action, project_id, number):
        with InstanceLock(self.config):
            if check(self.config)[1] != 2:
                raise SchemaError('Explicit db upgrade required')
            quiescent(self.config)
            project = next((p for p in self.config.projects if p.id == project_id and p.enabled), None)
            if not project:
                raise SchemaError('Enabled current project required')
            data = self.store.snapshot()
            issue = next((r for r in data['issues'] if r['repository'].casefold() == project.repository and
                          r['issue_number'] == number and not r['is_virtual']), None)
            if not issue:
                raise SchemaError('Unknown Issue identity')
            operation = next((o for o in data['recovery_operations'] if o['issue_id'] == issue['issue_id'] and
                              o['phase'] != 'completed'), None)
            if operation and operation['action'] != action:
                raise SchemaError('Another recovery operation must finish first')
            if not any(q['issue_id'] == issue['issue_id'] for q in data['recovery_quarantine']):
                completed = [o for o in data['recovery_operations'] if o['issue_id'] == issue['issue_id']
                             and o['action'] == action and o['phase'] == 'completed']
                if (completed and action == 'retry' and issue['state'] == 'discovered'
                        and issue['approved_at'] is None):
                    return sorted(completed, key=lambda o: o['updated_at'])[-1]['operation_id']
                raise SchemaError('Issue must be in recovery quarantine')
            if action == 'retry':
                return self.retry(project, issue, operation)
            _, facts = self.collect()
            external = facts[project.repository]
            pr = self.proven_pr(issue, data, external)
            if action == 'accept':
                if not pr:
                    raise SchemaError('Proven PR required')
                target = self.pr_target(issue, pr)
                # Local artifacts and uncertain approval remain independently blocked.
                resolved = {'pr-conflict', 'execution-uncertain', 'state-conflict'}
                execution = next((e for e in data['executions'] if e['issue_id'] == issue['issue_id']), None)
                if execution and execution['worktree_path']:
                    path = Path(execution['worktree_path']).absolute()
                    match = re.fullmatch(r'e-([0-9]+)-([0-9a-f]{32})', path.name)
                    owned = (match and int(match[1]) == execution['execution_id'] and
                             path.parent == self.git.area(project) and
                             not any(p.is_symlink() for p in (path, *path.parents)))
                    tree = next((t for t in external['local']['trees'][1:] if t['worktree'] == str(path)), None)
                    if (owned and tree and not tree.get('dirty') and
                            tree.get('branch') == 'refs/heads/dispatcher-v2/' + path.name[2:] and
                            tree.get('HEAD') == pr['head']['sha']):
                        resolved.add('local-worktree')
                    elif owned and not path.exists() and not tree:
                        resolved.add('local-worktree')
                branch = next(r['work_branch'] for r in data['issues']
                              if r['issue_id'] == issue['execution_group_id'])
                local_refs = dict(line.split() for line in external['local']['refs'].splitlines())
                remote_tip = external['local']['remote'].get('refs/heads/' + branch)
                local_tip = local_refs.get('refs/heads/' + branch)
                if ((not remote_tip or remote_tip == pr['head']['sha']) and
                        (not local_tip or local_tip == pr['head']['sha'])):
                    resolved.add('branch-conflict')
                observation = external['issues'][issue['issue_id']]['issue']
                if self.config.effective_labels(project)['approve'] not in observation['labels']:
                    resolved.add('approval-uncertain')
            else:
                root = next(r for r in data['issues'] if r['issue_id'] == issue['execution_group_id'])
                if (pr or issue['output_comment_id'] or
                        (root['work_branch'] and 'refs/heads/' + root['work_branch'] in external['local']['remote']) or
                        any(e['push_completed'] or e['pr_published'] for e in data['executions']
                            if e['issue_id'] == issue['issue_id'])):
                    raise SchemaError('Published result prevents cancellation')
                from .services import ALLOWED
                if State.CANCELLED not in ALLOWED.get(State(issue['state']), set()):
                    raise SchemaError('Cancellation transition forbidden')
                target, resolved = 'cancelled', {'execution-uncertain', 'approval-uncertain', 'state-conflict'}
            if (data, facts) != self.collect():
                raise SchemaError('External prerequisites changed')
            op = uuid4().hex
            with self.store.transaction() as db:
                db.execute('INSERT INTO recovery_operations VALUES (?,?,?,?,?,?,?,?)',
                           (op, issue['issue_id'], action, 'completed', utc_now(), utc_now(), None, '{}'))
                self.apply_result(db, issue, pr if action == 'accept' else None, target, op)
                for reason in resolved:
                    db.execute('DELETE FROM recovery_quarantine WHERE issue_id=? AND reason_code=?',
                               (issue['issue_id'], reason))
                self.store.event(db, issue['issue_id'], action, operation=op)
            return op

    def retry_facts(self, project, issue, execution, expected=None, *, removed_plan=None):
        data, facts = self.collect()
        external = facts[project.repository]
        root = next(r for r in data['issues'] if r['issue_id'] == issue['execution_group_id'])
        if self.candidates(issue, external, issue['work_branch'] or root['work_branch']):
            raise SchemaError('PR exists; retry refused')
        work_branch = issue['work_branch'] or root['work_branch']
        if not work_branch:
            from .input import parse_input
            comment = external['issues'][issue['issue_id']]['input']
            params, _ = parse_input(comment['body'] if comment else None)
            work_branch = params.branch or f"{'codex' if params.run_codex else 'manual'}/task-{issue['issue_number']:04d}"
        if self.candidates(issue, external, work_branch):
            raise SchemaError('PR exists; retry refused')
        if any(line.startswith('refs/heads/' + work_branch + ' ')
               for line in external['local']['refs'].splitlines()):
            raise SchemaError('Unknown local work branch retained')
        if 'refs/heads/' + work_branch in external['local']['remote']:
            raise SchemaError('Remote branch exists; retry refused')
        observation = external['issues'][issue['issue_id']]
        if observation['issue']['github_state'] != 'open':
            raise SchemaError('Retry requires an open Issue')
        if any(e and (e['push_completed'] or e['pr_published'] or e['comment_published']) for e in [execution]):
            raise SchemaError('Published result exists; retry refused')
        if issue['output_comment_id']:
            raise SchemaError('Published OUTPUT requires manual inspection')
        if (issue['correction_of_issue_id'] or
            len([r for r in data['issues'] if r['execution_group_id'] == issue['execution_group_id']]) != 1):
            raise SchemaError('Shared execution group retry requires separate resolution')
        tracked_children = sorted(r['child_issue_id'] for r in data['issue_relations']
                                  if r['parent_issue_id'] == issue['issue_id'])
        tracked_parents = sorted(r['parent_issue_id'] for r in data['issue_relations']
                                 if r['child_issue_id'] == issue['issue_id'])
        if (tracked_children != observation['children'] or
                tracked_parents != ([observation['parent']] if observation['parent'] else [])):
            raise SchemaError('Group relations changed')
        for child_id in tracked_children:
            child = next(r for r in data['issues'] if r['issue_id'] == child_id)
            if child['state'] not in ('done', 'cancelled') or any(
                    q['issue_id'] == child_id for q in data['recovery_quarantine']):
                raise SchemaError('Unverified child dependency prevents retry')
        for parent_id in tracked_parents:
            parent = next(r for r in data['issues'] if r['issue_id'] == parent_id)
            if parent['state'] not in ('discovered', 'pending', 'awaiting-children', None):
                raise SchemaError('Parent already executed; retry refused')
        current = next((e for e in data['executions'] if e['issue_id'] == issue['issue_id']), None)
        if current and execution:
            for key, value in execution.items():
                if key != 'error_message' and current[key] != value:
                    raise SchemaError('Execution changed since retry intent')
        if current and execution is None:
            raise SchemaError('Unexpected execution appeared during retry')
        cleanup = self.git.recovery_cleanup_plan(project, execution, external['local']) if execution else None
        if expected is not None and cleanup != expected:
            raise SchemaError('Local cleanup identity changed')
        if removed_plan:
            refs = dict(line.split() for line in external['local']['refs'].splitlines())
            if (Path(removed_plan['path']).exists() or removed_plan['branch'] in refs or
                    any(t['worktree'] == removed_plan['path'] for t in external['local']['trees'])):
                raise SchemaError('Retry cleanup is incomplete or artifacts reappeared')
        return cleanup

    def phase(self, operation, phase, **fields):
        with self.store.transaction() as db:
            db.execute('UPDATE recovery_operations SET phase=?,updated_at=? WHERE operation_id=?',
                       (phase, utc_now(), operation))
            if 'comment' in fields:
                db.execute('UPDATE recovery_operations SET github_comment_id=? WHERE operation_id=?',
                           (fields['comment'], operation))
            self.store.event(db, None, phase, operation=operation)
        self.fault(phase)

    def retry(self, project, issue, operation):
        data = self.store.snapshot()
        execution = next((e for e in data['executions'] if e['issue_id'] == issue['issue_id']), None)
        if not operation:
            cleanup = self.retry_facts(project, issue, execution)
            # Never store arbitrary task text or secret-bearing diagnostics in the archive.
            details = {'issue_id': issue['issue_id'], 'repository': project.repository,
                       'number': issue['issue_number'], 'cleanup': cleanup,
                       'execution': {k: v for k, v in execution.items() if k != 'error_message'} if execution else None,
                       'original_issue': {k: issue[k] for k in ('issue_id', 'issue_number', 'repository',
                           'state', 'execution_group_id', 'correction_of_issue_id', 'work_branch', 'base_branch',
                           'output_comment_id', 'approved_at', 'approved_hash')},
                       'approved_hash': issue['approved_hash']}
            op = uuid4().hex
            with self.store.transaction() as db:
                db.execute('INSERT INTO recovery_operations VALUES (?,?,?,?,?,?,?,?)',
                           (op, issue['issue_id'], 'retry', 'intent', utc_now(), utc_now(), None,
                            json.dumps(details, sort_keys=True)))
                self.store.event(db, issue['issue_id'], 'intent', operation=op)
            self.fault('intent')
            phase = 'intent'
        else:
            op, phase = operation['operation_id'], operation['phase']
            details = json.loads(operation['details'])
            if not isinstance(details, dict) or not {
                    'issue_id', 'repository', 'number', 'execution', 'original_issue', 'cleanup',
                    'approved_hash'}.issubset(details):
                raise SchemaError('Incomplete retry intent; preserve quarantine and inspect archive')
            if (details['issue_id'], details['repository'], details['number']) != (
                    issue['issue_id'], project.repository, issue['issue_number']):
                raise SchemaError('Operation identity mismatch')
            execution = details['execution']
        original_issue = details['original_issue']
        if phase == 'intent':
            with self.store.transaction() as db:
                current = db.execute('SELECT * FROM executions WHERE issue_id=?', (issue['issue_id'],)).fetchone()
                if current and current['error_message']:
                    db.execute('INSERT INTO error_log(issue_id,execution_id,error_code,error_stage,error_message,created_at) '
                               'VALUES (?,?,?,?,?,?)', (issue['issue_id'], current['execution_id'],
                                current['error_code'] or 'recovery_original', 'recovery_archive',
                                current['error_message'], utc_now()))
                diagnostics = [dict(r) for r in db.execute('SELECT id,error_code,error_stage,created_at,execution_id '
                               'FROM error_log WHERE issue_id=?', (issue['issue_id'],))]
                # Private original diagnostics stay in error_log; FK is detached at reset.
                safe_execution = {k: v for k, v in (execution or {}).items() if k != 'error_message'}
                db.execute('INSERT INTO recovery_execution_archive VALUES (?,?,?,?,?,?,?,?)',
                           (uuid4().hex, op, issue['issue_id'], execution['execution_id'] if execution else None,
                            json.dumps({'execution': safe_execution, 'approved_hash': details['approved_hash'],
                                        'cleanup': details['cleanup'], 'original_issue': details['original_issue']}),
                            json.dumps(diagnostics), 'recovery retry', utc_now()))
                db.execute('INSERT INTO recovery_approval_reset VALUES (?,?,?,?,0,NULL) '
                           'ON CONFLICT(issue_id) DO UPDATE SET operation_id=excluded.operation_id,'
                           'requested_at=excluded.requested_at,old_approved_hash=excluded.old_approved_hash,'
                           'approval_absent_observed=0,completed_at=NULL',
                           (issue['issue_id'], op, utc_now(), details['approved_hash']))
                db.execute("UPDATE recovery_operations SET phase='archived',updated_at=? WHERE operation_id=?",
                           (utc_now(), op))
                self.store.event(db, issue['issue_id'], 'archived', operation=op)
            self.fault('archived')
            phase = 'archived'
        if phase == 'archived':
            self.retry_facts(project, original_issue, execution, details['cleanup'])
            label = self.config.effective_labels(project)['approve']
            self.github.remove_label(project, issue['issue_number'], label)
            observed = self.github.issue(project, issue['issue_number'])
            if observed.issue_id != issue['issue_id'] or label in observed.labels:
                raise SchemaError('Old approval absence not observed')
            with self.store.transaction() as db:
                db.execute('UPDATE recovery_approval_reset SET approval_absent_observed=1 WHERE issue_id=?',
                           (issue['issue_id'],))
                db.execute("UPDATE recovery_operations SET phase='approval-reset',updated_at=? WHERE operation_id=?",
                           (utc_now(), op))
                self.store.event(db, issue['issue_id'], 'approval-reset', operation=op)
            self.fault('approval-reset')
            phase = 'approval-reset'
        if phase == 'approval-reset':
            self.retry_facts(project, original_issue, execution)
            self.git.recovery_remove(project, details['cleanup'])
            self.phase(op, 'cleaned')
            phase = 'cleaned'
        if phase == 'cleaned':
            self.retry_facts(project, original_issue, execution, removed_plan=details['cleanup'])
            with self.store.transaction() as db:
                db.execute('UPDATE error_log SET execution_id=NULL WHERE issue_id=?', (issue['issue_id'],))
                db.execute('DELETE FROM executions WHERE issue_id=?', (issue['issue_id'],))
                self.store.transition(db, issue, 'discovered', op)
                db.execute('UPDATE issues SET approved_title=NULL,approved_body=NULL,parameters_yaml=NULL,'
                           'approved_hash=NULL,approved_at=NULL,input_comment_id=NULL,approved_input=NULL,'
                           'is_approved=0,base_branch=NULL,work_branch=NULL,error_report_sent=0 WHERE issue_id=?',
                           (issue['issue_id'],))
                db.execute("UPDATE recovery_operations SET phase='reset',updated_at=? WHERE operation_id=?",
                           (utc_now(), op))
                self.store.event(db, issue['issue_id'], 'reset', operation=op)
            self.fault('reset')
            phase = 'reset'
        if phase in ('reset', 'comment-intent'):
            self.retry_facts(project, original_issue, execution, removed_plan=details['cleanup'])
            marker = '<!-- dispatcher-recovery:' + op + ' -->'
            comments = self.github.pages(f'repos/{project.repository}/issues/{issue["issue_number"]}/comments')
            matches = [c for c in comments if marker in (c.get('body') or '')]
            if len(matches) > 1:
                raise SchemaError('Duplicate recovery comments require inspection')
            if matches:
                comment_id = matches[0]['id']
            else:
                # Full successful pagination is required before every repeated POST.
                # A failed/incomplete GET raises above and retains the quarantine.
                self.phase(op, 'comment-intent')
                comment_id = self.github.comment(project, issue['issue_number'], RETRY_COMMENT + '\n' + marker)['id']
                self.fault('post-comment')
            if type(comment_id) is not int or comment_id <= 0:
                raise SchemaError('Invalid GitHub comment receipt')
            self.phase(op, 'commented', comment=comment_id)
            phase = 'commented'
        if phase == 'commented':
            self.retry_facts(project, original_issue, execution, removed_plan=details['cleanup'])
            current = self.github.issue(project, issue['issue_number'])
            label = self.config.effective_labels(project)['approve']
            if label in current.labels:
                # Approve added before retry completed is stale, too.
                self.github.remove_label(project, issue['issue_number'], label)
                current = self.github.issue(project, issue['issue_number'])
            if current.issue_id != issue['issue_id'] or label in current.labels:
                raise SchemaError('Approval reset not confirmed')
            with self.store.transaction() as db:
                # Relations were excluded and artifacts proven owned; these are exactly the resolved reasons.
                for reason in ('execution-uncertain', 'local-worktree', 'approval-uncertain', 'branch-conflict', 'state-conflict'):
                    db.execute('DELETE FROM recovery_quarantine WHERE issue_id=? AND reason_code=?',
                               (issue['issue_id'], reason))
                db.execute('UPDATE recovery_approval_reset SET completed_at=? WHERE issue_id=?',
                           (utc_now(), issue['issue_id']))
                db.execute("UPDATE recovery_operations SET phase='completed',updated_at=? WHERE operation_id=?",
                           (utc_now(), op))
                self.store.event(db, issue['issue_id'], 'retry', operation=op)
            self.fault('completed')
        return op
