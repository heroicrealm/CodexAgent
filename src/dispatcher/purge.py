"""Explicit local administrative purge. Recovery's ordinary API stays append-only."""
from importlib.resources import files
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import sys
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone

from .monitoring import project, snapshot, daemon
from . import db_guard
from .locking import InstanceLock
from .git import Git, GitError

ORDER = ('recovery_events', 'recovery_execution_archive', 'recovery_approval_reset',
         'recovery_quarantine', 'recovery_operations', 'error_log', 'state_history',
         'pull_requests', 'executions', 'debug_operations', 'issue_relations',
         'issues', 'recovery_sessions')


class PurgeGit(Git):
    """Never let read-only status refresh the source index during planning."""
    def command(self, args, operation):
        if (operation not in {'remote', 'rev-parse', 'status', 'worktree', 'for-each-ref', 'update-ref'}
                or operation == 'remote' and args[4:6] != ['get-url', 'origin']
                or operation == 'worktree' and args[4] not in ('list', 'remove')):
            raise ValueError('Nonlocal/unapproved purge Git operation')
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=120,
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'})
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitError('Local purge Git operation failed') from exc


def safe(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f'Symlink/noncanonical path refused: {path}')
    return path


def triggers(db):
    expected = {}
    statement = ''
    for line in files('dispatcher').joinpath('schema.sql').read_text().splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            match = re.search(r'CREATE TRIGGER (recovery_events_no_(?:update|delete))\b', statement)
            if match:
                expected[match[1]] = statement.strip().rstrip(';')
            statement = ''
    for name, sql in expected.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE type=\'trigger\' AND name=?', (name,)).fetchone()
        if not row or row[0].strip().rstrip(';') != sql:
            raise ValueError(f'Unexpected Recovery trigger: {name}')
    if len(expected) != 2:
        raise ValueError('Recovery trigger definitions missing')
    return expected


def repository_is(value, p):
    return isinstance(value, str) and value.lower() == p.repository


def references(value, p):
    """Repository text is reliable; a project ID requires a structured key."""
    if isinstance(value, str):
        if re.search(r'(?<![A-Za-z0-9_.-])' + re.escape(p.repository) + r'(?:\.git)?(?![A-Za-z0-9_.-])', value, re.IGNORECASE):
            return True
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return False
        return references(parsed, p) if not isinstance(parsed, str) else False
    if isinstance(value, dict):
        return any((k in ('project', 'project_id') and v == p.id) or references(v, p)
                   for k, v in value.items())
    return isinstance(value, list) and any(references(v, p) for v in value)


