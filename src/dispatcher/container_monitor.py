"""Read-only direct-child evidence and durable integration readiness.

No remote calls occur in the SQLite publication transaction. Two observations
bound observed churn; GitHub does not provide an atomic multi-resource snapshot.
"""
import fcntl
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from dispatcher import container_diagnostic
from dispatcher.github import GitHubError
from dispatcher.containers import child_snapshot
from dispatcher.pr_monitor import utc
from dispatcher.storage import now, content_hash
from dispatcher.workspace import TaskError

class SnapshotMismatch(TaskError):
    def __init__(self, source):
        super().__init__('child_snapshot_stale')
        self.source = source


class ContainerMonitor:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter

    def poll(self, project):
        rows = self.store.db.execute("SELECT * FROM tasks WHERE repository=? AND issue_type='container' AND state='awaiting_children'", (project.repository.casefold(),)).fetchall()
        for row in rows:
            database = Path(self.store.db.execute('PRAGMA database_list').fetchone()[2])
            with (database.parent / f'container-monitor-{row["issue_id"]}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if self.store.stop_requested(row):
                    continue
                try:
                    self.observe(project, row)
                except (GitHubError, TaskError, KeyError, ValueError, TypeError, AttributeError) as exc:
                    logging.getLogger(__name__).warning('container_monitor snapshot rejected: source=%s issue_id=%s',
                        getattr(exc, 'source', 'remote_read'), row['issue_id'])
                    self.invalidate(row, 'child_snapshot_stale')
                container_diagnostic.reconcile(self.store, self.adapter, row)

    def invalidate(self, row, code):
        with self.store.db as db:
            db.execute('UPDATE container_readiness SET ready_for_integration=0,conflict_count=MAX(conflict_count,1) WHERE container_issue_id=?', (row['issue_id'],))
            db.execute('''INSERT INTO container_blockers VALUES(?, 'monitor:parent', NULL, ?, ?, 1, ?)
                ON CONFLICT(container_issue_id,blocker_key) DO UPDATE SET code=excluded.code,active=1,updated_at=excluded.updated_at''', (row['issue_id'], code, code, now()))

    def local(self, row):
        # Include resources and operations, not just task versions: a publisher
        # may commit PR evidence without changing the Issue version.
        db = self.store.db
        return json.dumps({table: [dict(r) for r in db.execute(query, args)] for table, query, args in (
            ('tasks', 'SELECT * FROM tasks WHERE repository=? ORDER BY issue_id', (row['repository'],)),
            ('guards', 'SELECT * FROM command_guards ORDER BY repository,number', ()),
            ('iterations', 'SELECT * FROM task_iterations ORDER BY iteration_id', ()),
            ('prs', 'SELECT * FROM task_pull_requests WHERE repository_id=? ORDER BY pr_id', (row['repository_id'],)),
            ('operations', 'SELECT * FROM publication_operations ORDER BY operation_key', ()),
            ('containers', 'SELECT * FROM container_operations ORDER BY operation_key', ()),
            ('stops', 'SELECT * FROM stop_requests ORDER BY request_id', ()),
            ('relations', 'SELECT * FROM sub_issue_relations ORDER BY parent_issue_id,child_issue_id', ()),
            ('attempts', 'SELECT * FROM attempts ORDER BY attempt_id', ()),
            ('history', 'SELECT * FROM iteration_pr_history ORDER BY id', ()),
        )}, sort_keys=True)

    def unfinished(self, child):
        db = self.store.db
        return bool(db.execute("SELECT 1 FROM publication_operations o JOIN task_iterations i USING(iteration_id) WHERE i.issue_id=? AND o.outcome!='completed'", (child['issue_id'],)).fetchone()
            or db.execute("SELECT 1 FROM container_operations WHERE container_issue_id=? AND (phase!='confirmed' OR outcome!='success')", (child['issue_id'],)).fetchone()
            or db.execute("SELECT 1 FROM attempts WHERE repository=? AND number=? AND state='running'", (child['repository'], child['number'])).fetchone()
            or db.execute("SELECT 1 FROM stop_requests WHERE issue_id=? AND active=1", (child['issue_id'],)).fetchone())

    def prs(self, project, child):
        linked = self.store.db.execute('SELECT * FROM task_pull_requests WHERE issue_id=?', (child['issue_id'],)).fetchall()
        if not child['work_branch']:
            if linked:
                raise TaskError('child_pr_identity_conflict')
            return [], None
        endpoint = f'repos/{project.repository}/pulls'
        candidates = self.adapter.pages(endpoint + '?' + urlencode(dict(state='all', head=project.repository.split('/')[0] + ':' + child['work_branch'])))
        ids = [p['id'] for p in candidates]
        if len(ids) != len(set(ids)) or len(linked) > 1:
            raise TaskError('child_pr_identity_conflict')
        canonical = linked[0] if linked else None
        if canonical and canonical['pr_id'] not in ids:
            raise TaskError('child_pr_identity_conflict')
        if len(candidates) > 1:
            raise TaskError('child_pr_identity_conflict')
        evidence = []
        for candidate in candidates:
            pr = self.adapter.request(endpoint + '/' + str(candidate['number']))
            if (pr['id'] != candidate['id'] or pr['number'] != candidate['number']
                    or canonical and (pr['id'] != canonical['pr_id']
                        or pr['number'] != canonical['pr_number']
                        or canonical['repository_id'] != child['repository_id'])
                    or pr['head']['repo']['id'] != child['repository_id']
                    or pr['base']['repo']['id'] != child['repository_id']
                    or pr['head']['ref'] != child['work_branch']
                    or canonical and pr['head']['ref'] != canonical['head']
                    or not isinstance(pr['base']['ref'], str) or not pr['base']['ref']
                    or type(pr['merged']) is not bool
                    or pr['state'] not in ('open', 'closed')
                    or not isinstance(pr['head']['sha'], str) or not pr['head']['sha']):
                raise TaskError('child_pr_identity_conflict')
            observed = utc(self.adapter.response_date)
            if pr['merged']:
                if pr['state'] != 'closed' or datetime.fromisoformat(utc(pr['merged_at']).replace('Z', '+00:00')) > datetime.fromisoformat(observed.replace('Z', '+00:00')) or not pr.get('merge_commit_sha'):
                    raise TaskError('child_merge_unverified')
            evidence.append(dict(id=pr['id'], number=pr['number'], head=pr['head']['ref'],
                base=pr['base']['ref'], head_sha=pr['head']['sha'], state=pr['state'],
                merged=pr['merged'], merged_at=pr.get('merged_at'), merge_sha=pr.get('merge_commit_sha')))
        return evidence, canonical

    def parent_guard(self, project, row, parent):
        accepted = json.loads(row['snapshot'])
        names = {label['name'].casefold() for label in parent['labels']}
        # The accepted target branch is derived from the body at admission.
        # Hashing the freshly read body also fences any branch/YAML edit.
        semantic = dict(id=parent['id'], title=parent['title'], body=parent.get('body') or '',
                        author=parent['user']['login'],
                        target_branch=accepted['target_branch'])
        commands = {project.label(k).name.casefold() for k in ('approve', 'reject')}
        guard = self.store.guard(row['repository'], row['number'])
        if (parent['id'] != row['issue_id'] or parent['number'] != row['number']
                or parent['repository_url'].casefold() != f'https://api.github.com/repos/{project.repository}'.casefold()
                or parent['state'] != 'open' or content_hash(semantic) != row['content_hash']
                or project.task_label.casefold() not in names
                or project.label('emergency_stop').name.casefold() in names
                or names & commands
                or semantic['author'].casefold() not in {a.casefold() for a in project.allowed_authors}
                or row['approved_version'] != row['version'] or not row['available']
                or row['state'] not in ('awaiting_children', 'running') or row['issue_type'] != 'container'
                or not row['current_iteration_id'] or not row['work_branch']
                or self.store.stop_requested(row)
                or not guard or not guard['available'] or guard['version'] != row['version'] or guard['content_hash'] != row['content_hash']):
            raise SnapshotMismatch('parent_mismatch')

    def collect(self, project, row):
        parent = self.adapter.request(f'repos/{project.repository}/issues/{row["number"]}')
        self.parent_guard(project, row, parent)
        children = self.adapter.list_direct_children(project, row)
        relations = self.store.db.execute('SELECT * FROM sub_issue_relations WHERE parent_issue_id=?', (row['issue_id'],)).fetchall()
        historical = {r['child_issue_id'] for r in relations if r['participation'] != 'ignored'}
        result, blockers = [], {}
        seen = {c['id'] for c in children}
        removed = []
        previous = self.store.db.execute('SELECT observation FROM container_readiness WHERE container_issue_id=?', (row['issue_id'],)).fetchone()
        prior_children = {c['issue_id']: c for c in json.loads(previous['observation'])['children']} if previous else {}
        for child_id in historical - seen:
            prior = prior_children.get(child_id)
            child = self.store.db.execute('SELECT * FROM tasks WHERE issue_id=?', (child_id,)).fetchone()
            # Only a previously observed wrong-base merge can authorize removal.
            # Read the canonical PR again; disappearance/changed evidence fails closed.
            if prior and prior['conflict'] == 'child_merge_wrong_base' and child and child['state'] == 'done' and not self.unfinished(child):
                evidence, canonical = self.prs(project, child)
                other = self.store.db.execute("SELECT 1 FROM sub_issue_relations WHERE child_issue_id=? AND parent_issue_id!=? AND participation!='ignored'", (child_id, row['issue_id'])).fetchone()
                if canonical and not other and evidence == prior['prs'] and len(evidence) == 1 and evidence[0]['merged'] and evidence[0]['base'] != row['work_branch']:
                    removed.append(child_id)
                    continue
            blockers[child_id] = 'child_relation_removed'
        for raw in children:
            child = self.store.get(project.repository, raw['number'])
            ready = project.task_label.casefold() in {l['name'].casefold() for l in raw['labels']}
            if not ready and not child and raw['id'] not in historical:
                continue
            code, evidence = None, []
            if not child or child['issue_id'] != raw['id'] or child['repository_id'] != row['repository_id']:
                # A missing local FK cannot be recorded as a relation.
                raise SnapshotMismatch('child_mismatch')
            accepted_child = json.loads(child['snapshot'])
            if (raw['title'] != accepted_child['title'] or (raw.get('body') or '') != accepted_child['body']
                    or raw['user']['login'].casefold() != accepted_child['author'].casefold()):
                raise SnapshotMismatch('child_mismatch')
            try:
                evidence, canonical = self.prs(project, child)
                open_pr = any(p['state'] == 'open' for p in evidence)
                active = self.unfinished(child)
                if not ready:
                    if open_pr:
                        code = 'child_detached_with_open_pr'
                    elif active:
                        code = 'child_snapshot_stale'
                elif child['state'] == 'cancelled':
                    if open_pr or active:
                        code = 'child_cancelled_with_open_pr'
                elif evidence and not canonical:
                    code = 'child_pr_identity_conflict'
                elif child['state'] == 'done':
                    if not evidence or not evidence[0]['merged']:
                        code = 'child_merge_unverified'
                    elif evidence[0]['base'] != row['work_branch'] or child['base_branch'] != row['work_branch']:
                        code = 'child_merge_wrong_base'
                elif child['work_branch'] and child['base_branch'] != row['work_branch']:
                    code = 'child_merge_wrong_base'
                if (not ready and child['state'] == 'done' and canonical and evidence
                        and evidence[0]['merged'] and evidence[0]['base'] != row['work_branch']):
                    code = 'child_merge_wrong_base'
                other = self.store.db.execute("SELECT 1 FROM sub_issue_relations WHERE child_issue_id=? AND parent_issue_id!=? AND participation!='ignored'", (child['issue_id'], row['issue_id'])).fetchone()
                if other and child['work_branch']:
                    code = 'child_relation_removed'
            except TaskError as exc:
                code = exc.code
            if any(project.label(k).name.casefold() in {label['name'].casefold() for label in raw['labels']}
                   for k in ('approve', 'reject', 'emergency_stop')):
                code = 'child_snapshot_stale'
            if code:
                blockers[child['issue_id']] = code
            result.append(dict(issue_id=child['issue_id'], repository_id=child['repository_id'],
                number=child['number'], state=child['state'], ready=ready, prs=evidence,
                title=raw['title'], url=f'https://github.com/{project.repository}/issues/{child["number"]}',
                body=raw.get('body'), participating=ready or bool(code), conflict=code))
        head = self.adapter.request(f'repos/{project.repository}/git/ref/heads/' + quote(row['work_branch'], safe=''))
        if head['ref'] != 'refs/heads/' + row['work_branch'] or head['object']['type'] != 'commit' or not head['object']['sha']:
            raise SnapshotMismatch('parent_mismatch')
        return dict(parent=parent, collection=[child_snapshot(c) for c in children], children=result, blockers=blockers, removed=removed, parent_head=head['object']['sha'], iteration=row['current_iteration_id'])

    def observe(self, project, row):
        before = self.local(row)
        first = self.collect(project, row)
        second = self.collect(project, row)
        parent = second.pop('parent')
        first.pop('parent')
        if first != second:
            source = 'parent_mismatch' if first['parent_head'] != second['parent_head'] else 'child_mismatch'
            raise SnapshotMismatch(source)
        del second['collection']  # Never-managed, non-ready children leave no durable evidence.
        removed = second.pop('removed')
        encoded = json.dumps(second, sort_keys=True)
        children = [c for c in second['children'] if c['participating']]
        completed = sum(c['state'] in ('done', 'cancelled') and not c['conflict'] for c in children)
        ready = not second['blockers'] and completed == len(children)
        db = self.store.db
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = self.store.get(project.repository, row['number'])
            if self.local(row) != before or current['state'] != 'awaiting_children' or self.store.stop_requested(current):
                raise SnapshotMismatch('local_race')
            self.parent_guard(project, current, parent)
            # Resolve only monitor-owned blockers, after two complete observations.
            db.execute("UPDATE container_blockers SET active=0 WHERE container_issue_id=? AND blocker_key LIKE 'monitor:%'", (row['issue_id'],))
            for child_id, code in second['blockers'].items():
                db.execute('''INSERT INTO container_blockers VALUES(?,?,?,?,?,1,?) ON CONFLICT(container_issue_id,blocker_key)
                    DO UPDATE SET code=excluded.code,details=excluded.details,active=1,updated_at=excluded.updated_at''',
                    (row['issue_id'], 'monitor:' + str(child_id), child_id, code,
                     next((f"Ожидаемая base: {row['work_branch']}; фактическая base: {c['prs'][0]['base']}. Исключите ребёнка из прямых Sub-issues контейнера." for c in second['children'] if c['issue_id'] == child_id and c['prs'] and code == 'child_merge_wrong_base'), code), now()))
            for child_id in removed:
                db.execute("UPDATE sub_issue_relations SET participation='ignored',conflict_code='child_merge_wrong_base',observed_at=? WHERE parent_issue_id=? AND child_issue_id=?", (now(), row['issue_id'], child_id))
            for child in second['children']:
                db.execute('''INSERT INTO sub_issue_relations VALUES(?,?,?,?,?) ON CONFLICT(parent_issue_id,child_issue_id)
                    DO UPDATE SET observed_at=excluded.observed_at,participation=excluded.participation,conflict_code=excluded.conflict_code''',
                    (row['issue_id'], child['issue_id'], now(), 'blocked' if child['conflict'] else 'included' if child['ready'] else 'detached', child['conflict']))
            if db.execute('SELECT 1 FROM container_blockers WHERE container_issue_id=? AND active=1', (row['issue_id'],)).fetchone():
                ready = False
            previous = db.execute('SELECT * FROM container_readiness WHERE container_issue_id=?', (row['issue_id'],)).fetchone()
            generation = previous['generation'] if previous else 0
            if ready and (not previous or not previous['ready_for_integration'] or previous['observation'] != encoded):
                generation += 1
                db.execute('INSERT INTO container_integration_snapshots VALUES(?,?,?,?,?)', (row['issue_id'], generation, encoded, utc(self.adapter.response_date), now()))
            db.execute('''INSERT INTO container_readiness VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(container_issue_id)
                DO UPDATE SET generation=excluded.generation,ready_for_integration=excluded.ready_for_integration,
                observation=excluded.observation,children_count=excluded.children_count,completed_count=excluded.completed_count,
                waiting_count=excluded.waiting_count,conflict_count=excluded.conflict_count''',
                (row['issue_id'], generation, int(ready), encoded, len(children), completed,
                 sum(c['state'] not in ('done', 'cancelled') and not c['conflict'] for c in children), len(second['blockers'])))
