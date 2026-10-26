"""Project-isolated polling and idempotent label reconciliation."""
import sqlite3
import sys

from dispatcher.github import Adapter, GitHubError
from dispatcher.pr_monitor import PullRequestMonitor
from dispatcher.cancellation import CancellationDelivery
from dispatcher.initialization import ready
from dispatcher.logging import event
from dispatcher.queue import commands, REASONS


class SyncService:
    def __init__(self, config, store, adapter=None):
        self.config, self.store = config, store
        self.adapter = adapter or Adapter(config.github_executable)

    def log(self, name, project, *, task=None, reason=None, action=None):
        self.config.logs.mkdir(parents=True, exist_ok=True)
        event(self.config.logs / 'dispatcher.jsonl', name, level='WARN', project=project.id, task=task, reason=reason, action=action)

    def poll(self):
        result = dict(discovered=0, updated=0, unchanged=0, revoked=0, skipped_untrusted=0, errors=[], skipped=[], commands=[])
        for project in self.config.projects:
            if not project.enabled:
                continue
            if not ready(project):
                result['skipped'].append(project.id)
                print(f'WARN: {project.id}: run init; initialization missing or stale', file=sys.stderr)
                self.log('project_not_initialized', project)
                continue
            try:
                before = {r['number']: dict(r) for r in self.store.queue(project.repository)}
                items = self.adapter.fetch(project)
                observation = {}
                counts = self.store.apply(project, items, observation=observation)
                for key, count in counts.items():
                    result[key] += count
                from dispatcher.detachment import StopController
                stops = StopController(self.config, self.store, self.adapter)
                for stopped_row in self.store.queue(project.repository):
                    # Targeted reads also cover closed managed Issues.
                    observed_item = next((i for i in items if i['number'] == stopped_row['number']), None)
                    if observed_item is None or project.label('emergency_stop').name.casefold() in {n.casefold() for n in observed_item['labels']}:
                        try:
                            raw_control = None if observed_item is None else dict(id=observed_item['id'], number=observed_item['number'], labels=[dict(name=n) for n in observed_item['labels']])
                            stops.observe(project, stopped_row, raw_control)
                        except (GitHubError, KeyError, TypeError, ValueError):
                            self.log('stop_verification_failed', project, task=stopped_row['number'])
                    latest_attempt = self.store.db.execute("SELECT 1 FROM attempts WHERE repository=? AND number=? AND state='running'",
                        (stopped_row['repository'], stopped_row['number'])).fetchone()
                    if not latest_attempt:
                        stops.finalize(stopped_row)
                receipts = commands(self.store, self.adapter, project, items, before, observation)
                for receipt in receipts:
                    notify = receipt.pop('notify')
                    result['commands'].append(dict(project=project.id, **receipt))
                    if notify:
                        result['errors'].append(dict(project=project.id, issue=receipt['issue'],
                                                     error=REASONS[receipt['reason']], reason=receipt['reason'], retry=receipt['retry']))
                        self.log('command_verification_failed', project, task=receipt['issue'],
                                 reason=receipt['reason'], action=receipt['action'])
                for number in PullRequestMonitor(self.store, self.adapter).poll(project):
                    result['errors'].append(dict(project=project.id, issue=number, error='PR sync failed; will retry'))
                    self.log('pr_sync_failed', project, task=number)
                for number in CancellationDelivery(self.config, self.store, self.adapter).poll(project):
                    result['errors'].append(dict(project=project.id, issue=number, error='Cancellation delivery failed; will retry'))
                    self.log('cancellation_delivery_failed', project, task=number)
                # Always reconcile, even when a command was rejected. DB is authoritative.
                for row in self.store.queue(project.repository):
                    try:
                        self.adapter.reconcile(project, row, store=self.store)
                    except GitHubError:
                        result['errors'].append(dict(project=project.id, error='GitHub label sync failed; will retry'))
                        self.log('label_sync_failed', project)
            except (GitHubError, sqlite3.Error):
                result['errors'].append(dict(project=project.id, error='GitHub polling or storage failure'))
                self.log('poll_failed', project)
        from dispatcher import preparation_error
        preparation_error.flush(self.store, self.adapter, self.config.projects)
        from dispatcher.detachment import StopController
        StopController(self.config, self.store, self.adapter).diagnostics()
        from dispatcher.cleanup import CleanupService
        CleanupService(self.config, self.store).reconcile()
        return result

    def run(self, stop):
        while not stop.is_set():
            self.poll()
            stop.wait(self.config.poll_interval_seconds)
