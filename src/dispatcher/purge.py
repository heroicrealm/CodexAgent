"""Read-only purge inventory. Destructive execution is deliberately blocked.

Recovery's append-only FK-linked audit cannot be removed using the approved V2
schema. Do not disable triggers or pretend that a partial deletion is a purge.
"""
from .monitoring import project, snapshot, daemon


def plan(config, name):
    p = project(config, name)
    with snapshot(config) as (db, meta):
        ids = [r[0] for r in db.execute('SELECT issue_id FROM issues WHERE lower(repository)=?', (p.repository,))]
        counts = {'issues': len(ids)}
        marks = ','.join('?' for _ in ids) or 'NULL'
        for table in ('executions', 'pull_requests', 'state_history', 'error_log', 'debug_operations',
                      'recovery_operations', 'recovery_events', 'recovery_quarantine',
                      'recovery_execution_archive', 'recovery_approval_reset'):
            counts[table] = db.execute(f'SELECT count(*) FROM {table} WHERE issue_id IN ({marks})', ids).fetchone()[0]
        counts['debug_operations'] = db.execute(f'SELECT count(*) FROM debug_operations WHERE issue_id IN ({marks}) OR lower(repository)=?', (*ids, p.repository)).fetchone()[0]
        counts['issue_relations'] = db.execute(f'SELECT count(*) FROM issue_relations WHERE parent_issue_id IN ({marks}) OR child_issue_id IN ({marks})', (*ids, *ids)).fetchone()[0]
        executions = [dict(r) for r in db.execute(f'SELECT * FROM executions WHERE issue_id IN ({marks})', ids)]
        refs = [dict(r) for r in db.execute('SELECT issue_id,base_branch,work_branch FROM issues WHERE lower(repository)=?', (p.repository,))]
        blockers = ['Destructive purge is unavailable pending approval of append-only Recovery audit retention and FK policy; no triggers or safety guards are bypassed',
                    'Git resource ownership and interruption-safe deletion have not been established; inventory is not an executable cleanup plan']
        if meta[3]:
            blockers.append('Global Recovery barrier')
        if db.execute("SELECT 1 FROM recovery_operations WHERE phase<>'completed' UNION SELECT 1 FROM recovery_quarantine LIMIT 1").fetchone():
            blockers.append('Active recovery operations/quarantine')
    return dict(project=name, repository=p.repository, daemon=daemon(config), counts=counts,
                results=[str(config.workspace / 'results' / f"{e['execution_id']}.txt") for e in executions],
                worktrees=[e['worktree_path'] for e in executions if e['worktree_path']],
                working_refs=refs, checkout=str(p.checkout or config.workspace / 'repos' / p.id),
                directories=[str(config.workspace / 'worktrees' / p.id)],
                external_checkout_preserved=bool(p.checkout), executable=False, blockers=blockers)