def database_plan(db, p):
    rows = {t: [dict(r) for r in db.execute(f'SELECT rowid AS _rowid,* FROM {t}')] for t in ORDER}
    ids = {r['issue_id'] for r in rows['issues'] if r['repository'].lower() == p.repository}
    ops = {r['operation_id'] for r in rows['recovery_operations'] if r['issue_id'] in ids}
    selected = {t: [] for t in ORDER}
    blockers, updates = [], []
    for t in ORDER:
        if t == 'recovery_sessions':
            continue
        for row in rows[t]:
            owned = row.get('issue_id') in ids or (t == 'issue_relations' and
                    (row['parent_issue_id'] in ids or row['child_issue_id'] in ids))
            owned |= row.get('operation_id') in ops
            if t == 'recovery_events' and row['issue_id'] is None and row['operation_id'] is None:
                details = json.loads(row['details'])
                owned |= repository_is(details.get('repository'), p)
            owned |= t == 'debug_operations' and (row.get('repository') or '').lower() == p.repository
            if owned:
                # Mixed-identity audit events cannot be discarded with foreign history.
                if row.get('issue_id') is not None and row['issue_id'] not in ids:
                    blockers.append(f'Mixed ownership: {t} row {row["_rowid"]}')
                if row.get('operation_id') is not None and row['operation_id'] not in ops:
                    blockers.append(f'Foreign operation reference: {t} row {row["_rowid"]}')
                selected[t].append(row['_rowid'])
            elif references(row, p):
                blockers.append(f'Unscoped/shared reference: {t} row {row["_rowid"]}')
    for table in ('dispatcher_metadata', 'schema_migrations'):
        for row in db.execute(f'SELECT * FROM {table}'):
            if references(dict(row), p):
                blockers.append(f'Unscoped metadata reference: {table}')
    events = rows['recovery_events']
    for session in rows['recovery_sessions']:
        sid = session['session_id']
        summary = json.loads(session['summary'])
        identities = {r['issue_id']: r['repository'].lower() for r in rows['issues']}
        for entry in summary:
            if isinstance(entry, dict) and 'repository' in entry and type(entry.get('issue_id')) is int and entry['issue_id'] in identities:
                if not isinstance(entry['repository'], str) or entry['repository'].lower() != identities[entry['issue_id']]:
                    blockers.append(f'Mixed session summary identity: {sid}')
        removed = [v for v in summary if isinstance(v, dict) and
                   ((type(v.get('issue_id')) is int and v['issue_id'] in ids) or repository_is(v.get('repository'), p))]
        retained = [v for v in summary if v not in removed]
        own_events = [e for e in events if e['session_id'] == sid and e['_rowid'] in selected['recovery_events']]
        foreign_events = [e for e in events if e['session_id'] == sid and e['_rowid'] not in selected['recovery_events']]
        if not removed and not own_events and not references(session, p):
            continue
        if retained and references({k: v for k, v in session.items() if k != 'summary'}, p):
            blockers.append(f'Shared session identifier references repository: {sid}')
        if any(references(v, p) for v in retained):
            blockers.append(f'Unrecognized shared session summary: {sid}')
            continue
        if removed and not retained and all(e['issue_id'] is None and e['operation_id'] is None for e in foreign_events):
            selected['recovery_events'].extend(e['_rowid'] for e in foreign_events)
            foreign_events = []
        if not retained and not foreign_events:
            selected['recovery_sessions'].append(session['_rowid'])
        elif removed:
            # Current Recovery persists one {issue_id,reasons,target} per checked issue.
            if (not all(isinstance(v, dict) and set(v) in ({'issue_id', 'reasons', 'target'}, {'issue_id', 'reasons', 'target', 'repository', 'issue_number'})
                        and type(v['issue_id']) is int and isinstance(v['reasons'], list)
                        and (v['target'] is None or isinstance(v['target'], str)) for v in summary)
                    or session['checked_issues'] != len(summary)
                    or session['quarantined_issues'] != sum(bool(v['reasons']) for v in summary)):
                blockers.append(f'Unknown session summary/counter format: {sid}')
            else:
                updates.append(dict(rowid=session['_rowid'], summary=json.dumps(retained),
                    checked=len(retained), quarantined=sum(bool(v['reasons']) for v in retained)))
    # Cross-project self references must never be nulled implicitly.
    for row in rows['issues']:
        if row['issue_id'] not in ids and (row['execution_group_id'] in ids or row['correction_of_issue_id'] in ids):
            blockers.append(f'Foreign issue references target: {row["issue_id"]}')
    return selected, updates, blockers, rows


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def artifact_plan(config, p, rows):
    git = PurgeGit(config)
    checkout = safe(p.checkout or config.workspace / 'repos' / p.id)
    area = safe(config.workspace / 'worktrees' / p.id)
    blockers, trees, refs, paths, diagnostics_updates = [], [], [], [], []
    owned_execs = {r['execution_id'] for r in rows['executions'] if r['issue_id'] in
                  {i['issue_id'] for i in rows['issues'] if i['repository'].lower() == p.repository}}
    # Archived executions also own result files after retry removed executions.
    for row in rows['recovery_execution_archive']:
        if row['issue_id'] in {i['issue_id'] for i in rows['issues'] if i['repository'].lower() == p.repository}:
            if row['original_execution_id'] is not None:
                owned_execs.add(row['original_execution_id'])
    for n in sorted(owned_execs):
        path = safe(config.workspace / 'results' / f'{n}.txt')
        if path.exists():
            if not path.is_file():
                blockers.append(f'Nonregular result: {path}')
            else:
                paths.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    # Unmapped results cannot be attributed from an integer filename alone.
    known = {r['execution_id'] for r in rows['executions']}
    known |= {r['original_execution_id'] for r in rows['recovery_execution_archive']}
    results = safe(config.workspace / 'results')
    if results.exists():
        for path in results.iterdir():
            safe(path)
            if path.suffix != '.txt' or not path.stem.isdigit() or int(path.stem) not in known:
                blockers.append(f'Orphan result ownership unknown: {path}')
    diagnostics = safe(config.workspace / 'diagnostics')
    if diagnostics.exists():
        for path in diagnostics.rglob('*'):
            safe(path)
            if path.is_file():
                # A runtime poll line has authoritative context; preserve all other lines verbatim.
                raw = path.read_bytes()
                retained, changed = [], False
                for line in raw.splitlines(keepends=True):
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        if references(line.decode('utf-8', errors='replace'), p):
                            blockers.append(f'Unscoped diagnostic reference: {path}')
                        retained.append(line)
                        continue
                    context = value.get('context', {}) if isinstance(value, dict) else {}
                    if isinstance(context, dict) and repository_is(context.get('repository'), p):
                        changed = True
                    else:
                        retained.append(line)
                        if references(value, p):
                            blockers.append(f'Shared diagnostic requires reconciliation: {path}')
                if changed:
                    try:
                        content = b''.join(retained).decode('utf-8')
                    except UnicodeError:
                        blockers.append(f'Uninspectable diagnostic: {path}')
                    else:
                        diagnostics_updates.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), content=content))
    if p.checkout:
        # An external checkout may contain local project refs. Never modify it.
        blockers.append(f'External checkout preserved; Git ownership unproven: {checkout}')
    elif checkout.exists():
        origin = git.run(checkout, 'remote', 'get-url', 'origin')
        if origin not in (f'git@github.com:{p.repository}.git', f'https://github.com/{p.repository}.git'):
            blockers.append('Managed checkout origin mismatch')
        if not (checkout / '.git').is_dir() or safe(checkout / '.git').resolve() != (checkout / '.git').absolute():
            blockers.append('Shared checkout Git directory')
        entries = git.worktrees(p)
        if git.run(checkout, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
            blockers.append('Managed checkout contains user changes')
        for entry in entries[1:]:
            path = safe(entry['worktree'])
            match = re.fullmatch(r'e-(\d+)-[0-9a-f]{32}', path.name)
            expected = 'refs/heads/dispatcher-v2/' + path.name[2:]
            if (path.parent != area or not match or entry.get('branch') != expected
                    or 'locked' in entry or 'prunable' in entry):
                blockers.append(f'Unowned/shared worktree: {path}')
            elif git.run(path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
                blockers.append(f'Dirty worktree: {path}')
            else:
                trees.append(dict(path=str(path), branch=expected, head=entry['HEAD']))
        local_refs = git.run(checkout, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads').splitlines()
        for line in local_refs:
            ref, head = line.split()
            if ref.startswith('refs/heads/dispatcher-v2/'):
                if not re.fullmatch(r'refs/heads/dispatcher-v2/\d+-[0-9a-f]{32}', ref):
                    blockers.append(f'Unknown internal branch format: {ref}')
                else:
                    refs.append(dict(ref=ref, head=head))
            elif ref != 'refs/heads/' + p.base_branch:
                blockers.append(f'Unowned local branch in managed checkout: {ref}')
    elif area.exists() and any(area.iterdir()):
        blockers.append('Worktrees without managed checkout')
    if area.exists():
        registered = {t['path'] for t in trees}
        for child in area.iterdir():
            if str(safe(child)) not in registered:
                blockers.append(f'Unregistered managed artifact: {child}')
    # A checkout cannot own another configured project's checkout/worktree roots.
    roots = (checkout, area)
    for other in config.projects:
        if other.id != p.id:
            for path in (other.checkout or config.workspace / 'repos' / other.id,
                         config.workspace / 'worktrees' / other.id):
                path = safe(path)
                if any(path.is_relative_to(root) or root.is_relative_to(path) for root in roots):
                    blockers.append(f'Shared project path: {path}')
    protected = [config.database, config.root_path or config.workspace, config.workspace,
                 (config.root_path or config.workspace) / 'backups']
    if any(safe(path).is_relative_to(root) for path in protected for root in roots):
        blockers.append('Managed deletion overlaps protected dispatcher data')
    for item in paths + diagnostics_updates:
        if safe(item['path']) in (safe(config.database), safe(db_guard.marker(config))):
            blockers.append('Artifact overlaps database/workspace identity')
    return dict(results=paths, diagnostics=diagnostics_updates, worktrees=trees, working_refs=refs, checkout=str(checkout),
                directories=[str(area)], external_checkout_preserved=bool(p.checkout)), blockers


def journal_path(config, name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('Invalid project ID')
    return safe((config.root_path or config.workspace) / 'backups' / 'purge' / f'{name}.json')


def inspect_attempts(config, area):
    found, status = [], []
    if area.exists():
        for path in sorted(area.iterdir()):
            path = safe(path)
            found.append(str(path))
            if path.suffix == '.sqlite':
                try:
                    db_guard.check(config, path=path)
                    status.append(f'{path}: backup verified')
                except (ValueError, RuntimeError, OSError, sqlite3.DatabaseError) as exc:
                    status.append(f'{path}: backup verification FAILED: {exc}')
            elif path.suffix == '.json':
                try:
                    state = json.loads(path.read_text())
                    status.append(f'{path}: phase={state["phase"]}, backup={state["backup"]}')
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    status.append(f'{path}: journal verification FAILED: {exc}')
            else:
                status.append(f'{path}: unexpected prior-attempt artifact')
    return found, status


def control_admission(config):
    """Read-only removal admission; never unlink a socket (including replacements).

    Execution calls this while owning BOTH InstanceLock descriptors. A managed
    listener/executor cannot start until those descriptors are released.
    Restore deliberately continues using the stricter db_guard.quiescent.
    """
    import errno
    import socket
    import stat
    control = safe(config.workspace / '.control.sock')
    # Even free locks must not override contradictory evidence of a live PID.
    monitor = safe(config.workspace / '.monitor.json')
    if monitor.exists():
        meta = json.loads(monitor.read_text())
        if not isinstance(meta, dict):
            raise ValueError('Uncertain daemon process identity')
        pid = meta.get('pid')
        if type(pid) is not int or pid <= 0:
            raise ValueError('Uncertain daemon process identity')
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError('Daemon process identity still live or reused')
    try:
        before = control.lstat()
    except FileNotFoundError:
        return {'state': 'absent'}
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
        raise ValueError('Unsafe control socket type/owner')
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(control))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise ValueError('Control listener absence not proven') from exc
        else:
            raise ValueError('Active control socket listener')
    # Linux exposes bound-but-not-listening owners too: ECONNREFUSED alone
    # cannot distinguish a leftover from a server between bind() and listen().
    # Missing/unreadable kernel evidence fails closed through the caller.
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and fields[7] == str(control):
            raise ValueError('Control socket still has a live kernel owner')
    after = control.lstat()
    identity = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError('Control socket changed during inspection')
    return {'state': 'stale', 'identity': list(identity(after)),
            'action': 'Preserve stale socket; removal holds both instance locks'}


def removal_quiescent(config):
    control_admission(config)
    path = db_guard.safe_path(config.database)
    for suffix in ('-wal', '-shm'):
        if Path(str(path) + suffix).exists():
            raise ValueError('Working WAL/SHM present; checkpoint after shutdown')


def plan(config, name):
    # YAML may already have committed when an interruption leaves the backup behind.
    if not any(p.id == name for p in config.projects):
        journal = journal_path(config, name)
        if journal.is_file():
            state = json.loads(journal.read_text())
            found, status = inspect_attempts(config, journal.parent)
            return dict(project=name, executable=False, counts={}, previous_attempts=found, previous_attempt_status=status,
                blockers=[f'Prior purge phase={state.get("phase")}; backup preserved: {state.get("backup")}; '
                          f'journal: {journal}. Restore YAML from original_config and use db restore BACKUP; '
                          'reconcile Recovery and explicitly archive failed-attempt files before retry.'])
    p = project(config, name)
    with snapshot(config) as (db, meta):
        triggers(db)
        validate_connection(db)
        selected, updates, blockers, rows = database_plan(db, p)
        if meta[3]:
            blockers.append('Global Recovery barrier')
        if db.execute("SELECT 1 FROM recovery_operations WHERE phase<>'completed' UNION SELECT 1 FROM recovery_quarantine LIMIT 1").fetchone():
            blockers.append('Active recovery operations/quarantine')
        if db.execute("SELECT 1 FROM executions WHERE status IN ('reserved','preparing','executing','publishing')").fetchone():
            blockers.append('Executor shutdown not proven: unfinished execution')
        try:
            artifacts, problems = artifact_plan(config, p, rows)
        except (ValueError, RuntimeError, OSError) as exc:
            artifacts = dict(results=[], diagnostics=[], worktrees=[], working_refs=[],
                checkout=str(p.checkout or config.workspace / 'repos' / p.id),
                directories=[str(config.workspace / 'worktrees' / p.id)],
                external_checkout_preserved=bool(p.checkout))
            problems = [f'Artifact ownership inspection failed: {exc}']
        blockers.extend(problems)
    state = daemon(config)
    if state['state'] != 'STOPPED' or (config.workspace / '.control.sock').is_symlink():
        blockers.append('Daemon shutdown not proven')
    control = {'state': 'unsafe'}
    try:
        control = control_admission(config)
    except (ValueError, OSError, TypeError) as exc:
        blockers.append(f'Control admission: {exc}')
    for suffix in ('-wal', '-shm'):
        if Path(str(config.database) + suffix).exists():
            blockers.append('Working SQLite sidecars present')
    journal = journal_path(config, name)
    leftovers, attempt_status = inspect_attempts(config, journal.parent)
    if leftovers:
        blockers.append('Prior purge backup/journal present; restore configuration from journal.original_config if needed, use db restore BACKUP and reconcile Recovery; explicitly archive failed-attempt files outside backups/purge before retry: ' + ', '.join(leftovers))
    return dict(project=name, repository=p.repository, counts={t: len(v) for t, v in selected.items()},
                rows=selected, database_fingerprint=fingerprint(rows), session_updates=updates, **artifacts, daemon=state,
                control=control, managed_checkout=Path(artifacts['checkout']).exists() and not artifacts['external_checkout_preserved'],
                database='OK', recovery='BLOCKED' if meta[3] or any('recovery' in b.lower() for b in blockers) else 'READY',
                metadata=list(meta), previous_attempts=leftovers, previous_attempt_status=attempt_status, executable=not blockers, blockers=blockers)


def sql_purge(db, p, expected, checkpoint=lambda phase: None):
    """One transaction, including transactional trigger DDL; no executescript."""
    db.execute('PRAGMA foreign_keys=ON')
    if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('Foreign keys unavailable')
    try:
        db.execute('BEGIN IMMEDIATE')
        definitions = triggers(db)
        if list(validate_connection(db)) != expected['metadata'] or expected['metadata'][3]:
            raise ValueError('Database identity/Recovery admission changed')
        selected, updates, blockers, before = database_plan(db, p)
        if (blockers or selected != expected['rows'] or updates != expected['session_updates']
                or fingerprint(before) != expected['database_fingerprint']):
            raise ValueError('Database plan changed')
        for name in definitions:
            db.execute(f'DROP TRIGGER {name}')
        checkpoint('drop')
        for change in updates:
            db.execute('UPDATE recovery_sessions SET summary=?,checked_issues=?,quarantined_issues=? WHERE rowid=?',
                       (change['summary'], change['checked'], change['quarantined'], change['rowid']))
        for table in ORDER:
            if selected[table]:
                # One statement is essential for intra-project self-referencing issues.
                rowids = ','.join(str(int(v)) for v in selected[table])
                db.execute(f'DELETE FROM {table} WHERE rowid IN ({rowids})')
        checkpoint('delete')
        for sql in definitions.values():
            db.execute(sql)
        checkpoint('create')
        triggers(db)
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Foreign key check failed')
        remaining, _, problems, after = database_plan(db, p)
        for table in ORDER:
            unchanged = [r for r in before[table] if r['_rowid'] not in selected[table]]
            if table == 'recovery_sessions':
                for row in unchanged:
                    for change in updates:
                        if row['_rowid'] == change['rowid']:
                            row.update(summary=change['summary'], checked_issues=change['checked'],
                                       quarantined_issues=change['quarantined'])
            if unchanged != after[table]:
                raise ValueError(f'Unrelated data changed: {table}')
        if any(remaining.values()) or problems:
            raise ValueError('Project references remain')
        validate_connection(db)
        checkpoint('commit')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    triggers(db)
    validate_connection(db)


def validate_connection(db):
    factory = db.row_factory
    try:
        db.row_factory = None
        metadata = db_guard.validate(db)
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        return metadata
    finally:
        db.row_factory = factory


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(path, content):
    fd, temp = tempfile.mkstemp(prefix='.purge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def write_journal(path, value):
    safe(path)
    fd, temp = tempfile.mkstemp(prefix='.purge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def execute(config, name, config_path, *, stdin=None, output=print, checkpoint=lambda phase: None):
    """Caller gets a precise plan before exact-ID terminal confirmation."""
    from .projects import edit
    from .config import load_config
    import shutil
    stdin = stdin or sys.stdin
    initial = plan(config, name)
    from .monitoring_output import removal_plan_text, render_removal_plan
    if output is print:
        render_removal_plan(initial, dry_run=False)
    else:
        output(removal_plan_text(initial, dry_run=False))
    if not initial['executable']:
        raise ValueError('Purge refused: ' + '; '.join(initial['blockers']))
    if not stdin.isatty():
        raise ValueError('Purge requires an interactive terminal')
    output(f'Type full project.id ({name}) to permanently purge local history:')
    try:
        confirmation = stdin.readline().rstrip('\r\n')
    except KeyboardInterrupt:
        raise ValueError('Removal cancelled') from None
    if confirmation != name:
        raise ValueError('Purge confirmation mismatch')
    backup = None
    journal = journal_path(config, name)
    phase = 'preflight'
    try:
        with InstanceLock(config):
            db_guard.check(config, execution=True)
            removal_quiescent(config)
            # plan() observes our own locks as UNKNOWN; compare excluding daemon only.
            current = plan(config, name)
            current['daemon'] = initial['daemon']
            current['blockers'] = [v for v in current['blockers'] if v != 'Daemon shutdown not proven']
            current['executable'] = not current['blockers']
            if current != initial:
                raise ValueError('Purge plan changed; run dry-run again')
            with edit(Path(config_path)) as (locked_config, data):
                if locked_config != config:
                    raise ValueError('Project configuration changed')
                safe(config_path)
                roots = [Path(initial['checkout']), *map(Path, initial['directories'])]
                if (any(Path(config_path).absolute().is_relative_to(root) for root in roots)
                        or str(Path(config_path).absolute()) in {v['path'] for v in initial['results'] + initial['diagnostics']}):
                    raise ValueError('Configuration overlaps deletion root')
                journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                journal.parent.chmod(0o700)
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                backup = journal.parent / f'{stamp}-{name}-{uuid.uuid4().hex}.sqlite'
                db_guard.backup(config.database, backup)
                db_guard.check(config, path=backup)
                state = dict(phase='backup', backup=str(backup), plan=initial,
                             original_config=Path(config_path).read_text(), config_path=str(Path(config_path).absolute()),
                             instance=db_guard.check(config)[2])
                write_journal(journal, state)
                checkpoint('backup')
                phase = 'database-intent'
                state['phase'] = phase
                write_journal(journal, state)
                with db_guard.connection(config.database) as db:
                    db.row_factory = sqlite3.Row
                    sql_purge(db, project(config, name), initial, checkpoint)
                phase = 'database-committed'
                state['phase'] = phase
                write_journal(journal, state)
                checkpoint(phase)
                git = PurgeGit(config)
                for tree in initial['worktrees']:
                    safe(tree['path'])
                    # Recheck Git identity immediately before local removal.
                    if git.run(tree['path'], 'rev-parse', 'HEAD') != tree['head']:
                        raise ValueError('Worktree HEAD changed')
                    if git.run(tree['path'], 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
                        raise ValueError('Worktree became dirty')
                    git.run(initial['checkout'], 'worktree', 'remove', tree['path'])
                    checkpoint('worktree')
                for ref in initial['working_refs']:
                    git.run(initial['checkout'], 'update-ref', '-d', ref['ref'], ref['head'])
                    checkpoint('ref')
                for item in initial['results']:
                    path = safe(item['path'])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                        raise ValueError('Result changed')
                    path.unlink()
                    checkpoint('result')
                for item in initial['diagnostics']:
                    path = safe(item['path'])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                        raise ValueError('Diagnostic changed')
                    if item['content']:
                        atomic_text(path, item['content'])
                    else:
                        path.unlink()
                    checkpoint('diagnostic')
                for path in initial['directories']:
                    path = safe(path)
                    if path.exists():
                        path.rmdir()
                checkout = safe(initial['checkout'])
                if checkout.exists():
                    # Recheck isolation/cleanliness after removing the planned worktrees and refs.
                    if len(git.worktrees(project(config, name))) != 1:
                        raise ValueError('New shared worktree appeared')
                    if git.run(checkout, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'):
                        raise ValueError('Managed checkout became dirty')
                    remaining_refs = git.run(checkout, 'for-each-ref', '--format=%(refname)', 'refs/heads').splitlines()
                    if any(ref != 'refs/heads/' + project(config, name).base_branch for ref in remaining_refs):
                        raise ValueError('Unplanned local ref appeared')
                    shutil.rmtree(checkout)
                phase = 'files-removed'
                state['phase'] = phase
                write_journal(journal, state)
                checkpoint(phase)
                with snapshot(config) as (db, _):
                    remaining, updates, problems, final_rows = database_plan(db, project(config, name))
                    triggers(db)
                    if any(remaining.values()) or updates or problems:
                        raise ValueError('Final project reference check failed')
                final_artifacts, final_problems = artifact_plan(config, project(config, name), final_rows)
                if final_problems or any(final_artifacts[k] for k in ('results', 'diagnostics', 'worktrees', 'working_refs')):
                    raise ValueError('Final artifact inspection failed: ' + '; '.join(final_problems))
                if any(Path(v).exists() for v in [initial['checkout'], *initial['directories']]):
                    raise ValueError('Managed artifacts remain')
                data['projects'] = [v for v in data['projects'] if v['id'] != name]
                phase = 'yaml-intent'
                state['phase'] = phase
                write_journal(journal, state)
                checkpoint(phase)
            phase = 'yaml-removed'
            state['phase'] = phase
            write_journal(journal, state)
            checkpoint(phase)
            if any(p.id == name for p in load_config(config_path).projects):
                raise ValueError('Configuration removal failed')
            db_guard.check(config, execution=True)
            backup.unlink()
            journal.unlink()
            sync_directory(journal.parent)
    except BaseException as exc:
        if backup is not None and backup.exists():
            raise ValueError(f'FAIL purge phase={phase}; backup preserved: {backup}; journal: {journal}. '
                             'Stop dispatcher; restore YAML from journal.original_config if needed; use db restore BACKUP, then reconcile Recovery. '
                             'Explicitly archive failed-attempt files outside backups/purge before retry. '
                             f'Cause: {exc}') from exc
        raise
    output(f'PASS purge: {sum(initial["counts"].values())} database rows, '
           f'{len(initial["results"])} results, {len(initial["worktrees"])} worktrees; temporary backup removed')
