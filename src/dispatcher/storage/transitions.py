"""Guarded stage 6 API. Evidence is supplied by a verified GitHub read, never text commands.

The stage 6.3 publisher supplies verified REST evidence to this contract.
"""
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from . import now


class Outcome(StrEnum):
    APPLIED = 'applied'
    ALREADY_APPLIED = 'already_applied'
    INVALID_TRANSITION = 'invalid_transition'
    STALE_VERSION = 'stale_version'
    UNAUTHORIZED = 'unauthorized'
    MISSING_EVIDENCE = 'missing_evidence'
    IDENTITY_UNRESOLVED = 'identity_unresolved'
    CONFLICT = 'conflict'


@dataclass(frozen=True)
class Authority:
    actor: str
    allowed_authors: tuple[str, ...]
    role: str = 'issue_owner'
    human: bool = True

    def permits(self, role):
        return (self.human and self.role == role and bool(self.actor)
                and self.actor.casefold() in {a.casefold() for a in self.allowed_authors})


@dataclass(frozen=True)
class Evidence:
    repository_id: int
    issue_id: int
    issue_number: int
    # Both timestamps must originate at GitHub, not the dispatcher's wall clock.
    observed_at: str
    event_at: str
    state_observed_at: str
    content_hash: str
    iteration_id: int | None
    expected_state: str
    available: bool = True
    pr_id: int | None = None
    pr_number: int | None = None
    head: str | None = None
    base: str | None = None
    head_sha: str | None = None
    pr_state: str | None = None
    merged: bool | None = None
    merged_at: str | None = None
    # Verified Issue identity/ownership proof, not branch-name inference.
    linked_issue_id: int | None = None


@dataclass(frozen=True)
class Reservation:
    branch: str
    maximum: int
    observation: str


