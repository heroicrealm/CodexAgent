"""Approval boundaries shared by CLI and GitHub polling. Never executes tasks."""
from datetime import datetime

from dispatcher.github import GitHubError


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


REASONS = {
    'untrusted_actor': 'command actor is not an allowlisted human',
    'stale_version': 'command is not bound to the current observed version',
    'missing_server_fence': 'GitHub server observation time is unavailable',
    'no_event_proof': 'no matching labeled event proves the command actor',
    'proof_unavailable': 'GitHub command history or revision proof is unavailable',
    'ambiguous_event_time': 'command is not strictly newer than the server revision/eligibility boundary',
    'eligibility_changed': 'Issue readiness or content changed during verification',
    'conflicting_decision': 'another decision or observation won the SQLite transaction',
    'already_processed': 'this event was already recorded and cannot be applied again',
}
RETRY = 'Inspect show; remove the command labels, poll until their absence is observed, then reapply for the current version; or use CLI approve/reject.'


def _reason(store, project, issue, previous, row, events, proof, event):
    actor = event.get('actor') or {}
    if (actor.get('type') != 'User'
            or actor.get('login', '').casefold() not in {a.casefold() for a in project.allowed_authors}):
        return 'untrusted_actor'
    if not previous or not previous['available'] or previous['version'] != row['version'] or previous['content_hash'] != row['content_hash']:
        return 'stale_version'
    names = {label['name'].casefold() for label in proof['labels']['nodes']}
    if (not issue['eligible'] or proof['state'] != 'OPEN' or proof['labels']['pageInfo']['hasNextPage']
            or project.task_label.casefold() not in names or event['label']['name'].casefold() not in names
            or proof['databaseId'] != issue['id'] or proof['title'] != issue['title'] or proof['body'] != issue['body']
            or proof['author']['login'].casefold() != issue['author'].casefold()):
        return 'eligibility_changed'
    guard = store.guard(project.repository, issue['number'])
    if not guard or not guard['server_at']:
        return 'missing_server_fence'
    if guard['version'] != row['version'] or guard['content_hash'] != row['content_hash']:
        return 'stale_version'
    # All ordered times originate at GitHub. Local time is audit metadata only.
    boundary = [timestamp(guard['server_at'])]
    if proof['lastEditedAt'] is not None:
        boundary.append(timestamp(proof['lastEditedAt']))
    for change in events:
        if (change.get('event') in ('renamed', 'transferred', 'closed', 'reopened')
                or (change.get('event') in ('labeled', 'unlabeled')
                    and change.get('label', {}).get('name', '').casefold() == project.task_label.casefold())):
            boundary.append(timestamp(change['created_at']))
            if change['id'] >= event['id']:
                return 'ambiguous_event_time'
    if any(timestamp(event['created_at']) <= value for value in boundary):
        return 'ambiguous_event_time'
    return None


def commands(store, adapter, project, items, before, observation=None):
    """Return safe, per-command receipts; one rejection cannot stop other issues.

    An unprovable event is durably quarantined until label absence is observed.
    Recovery of GitHub alone never turns a previously unprovable label into consent.
    """
    reports = []
    for issue in items:
        names = {name.casefold() for name in issue['labels']}
        actions = {project.label(k).name.casefold(): k for k in ('approve', 'reject')}
        row = (observation.get(issue['number']) if observation is not None
               else store.get(project.repository, issue['number']))
        if row is None:
            continue
        active = []
        for name, action in actions.items():
            if name not in names:
                store.clear_block(project.repository, issue['number'], action, issue.get('server_observed_at'),
                                  row.get('observation_token') if isinstance(row, dict) else None)
            else:
                block = store.blocked(project.repository, issue['number'], action)
                if block and block['event_id'] is None:
                    reports.append(dict(issue=issue['number'], action=action, version=row['version'],
                                        outcome='quarantined', reason=block['reason'], retry=RETRY, notify=False))
                else:
                    active.append((name, action))
        if not active:
            continue
        try:
            events, proof = adapter.command_events(project, issue['number'])
            latest = {}
            for event in sorted(events, key=lambda e: e['id']):
                if event.get('event') in ('labeled', 'unlabeled'):
                    latest[event['label']['name'].casefold()] = event
        except (GitHubError, KeyError, TypeError, ValueError, AttributeError):
            events, proof, latest = [], None, {}
        for name, action in active:
            event = latest.get(name)
            reason = 'proof_unavailable' if proof is None else None
            event_id = None
            if reason is None and (not event or event['event'] != 'labeled' or type(event.get('id')) is not int):
                reason = 'no_event_proof'
            elif reason is None:
                event_id = event['id']
                try:
                    reason = _reason(store, project, issue, before.get(issue['number']), row, events, proof, event)
                except (KeyError, TypeError, ValueError, AttributeError):
                    reason = 'proof_unavailable'
            result = None
            if event_id is not None:
                result = store.decide(project.repository, issue['number'], row['version'], action,
                                      'github', event_id, valid=reason is None,
                                      expected_observation=row.get('observation_token') if isinstance(row, dict) else None,
                                      expected_synced_at=row['synced_at'])
            if result == 'duplicate':
                block = store.blocked(project.repository, issue['number'], action)
                reports.append(dict(issue=issue['number'], action=action, version=row['version'], outcome='duplicate',
                                    reason=block['reason'] if block else 'already_processed', retry=RETRY, notify=False))
                continue
            if reason is None and result in ('applied', 'already_applied'):
                reports.append(dict(issue=issue['number'], action=action, version=row['version'],
                                    outcome=result, reason=None, notify=False))
                continue
            reason = reason or 'conflicting_decision'
            notify = store.quarantine(project.repository, issue['number'], action,
                                      issue.get('server_observed_at'), event_id, reason)
            reports.append(dict(issue=issue['number'], action=action, version=row['version'],
                                outcome='rejected', reason=reason, retry=RETRY, notify=notify))
    return reports


def decide_cli(store, adapter, project, number, action):
    previous = store.get(project.repository, number)
    if previous is None:
        raise RuntimeError('Unknown issue; run poll and show before deciding')
    expected = previous['version']
    # A complete fresh fetch preserves revocation semantics and never approves on API failure.
    try:
        items = adapter.fetch(project)
    except GitHubError:
        store.decide(project.repository, number, expected, action, valid=False)
        raise
    observation = {}
    store.apply(project, items, observation=observation)
    current = observation[number]
    result = store.decide(project.repository, number, expected, action,
                          expected_synced_at=current['synced_at'], expected_observation=current['observation_token'])
    if result not in ('applied', 'already_applied'):
        raise RuntimeError('Decision conflicts with current version, readiness or previous decision; inspect show')
    try:
        adapter.reconcile(project, store.get(project.repository, number))
    except GitHubError:
        return result, False
    return result, True
