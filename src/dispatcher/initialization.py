"""Project readiness and atomic, concurrency-checked initialization metadata."""
import fcntl
import errno
import hashlib
import json
import os
import tempfile
from pathlib import Path
from dataclasses import asdict

import yaml

from dispatcher.config import COLORS, ConfigError, load
from dispatcher.github import Adapter, GitHubError
from dispatcher.logging import event


def fingerprint(project):
    value = dict(repository=project.repository.casefold(),
                 labels={key: project.label(key).name for key in COLORS},
                 emergency_stop=asdict(project.label('emergency_stop')))
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ready(project):
    return project.enabled and project.init_status == 'success' and project.init_fingerprint == fingerprint(project)


def save_result(config, project, success, message=None):
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
                            init_message='' if success else (message or 'GitHub initialization failed; check access and retry init'),
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


def initialize(config, update=False, adapter=None, reports=None):
    adapter = adapter or Adapter(config.github_executable)
    results = []
    for project in config.projects:
        if not project.enabled:
            continue
        success, message, reason = False, '', ''
        phase = 'Repository access check'
        operation = 'repository_access'
        try:
            adapter.request(f'repos/{project.repository}')
            phase, operation = 'Label listing', 'label_list'
            existing = {v['name'].casefold(): v for v in adapter.labels(project)}
            for key in COLORS:
                label = project.label(key)
                previous = existing.get(label.name.casefold())
                if previous is None:
                    phase, operation = f'Label creation ({key})', 'label_create'
                    adapter.write(f'repos/{project.repository}/labels', 'POST',
                                  dict(name=label.name, color=label.color, description=label.description))
                elif update:
                    phase, operation = f'Label update ({key})', 'label_update'
                    from urllib.parse import quote
                    adapter.write(f'repos/{project.repository}/labels/{quote(previous["name"], safe="")}',
                                  'PATCH', dict(color=label.color, description=label.description))
            success = True
        except GitHubError as exc:
            message = f'{phase} failed: {exc.safe_reason()}'
            reason = f'{operation}_http_{exc.status}' if exc.status else f'{operation}_request_failed'
        except (KeyError, TypeError, AttributeError):
            message, reason = f'{phase} failed: invalid GitHub response', f'{operation}_invalid_response'
        saved = False
        try:
            save_result(config, project, success, message)
            saved = True
        except ConfigError:
            success = False
            message = 'Configuration save not confirmed: configuration changed or is invalid; rerun init'
            reason = 'configuration_changed_or_invalid'
        except OSError as exc:
            success = False
            cause = {errno.EACCES: 'permission denied', errno.EPERM: 'permission denied',
                     errno.ENOSPC: 'no space left', errno.EROFS: 'read-only filesystem'}.get(exc.errno, 'filesystem error')
            message = f'Configuration save not confirmed: {cause}; fix storage/permissions and rerun init'
            reason = 'configuration_save_failed'
        if reports is not None:
            reports.append(dict(project=project.id, success=success, saved=saved, message=message, reason=reason))
        if not success:
            try:
                config.logs.mkdir(parents=True, exist_ok=True)
                event(config.logs / 'dispatcher.jsonl', 'init_failed' if saved else 'init_save_failed',
                      level='WARN', project=project.id, reason=reason)
            except OSError:
                # CLI still reports the failure even if the log cannot be written.
                pass
        results.append((project.id, success))
    return results
