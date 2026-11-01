"""Strict configuration loading; relative paths are relative to the YAML file."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import yaml


class ConfigError(ValueError):
    pass


COLORS = dict(zip(
    'ready discovered approve reject pending running review done failed rejected revoked cancelled interrupted'.split(),
    '0E8A16 D4C5F9 0E8A16 B60205 FBCA04 1D76DB 5319E7 0E8A16 D93F0B B60205 C2E0C6 E99695 F9D0C4'.split()))

COLORS['review_on_error'] = 'D93F0B'
COLORS['emergency_stop'] = 'B60205'
COLORS['awaiting_children'] = 'FBCA04'
COLORS['detached'] = 'C2E0C6'


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str = ''


@dataclass(frozen=True)
class Project:
    id: str
    enabled: bool
    repository: str
    base_branch: str
    task_label: str
    allowed_authors: tuple[str, ...] = ()
    labels: dict[str, Label] = field(default_factory=dict)
    init_status: str = ''
    init_message: str = ''
    init_fingerprint: str = ''

    def label(self, key):
        return self.labels.get(key, Label(self.task_label if key == 'ready' else f'dispatcher:{key.replace("_", "-")}', COLORS[key],
            'Terminal error; manual resolution required' if key == 'review_on_error' else 'Emergency stop; remove manually to reconnect' if key == 'emergency_stop' else ''))


@dataclass(frozen=True)
class Config:
    source: Path
    workspace: Path
    logs: Path
    poll_interval_seconds: int
    max_parallel_tasks: int
    executable: str
    projects: tuple[Project, ...]
    force_grace_timeout_seconds: int = 5
    github_executable: str = "gh"
    timeout_seconds: int = 7200
    execution_mode: str = 'full-auto'
    quota_enabled: bool = True
    quota_min_remaining_percent: int = 10
    quota_check_interval_seconds: int = 300
    quota_retry_after_reset_seconds: int = 60
    attachment_max_files: int = 20
    attachment_max_bytes: int = 20971520
    attachment_timeout_seconds: int = 30
    emergency_poll_interval_seconds: int = 5


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
    grace = positive('force_grace_timeout_seconds', 5)
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
    codex = section('codex')
    quota = codex.get('quota', {})
    attachments = section('attachments')
    if not isinstance(quota, dict):
        raise ConfigError('codex.quota must be a mapping')
    options = {'emergency_poll_interval_seconds': positive('emergency_poll_interval_seconds', 5)}
    for key, values, name, default in (
        ('timeout_seconds', codex, 'timeout_seconds', 7200),
        ('quota_check_interval_seconds', quota, 'check_interval_seconds', 300),
        ('quota_retry_after_reset_seconds', quota, 'retry_after_reset_seconds', 60),
        ('attachment_max_files', attachments, 'max_files', 20),
        ('attachment_max_bytes', attachments, 'max_bytes', 20971520),
        ('attachment_timeout_seconds', attachments, 'timeout_seconds', 30)):
        value = values.get(name, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f'{key} must be a positive integer')
        options[key] = value
    options['execution_mode'] = codex.get('execution_mode', 'full-auto')
    if options['execution_mode'] != 'full-auto':
        raise ConfigError('codex.execution_mode must be full-auto')
    options['quota_enabled'] = quota.get('enabled', True)
    options['quota_min_remaining_percent'] = quota.get('min_remaining_percent', 10)
    if type(options['quota_enabled']) is not bool:
        raise ConfigError('codex.quota.enabled must be boolean')
    if type(options['quota_min_remaining_percent']) is not int or not 0 <= options['quota_min_remaining_percent'] <= 100:
        raise ConfigError('codex.quota.min_remaining_percent must be 0..100')
    projects = data.get('projects')
    if not isinstance(projects, list) or not projects:
        raise ConfigError('projects must be a nonempty array')
    result, ids, repositories = [], set(), set()
    for item in projects:
        if not isinstance(item, dict):
            raise ConfigError('Each project must be a mapping')
        item = dict(item)
        item.setdefault('base_branch', item.get('default_branch', 'main'))
        labels = item.get('labels', {})
        security = item.get('security', {})
        if not isinstance(labels, dict) or not isinstance(security, dict):
            raise ConfigError('project labels/security must be mappings')
        parsed = {}
        for key, color in COLORS.items():
            value = labels.get(key, item.get('task_label', 'codex-ready') if key == 'ready' else (f'dispatcher:{key.replace("_", "-")}' if key in ('emergency_stop', 'awaiting_children') else f'dispatcher:{key}'))
            if isinstance(value, str):
                value = dict(name=value)
            if not isinstance(value, dict):
                raise ConfigError('label must be a string or mapping')
            name = string(value.get('name'), f'labels.{key}.name')
            color = value.get('color', color)
            description = value.get('description', 'Emergency stop; remove manually to reconnect' if key == 'emergency_stop' else '')
            if not isinstance(color, str) or not re.fullmatch('[0-9a-fA-F]{6}', color):
                raise ConfigError('label color must be six RGB hex digits')
            if not isinstance(description, str) or '\x00' in description:
                raise ConfigError('label description must be a string')
            parsed[key] = Label(name, color, description)
        if len({label.name.casefold() for label in parsed.values()}) != len(parsed):
            raise ConfigError('label names must be unique ignoring case')
        item['labels'] = parsed
        item['task_label'] = parsed['ready'].name
        for key in ('init_status', 'init_message', 'init_fingerprint'):
            value = item.setdefault(key, '')
            if not isinstance(value, str):
                raise ConfigError(f'{key} must be a string')
        if item['init_status'] not in ('', 'success', 'fail'):
            raise ConfigError('invalid init_status')
        authors = security.get('allowed_authors', [])
        if not isinstance(authors, list) or any(not isinstance(a, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', a) for a in authors):
            raise ConfigError('security.allowed_authors must be a list of GitHub logins')
        item['allowed_authors'] = tuple(authors)
        for key in ('id', 'repository', 'base_branch', 'task_label'):
            string(item.get(key), f'project.{key}')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', item['id']):
            raise ConfigError('project.id must be a safe directory identifier')
        if item['id'] in ids:
            raise ConfigError('project.id must be unique')
        ids.add(item['id'])
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', item['repository']) or item['repository'].split('/')[1] in ('.', '..'):
            raise ConfigError('project.repository must be owner/name')
        repository = item['repository'].casefold()
        if repository in repositories:
            raise ConfigError('project.repository must be unique')
        repositories.add(repository)
        if type(item.get('enabled')) is not bool:
            raise ConfigError('project.enabled must be boolean')
        result.append(Project(**{k: item[k] for k in Project.__dataclass_fields__}))
    return Config(source, directory('workspace'), directory('logs'), interval, parallel, executable, tuple(result), grace, string(section('executables').get('github', 'gh'), 'executables.github'), **options)
