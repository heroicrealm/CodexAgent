"""Strict configuration loading; relative paths are relative to the YAML file."""
from dataclasses import dataclass
from pathlib import Path
import re
import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Project:
    id: str
    enabled: bool
    repository: str
    base_branch: str
    task_label: str


@dataclass(frozen=True)
class Config:
    source: Path
    workspace: Path
    logs: Path
    poll_interval_seconds: int
    max_parallel_tasks: int
    executable: str
    projects: tuple[Project, ...]


def load(path: str | Path) -> Config:
    source = Path(path).expanduser().resolve()
    try:
        data = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read YAML configuration: {source}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a mapping")
    def section(name):
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a mapping")
        return value
    dispatcher, paths = section('dispatcher'), section('paths')
    def positive(name, default):
        value = dispatcher.get(name, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"dispatcher.{name} must be a positive integer")
        return value
    interval = positive('poll_interval_seconds', 30)
    parallel = positive('max_parallel_tasks', 1)
    if dispatcher.get('shutdown_policy', 'wait') != 'wait':
        raise ConfigError('dispatcher.shutdown_policy must be wait')
    if section('github').get('auth_method', 'gh') != 'gh':
        raise ConfigError('github.auth_method must be gh')
    def string(value, label):
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ConfigError(f'{label} must be a nonempty string')
        return value
    def directory(name):
        value = Path(string(paths.get(name, f'~/codex-dispatcher/{name}'), f'paths.{name}')).expanduser()
        value = (source.parent / value).resolve() if not value.is_absolute() else value.resolve()
        if str(value) == '/mnt' or str(value).startswith('/mnt/'):
            raise ConfigError(f'paths.{name} must be in the Linux filesystem, outside /mnt')
        return value
    executable = string(section('codex').get('executable', 'codex'), 'codex.executable')
    projects = data.get('projects')
    if not isinstance(projects, list) or not projects:
        raise ConfigError('projects must be a nonempty array')
    result, ids = [], set()
    for item in projects:
        if not isinstance(item, dict):
            raise ConfigError('Each project must be a mapping')
        for key in ('id', 'repository', 'base_branch', 'task_label'):
            string(item.get(key), f'project.{key}')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', item['id']):
            raise ConfigError('project.id must be a safe directory identifier')
        if item['id'] in ids:
            raise ConfigError('project.id must be unique')
        ids.add(item['id'])
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', item['repository']) or item['repository'].split('/')[1] in ('.', '..'):
            raise ConfigError('project.repository must be owner/name')
        if type(item.get('enabled')) is not bool:
            raise ConfigError('project.enabled must be boolean')
        result.append(Project(**{k: item[k] for k in Project.__dataclass_fields__}))
    return Config(source, directory('workspace'), directory('logs'), interval, parallel, executable, tuple(result))
