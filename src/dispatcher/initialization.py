"""Project readiness and atomic, concurrency-checked initialization metadata."""
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

from dispatcher.config import COLORS, ConfigError, load
from dispatcher.github import Adapter, GitHubError


def fingerprint(project):
    value = dict(repository=project.repository.casefold(),
                 labels={key: project.label(key).name for key in COLORS})
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ready(project):
    return project.enabled and project.init_status == 'success' and project.init_fingerprint == fingerprint(project)


def save_result(config, project, success):
    """Serialize cooperating writers and reject concurrent configuration replacement.

    Reload under a sidecar lock so results for separate projects do not overwrite
    each other. A changed initialization contract never receives a success stamp.
    """
    source = config.source
    with open(str(source) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = source.read_bytes()
        current = load(source)
        target = next((p for p in current.projects if p.id == project.id), None)
        if target is None or fingerprint(target) != fingerprint(project):
            raise ConfigError('Configuration changed during init; rerun init')
        data = yaml.safe_load(original)
        for item in data['projects']:
            if item['id'] == project.id:
                item.update(init_status='success' if success else 'fail',
                            init_message='' if success else 'GitHub initialization failed; check access and retry init',
                            init_fingerprint=fingerprint(project) if success else '')
        descriptor, temporary = tempfile.mkstemp(dir=source.parent, prefix='.' + source.name)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), source.stat().st_mode & 0o777)
                yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            if source.read_bytes() != original:
                raise ConfigError('Concurrent configuration edit; rerun init')
            os.replace(temporary, source)
            directory = os.open(source.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)


def initialize(config, update=False, adapter=None):
    adapter = adapter or Adapter(config.github_executable)
    results = []
    for project in config.projects:
        if not project.enabled:
            continue
        success = False
        try:
            adapter.request(f'repos/{project.repository}')
            existing = {v['name'].casefold(): v for v in adapter.labels(project)}
            for key in COLORS:
                label = project.label(key)
                previous = existing.get(label.name.casefold())
                if previous is None:
                    adapter.write(f'repos/{project.repository}/labels', 'POST',
                                  dict(name=label.name, color=label.color, description=label.description))
                elif update:
                    from urllib.parse import quote
                    adapter.write(f'repos/{project.repository}/labels/{quote(previous["name"], safe="")}',
                                  'PATCH', dict(color=label.color, description=label.description))
            success = True
        except (GitHubError, KeyError, TypeError):
            pass
        try:
            save_result(config, project, success)
        except (ConfigError, OSError):
            # A concurrent contract edit or disk failure must not stop other projects.
            success = False
        results.append((project.id, success))
    return results
