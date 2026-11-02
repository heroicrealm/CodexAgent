"""Consume readiness through the existing attempt and publication pipelines."""
import json
import hashlib
from dispatcher.container_monitor import ContainerMonitor
from dispatcher.workspace import TaskError


def semantic(observation, *, after_run=False):
    value = json.loads(json.dumps(observation))
    value.pop('collection', None)
    value.pop('removed', None)
    value.pop('parent', None)
    if after_run:
        value.pop('parent_head', None)
    for child in value['children']:
        child.pop('updated_at', None)
        child.pop('labels', None)
    return value


def verified(store, adapter, project, row, snapshot, *, after_run=False, expected_head=None):
    monitor = ContainerMonitor(store, adapter)
    first = semantic(monitor.collect(project, row))
    second = semantic(monitor.collect(project, row))
    if (first != second or semantic(first, after_run=after_run) != semantic(snapshot, after_run=after_run)
            or first['blockers'] or expected_head is not None and first['parent_head'] != expected_head):
        raise TaskError('child_snapshot_stale')
    return first


def readiness(store, adapter, project, row):
    ready = store.db.execute('SELECT * FROM container_readiness WHERE container_issue_id=?', (row['issue_id'],)).fetchone()
    if not ready or not ready['ready_for_integration'] or store.db.execute(
            'SELECT 1 FROM container_blockers WHERE container_issue_id=? AND active=1', (row['issue_id'],)).fetchone():
        raise TaskError('child_snapshot_stale')
    snapshot = store.db.execute('SELECT snapshot FROM container_integration_snapshots WHERE container_issue_id=? AND generation=?',
                                (row['issue_id'], ready['generation'])).fetchone()
    if not snapshot or semantic(json.loads(snapshot['snapshot'])) != semantic(json.loads(ready['observation'])):
        raise TaskError('child_snapshot_stale')
    verified(store, adapter, project, row, json.loads(snapshot['snapshot']))
    return dict(generation=ready['generation'], snapshot=json.loads(snapshot['snapshot']))


def publication_guard(store, adapter, project, row):
    operation = store.db.execute("SELECT payload FROM container_operations WHERE container_issue_id=? AND iteration_id=? AND kind LIKE 'integration:%'",
                                 (row['issue_id'], row['current_iteration_id'])).fetchall()
    if len(operation) != 1:
        raise TaskError('child_snapshot_stale')
    if store.db.execute('SELECT 1 FROM container_blockers WHERE container_issue_id=? AND active=1', (row['issue_id'],)).fetchone():
        raise TaskError('child_snapshot_stale')
    attempt = store.db.execute('SELECT result_sha FROM attempts WHERE iteration_id=?', (row['current_iteration_id'],)).fetchone()
    verified(store, adapter, project, row, json.loads(operation[0]['payload'])['snapshot'], after_run=True,
             expected_head=attempt['result_sha'] if attempt else None)


def manual_diff(store, adapter, git, project, row):
    """Read-only diff gate before reserving an attempt; no empty commits/retries."""
    from dispatcher.containers import ContainerService
    from dispatcher import container_diagnostic
    monitor = ContainerMonitor(store, adapter)
    monitor.observe(project, row)
    current = store.db.execute('SELECT * FROM container_readiness WHERE container_issue_id=?', (row['issue_id'],)).fetchone()
    if (not current or current['waiting_count'] or current['conflict_count'] or store.db.execute(
            "SELECT 1 FROM container_blockers WHERE container_issue_id=? AND active=1 AND code!='no_diff'", (row['issue_id'],)).fetchone()):
        return False
    operation = store.db.execute("SELECT * FROM container_operations WHERE container_issue_id=? AND iteration_id=? AND kind='branch' AND phase='confirmed' AND outcome='success'",
        (row['issue_id'], row['current_iteration_id'])).fetchone()
    if not operation:
        raise TaskError('branch_ownership_conflict')
    payload = json.loads(operation['payload'])
    repo, _, _ = git.paths(project, row['number'])
    origin = git.git(repo, 'remote', 'get-url', 'origin').stdout.strip()
    if hashlib.sha256(origin.encode()).hexdigest() != payload.get('origin_hash'):
        raise TaskError('origin_changed')
    git.git(repo, 'fetch', 'origin')
    diff = git.git(repo, 'diff', '--quiet', f"refs/remotes/origin/{row['base_branch']}...refs/remotes/origin/{row['work_branch']}", check=False)
    if diff.returncode not in (0, 1):
        raise TaskError('git_operation_failed')
    if not diff.returncode:
        ContainerService(store, adapter, git).block(row, 'no_diff')
        container_diagnostic.reconcile(store, adapter, row)
        return False
    with store.db as db:
        db.execute("UPDATE container_blockers SET active=0 WHERE container_issue_id=? AND blocker_key='parent' AND code='no_diff'", (row['issue_id'],))
    ContainerMonitor(store, adapter).observe(project, row)
    return True


def branch_guard(store, git, project, row, snapshot):
    claim = store.db.execute("SELECT payload FROM container_operations WHERE container_issue_id=? AND iteration_id=? AND kind='branch' AND phase='confirmed' AND outcome='success'",
        (row['issue_id'], row['current_iteration_id'])).fetchone()
    if not claim or store.db.execute('SELECT 1 FROM tasks WHERE repository=? AND work_branch=? AND issue_id!=?',
            (row['repository'], row['work_branch'], row['issue_id'])).fetchone():
        raise TaskError('branch_ownership_conflict')
    claim = json.loads(claim['payload'])
    repo, _, _ = git.paths(project, row['number'])
    origin = git.git(repo, 'remote', 'get-url', 'origin').stdout.strip()
    if (claim['branch'] != row['work_branch'] or claim['base'] != row['base_branch']
            or hashlib.sha256(origin.encode()).hexdigest() != claim.get('origin_hash')):
        raise TaskError('origin_changed')
    ref = 'refs/heads/' + row['work_branch']
    if git.git(repo, 'ls-remote', 'origin', ref).stdout.split() != [snapshot['parent_head'], ref]:
        raise TaskError('remote_head_changed')
