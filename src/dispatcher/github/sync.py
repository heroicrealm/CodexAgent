"""Project-isolated, read-only polling orchestration."""
import sqlite3

from dispatcher.github import Adapter, GitHubError


class SyncService:
    def __init__(self, config, store, adapter=None):
        self.config, self.store = config, store
        self.adapter = adapter or Adapter(config.github_executable)

    def poll(self):
        result = dict(discovered=0, updated=0, unchanged=0, revoked=0, skipped_untrusted=0, errors=[])
        for project in self.config.projects:
            if not project.enabled:
                continue
            try:
                counts = self.store.apply(project, self.adapter.fetch(project))
                for key, count in counts.items():
                    result[key] += count
            except (GitHubError, sqlite3.Error):
                # Neither stderr, response bodies nor exception strings are safe to log.
                result['errors'].append(dict(project=project.id, error='GitHub polling or storage failure'))
        return result

    def run(self, stop):
        """threading.Event control: interruptible between requests and during intervals."""
        while not stop.is_set():
            self.poll()
            stop.wait(self.config.poll_interval_seconds)
