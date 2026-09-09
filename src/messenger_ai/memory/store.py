from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS memory_contacts (
  contact_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_bindings (
  binding_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES memory_contacts(contact_id),
  platform TEXT NOT NULL, account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  evidence_hash TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(platform, account_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS memory_consumed_events (
  consumer_name TEXT NOT NULL, event_id TEXT NOT NULL, consumed_at TEXT NOT NULL,
  PRIMARY KEY(consumer_name, event_id)
);
CREATE TABLE IF NOT EXISTS memory_messages (
  message_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  event_id TEXT NOT NULL, message_key TEXT NOT NULL, observed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'inbound', payload_json TEXT NOT NULL,
  UNIQUE(contact_id, conversation_id, message_key), UNIQUE(event_id)
);
CREATE INDEX IF NOT EXISTS ix_memory_messages_context
  ON memory_messages(contact_id, conversation_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS memory_summaries (
  summary_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  created_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_summaries_context
  ON memory_summaries(contact_id, conversation_id, created_at DESC);
CREATE TABLE IF NOT EXISTS memory_facts (
  fact_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  status TEXT NOT NULL, hard_rule INTEGER NOT NULL, created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_facts_context
  ON memory_facts(contact_id, conversation_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS memory_preferences (
  preference_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  hard_rule INTEGER NOT NULL, verified_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_preferences_context
  ON memory_preferences(contact_id, conversation_id, verified_at DESC);
CREATE TABLE IF NOT EXISTS memory_relationships (
  state_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  verified_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_relationships_context
  ON memory_relationships(contact_id, conversation_id, verified_at DESC);
CREATE TABLE IF NOT EXISTS memory_deletions (
  deletion_id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, recoverable_until TEXT NOT NULL,
  snapshot_json TEXT NOT NULL, receipt_json TEXT NOT NULL, restored_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_audit (
  audit_id TEXT PRIMARY KEY, action TEXT NOT NULL, contact_id TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def encode(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteMemoryStore:
    """M6-owned SQLite database; every context query is pair-scoped."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(DDL)
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(memory_messages)")}
            if "direction" not in columns:
                self.connection.execute("ALTER TABLE memory_messages ADD COLUMN direction TEXT NOT NULL DEFAULT 'inbound'")

    @contextmanager
    def uow(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()
