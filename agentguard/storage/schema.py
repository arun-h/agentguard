"""
SQLite schema for AgentGuard.

Reference: EDS Section 5.3 (Complete Schema).
"""

SCHEMA_VERSION = 1

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    framework    TEXT NOT NULL,
    agent_id     TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    metadata     TEXT
);
"""

CREATE_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
"""

CREATE_APPROVALS_TABLE = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id      TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    tool_name        TEXT NOT NULL,
    arguments_hash   TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING',
    created_at       TEXT NOT NULL,
    updated_at       TEXT,
    ttl_seconds      INTEGER NOT NULL DEFAULT 3600,
    approver         TEXT,
    notes            TEXT
);
"""

CREATE_APPROVALS_RUN_INDEX = """
CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id);
"""

CREATE_APPROVALS_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
"""

CREATE_APPROVALS_COMPOSITE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_composite
    ON approvals(run_id, tool_name, arguments_hash, policy_version);
"""

CREATE_AUDIT_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL,
    approval_id        TEXT REFERENCES approvals(approval_id),
    timestamp          TEXT NOT NULL,
    tool_name          TEXT NOT NULL,
    arguments_hash     TEXT NOT NULL,
    policy_version     TEXT NOT NULL,
    decision           TEXT NOT NULL,
    reason             TEXT NOT NULL,
    rule_matched       TEXT,
    budget_calls_used  INTEGER,
    budget_cost_used   REAL,
    loop_count         INTEGER,
    agent_id           TEXT,
    framework          TEXT,
    metadata           TEXT
);
"""

CREATE_AUDIT_RUN_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_run_id ON audit_log(run_id);
"""

CREATE_AUDIT_TIMESTAMP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
"""

CREATE_AUDIT_TOOL_NAME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_tool_name ON audit_log(tool_name);
"""

CREATE_AUDIT_DECISION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_decision ON audit_log(decision);
"""

# Applied in order on a fresh database.
ALL_DDL_STATEMENTS = [
    CREATE_MIGRATIONS_TABLE,
    CREATE_RUNS_TABLE,
    CREATE_RUNS_INDEX,
    CREATE_APPROVALS_TABLE,
    CREATE_APPROVALS_RUN_INDEX,
    CREATE_APPROVALS_STATUS_INDEX,
    CREATE_APPROVALS_COMPOSITE_INDEX,
    CREATE_AUDIT_LOG_TABLE,
    CREATE_AUDIT_RUN_INDEX,
    CREATE_AUDIT_TIMESTAMP_INDEX,
    CREATE_AUDIT_TOOL_NAME_INDEX,
    CREATE_AUDIT_DECISION_INDEX,
]