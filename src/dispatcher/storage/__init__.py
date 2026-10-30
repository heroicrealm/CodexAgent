"""Versioned SQLite snapshots. Transactions serialize polling and future approval."""
import os
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone


MUTABLE = ('discovered', 'pending', 'revoked')


def now():
    return datetime.now(timezone.utc).isoformat()


def content_hash(issue):
    # Labels, timestamps and other transport metadata do not invalidate approval.
    value = {key: issue[key] for key in ('id', 'title', 'body', 'target_branch', 'author')}
    value['author'] = value['author'].casefold()
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path):
        from .migrations import prepare, initialize, upgrade604, upgrade605, upgrade606, upgrade607, upgrade608, upgrade609, upgrade610, VERSION
        prepare(path)
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        try:
            upgrade604(self.db)
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                initialize(self.db)
                self.db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
                self.db.executemany('INSERT OR IGNORE INTO schema_migrations VALUES(?)', [(1,), (2,), (3,), (VERSION,), (604,)])
                upgrade605(self.db)
                upgrade606(self.db)
                upgrade607(self.db)
                upgrade608(self.db)
                upgrade609(self.db)
                upgrade610(self.db)
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def get(self, repository, number):
        return self.db.execute('SELECT * FROM tasks WHERE repository=? AND number=?',
                               (repository.casefold(), number)).fetchone()

    def confirm(self, repository, number, expected_version):
        """Stage 3 contract: confirm only the available, current discovered version.

        Polling never invokes this operation. A stale version or ineligible
        state returns False without changing the record or its history.
        """
        repository = repository.casefold()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            before = self.get(repository, number)
            applied = (before is not None and before['version'] == expected_version
                       and not self.configuration_blocked(before)
                       and before['available'] and before['state'] == 'discovered'
                       and self._set_state(before, 'pending', expected_version))
            if applied:
                row = self.get(repository, number)
                key = 'confirm:' + uuid.uuid4().hex
                receipt = self.db.execute("INSERT INTO decisions(repository,number,version,source,action,result,created_at,event_id) VALUES(?,?,?,'internal','approve','applied',?,?)",
                                          (repository, number, expected_version, now(), key))
                iteration = self._new_iteration(row, expected_version, receipt.lastrowid)
                self._record_transition(before, 'pending', key, 'local_operator', iteration_id=iteration)
                self._history(repository, number, row['version'], 'confirmed', row['snapshot'], now())
            return bool(applied)

    def queue(self, repository=None):
        if repository:
            return self.db.execute('SELECT * FROM tasks WHERE repository=? ORDER BY number',
                                   (repository.casefold(),)).fetchall()
        return self.db.execute('SELECT * FROM tasks ORDER BY repository,number').fetchall()

    def decide(self, repository, number, version, action, source='cli', event_id=None, valid=True, expected_synced_at=None, expected_observation=None, expected_state=None, evidence=None, actor=None):
        """First committed decision wins, including across independent connections."""
        if action not in ('approve', 'reject', 'cancel') or source not in ('cli', 'github'):
            raise ValueError('Invalid decision')
        repository = repository.casefold()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if event_id is not None and self.db.execute(
                    'SELECT 1 FROM decisions WHERE repository=? AND event_id=?', (repository, str(event_id))).fetchone():
                return 'duplicate'
            row = self.get(repository, number)
            valid = valid and not self.stop_requested(row)
            valid = valid and not (action == 'approve' and self.configuration_blocked(row))
            valid = valid and (expected_state is None or (row is not None and row['state'] == expected_state))
            valid = valid and (expected_synced_at is None or (row is not None and row['synced_at'] == expected_synced_at))
            blocked = self.blocked(repository, number, action) if source == 'github' else None
            valid = valid and not (blocked is not None and blocked['event_id'] is None)
            guard = self.guard(repository, number)
            valid = valid and (expected_observation is None or (guard is not None and guard['observation_token'] == expected_observation))
            result = 'unverified' if not valid else 'conflict'
            repeat = (row is not None and row['state'] == 'rejected'
                      and action in ('approve', 'cancel') and expected_state == 'rejected')
            if (valid and row and row['version'] == version and row['available']
                    and ((row['state'] == 'discovered' and action != 'cancel') or repeat)):
                state = {'approve': 'pending', 'reject': 'rejected', 'cancel': 'cancelled'}[action]
                self._set_state(row, state, version if action == 'approve' else None)
                iteration = self._new_iteration(row, version) if action == 'approve' else row['current_iteration_id']
                self._record_transition(row, state, f'{repository}:event:{event_id}' if event_id is not None else 'cli:' + uuid.uuid4().hex,
                                        actor or ('verified_github_operator' if source == 'github' else 'local_operator'), evidence=evidence, iteration_id=iteration)
                if action == 'cancel':
                    self.db.execute('INSERT INTO cancellations(issue_id) VALUES(?)', (row['issue_id'],))
                self._history(repository, number, version, action, row['snapshot'], now())
                result = 'applied'
            elif valid and row and row['version'] == version and row['state'] == {'approve': 'pending', 'reject': 'rejected', 'cancel': 'cancelled'}[action]:
                result = 'already_applied'
            receipt = self.db.execute('INSERT INTO decisions(repository,number,version,source,action,result,created_at,event_id) VALUES(?,?,?,?,?,?,?,?)',
                            (repository, number, version, source, action, result, now(),
                             str(event_id) if event_id is not None else None))
            if result == 'applied' and action == 'approve':
                self.db.execute('UPDATE task_iterations SET decision_id=? WHERE iteration_id=?', (receipt.lastrowid, iteration))
            return result

    def configuration_blocked(self, row):
        from dispatcher.preparation_error import blocked
        return self.stop_requested(row) or blocked(self, row)

    def stop_requested(self, row):
        return bool(row is not None and self.db.execute(
            'SELECT 1 FROM stop_requests WHERE issue_id=? AND active=1', (row['issue_id'],)).fetchone())

    def reset_confirmation(self, repository, number, expected_version):
        """Compare-and-reset operation for stage 3; never changes active attempts."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cursor = self.db.execute('''UPDATE tasks SET approved_version=NULL,state='discovered'
                WHERE repository=? AND number=? AND version=? AND available=1
                AND state IN ('pending','discovered')''',
                (repository.casefold(), number, expected_version))
            return cursor.rowcount == 1

    def guard(self, repository, number):
        return self.db.execute('SELECT * FROM command_guards WHERE repository=? AND number=?',
                               (repository.casefold(), number)).fetchone()

    def quarantine(self, repository, number, action, server_at, event_id, reason):
        """Persist a rejection; unknown events stay blocked until label absence.

        Returns whether the diagnostic changed, suppressing repeated warnings.
        """
        repository = repository.casefold()
        identity = str(event_id) if event_id is not None else None
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.blocked(repository, number, action)
            if prior:
                if prior['event_id'] is None:
                    return False
                if prior['event_id'] == identity and prior['reason'] == reason:
                    return False
            self.db.execute('INSERT OR REPLACE INTO command_quarantine VALUES(?,?,?,?,?,?)',
                            (repository, number, action, server_at, identity, reason))
        return True

    def blocked(self, repository, number, action):
        return self.db.execute('SELECT * FROM command_quarantine WHERE repository=? AND number=? AND action=?',
                               (repository.casefold(), number, action)).fetchone()

    def clear_block(self, repository, number, action, server_at=None, expected_observation=None):
        """Only a newer, current observation of absence clears an unknown event."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            block = self.blocked(repository, number, action)
            if block is None:
                return
            guard = self.guard(repository, number)
            if expected_observation is not None and (guard is None or guard['observation_token'] != expected_observation):
                return
            if block['event_id'] is None:
                try:
                    if server_at is None:
                        return
                    stamp = datetime.fromisoformat(server_at.replace('Z', '+00:00'))
                    if block['server_at'] is None:
                        self.db.execute('UPDATE command_quarantine SET server_at=? WHERE repository=? AND number=? AND action=?',
                                        (server_at, repository.casefold(), number, action))
                        return
                    if stamp <= datetime.fromisoformat(block['server_at'].replace('Z', '+00:00')):
                        return
                except (ValueError, TypeError):
                    return
            self.db.execute('DELETE FROM command_quarantine WHERE repository=? AND number=? AND action=?',
                            (repository.casefold(), number, action))

    def apply(self, project, issues, *, observation=None):
        counts = dict(discovered=0, updated=0, unchanged=0, revoked=0, skipped_untrusted=0)
        repository, stamp = project.repository.casefold(), now()
        seen = set()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for issue in issues:
                from .transitions import rest_id
                number = issue['number']
                if not rest_id(issue['id']) or not rest_id(number) or (issue.get('repository_id') is not None and not rest_id(issue['repository_id'])):
                    raise sqlite3.IntegrityError('Unresolved GitHub identity')
                if issue.get('repository_id') is not None and self.db.execute(
                        'SELECT 1 FROM tasks WHERE repository_id=? AND repository!=?', (issue['repository_id'], repository)).fetchone():
                    raise sqlite3.IntegrityError('Repository identity conflict')
                seen.add(number)
                row = self.get(repository, number)
                persisted = {key: value for key, value in issue.items() if key != 'server_observed_at'}
                names = {n.casefold() for n in issue.get('labels', [])}
                emergency = project.label('emergency_stop').name.casefold() in names
                if row and row['issue_id'] != issue['id']:
                    raise sqlite3.IntegrityError('GitHub issue identity conflict')
                if row:
                    from dispatcher.detachment import accept_observation
                    if not accept_observation(self, row, issue.get('server_observed_at')):
                        counts['unchanged'] += 1
                        continue
                if row and 'labels' in issue and project.task_label.casefold() not in names and not emergency:
                    from dispatcher.detachment import request_in_transaction
                    request_in_transaction(self, row, 'ready_removed', {'observation': persisted, 'observed_at': issue.get('server_observed_at')})
                    row = self.get(repository, number)
                if row and row['state'] == 'detached' and issue['eligible'] and not emergency:
                    stop = self.db.execute('SELECT * FROM stop_requests WHERE issue_id=? AND active=1', (row['issue_id'],)).fetchone()
                    from dispatcher.detachment import newer_observation
                    if stop and stop['finalized_at'] and newer_observation(stop, issue) and not self.db.execute(
                            "SELECT 1 FROM cleanup_jobs WHERE issue_id=? AND state!='completed'", (row['issue_id'],)).fetchone():
                        self._record_transition(row, 'discovered', 'reattach:' + str(stop['request_id']), 'ready_observer', {'observed_at':issue.get('server_observed_at')})
                        self._history(repository, number, row['version'], 'reattached', json.dumps(persisted), stamp)
                        self.db.execute('UPDATE stop_requests SET active=0 WHERE request_id=?', (stop['request_id'],))
                        self.db.execute("UPDATE tasks SET state='discovered',available=1,approved_version=NULL,current_iteration_id=NULL WHERE issue_id=?", (row['issue_id'],))
                        row = self.get(repository, number)
                if row and (self.stop_requested(row) or emergency):
                    self.db.execute('UPDATE tasks SET available=0,observed=?,synced_at=? WHERE issue_id=?',
                                    (json.dumps(persisted), stamp, row['issue_id']))
                    counts['unchanged'] += 1
                    continue
                if emergency:
                    issue = dict(issue, eligible=False)
                if not issue['eligible']:
                    if issue['author'].casefold() not in {a.casefold() for a in project.allowed_authors}:
                        counts['skipped_untrusted'] += 1
                    if row:
                        self._revoke(row, stamp, json.dumps(persisted), counts)
                    continue
                snapshot, digest = json.dumps(persisted, sort_keys=True), content_hash(issue)
                if row is None:
                    self.db.execute('INSERT INTO tasks(repository,number,issue_id,project_id,state,available,version,content_hash,snapshot,observed,approved_version,created_at,synced_at,repository_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (repository, number, issue['id'], project.id, 'discovered', 1, 1, digest,
                         snapshot, snapshot, None, stamp, stamp, issue.get("repository_id")))
                    self._history(repository, number, 1, 'discovered', snapshot, stamp)
                    counts['discovered'] += 1
                    continue
                if row['issue_id'] != issue['id']:
                    raise sqlite3.IntegrityError('GitHub issue identity conflict')
                repository_id = issue.get('repository_id')
                if repository_id is not None:
                    if row['repository_id'] not in (None, repository_id):
                        raise sqlite3.IntegrityError('GitHub repository identity conflict')
                    self.db.execute('UPDATE tasks SET repository_id=? WHERE repository=? AND number=?',
                                    (repository_id, repository, number))
                changed = row['content_hash'] != digest
                if row['state'] in (*MUTABLE, 'rejected'):
                    version = row['version'] + int(changed)
                    state = ('rejected' if row['state'] == 'rejected' else
                             'discovered' if changed or not row['available'] or row['state'] == 'revoked' else row['state'])
                    approved = row['approved_version'] if state == 'pending' else None
                    self.db.execute('''UPDATE tasks SET snapshot=?,observed=?,content_hash=?,version=?,
                        state=?,available=1,approved_version=?,synced_at=? WHERE repository=? AND number=?''',
                        (snapshot, snapshot, digest, version, state, approved, stamp, repository, number))
                    if changed or not row['available']:
                        self._history(repository, number, version, 'updated' if changed else 'restored', snapshot, stamp)
                    counts['updated' if changed or not row['available'] else 'unchanged'] += 1
                else:
                    if row['observed'] != snapshot:
                        self._history(repository, number, row['version'], 'observed', snapshot, stamp)
                    self.db.execute('UPDATE tasks SET observed=?,synced_at=? WHERE repository=? AND number=?',
                                    (snapshot, stamp, repository, number))
                    counts['updated' if changed else 'unchanged'] += 1
            for row in self.db.execute('SELECT * FROM tasks WHERE repository=?', (repository,)).fetchall():
                if row['number'] not in seen:
                    self._revoke(row, stamp, row['observed'], counts)
            by_number = {issue['number']: issue for issue in issues}
            for current in self.queue(repository):
                prior = self.guard(repository, current['number'])
                raw = by_number.get(current['number'], {})
                reset = (not prior or prior['version'] != current['version']
                         or prior['content_hash'] != current['content_hash']
                         or prior['available'] != current['available'])
                server_at = None if reset else prior['server_at']
                if current['available'] and server_at is None:
                    server_at = raw.get('server_observed_at')
                token = uuid.uuid4().hex
                self.db.execute('INSERT OR REPLACE INTO command_guards VALUES(?,?,?,?,?,?,?)',
                                (repository, current['number'], current['version'], current['content_hash'],
                                 current['available'], server_at, token))
                if observation is not None:
                    observation[current['number']] = dict(current, observation_token=token)
        return counts

    def _history(self, repository, number, version, event, snapshot, stamp):
        self.db.execute('INSERT INTO history(repository,number,version,event,snapshot,created_at) VALUES(?,?,?,?,?,?)',
                        (repository, number, version, event, snapshot, stamp))

    def _revoke(self, row, stamp, observed, counts):
        mutable = row['state'] in MUTABLE
        self.db.execute('''UPDATE tasks SET observed=?,synced_at=?,available=0,
            state=?,approved_version=? WHERE repository=? AND number=?''',
            (observed, stamp, 'revoked' if mutable else row['state'],
             None if mutable else row['approved_version'], row['repository'], row['number']))
        if row['available']:
            self._history(row['repository'], row['number'], row['version'], 'revoked', observed, stamp)
            counts['revoked'] += 1

    def _new_iteration(self, row, version, decision_id=None):
        number = self.db.execute('SELECT COALESCE(MAX(iteration_number),0)+1 FROM task_iterations WHERE issue_id=?',
                                 (row['issue_id'],)).fetchone()[0]
        cursor = self.db.execute('INSERT INTO task_iterations(issue_id,iteration_number,approved_version,decision_id,created_at) VALUES(?,?,?,?,?)',
                                (row['issue_id'], number, version, decision_id, now()))
        self.db.execute('UPDATE tasks SET current_iteration_id=? WHERE issue_id=?', (cursor.lastrowid, row['issue_id']))
        return cursor.lastrowid

    def transition(self, **request):
        from .transitions import transition
        return transition(self, **request)

    def _set_state(self, row, target, approved_version, *, current_iteration=None):
        """Shared CAS primitive; caller verifies authority/evidence and journals atomically."""
        if row is None or self.stop_requested(row) or row['state'] in ('done', 'cancelled', 'review_on_error'):
            return False
        expected_iteration = row['current_iteration_id'] if current_iteration is None else current_iteration
        cursor = self.db.execute("""UPDATE tasks SET state=?,approved_version=?
            WHERE issue_id=? AND state=? AND version=? AND current_iteration_id IS ?""",
            (target, approved_version, row['issue_id'], row['state'], row['version'], expected_iteration))
        return cursor.rowcount == 1

    def _record_transition(self, row, target, event_key, actor, evidence=None, iteration_id=None):
        """Shared transactional journal for legacy and guarded runtime paths."""
        self.db.execute('INSERT INTO task_transitions VALUES(?,?,?,?,?,?,?,?,?)',
                        (event_key, row['issue_id'], actor, row['state'], target, row['version'],
                         iteration_id if iteration_id is not None else row['current_iteration_id'],
                         json.dumps(evidence or {}, sort_keys=True), now()))

    def publication(self, operation_key, iteration_id, phase='intent', outcome='pending', *, retry=False):
        """Persist publication progress without reserving or starting Codex."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute('SELECT * FROM publication_operations WHERE operation_key=?', (operation_key,)).fetchone()
            if prior and prior['iteration_id'] != iteration_id:
                return 'conflict'
            if prior and prior['outcome'] == 'completed':
                return 'already_applied'
            self.db.execute("""INSERT INTO publication_operations VALUES(?,?,?,?,?,?)
                ON CONFLICT(operation_key) DO UPDATE SET phase=excluded.phase,outcome=excluded.outcome,
                retry_count=publication_operations.retry_count+?,updated_at=excluded.updated_at""",
                (operation_key, iteration_id, phase, outcome, 0, now(), int(retry)))
            return 'applied'


    def branches(self, repository, number):
        """Read current Issue branch parameters; attempt.branch remains historical."""
        from .migrations import BRANCH_COLUMNS
        row = self.get(repository, number)
        return None if row is None else {name: row[name] for name in BRANCH_COLUMNS}

    def save_branches(self, repository, number, *, expected_version, expected_iteration_id,
                      expected, base_branch=None, work_branch=None, branch_source=None,
                      branch_status='unresolved', branch_error=None, branch_prepared_at=None):
        """Compare-and-swap all branch fields and audit atomically.

        `expected` is the complete result of branches(). False means stale or
        frozen parameters. Constraint violations raise sqlite3.IntegrityError.
        """
        from .migrations import BRANCH_COLUMNS
        from dispatcher.github import valid_branch
        values = dict(base_branch=base_branch, work_branch=work_branch,
                      branch_source=branch_source, branch_status=branch_status,
                      branch_error=branch_error, branch_prepared_at=branch_prepared_at)
        if expected is None or set(expected) != set(BRANCH_COLUMNS):
            raise ValueError('Complete expected branch snapshot required')
        if any(value is not None and (not isinstance(value, str) or not valid_branch(value))
               for value in (base_branch, work_branch)):
            raise ValueError('Invalid branch name')
        if branch_status in ('resolved', 'prepared') and not (base_branch and work_branch and branch_source):
            raise ValueError('Resolved branches require base, work and source')
        if branch_status == 'prepared' and not branch_prepared_at:
            raise ValueError('Prepared branch requires timestamp')
        repository = repository.casefold()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(repository, number)
            if (row is None or row['version'] != expected_version
                    or row['current_iteration_id'] != expected_iteration_id
                    or self.branches(repository, number) != expected):
                return False
            if values == expected:
                return True
            if row['state'] in ('running', 'review', 'done', 'cancelled', 'review_on_error') or self.db.execute(
                    "SELECT 1 FROM attempts WHERE repository=? AND number=? AND state='running'",
                    (repository, number)).fetchone():
                return False
            self.db.execute('UPDATE tasks SET ' + ','.join(f'{name}=?' for name in values)
                            + ' WHERE repository=? AND number=?', (*values.values(), repository, number))
            self._history(repository, number, row['version'], 'branches_updated',
                          json.dumps({'before': expected, 'after': values}, sort_keys=True), now())
            return True
