"""SQLite schema owned by M1.

Migrations are deliberately idempotent: the first delivery has one schema
version and a database can be opened repeatedly by short-lived CLI processes.
"""

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    causation_id TEXT,
    correlation_id TEXT,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    producer TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_offsets (
    consumer_name TEXT NOT NULL,
    event_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (consumer_name, event_id)
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_bindings (
    binding_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    platform_identity TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, platform_identity)
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    last_message_key TEXT,
    last_inbound_at TEXT,
    stable_after TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    platform_message_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    text TEXT NOT NULL,
    content_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, platform_message_key)
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation_time
    ON messages(conversation_id, observed_at, platform_message_key);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source_message_keys_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_drafts_conversation_active
    ON drafts(conversation_id, status);
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pacing_plans (
    pacing_plan_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_message_keys_json TEXT NOT NULL,
    quiet_until TEXT NOT NULL,
    earliest_send_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    pacing_rule_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plans_due ON pacing_plans(status, earliest_send_at);
CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    expected_last_message_key TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    authorization_type TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    draft_id TEXT NOT NULL,
    authorization_id TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    commit_intent INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    claimed_at TEXT,
    delivered_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_available ON outbox(status, available_at);
CREATE TABLE IF NOT EXISTS adapter_capabilities (
    capability_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rulepacks (
    rulepack_id TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(rulepack_id, version)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
