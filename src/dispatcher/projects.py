"""Serialized local configuration edits and fail-closed repository admission."""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml
from .config import load_config, Project
from .monitoring import project, snapshot


@contextmanager
def edit(path):
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink configuration paths forbidden')
    lock = path.with_name(path.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        original = path.read_bytes()
        config = load_config(path)
        data = yaml.safe_load(original)
        yield config, data
        # Validate without exposing an invalid configuration to another process.
        fd, temp = tempfile.mkstemp(prefix='.config-', suffix='.yaml', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as target:
                yaml.safe_dump(data, target, sort_keys=False, allow_unicode=True)
                target.flush()
                os.fsync(target.fileno())
            load_config(Path(temp))
            if path.read_bytes() != original:
                raise ValueError('Configuration changed outside CLI; retry')
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temp).unlink(missing_ok=True)


def validate_remote(repository, transport=None, run=None):
    from .github import GhTransport
    transport = transport or GhTransport()
    run = run or subprocess.run
    try:
        info = transport('GET', f'/repos/{repository}', None)
        if not isinstance(info, dict) or info.get('full_name', '').lower() != repository.lower():
            raise ValueError('Repository existence/access could not be verified')
        if info.get('archived') or info.get('disabled') or info.get('has_issues') is False:
            raise ValueError('Repository is archived/disabled or Issues are disabled')
        permissions = info.get('permissions', {})
        if permissions.get('pull') is False or permissions.get('push') is False:
            raise ValueError('Insufficient repository read/write permissions')
        result = run(['git', 'ls-remote', f'git@github.com:{repository}.git'], capture_output=True,
                     text=True, timeout=60, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                               'GIT_SSH_COMMAND': 'ssh -oBatchMode=yes'})
        if result.returncode:
            raise ValueError('Git remote access failed (SSH authentication is independent of gh API)')
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Repository access verification failed') from exc
    return 'WARN: API repository permissions checked where available; token-specific Issues/PR/labels write permissions cannot be proven without writes'


def mutate(path, action, name, *, repository=None, base_branch='main', checkout=None):
    warning = None
    with edit(path) as (config, data):
        entries = data.setdefault('projects', [])
        if action == 'add':
            candidate = Project(name, repository, base_branch=base_branch, checkout=Path(checkout) if checkout else None)
            # Reuse complete configuration validation before any network calls.
            replace(config, projects=(*config.projects, candidate))
            from .input import branch_name
            branch_name(base_branch)
            with snapshot(config) as (db, _):
                if db.execute('SELECT 1 FROM issues WHERE lower(repository)=? UNION SELECT 1 FROM debug_operations WHERE lower(repository)=? LIMIT 1', (candidate.repository, candidate.repository)).fetchone():
                    raise ValueError('Repository has retained local history; purge is required before add')
            warning = validate_remote(candidate.repository)
            entry = dict(id=name, repository=candidate.repository, base_branch=base_branch)
            if checkout:
                entry['checkout'] = checkout
            entries.append(entry)
        else:
            project(config, name)
            entry = next(e for e in entries if e['id'] == name)
            if action == 'remove':
                entries.remove(entry)
            else:
                entry['enabled'] = action == 'enable'
    return warning


def inspect_projects(config, name=None):
    selected = (project(config, name),) if name else config.projects
    return [dict(id=p.id, repository=p.repository, enabled=p.enabled, base_branch=p.base_branch,
                 checkout=str(p.checkout or config.workspace / 'repos' / p.id), labels=config.effective_labels(p)) for p in selected]
