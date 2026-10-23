"""Approval boundaries shared by CLI and GitHub polling. Never executes tasks."""
from datetime import datetime

from dispatcher.github import GitHubError
from dispatcher.storage import content_hash


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def commands(store, adapter, project, items, before, observation=None):
    for issue in items:
        names = {name.casefold() for name in issue['labels']}
        actions = {project.label(k).name.casefold(): k for k in ('approve', 'reject')}
        if not names.intersection(actions):
            continue
        events, proof = adapter.command_events(project, issue['number'])
        # Verify content returned by the independent edit-time query too.
        same = (proof.get('databaseId') == issue['id'] and proof['title'] == issue['title'] and proof['body'] == issue['body']
                and proof.get('author', {}).get('login', '').casefold() == issue['author'].casefold())
        previous = before.get(issue['number'])
        row = (observation.get(issue['number']) if observation is not None
               else store.get(project.repository, issue['number']))
        if row is None:
            continue
        boundary = store.db.execute("""SELECT created_at FROM history WHERE repository=? AND number=?
            AND event IN ('discovered','updated','restored','revoked') ORDER BY id DESC LIMIT 1""",
            (project.repository.casefold(), issue['number'])).fetchone()
        latest = {}
        try:
            for event in sorted(events, key=lambda e: e['id']):
                if event.get('event') in ('labeled', 'unlabeled'):
                    latest[event['label']['name'].casefold()] = event
            # Title edits and eligibility churn may leave content_hash unchanged
            # (edit/revert or ready removal/restoration between polls).
            changed_at = [timestamp(e['created_at']) for e in events
                          if e.get('event') in ('renamed', 'transferred', 'closed', 'reopened')
                          or (e.get('event') in ('labeled', 'unlabeled')
                              and e.get('label', {}).get('name', '').casefold() == project.task_label.casefold())]
            for name, action in actions.items():
                event = latest.get(name)
                if name not in names:
                    continue
                if not event or event['event'] != 'labeled':
                    raise GitHubError('Cannot prove who set the command label')
                actor = event.get('actor') or {}
                event_time = timestamp(event['created_at'])
                valid = bool(same and previous and previous['available'] and issue['eligible']
                    and previous['content_hash'] == content_hash(issue)
                    and previous['version'] == row['version']
                    and actor.get('type') == 'User'
                    and actor.get('login', '').casefold() in {a.casefold() for a in project.allowed_authors}
                    and boundary and event_time > timestamp(boundary['created_at'])
                    and all(event_time > changed for changed in changed_at)
                    and (proof['lastEditedAt'] is None or event_time > timestamp(proof['lastEditedAt'])))
                result = store.decide(project.repository, issue['number'], row['version'], action,
                                      'github', event['id'], valid, expected_synced_at=row['synced_at'])
                if result in ('unverified', 'conflict'):
                    raise GitHubError('Command rejected: untrusted, stale or conflicting')
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GitHubError('Cannot verify command event') from None


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
                          expected_synced_at=current['synced_at'])
    if result not in ('applied', 'already_applied'):
        raise RuntimeError('Decision conflicts with current version, readiness or previous decision; inspect show')
    try:
        adapter.reconcile(project, store.get(project.repository, number))
    except GitHubError:
        return result, False
    return result, True