def rest_id(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def transition(store, *, repository, number, expected_state, version, iteration_id,
               target, event_key, authority, evidence, reservation=None):
    """CAS and receipt/history/decision/iteration writes share one transaction.

    Caller authenticates the source and obtains complete network evidence before
    entering this function. No network calls occur under the SQLite writer lock.
    """
    db = store.db
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = store.get(repository, number)
        if row is None or not rest_id(row['repository_id']) or not rest_id(row['issue_id']):
            return Outcome.IDENTITY_UNRESOLVED
        if not isinstance(authority, Authority) or not authority.permits(
                'observer' if target in ('review', 'done', 'rejected') and expected_state != 'discovered' else 'issue_owner'):
            return Outcome.UNAUTHORIZED
        if not event_key or not isinstance(event_key, str):
            return Outcome.MISSING_EVIDENCE
        prior = db.execute('SELECT * FROM task_transitions WHERE event_key=?', (event_key,)).fetchone()
        if prior:
            if (prior['issue_id'] == row['issue_id'] and prior['from_state'] == expected_state
                    and prior['to_state'] == target and prior['version'] == version
                    and prior['actor'] == authority.actor
                    and json.loads(prior['evidence']).get('expected_iteration_id') == iteration_id):
                return Outcome.ALREADY_APPLIED
            return Outcome.CONFLICT
        if row['version'] != version or row['current_iteration_id'] != iteration_id:
            return Outcome.STALE_VERSION
        edges = {('discovered', 'pending'), ('discovered', 'rejected'), ('pending', 'running'),
                 ('running', 'review'), ('review', 'done'), ('review', 'rejected'),
                 ('rejected', 'pending'), ('rejected', 'cancelled')}
        if row['state'] != expected_state or (expected_state, target) not in edges:
            return Outcome.INVALID_TRANSITION
        if not isinstance(evidence, Evidence) or not evidence.available:
            return Outcome.MISSING_EVIDENCE
        if not all(rest_id(v) for v in (evidence.repository_id, evidence.issue_id, evidence.issue_number)):
            return Outcome.IDENTITY_UNRESOLVED
        if evidence.iteration_id != iteration_id or evidence.expected_state != expected_state:
            return Outcome.STALE_VERSION
        if evidence.content_hash != row['content_hash']:
            return Outcome.STALE_VERSION
        if (evidence.repository_id != row['repository_id'] or evidence.issue_id != row['issue_id']
                or evidence.issue_number != number):
            return Outcome.CONFLICT
        try:
            observed, event, fence = [datetime.fromisoformat(v.replace('Z', '+00:00')) for v in
                                      (evidence.observed_at, evidence.event_at, evidence.state_observed_at)]
            if any(v.tzinfo is None for v in (observed, event, fence)) or event > observed or event <= fence:
                return Outcome.MISSING_EVIDENCE
        except (ValueError, TypeError, AttributeError):
            return Outcome.MISSING_EVIDENCE
        guard = store.guard(repository, number)
        if guard and guard['server_at'] and fence < datetime.fromisoformat(guard['server_at'].replace('Z', '+00:00')):
            return Outcome.MISSING_EVIDENCE
        request_iteration = iteration_id
        # Bind a repeated approve to the specific rejected transition, not just
        # a content version which may remain unchanged across iterations.
        if expected_state == 'rejected':
            rejected = db.execute("SELECT evidence FROM task_transitions WHERE issue_id=? AND to_state='rejected' ORDER BY rowid DESC LIMIT 1",
                                  (row['issue_id'],)).fetchone()
            if not rejected:
                return Outcome.MISSING_EVIDENCE
            last = json.loads(rejected['evidence']).get('observed_at')
            if not last:
                return Outcome.MISSING_EVIDENCE
            if fence < datetime.fromisoformat(last.replace('Z', '+00:00')):
                return Outcome.MISSING_EVIDENCE
        if target in ('pending', 'running') and not row['available']:
            return Outcome.MISSING_EVIDENCE
        if target == 'running':
            from dispatcher.execution_store import ExecutionStore
            from dispatcher.github import valid_branch
            if (not isinstance(reservation, Reservation) or not valid_branch(reservation.branch)
                    or type(reservation.maximum) is not int or reservation.maximum <= 0):
                return Outcome.MISSING_EVIDENCE
            attempt = ExecutionStore(store)._reserve(row, reservation.branch, reservation.maximum,
                reservation.observation, event_key=event_key, actor=authority.actor,
                evidence=dict(asdict(evidence), expected_iteration_id=request_iteration))
            return Outcome.APPLIED if attempt else Outcome.CONFLICT
        if target in ('review', 'done') or (expected_state == 'review' and target == 'rejected'):
            if not rest_id(iteration_id):
                return Outcome.MISSING_EVIDENCE
            if (not rest_id(evidence.pr_id) or not rest_id(evidence.pr_number)
                    or evidence.linked_issue_id != row['issue_id']
                    or not all(isinstance(v, str) and v for v in (evidence.head, evidence.base, evidence.head_sha))):
                return Outcome.MISSING_EVIDENCE
            if not re.fullmatch(r'[0-9a-f]{40,64}', evidence.head_sha):
                return Outcome.MISSING_EVIDENCE
            if evidence.merged_at:
                try:
                    merged = datetime.fromisoformat(evidence.merged_at.replace('Z', '+00:00'))
                    if merged.tzinfo is None or merged > observed:
                        return Outcome.MISSING_EVIDENCE
                except (ValueError, TypeError, AttributeError):
                    return Outcome.MISSING_EVIDENCE
            if target == 'review' and (evidence.pr_state != 'open' or evidence.merged is not False or evidence.merged_at):
                return Outcome.MISSING_EVIDENCE
            if target == 'done' and (evidence.pr_state != 'closed' or evidence.merged is not True or not evidence.merged_at):
                return Outcome.MISSING_EVIDENCE
            if target == 'rejected' and (evidence.pr_state != 'closed' or evidence.merged is not False or evidence.merged_at):
                return Outcome.MISSING_EVIDENCE
            linked = db.execute('SELECT * FROM task_pull_requests WHERE issue_id=? OR pr_id=? OR (repository_id=? AND pr_number=?)',
                                (row['issue_id'], evidence.pr_id, row['repository_id'], evidence.pr_number)).fetchall()
            if linked:
                if len(linked) != 1 or any(linked[0][k] != v for k, v in {
                        'issue_id': row['issue_id'], 'pr_id': evidence.pr_id, 'repository_id': row['repository_id'],
                        'pr_number': evidence.pr_number, 'head': evidence.head, 'base': evidence.base}.items()):
                    return Outcome.CONFLICT
            elif target != 'review':
                return Outcome.MISSING_EVIDENCE
            db.execute('''INSERT INTO task_pull_requests VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pr_id) DO UPDATE SET head_sha=excluded.head_sha,state=excluded.state,merged_at=excluded.merged_at''',
                (evidence.pr_id, row['issue_id'], row['repository_id'], evidence.pr_number,
                 evidence.head, evidence.base, evidence.head_sha, evidence.pr_state, evidence.merged_at))
            db.execute('INSERT INTO iteration_pr_history(iteration_id,pr_id,head_sha,created_at) VALUES(?,?,?,?)',
                       (iteration_id, evidence.pr_id, evidence.head_sha, now()))
        stamp = now()
        if target == 'pending' or target == 'cancelled' or expected_state == 'discovered':
            action = 'approve' if target == 'pending' else ('cancel' if target == 'cancelled' else 'reject')
            decision = db.execute('INSERT INTO decisions(repository,number,version,source,action,result,created_at,event_id) VALUES(?,?,?,?,?,?,?,?)',
                                 (repository.casefold(), number, version, 'contract', action, 'applied', stamp, event_key))
            if target == 'pending':
                iteration_id = store._new_iteration(row, version, decision.lastrowid)
        if not store._set_state(row, target, version if target == 'pending' else row['approved_version'],
                                current_iteration=iteration_id):
            raise RuntimeError('Transition CAS failed inside serialized transaction')
        store._record_transition(row, target, event_key, authority.actor, dict(asdict(evidence), expected_iteration_id=request_iteration), iteration_id)
        store._history(repository.casefold(), number, version, target, row['snapshot'], stamp)
        return Outcome.APPLIED
