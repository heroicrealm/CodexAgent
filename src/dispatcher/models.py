from dataclasses import dataclass, field
from enum import StrEnum
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class State(StrEnum):
    DISCOVERED = "discovered"
    AWAITING_CHILDREN = "awaiting-children"
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    MANUAL_RUNNING = "manual-running"
    REVIEW = "review"
    REJECTED = "rejected"
    NEED_FIX = "need-fix"
    ERROR = "error"
    DONE = "done"
    CANCELLED = "cancelled"


ACTIVE = frozenset({State.PENDING, State.AWAITING_CHILDREN, State.PREPARING,
                    State.RUNNING, State.MANUAL_RUNNING, State.REVIEW})


class GitHubState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PRStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class ExecutionStatus(StrEnum):
    RESERVED = "reserved"
    PREPARING = "preparing"
    EXECUTING = "executing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class IssueObservation:
    issue_id: int
    repository: str
    issue_number: int
    title: str
    github_state: GitHubState = GitHubState.OPEN
    labels: frozenset[str] = frozenset()
    body: str = ""


@dataclass(frozen=True)
class Relation:
    parent_issue_id: int
    child_issue_id: int


@dataclass(frozen=True)
class PRObservation:
    pr_id: int
    issue_id: int
    repository: str
    pr_number: int
    head_branch: str
    base_branch: str
    status: PRStatus
    merged_at: str | None = None
    head_commit: str | None = None


@dataclass(frozen=True)
class ApprovedSnapshot:
    title: str
    body: str
    parameters_yaml: str | None
    approved_hash: str
    input_comment_id: int | None = None
    approved_input: str | None = None


@dataclass(frozen=True)
class PollResult:
    poll_id: str
    issues: tuple[IssueObservation, ...]
    relations: tuple[Relation, ...]
    prs: tuple[PRObservation, ...]
    complete: bool


@dataclass(frozen=True)
class SyncResult:
    registered: int
    transitioned: int


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Execution:
    execution_id: int
    issue_id: int
    status: ExecutionStatus
    codex_started_at: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: int
    exit_code: int | None
    error_code: str | None = None


@dataclass(frozen=True)
class PublishResult:
    published: bool
    pr_id: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class InputComment:
    id: int
    body: str


@dataclass
class ObservationBatch:
    """Only constructed/applied after all required GitHub pages have succeeded."""
    poll_id: str
    issues: tuple[IssueObservation, ...]
    parents: dict[int, IssueObservation | None] = field(default_factory=dict)
    children: dict[int, tuple[IssueObservation, ...]] = field(default_factory=dict)
    approvals: dict[int, tuple[IssueObservation, InputComment | None]] = field(default_factory=dict)
    corrections: dict[int, IssueObservation] = field(default_factory=dict)
    prs: tuple[PRObservation, ...] = ()
    approval_errors: dict[int, str] = field(default_factory=dict)
