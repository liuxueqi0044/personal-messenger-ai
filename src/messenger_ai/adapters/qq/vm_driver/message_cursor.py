from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .sequence_alignment import unique_suffix_start

_OPERATOR_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
IDENTITY_SCHEMA_VERSION = 1
CURRENT_IDENTITY_SCHEMA = (
    f"qq-message-identity-v{IDENTITY_SCHEMA_VERSION}:"
    "sha256-json(direction,text,message_key)"
)
IDENTITY_SCHEMA_MIGRATION_REQUIRED = "message_cursor_schema_migration_required"


def _identity(row: dict[str, object]) -> str:
    # `conversation_internal_id` is a current QQ UI locator.  It changes when
    # QQ recreates a session/window and must never be persisted as a message
    # anchor.  The ordered hash snapshot contains no message body itself.
    payload = {key: row.get(key) for key in ("direction", "text", "message_key")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _snapshot_sha256(snapshot_json: str, identity_schema: str | None) -> str:
    # Cursor snapshots have historically used default JSON spacing.  CAS must
    # reflect the schema and ordered identities, not harmless serializer
    # differences.  A NULL schema is intentionally tokenizable so an operator
    # can safely CAS-reanchor a legacy row.
    canonical = json.dumps(
        {
            "identity_schema": identity_schema,
            "ordered_identities": json.loads(snapshot_json),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReanchorResult:
    """Result of a compare-and-swap reanchor with no message content."""

    applied: bool
    idempotent: bool
    snapshot_sha256: str
    next_seq: int


class MessageCursorStore:
    """Transactional per-conversation snapshot, sequence and delivery outbox."""

    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS cursor_state(
          conversation_id TEXT PRIMARY KEY, next_seq INTEGER NOT NULL,
          snapshot_json TEXT NOT NULL, identity_schema TEXT
        );
        CREATE TABLE IF NOT EXISTS observation_outbox(
          outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          local_key TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(conversation_id,local_key)
        );
        CREATE TABLE IF NOT EXISTS cursor_reanchor_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          operator_id TEXT NOT NULL, reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
          expected_snapshot_sha256 TEXT NOT NULL, observed_snapshot_sha256 TEXT NOT NULL,
          replacement_snapshot_sha256 TEXT NOT NULL, next_seq INTEGER NOT NULL,
          outcome TEXT NOT NULL CHECK(outcome IN ('applied','idempotent','cas_mismatch','outbox_not_settled'))
        );
        CREATE TRIGGER IF NOT EXISTS cursor_reanchor_audit_no_update
          BEFORE UPDATE ON cursor_reanchor_audit
          BEGIN SELECT RAISE(ABORT, 'cursor_reanchor_audit_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS cursor_reanchor_audit_no_delete
          BEFORE DELETE ON cursor_reanchor_audit
          BEGIN SELECT RAISE(ABORT, 'cursor_reanchor_audit_append_only'); END;
        """)
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(cursor_state)")
        }
        if "identity_schema" not in columns:
            # SQLite adds the nullable column without rewriting legacy rows;
            # their NULL value is the explicit unversioned state.
            self.connection.execute(
                "ALTER TABLE cursor_state ADD COLUMN identity_schema TEXT"
            )

    @staticmethod
    def _identities(bubbles: list[dict[str, object]]) -> list[str]:
        return [_identity(item) for item in bubbles]

    def snapshot_token(self, conversation_id: str) -> str | None:
        """Return the opaque CAS token for a conversation's persisted snapshot."""
        row = self.connection.execute(
            """SELECT snapshot_json,identity_schema FROM cursor_state
               WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
        return (
            _snapshot_sha256(str(row["snapshot_json"]), row["identity_schema"])
            if row is not None
            else None
        )

    def _append_reanchor_audit(self, *, conversation_id: str, operator_id: str,
                               reason_code: str, expected: str,
                               observed: str, replacement: str, next_seq: int,
                               outcome: str) -> None:
        # This table deliberately contains only fixed metadata and SHA-256
        # values.  It must remain append-only: no update/delete API exists.
        self.connection.execute(
            """INSERT INTO cursor_reanchor_audit(
                   conversation_id,operator_id,reason_code,created_at,
                   expected_snapshot_sha256,observed_snapshot_sha256,
                   replacement_snapshot_sha256,next_seq,outcome
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (conversation_id, operator_id, reason_code, datetime.now(UTC).isoformat(),
             expected, observed, replacement, next_seq, outcome),
        )

    def reanchor_snapshot(self, conversation_id: str, bubbles: list[dict[str, object]], *,
                          expected_snapshot_sha256: str, operator_id: str,
                          reason_code: str) -> ReanchorResult:
        """CAS-replace one settled conversation snapshot without changing sequence.

        Reanchoring cannot discard observations waiting to be delivered.  A
        repeated request after a successful replacement is idempotent only if
        the current snapshot is already the requested replacement.
        """
        if not isinstance(expected_snapshot_sha256, str) or len(expected_snapshot_sha256) != 64:
            raise ValueError("cursor_reanchor_token_invalid")
        try:
            int(expected_snapshot_sha256, 16)
        except ValueError as exc:
            raise ValueError("cursor_reanchor_token_invalid") from exc
        if not isinstance(operator_id, str) or not _OPERATOR_ID.fullmatch(operator_id):
            raise ValueError("cursor_reanchor_operator_invalid")
        if not isinstance(reason_code, str) or not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("cursor_reanchor_reason_invalid")
        replacement_json = json.dumps(self._identities(bubbles), separators=(",", ":"))
        replacement = _snapshot_sha256(replacement_json, CURRENT_IDENTITY_SCHEMA)
        expected = expected_snapshot_sha256.lower()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """SELECT next_seq,snapshot_json,identity_schema FROM cursor_state
                   WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("cursor_reanchor_snapshot_missing")
            current_json = str(row["snapshot_json"])
            current_schema = row["identity_schema"]
            next_seq = int(row["next_seq"])
            observed = _snapshot_sha256(current_json, current_schema)
            unsettled = self.connection.execute(
                """SELECT 1 FROM observation_outbox WHERE conversation_id=?
                   AND status IN ('pending','dispatching') LIMIT 1""", (conversation_id,)
            ).fetchone() is not None
            if unsettled:
                self._append_reanchor_audit(conversation_id=conversation_id, operator_id=operator_id,
                                            reason_code=reason_code, expected=expected,
                                            observed=observed, replacement=replacement,
                                            next_seq=next_seq, outcome="outbox_not_settled")
                self.connection.execute("COMMIT")
                raise RuntimeError("cursor_reanchor_outbox_not_settled")
            if expected != observed:
                if observed == replacement:
                    self._append_reanchor_audit(conversation_id=conversation_id, operator_id=operator_id,
                                                reason_code=reason_code, expected=expected,
                                                observed=observed, replacement=replacement,
                                                next_seq=next_seq, outcome="idempotent")
                    self.connection.execute("COMMIT")
                    return ReanchorResult(False, True, observed, next_seq)
                self._append_reanchor_audit(conversation_id=conversation_id, operator_id=operator_id,
                                            reason_code=reason_code, expected=expected,
                                            observed=observed, replacement=replacement,
                                            next_seq=next_seq, outcome="cas_mismatch")
                self.connection.execute("COMMIT")
                raise RuntimeError("cursor_reanchor_cas_mismatch")
            changed = self.connection.execute(
                """UPDATE cursor_state SET snapshot_json=?,identity_schema=?
                   WHERE conversation_id=? AND snapshot_json=? AND identity_schema IS ?""",
                (
                    replacement_json,
                    CURRENT_IDENTITY_SCHEMA,
                    conversation_id,
                    current_json,
                    current_schema,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("cursor_reanchor_cas_mismatch")
            self._append_reanchor_audit(conversation_id=conversation_id, operator_id=operator_id,
                                        reason_code=reason_code, expected=expected,
                                        observed=observed, replacement=replacement,
                                        next_seq=next_seq, outcome="applied")
            self.connection.execute("COMMIT")
            return ReanchorResult(True, False, replacement, next_seq)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def has_snapshot(self, conversation_id: str) -> bool:
        """Return whether this conversation has already adopted a baseline."""
        return self.connection.execute(
            "SELECT 1 FROM cursor_state WHERE conversation_id=?", (conversation_id,)
        ).fetchone() is not None

    def ingest_snapshot(self, conversation_id: str, bubbles: list[dict[str, object]]) -> tuple[str, ...]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT * FROM cursor_state WHERE conversation_id=?", (conversation_id,)).fetchone()
            identities = self._identities(bubbles)
            if row is None:
                self.connection.execute(
                    """INSERT INTO cursor_state(
                           conversation_id,next_seq,snapshot_json,identity_schema
                       ) VALUES(?,?,?,?)""",
                    (
                        conversation_id,
                        1,
                        json.dumps(identities),
                        CURRENT_IDENTITY_SCHEMA,
                    ),
                )
                self.connection.execute("COMMIT")
                return ()  # first observation establishes a baseline
            before = json.loads(row["snapshot_json"])
            if row["identity_schema"] != CURRENT_IDENTITY_SCHEMA:
                # Both snapshots being empty is the only automatic migration
                # for which replay is impossible: there are no identities to
                # reinterpret or emit.  Every nonempty legacy/version-mismatched
                # row requires an explicit operator reanchor.
                if row["identity_schema"] is None and not before and not identities:
                    self.connection.execute(
                        """UPDATE cursor_state SET identity_schema=?
                           WHERE conversation_id=? AND identity_schema IS NULL""",
                        (CURRENT_IDENTITY_SCHEMA, conversation_id),
                    )
                    self.connection.execute("COMMIT")
                    return ()
                raise ValueError(IDENTITY_SCHEMA_MIGRATION_REQUIRED)
            longest = unique_suffix_start(before, identities)
            next_seq = int(row["next_seq"])
            keys: list[str] = []
            for bubble in bubbles[longest:]:
                key = str(next_seq); next_seq += 1
                self.connection.execute("INSERT INTO observation_outbox(conversation_id,local_key,payload_json) VALUES(?,?,?)",
                                        (conversation_id, key, json.dumps(bubble, sort_keys=True, ensure_ascii=False)))
                keys.append(key)
            self.connection.execute(
                """UPDATE cursor_state SET next_seq=?,snapshot_json=?,identity_schema=?
                   WHERE conversation_id=?""",
                (
                    next_seq,
                    json.dumps(identities),
                    CURRENT_IDENTITY_SCHEMA,
                    conversation_id,
                ),
            )
            self.connection.execute("COMMIT")
            return tuple(keys)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def bootstrap_last_inbound_once(self, conversation_id: str,
                                    bubbles: list[dict[str, object]]) -> tuple[str, ...]:
        """Seed history while emitting only the operator-approved final inbound row."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT 1 FROM cursor_state WHERE conversation_id=?", (conversation_id,)).fetchone()
            if existing is not None:
                self.connection.execute("COMMIT")
                return ()
            if not bubbles or bubbles[-1].get("direction") != "inbound":
                raise ValueError("bootstrap_last_message_not_inbound")
            identities = self._identities(bubbles)
            self.connection.execute(
                """INSERT INTO cursor_state(
                       conversation_id,next_seq,snapshot_json,identity_schema
                   ) VALUES(?,?,?,?)""",
                (
                    conversation_id,
                    2,
                    json.dumps(identities),
                    CURRENT_IDENTITY_SCHEMA,
                ),
            )
            self.connection.execute(
                "INSERT INTO observation_outbox(conversation_id,local_key,payload_json) VALUES(?,?,?)",
                (conversation_id, "1", json.dumps(bubbles[-1], sort_keys=True, ensure_ascii=False)))
            self.connection.execute("COMMIT")
            return ("1",)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def claim(self, conversation_id: str) -> list[sqlite3.Row]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute("SELECT * FROM observation_outbox WHERE conversation_id=? AND status='pending' ORDER BY outbox_id", (conversation_id,)).fetchall()
            self.connection.executemany("UPDATE observation_outbox SET status='dispatching' WHERE outbox_id=?", [(r["outbox_id"],) for r in rows])
            self.connection.execute("COMMIT")
            return rows
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def acknowledge(self, outbox_id: int) -> bool:
        return bool(self.connection.execute("UPDATE observation_outbox SET status='delivered' WHERE outbox_id=? AND status='dispatching'", (outbox_id,)).rowcount)

    def acknowledge_keys(self, conversation_id: str, local_keys: tuple[str, ...]) -> int:
        if not local_keys:
            return 0
        placeholders = ",".join("?" for _ in local_keys)
        return self.connection.execute(
            f"UPDATE observation_outbox SET status='delivered' WHERE conversation_id=? AND local_key IN ({placeholders}) AND status='dispatching'",
            (conversation_id, *local_keys),
        ).rowcount

    def recover(self) -> int:
        return self.connection.execute("UPDATE observation_outbox SET status='pending' WHERE status='dispatching'").rowcount

    def close(self) -> None:
        self.connection.close()
