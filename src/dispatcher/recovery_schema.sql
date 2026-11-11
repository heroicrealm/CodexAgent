CREATE TABLE recovery_sessions (
    session_id TEXT NOT NULL PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status='completed'),
    checked_issues INTEGER NOT NULL CHECK(checked_issues>=0),
    quarantined_issues INTEGER NOT NULL CHECK(quarantined_issues>=0),
    summary TEXT NOT NULL CHECK(json_valid(summary) AND json_type(summary)='array')
);
CREATE TABLE recovery_operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    action TEXT NOT NULL CHECK(action IN ('accept','retry','cancel')),
    phase TEXT NOT NULL CHECK(phase IN ('intent','archived','approval-reset','cleaned','reset','comment-intent','commented','completed')),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    github_comment_id INTEGER CHECK(github_comment_id IS NULL OR github_comment_id>0),
    details TEXT NOT NULL CHECK(json_valid(details) AND json_type(details)='object')
);
CREATE UNIQUE INDEX idx_recovery_active ON recovery_operations(issue_id) WHERE phase<>'completed';
CREATE TABLE recovery_quarantine (
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    reason_code TEXT NOT NULL CHECK(reason_code IN ('execution-uncertain','branch-conflict','pr-conflict','local-worktree','approval-uncertain','group-dependency','state-conflict')),
    details TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(issue_id,reason_code)
);
CREATE TABLE recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER REFERENCES issues(issue_id),
    session_id TEXT REFERENCES recovery_sessions(session_id),
    operation_id TEXT REFERENCES recovery_operations(operation_id),
    action TEXT NOT NULL,
    reason_code TEXT,
    details TEXT NOT NULL CHECK(json_valid(details) AND json_type(details)='object'),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_recovery_events_issue ON recovery_events(issue_id,id);
CREATE TABLE recovery_execution_archive (
    archive_id TEXT NOT NULL PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES recovery_operations(operation_id),
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    original_execution_id INTEGER,
    execution_snapshot TEXT NOT NULL CHECK(json_valid(execution_snapshot) AND json_type(execution_snapshot)='object'),
    related_diagnostics TEXT NOT NULL CHECK(json_valid(related_diagnostics) AND json_type(related_diagnostics)='array'),
    reason TEXT NOT NULL,
    archived_at TEXT NOT NULL
);
CREATE INDEX idx_recovery_archive_issue ON recovery_execution_archive(issue_id);
CREATE TABLE recovery_approval_reset (
    issue_id INTEGER PRIMARY KEY REFERENCES issues(issue_id),
    operation_id TEXT NOT NULL REFERENCES recovery_operations(operation_id),
    requested_at TEXT NOT NULL,
    old_approved_hash TEXT,
    approval_absent_observed INTEGER NOT NULL CHECK(approval_absent_observed IN (0,1)),
    completed_at TEXT
);

CREATE TRIGGER recovery_events_no_update BEFORE UPDATE ON recovery_events
BEGIN
    SELECT RAISE(ABORT,'recovery events are append-only');
END;
CREATE TRIGGER recovery_events_no_delete BEFORE DELETE ON recovery_events
BEGIN
    SELECT RAISE(ABORT,'recovery events are append-only');
END;
