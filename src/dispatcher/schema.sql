CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE issues (
    issue_id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL CHECK(issue_number > 0),
    project_id TEXT NOT NULL,
    execution_group_id INTEGER REFERENCES issues(issue_id),
    correction_of_issue_id INTEGER REFERENCES issues(issue_id),

    is_virtual INTEGER NOT NULL DEFAULT 0 CHECK(is_virtual IN (0, 1)),
    state TEXT,
    github_state TEXT CHECK(github_state IN ('open', 'closed')),
    is_ready INTEGER NOT NULL DEFAULT 0 CHECK(is_ready IN (0, 1)),
    is_approved INTEGER NOT NULL DEFAULT 0 CHECK(is_approved IN (0, 1)),

    title TEXT,
    approved_title TEXT,
    approved_body TEXT,
    parameters_yaml TEXT,
    approved_hash TEXT,
    approved_at TEXT,

    base_branch TEXT,
    work_branch TEXT,
    error_report_sent INTEGER NOT NULL DEFAULT 0
        CHECK(error_report_sent IN (0, 1)),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,

    UNIQUE(repository, issue_number),
    CHECK (correction_of_issue_id IS NULL OR correction_of_issue_id <> issue_id),
    CHECK (is_virtual = 1 OR execution_group_id IS NOT NULL),
    CHECK (
        (is_virtual = 1 AND state IS NULL AND is_ready = 0)
        OR (is_virtual = 0 AND state IS NOT NULL AND title IS NOT NULL)
    )
);

CREATE TABLE issue_relations (
    parent_issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    child_issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    PRIMARY KEY(parent_issue_id, child_issue_id),
    CHECK(parent_issue_id <> child_issue_id)
);

CREATE TABLE executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL UNIQUE REFERENCES issues(issue_id),
    status TEXT NOT NULL CHECK(status IN
        ('reserved','preparing','executing','publishing','completed','failed','interrupted')),
    branch_name TEXT,
    worktree_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    codex_started_at TEXT,
    codex_finished_at TEXT,
    exit_code INTEGER,
    error_code TEXT,
    error_message TEXT,
    initial_commit TEXT,
    result_commit TEXT,
    push_completed INTEGER NOT NULL DEFAULT 0 CHECK(push_completed IN (0,1)),
    pr_published INTEGER NOT NULL DEFAULT 0 CHECK(pr_published IN (0,1)),
    comment_published INTEGER NOT NULL DEFAULT 0 CHECK(comment_published IN (0,1))
);

CREATE TABLE pull_requests (
    pr_id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL CHECK(pr_number > 0),
    head_branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed','merged')),
    merged_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(repository, pr_number)
);

CREATE TABLE state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    old_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(issue_id),
    execution_id INTEGER REFERENCES executions(execution_id),
    error_code TEXT NOT NULL,
    error_stage TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE debug_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('api', 'verbose')),
    component TEXT NOT NULL,
    operation TEXT NOT NULL,
    repository TEXT,
    issue_id INTEGER,
    poll_id TEXT,
    method TEXT,
    endpoint TEXT,
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
    result TEXT NOT NULL,
    http_status INTEGER,
    error_message TEXT
);

CREATE INDEX idx_issues_state ON issues(state);
CREATE INDEX idx_issues_project_state ON issues(project_id, state);
CREATE INDEX idx_relations_child ON issue_relations(child_issue_id);
CREATE INDEX idx_pr_status ON pull_requests(status);
CREATE INDEX idx_pr_issue ON pull_requests(issue_id);
CREATE INDEX idx_history_issue ON state_history(issue_id, id);
CREATE INDEX idx_error_issue ON error_log(issue_id, id);
CREATE INDEX idx_debug_created ON debug_operations(created_at);
CREATE INDEX idx_debug_poll ON debug_operations(poll_id);
CREATE INDEX idx_debug_operation ON debug_operations(operation);

CREATE INDEX idx_issues_correction_of ON issues(correction_of_issue_id);
