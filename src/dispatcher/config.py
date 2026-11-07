"""V2 configuration, independent from V1 paths and configuration."""
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
import re
import yaml

LABEL_KEYS = frozenset({"codex-ready", "approve", "discovered", "awaiting-children",
    "pending", "running", "manual-running", "review", "rejected", "done",
    "cancelled", "error", "need-fix"})
DEFAULT_LABELS = {key: f"dispatcher:{key}" for key in LABEL_KEYS}


def is_v1_path(path: Path) -> bool:
    """Conservative guard that also works when installed outside the repository."""
    return any(part.casefold() == 'v1' for part in path.resolve().parts)


def validate_labels(labels: Mapping[str, str], *, partial: bool = False) -> dict[str, str]:
    if not isinstance(labels, Mapping) or set(labels) - LABEL_KEYS:
        raise ValueError("Unsupported label keys")
    if not partial and set(labels) != LABEL_KEYS:
        raise ValueError("Incomplete labels map")
    if any(not isinstance(v, str) or not v.strip() for v in labels.values()):
        raise ValueError("Label names must be nonempty strings")
    if len(set(labels.values())) != len(labels):
        raise ValueError("Ambiguous label names: distinct keys must have distinct labels")
    return dict(labels)


@dataclass(frozen=True)
class Project:
    id: str
    repository: str
    labels: Mapping[str, str] = field(default_factory=dict)
    base_branch: str = "main"
    checkout: Path | None = None


@dataclass(frozen=True)
class Config:
    workspace: Path
    database: Path
    projects: tuple[Project, ...]
    labels: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    debug_mode: str = "off"
    retention_days: int = 7
    poll_interval: float = 30
    codex_command: tuple[str, ...] = ("codex", "exec", "--sandbox", "workspace-write", "-")

    def __post_init__(self) -> None:
        validate_labels(self.labels)
        if (type(self.poll_interval) not in (int, float) or self.poll_interval <= 0
                or not isinstance(self.codex_command, (tuple, list)) or not self.codex_command
                or any(not isinstance(v, str) or not v for v in self.codex_command)):
            raise ValueError("Positive poll_interval and nonempty codex_command required")
        if self.debug_mode not in {"off", "api", "verbose"}:
            raise ValueError("debug.mode must be off/api/verbose (quote 'off' in YAML)")
        if type(self.retention_days) is not int or self.retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if len({p.repository for p in self.projects}) != len(self.projects):
            raise ValueError("Repository must belong to one project")
        if len({p.id for p in self.projects}) != len(self.projects):
            raise ValueError("Duplicate project ID")
        for path in (self.workspace, self.database):
            if is_v1_path(path):
                raise ValueError("V2 paths must not point into V1")
        for project in self.projects:
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project.id)
                    or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', project.repository)):
                raise ValueError("Project requires id and owner/repository")
            if project.checkout and is_v1_path(project.checkout):
                raise ValueError("Project checkout must not point into V1")
            self.effective_labels(project)

    def effective_labels(self, project: Project) -> dict[str, str]:
        return validate_labels({**self.labels, **validate_labels(project.labels, partial=True)})

    def status_label(self, project: Project, state: str) -> str | None:
        if state == "preparing":
            return None
        return self.effective_labels(project)[state]


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text())
    dispatcher = data.get("dispatcher", {})
    debug = dispatcher.get("debug", {})
    # PyYAML's YAML 1.1 parser treats unquoted off as False.
    mode = debug.get("mode", "off")
    if mode is False:
        mode = "off"
    base = path.resolve().parent
    command = dispatcher.get('codex_command', ['codex', 'exec', '--sandbox', 'workspace-write', '-'])
    if not isinstance(command, list):
        raise ValueError('codex_command must be an argv list')
    return Config(workspace=base / dispatcher.get("workspace", "../workspace-v2"),
        database=base / dispatcher.get("database", "../runtime-v2/dispatcher.sqlite3"),
        projects=tuple(Project(**{**p, "checkout": base / p["checkout"] if p.get("checkout") else None})
                       for p in data.get("projects", [])),
        labels={**DEFAULT_LABELS, **validate_labels(dispatcher.get("labels", {}), partial=True)},
        debug_mode=mode, retention_days=debug.get("retention_days", 7),
        poll_interval=dispatcher.get("poll_interval", 30),
        codex_command=tuple(command))
