"""Audited settlement of a definitively terminal cross-database send lane.

This module never sends, authorizes, observes, or calls a model.  It exists so
an operator can close a stale runtime projection only when the Hub, QQ bridge,
and pacing authorities all prove that no commit happened.
"""

from __future__ import annotations

import gc
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_OPERATOR = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = {"failed": "rejected", "cancelled": "cancelled"}
_RUNTIME_AUDIT_TABLE = "runtime_send_settlement_audit_v2"
_HUB_AUDIT_TABLE = "terminal_send_outbox_cancel_audit_v2"


class SettlementRefused(RuntimeError):
    """The authorities do not prove one exact no-commit terminal outcome."""


@dataclass(frozen=True, slots=True)
class TerminalSettlementTarget:
    settlement_id: str
    operator_id: str
    reason_code: str
    conversation_id: str
    pacing_plan_id: str
    segment_index: int
    operation_id: str
    authorization_id: str
    draft_id: str
    hub_send_outbox_id: str
    hub_stable_outbox_id: str
    hub_draft_outbox_id: str
    binding_id: str
    binding_revision: int
    conversation_revision: int
    body_hash: str
    terminal_status: Literal["failed", "cancelled"] = "failed"
    pacing_cancel_reason: str = "revalidation_rejected"

    def validate(self) -> None:
        values = (
            self.settlement_id,
            self.conversation_id,
            self.pacing_plan_id,
            self.operation_id,
            self.authorization_id,
            self.draft_id,
            self.hub_send_outbox_id,
            self.hub_stable_outbox_id,
            self.hub_draft_outbox_id,
            self.binding_id,
            self.pacing_cancel_reason,
        )
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("terminal settlement identifiers must be non-empty")
        if len(
            {
                self.hub_send_outbox_id,
                self.hub_stable_outbox_id,
                self.hub_draft_outbox_id,
            }
        ) != 3:
            raise ValueError("terminal settlement outbox identifiers must be distinct")
        if _OPERATOR.fullmatch(self.operator_id) is None:
            raise ValueError("terminal settlement operator is invalid")
        if _REASON.fullmatch(self.reason_code) is None:
            raise ValueError("terminal settlement reason is invalid")
        if self.segment_index < 0 or self.binding_revision < 1 or self.conversation_revision < 1:
            raise ValueError("terminal settlement coordinates are invalid")
        if _SHA256.fullmatch(self.body_hash) is None:
            raise ValueError("terminal settlement body hash is invalid")
        if self.terminal_status not in _TERMINAL:
            raise ValueError("terminal settlement status is unsupported")


@dataclass(frozen=True, slots=True)
class TerminalSettlementResult:
    applied: bool
    idempotent: bool
    evidence_sha256: str
    terminal_status: str
    artifact_status: str
    legacy_due_operation_id_missing: bool


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _one(row: sqlite3.Row | None, code: str) -> sqlite3.Row:
    if row is None:
        raise SettlementRefused(code)
    return row


def _attach(connection: sqlite3.Connection, data_dir: Path) -> None:
    for alias, filename in (
        ("hubdb", "hub.sqlite3"),
        ("bridgedb", "qq-vm-bridge.sqlite3"),
        ("pacingdb", "pacing.sqlite3"),
    ):
        path = (data_dir / filename).resolve()
        if not path.is_file():
            raise SettlementRefused("SEND_SETTLEMENT_DATABASE_MISSING")
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))


def _ensure_audit_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS runtime_send_settlement_audit_v2(
          settlement_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          pacing_plan_id TEXT NOT NULL,
          segment_index INTEGER NOT NULL,
          operation_id TEXT NOT NULL,
          authorization_id TEXT NOT NULL,
          draft_id TEXT NOT NULL,
          hub_send_outbox_id TEXT NOT NULL,
          hub_stable_outbox_id TEXT NOT NULL,
          hub_draft_outbox_id TEXT NOT NULL,
          binding_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL,
          conversation_revision INTEGER NOT NULL,
          body_hash TEXT NOT NULL,
          terminal_status TEXT NOT NULL,
          artifact_status TEXT NOT NULL,
          operator_id TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          evidence_sha256 TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          settled_at TEXT NOT NULL,
          UNIQUE(pacing_plan_id,segment_index,operation_id)
        ) WITHOUT ROWID"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_send_settlement_audit_v2_no_update
           BEFORE UPDATE ON runtime_send_settlement_audit_v2
           BEGIN
             SELECT RAISE(ABORT, 'runtime_send_settlement_audit_append_only');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_send_settlement_audit_v2_no_delete
           BEFORE DELETE ON runtime_send_settlement_audit_v2
           BEGIN
             SELECT RAISE(ABORT, 'runtime_send_settlement_audit_append_only');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_send_settlement_audit_v2_no_replace
           BEFORE INSERT ON runtime_send_settlement_audit_v2
           WHEN EXISTS(
             SELECT 1 FROM runtime_send_settlement_audit_v2
             WHERE settlement_id=NEW.settlement_id
                OR (pacing_plan_id=NEW.pacing_plan_id
                    AND segment_index=NEW.segment_index
                    AND operation_id=NEW.operation_id)
           )
           BEGIN
             SELECT RAISE(ABORT, 'runtime_send_settlement_audit_append_only');
           END"""
    )


def _hub_outbox_identity(row: sqlite3.Row, *, role: str) -> str:
    return _sha256(
        _canonical(
            {
                "role": role,
                "outbox_id": row["outbox_id"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "payload_sha256": _sha256(str(row["payload_json"])),
                "attempt_count": int(row["attempt_count"]),
                "created_at": row["created_at"],
                "available_at": row["available_at"],
            }
        )
    )


def _hub_schema_table_exists(
    connection: sqlite3.Connection, *, attached: bool, table: str
) -> bool:
    schema = "hubdb.sqlite_master" if attached else "sqlite_master"
    return (
        connection.execute(
            f"SELECT 1 FROM {schema} WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _hub_schema_trigger_exists(
    connection: sqlite3.Connection, *, attached: bool, trigger: str
) -> bool:
    schema = "hubdb.sqlite_master" if attached else "sqlite_master"
    return (
        connection.execute(
            f"SELECT 1 FROM {schema} WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        is not None
    )


def _runtime_audit_guards_exist(connection: sqlite3.Connection) -> bool:
    required = {
        "runtime_send_settlement_audit_v2_no_update",
        "runtime_send_settlement_audit_v2_no_delete",
        "runtime_send_settlement_audit_v2_no_replace",
    }
    present = {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master WHERE type='trigger'
               AND name IN (?,?,?)""",
            tuple(sorted(required)),
        )
    }
    return present == required


def _hub_audit_guards_exist(
    connection: sqlite3.Connection, *, attached: bool
) -> bool:
    required = {
        "terminal_send_outbox_cancel_audit_v2_no_update",
        "terminal_send_outbox_cancel_audit_v2_no_delete",
        "terminal_send_outbox_cancel_audit_v2_no_replace",
    }
    return all(
        _hub_schema_trigger_exists(
            connection, attached=attached, trigger=trigger
        )
        for trigger in required
    )


def _hub_lane_aggregate_ids(
    connection: sqlite3.Connection,
    target: TerminalSettlementTarget,
    *,
    attached: bool,
) -> set[str]:
    """Every HubService aggregate this send lane can still influence.

    HubService keys its outbox rows by an aggregate: the conversation for
    ``conversation.stable_window``/``pacing.due``, the draft for
    ``draft.created``, the operation for ``send.requested``, and the
    authorization row.  Review and policy stages, when present, key by the
    review/decision row for a draft.  Sibling drafts in the same conversation
    are included too: they share the conversation aggregate, so a pending
    ``draft.created`` for one of them is still work this lane must not settle
    over.  The lineage is enumerated from live rows so a related task cannot
    be ignored merely because it is not one of the three cancel candidates.
    """

    prefix = "hubdb." if attached else ""
    lineage = {
        target.conversation_id,
        target.pacing_plan_id,
        target.draft_id,
        target.operation_id,
        target.authorization_id,
    }
    draft_ids = {target.draft_id}
    if _hub_schema_table_exists(connection, attached=attached, table="drafts"):
        draft_ids.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT draft_id FROM {prefix}drafts WHERE conversation_id=?",
                (target.conversation_id,),
            )
            if row[0]
        )
    draft_ids.discard("")
    lineage.update(draft_ids)
    ordered_drafts = sorted(draft_ids)
    draft_placeholders = ",".join("?" for _ in ordered_drafts)
    for table, column in (
        ("review_requests", "review_id"),
        ("policy_decisions", "decision_id"),
    ):
        if not _hub_schema_table_exists(connection, attached=attached, table=table):
            continue
        lineage.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT {column} FROM {prefix}{table} "
                f"WHERE draft_id IN ({draft_placeholders})",
                tuple(ordered_drafts),
            )
            if row[0]
        )
    lineage.discard("")
    return lineage


def _assert_hub_lane_has_no_other_active_work(
    connection: sqlite3.Connection,
    target: TerminalSettlementTarget,
    *,
    attached: bool,
    lineage: set[str],
) -> None:
    """Refuse if the lane still owns pending/dispatching Hub outbox work.

    Only the three exactly named rows may be cancelled, so any other
    pending/dispatching row bound to the same conversation/plan/draft/operation
    /pacing/review/policy lineage blocks settlement.  Rows owned by unrelated
    conversations are ignored by construction.
    """

    prefix = "hubdb." if attached else ""
    ordered = sorted(lineage)
    placeholders = ",".join("?" for _ in ordered)
    conflict = connection.execute(
        f"""SELECT outbox_id FROM {prefix}outbox
            WHERE status IN ('pending','dispatching')
              AND outbox_id NOT IN (?,?,?)
              AND aggregate_id IN ({placeholders})
            LIMIT 1""",
        (
            target.hub_send_outbox_id,
            target.hub_stable_outbox_id,
            target.hub_draft_outbox_id,
            *ordered,
        ),
    ).fetchone()
    if conflict is not None:
        raise SettlementRefused("SEND_SETTLEMENT_OTHER_WORK_ACTIVE")


def _collect_hub_outbox_evidence(
    connection: sqlite3.Connection,
    target: TerminalSettlementTarget,
    *,
    attached: bool,
) -> dict[str, object]:
    prefix = "hubdb." if attached else ""
    rows = connection.execute(
        f"""SELECT * FROM {prefix}outbox
            WHERE outbox_id IN (?,?,?)""",
        (
            target.hub_send_outbox_id,
            target.hub_stable_outbox_id,
            target.hub_draft_outbox_id,
        ),
    ).fetchall()
    if len(rows) != 3:
        raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_MISSING")
    by_id = {str(row["outbox_id"]): row for row in rows}
    specs = (
        (
            "send_requested",
            target.hub_send_outbox_id,
            "send.requested",
            target.operation_id,
        ),
        (
            "stable_window",
            target.hub_stable_outbox_id,
            "conversation.stable_window",
            target.conversation_id,
        ),
        (
            "draft_created",
            target.hub_draft_outbox_id,
            "draft.created",
            target.draft_id,
        ),
    )
    audit_exists = _hub_schema_table_exists(
        connection,
        attached=attached,
        table=_HUB_AUDIT_TABLE,
    )
    audit_guards = audit_exists and _hub_audit_guards_exist(
        connection, attached=attached
    )
    projected: list[dict[str, object]] = []
    payloads: dict[str, dict[str, object]] = {}
    for role, outbox_id, event_type, aggregate_id in specs:
        row = by_id.get(outbox_id)
        if row is None:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_MISSING")
        if (
            row["event_type"] != event_type
            or row["aggregate_id"] != aggregate_id
            or row["status"] not in {"pending", "cancelled"}
            or int(row["attempt_count"]) != 0
        ):
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_MISMATCH")
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_INVALID") from exc
        if role == "send_requested":
            payload_matches = payload == {"operation_id": target.operation_id}
        elif role == "stable_window":
            payload_matches = (
                isinstance(payload, dict)
                and set(payload) == {"conversation_id", "last_message_key"}
                and payload.get("conversation_id") == target.conversation_id
                and isinstance(payload.get("last_message_key"), str)
                and bool(payload["last_message_key"])
            )
        else:
            draft_text = payload.get("text") if isinstance(payload, dict) else None
            payload_matches = (
                isinstance(payload, dict)
                and set(payload)
                == {
                    "draft_id",
                    "conversation_id",
                    "contact_id",
                    "text",
                    "source_message_keys",
                    "rule_version",
                    "status",
                    "text_hash",
                }
                and payload.get("draft_id") == target.draft_id
                and payload.get("conversation_id") == target.conversation_id
                and payload.get("contact_id") == target.binding_id
                and isinstance(draft_text, str)
                and _sha256(draft_text) == target.body_hash
                and payload.get("text_hash") == target.body_hash
                and isinstance(payload.get("source_message_keys"), list)
                and isinstance(payload.get("rule_version"), str)
                and bool(payload["rule_version"])
                and payload.get("status") == "created"
            )
        if not payload_matches:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_PAYLOAD_MISMATCH")
        payloads[role] = payload
        identity_sha256 = _hub_outbox_identity(row, role=role)
        audit = (
            connection.execute(
                f"""SELECT * FROM {prefix}terminal_send_outbox_cancel_audit_v2
                    WHERE outbox_id=?""",
                (outbox_id,),
            ).fetchone()
            if audit_exists
            else None
        )
        if (row["status"] == "cancelled" or audit is not None) and not audit_guards:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_SCHEMA_UNSAFE")
        if row["status"] == "cancelled":
            if audit is None:
                raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_MISSING")
            if (
                audit["settlement_id"] != target.settlement_id
                or audit["role"] != role
                or audit["operator_id"] != target.operator_id
                or audit["reason_code"] != target.reason_code
                or audit["identity_sha256"] != identity_sha256
                or int(row["version"]) != int(audit["original_version"]) + 1
            ):
                raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_MISMATCH")
        elif audit is not None:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_MISMATCH")
        projected.append(
            {
                "role": role,
                "outbox_id": outbox_id,
                "identity_sha256": identity_sha256,
                "terminal_status": "cancelled",
            }
        )

    # The authorization that froze this send names the exact inbound message
    # the lane was planned against.  A stale ``conversation.stable_window``
    # carrying a different key belongs to a later inbound; cancelling it would
    # silently drop that newer lane's planning notification.
    authorization = connection.execute(
        f"""SELECT conversation_id,draft_id,text_hash,expected_last_message_key
            FROM {prefix}authorizations WHERE authorization_id=?""",
        (target.authorization_id,),
    ).fetchone()
    if authorization is None:
        raise SettlementRefused("SEND_SETTLEMENT_HUB_AUTHORIZATION_MISSING")
    expected_last_message_key = authorization["expected_last_message_key"]
    if (
        authorization["conversation_id"] != target.conversation_id
        or authorization["draft_id"] != target.draft_id
        or authorization["text_hash"] != target.body_hash
        or not isinstance(expected_last_message_key, str)
        or not expected_last_message_key
    ):
        raise SettlementRefused("SEND_SETTLEMENT_HUB_AUTHORIZATION_MISMATCH")
    stable_window_key = str(payloads["stable_window"]["last_message_key"])
    if stable_window_key != expected_last_message_key:
        raise SettlementRefused("SEND_SETTLEMENT_HUB_STABLE_WINDOW_MISMATCH")

    # ``SQLiteHubStore.stable_window_is_current`` treats the conversation row
    # as authoritative, so the current conversation key must still equal the
    # authorized key.  A newer inbound would have advanced it.
    conversation = connection.execute(
        f"SELECT last_message_key FROM {prefix}conversations WHERE conversation_id=?",
        (target.conversation_id,),
    ).fetchone()
    if conversation is None:
        raise SettlementRefused("SEND_SETTLEMENT_HUB_CONVERSATION_MISSING")
    conversation_key = conversation["last_message_key"]
    if conversation_key != expected_last_message_key:
        raise SettlementRefused("SEND_SETTLEMENT_HUB_CONVERSATION_KEY_MISMATCH")

    lineage = _hub_lane_aggregate_ids(connection, target, attached=attached)
    _assert_hub_lane_has_no_other_active_work(
        connection, target, attached=attached, lineage=lineage
    )
    return {
        "outboxes": projected,
        "authorization_expected_last_message_key_sha256": _sha256(
            expected_last_message_key
        ),
        "stable_window_last_message_key_sha256": _sha256(stable_window_key),
        "conversation_last_message_key_sha256": _sha256(str(conversation_key)),
        "lineage_aggregate_ids_sha256": _sha256(_canonical(sorted(lineage))),
        "other_related_active": 0,
    }


def _ensure_hub_outbox_audit_schema(
    connection: sqlite3.Connection, *, attached: bool
) -> None:
    prefix = "hubdb." if attached else ""
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {prefix}terminal_send_outbox_cancel_audit_v2(
          outbox_id TEXT PRIMARY KEY,
          settlement_id TEXT NOT NULL,
          role TEXT NOT NULL,
          original_version INTEGER NOT NULL,
          operator_id TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          identity_sha256 TEXT NOT NULL,
          cancelled_at TEXT NOT NULL
        ) WITHOUT ROWID"""
    )
    connection.execute(
        f"""CREATE TRIGGER IF NOT EXISTS {prefix}terminal_send_outbox_cancel_audit_v2_no_update
           BEFORE UPDATE ON terminal_send_outbox_cancel_audit_v2
           BEGIN
             SELECT RAISE(ABORT, 'terminal_send_outbox_cancel_audit_append_only');
           END"""
    )
    connection.execute(
        f"""CREATE TRIGGER IF NOT EXISTS {prefix}terminal_send_outbox_cancel_audit_v2_no_delete
           BEFORE DELETE ON terminal_send_outbox_cancel_audit_v2
           BEGIN
             SELECT RAISE(ABORT, 'terminal_send_outbox_cancel_audit_append_only');
           END"""
    )
    connection.execute(
        f"""CREATE TRIGGER IF NOT EXISTS {prefix}terminal_send_outbox_cancel_audit_v2_no_replace
           BEFORE INSERT ON terminal_send_outbox_cancel_audit_v2
           WHEN EXISTS(
             SELECT 1 FROM terminal_send_outbox_cancel_audit_v2
             WHERE outbox_id=NEW.outbox_id
           )
           BEGIN
             SELECT RAISE(ABORT, 'terminal_send_outbox_cancel_audit_append_only');
           END"""
    )


def _settle_hub_outboxes(
    connection: sqlite3.Connection,
    target: TerminalSettlementTarget,
    *,
    attached: bool,
) -> None:
    prefix = "hubdb." if attached else ""
    rows = connection.execute(
        f"SELECT * FROM {prefix}outbox WHERE outbox_id IN (?,?,?)",
        (
            target.hub_send_outbox_id,
            target.hub_stable_outbox_id,
            target.hub_draft_outbox_id,
        ),
    ).fetchall()
    roles = {
        target.hub_send_outbox_id: "send_requested",
        target.hub_stable_outbox_id: "stable_window",
        target.hub_draft_outbox_id: "draft_created",
    }
    for row in rows:
        if row["status"] == "cancelled":
            continue
        role = roles[str(row["outbox_id"])]
        changed = connection.execute(
            f"""UPDATE {prefix}outbox SET status='cancelled',version=version+1
                WHERE outbox_id=? AND status='pending' AND attempt_count=0
                  AND version=?""",
            (row["outbox_id"], int(row["version"])),
        ).rowcount
        if changed != 1:
            raise SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_CAS_LOST")
        connection.execute(
            f"""INSERT INTO {prefix}terminal_send_outbox_cancel_audit_v2(
                 outbox_id,settlement_id,role,original_version,operator_id,
                 reason_code,identity_sha256,cancelled_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                row["outbox_id"],
                target.settlement_id,
                role,
                int(row["version"]),
                target.operator_id,
                target.reason_code,
                _hub_outbox_identity(row, role=role),
                datetime.now(UTC).isoformat(),
            ),
        )
    _collect_hub_outbox_evidence(connection, target, attached=attached)


def _active_count(connection: sqlite3.Connection, target: TerminalSettlementTarget) -> int:
    queries: tuple[tuple[str, tuple[object, ...]], ...] = (
        (
            """SELECT COUNT(*) FROM runtime_event_outbox WHERE aggregate_id=?
               AND status IN ('pending','dispatching')""",
            (target.conversation_id,),
        ),
        (
            """SELECT COUNT(*) FROM runtime_planning_jobs WHERE conversation_id=?
               AND status IN ('pending','running')""",
            (target.conversation_id,),
        ),
        (
            """SELECT COUNT(*) FROM runtime_plan_artifacts WHERE conversation_id=?
               AND pacing_plan_id<>? AND status='waiting'""",
            (target.conversation_id, target.pacing_plan_id),
        ),
        (
            """SELECT COUNT(*) FROM runtime_segment_executions
               WHERE conversation_id=?
                 AND NOT (pacing_plan_id=? AND segment_index=?)
                 AND status NOT IN ('verified','failed','cancelled','rejected')""",
            (target.conversation_id, target.pacing_plan_id, target.segment_index),
        ),
        (
            """SELECT COUNT(*) FROM hubdb.send_operations s
               JOIN hubdb.drafts d ON d.draft_id=s.draft_id
               WHERE d.conversation_id=? AND s.operation_id<>?
                 AND s.status NOT IN
                     ('verified','failed','cancelled','send_uncertain')""",
            (target.conversation_id, target.operation_id),
        ),
        (
            """SELECT COUNT(*) FROM hubdb.review_requests r
               JOIN hubdb.drafts d ON d.draft_id=r.draft_id
               WHERE d.conversation_id=? AND r.status='pending'""",
            (target.conversation_id,),
        ),
        (
            """SELECT COUNT(*) FROM hubdb.outbox o
               LEFT JOIN hubdb.send_operations s ON s.operation_id=o.aggregate_id
               LEFT JOIN hubdb.drafts d ON d.draft_id=s.draft_id
               WHERE o.status IN ('pending','dispatching')
                 AND o.outbox_id NOT IN (?,?,?)
                 AND (o.aggregate_id=? OR d.conversation_id=?)""",
            (
                target.hub_send_outbox_id,
                target.hub_stable_outbox_id,
                target.hub_draft_outbox_id,
                target.conversation_id,
                target.conversation_id,
            ),
        ),
        (
            """SELECT COUNT(*) FROM bridgedb.qq_vm_ops WHERE conversation_id=?
               AND operation_id<>? AND status NOT IN
                   ('verified','failed','cancelled','send_uncertain')""",
            (target.conversation_id, target.operation_id),
        ),
        (
            """SELECT COUNT(*) FROM pacingdb.m10_plans WHERE conversation_id=?
               AND pacing_plan_id<>?
               AND status IN ('waiting','due_for_revalidation')""",
            (target.conversation_id, target.pacing_plan_id),
        ),
        (
            """SELECT COUNT(*) FROM pacingdb.m10_due_outbox o
               JOIN pacingdb.m10_plans p
                 ON p.pacing_plan_id=o.pacing_plan_id
               WHERE p.conversation_id=?
                 AND NOT (o.pacing_plan_id=? AND o.segment_index=?)
                 AND o.status IN
                     ('pending','dispatching','dispatching_nonrecoverable')""",
            (target.conversation_id, target.pacing_plan_id, target.segment_index),
        ),
    )
    return sum(int(connection.execute(sql, params).fetchone()[0]) for sql, params in queries)


def _assert_atomic_journal_modes(connection: sqlite3.Connection) -> None:
    """Require DELETE journals so the attached multi-DB commit is atomic."""

    for schema in ("main", "hubdb", "bridgedb", "pacingdb"):
        row = connection.execute(f"PRAGMA {schema}.journal_mode").fetchone()
        mode = str(row[0]).lower() if row is not None else ""
        if mode != "delete":
            raise SettlementRefused("SEND_SETTLEMENT_JOURNAL_MODE_UNSAFE")


def _prepare_atomic_journal_modes(data_dir: Path) -> None:
    """Checkpoint stopped WAL databases and switch them to DELETE journals."""

    # Closed cyclic store objects can retain finalized SQLite handles until a
    # collection pass.  Release those local handles before requesting the
    # exclusive journal-mode transition; live external writers still fail.
    gc.collect()
    for filename in (
        "runtime.sqlite3",
        "hub.sqlite3",
        "qq-vm-bridge.sqlite3",
        "pacing.sqlite3",
    ):
        connection = sqlite3.connect(data_dir / filename, isolation_level=None)
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            mode = str(row[0]).lower() if row is not None else ""
            if mode == "wal":
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise SettlementRefused("SEND_SETTLEMENT_WAL_CHECKPOINT_BUSY")
            changed = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if changed is None or str(changed[0]).lower() != "delete":
                raise SettlementRefused("SEND_SETTLEMENT_JOURNAL_MODE_UNSAFE")
        except sqlite3.OperationalError as exc:
            raise SettlementRefused("SEND_SETTLEMENT_JOURNAL_MODE_BUSY") from exc
        finally:
            connection.close()


def _collect_evidence(
    connection: sqlite3.Connection, target: TerminalSettlementTarget
) -> tuple[dict[str, object], str, str]:
    global_control = _one(
        connection.execute(
            "SELECT revision,paused,reason FROM runtime_global_control WHERE singleton=1"
        ).fetchone(),
        "SEND_SETTLEMENT_GLOBAL_CONTROL_MISSING",
    )
    if not bool(global_control["paused"]):
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_NOT_PAUSED")
    segment = _one(
        connection.execute(
            """SELECT * FROM runtime_segment_executions
               WHERE pacing_plan_id=? AND segment_index=?""",
            (target.pacing_plan_id, target.segment_index),
        ).fetchone(),
        "SEND_SETTLEMENT_RUNTIME_SEGMENT_MISSING",
    )
    artifact = _one(
        connection.execute(
            "SELECT * FROM runtime_plan_artifacts WHERE pacing_plan_id=?",
            (target.pacing_plan_id,),
        ).fetchone(),
        "SEND_SETTLEMENT_RUNTIME_ARTIFACT_MISSING",
    )
    expected_segment = (
        target.conversation_id,
        target.body_hash,
        target.authorization_id,
        target.operation_id,
        target.binding_revision,
        target.conversation_revision,
    )
    actual_segment = (
        segment["conversation_id"],
        segment["body_hash"],
        segment["authorization_id"],
        segment["operation_id"],
        int(segment["binding_revision"]),
        int(segment["conversation_revision"]),
    )
    if actual_segment != expected_segment:
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_SEGMENT_MISMATCH")
    final_artifact_status = _TERMINAL[target.terminal_status]
    if segment["status"] not in {"authorized", target.terminal_status}:
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_STATUS_UNSAFE")
    if artifact["status"] not in {"waiting", final_artifact_status}:
        raise SettlementRefused("SEND_SETTLEMENT_ARTIFACT_STATUS_UNSAFE")
    if (
        artifact["conversation_id"] != target.conversation_id
        or int(artifact["binding_revision"]) != target.binding_revision
        or int(artifact["conversation_revision"]) != target.conversation_revision
        or artifact["contact_id"] != target.binding_id
    ):
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_ARTIFACT_MISMATCH")
    try:
        segment_drafts = json.loads(artifact["segment_draft_ids_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_ARTIFACT_INVALID") from exc
    if (
        not isinstance(segment_drafts, list)
        or target.segment_index >= len(segment_drafts)
        or segment_drafts[target.segment_index] != target.draft_id
    ):
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_DRAFT_MISMATCH")

    hub = _one(
        connection.execute(
            """SELECT s.status AS operation_status,s.error_code,s.commit_intent,
                      s.authorization_id,s.draft_id,d.conversation_id,d.contact_id,
                      d.text AS draft_text,d.text_hash,
                      a.conversation_id AS authorization_conversation,
                      a.draft_id AS authorization_draft,a.text_hash AS authorization_hash
               FROM hubdb.send_operations s
               JOIN hubdb.drafts d ON d.draft_id=s.draft_id
               JOIN hubdb.authorizations a ON a.authorization_id=s.authorization_id
               WHERE s.operation_id=?""",
            (target.operation_id,),
        ).fetchone(),
        "SEND_SETTLEMENT_HUB_OPERATION_MISSING",
    )
    if (
        hub["operation_status"] != target.terminal_status
        or bool(hub["commit_intent"])
        or hub["authorization_id"] != target.authorization_id
        or hub["draft_id"] != target.draft_id
        or hub["conversation_id"] != target.conversation_id
        or hub["contact_id"] != target.binding_id
        or not isinstance(hub["draft_text"], str)
        or _sha256(hub["draft_text"]) != target.body_hash
        or hub["text_hash"] != target.body_hash
        or hub["authorization_conversation"] != target.conversation_id
        or hub["authorization_draft"] != target.draft_id
        or hub["authorization_hash"] != target.body_hash
    ):
        raise SettlementRefused("SEND_SETTLEMENT_HUB_EVIDENCE_MISMATCH")
    hub_outboxes = _collect_hub_outbox_evidence(
        connection, target, attached=True
    )

    bridge = _one(
        connection.execute(
            "SELECT * FROM bridgedb.qq_vm_ops WHERE operation_id=?",
            (target.operation_id,),
        ).fetchone(),
        "SEND_SETTLEMENT_BRIDGE_OPERATION_MISSING",
    )
    if (
        bridge["status"] != target.terminal_status
        or bool(bridge["commit_intent"])
        or bridge["draft_id"] != target.draft_id
        or bridge["conversation_id"] != target.conversation_id
        or bridge["binding_id"] != target.binding_id
        or bridge["segment_ref"]
        != f"{target.pacing_plan_id}:{target.segment_index}"
        or int(bridge["binding_revision"]) != target.binding_revision
        or int(bridge["conversation_revision"]) != target.conversation_revision
        or bridge["text_hash"] != target.body_hash
    ):
        raise SettlementRefused("SEND_SETTLEMENT_BRIDGE_EVIDENCE_MISMATCH")
    bridge_receipts = int(
        connection.execute(
            "SELECT COUNT(*) FROM bridgedb.qq_vm_receipts WHERE operation_id=?",
            (target.operation_id,),
        ).fetchone()[0]
    )
    if bridge_receipts:
        raise SettlementRefused("SEND_SETTLEMENT_BRIDGE_RECEIPT_PRESENT")

    pacing_plan = _one(
        connection.execute(
            "SELECT * FROM pacingdb.m10_plans WHERE pacing_plan_id=?",
            (target.pacing_plan_id,),
        ).fetchone(),
        "SEND_SETTLEMENT_PACING_PLAN_MISSING",
    )
    if (
        pacing_plan["conversation_id"] != target.conversation_id
        or pacing_plan["contact_id"] != target.binding_id
        or pacing_plan["status"] != final_artifact_status
        or pacing_plan["cancel_reason"] != target.pacing_cancel_reason
    ):
        raise SettlementRefused("SEND_SETTLEMENT_PACING_PLAN_MISMATCH")
    try:
        plan_payload = json.loads(pacing_plan["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise SettlementRefused("SEND_SETTLEMENT_PACING_PAYLOAD_INVALID") from exc
    segments = plan_payload.get("segments")
    drafts = plan_payload.get("segment_draft_ids")
    if (
        plan_payload.get("pacing_plan_id") != target.pacing_plan_id
        or plan_payload.get("conversation_id") != target.conversation_id
        or plan_payload.get("contact_id") != target.binding_id
        or not isinstance(segments, list)
        or target.segment_index >= len(segments)
        or _sha256(str(segments[target.segment_index])) != target.body_hash
        or not isinstance(drafts, list)
        or target.segment_index >= len(drafts)
        or drafts[target.segment_index] != target.draft_id
    ):
        raise SettlementRefused("SEND_SETTLEMENT_PACING_PAYLOAD_MISMATCH")
    receipt_rows = connection.execute(
        """SELECT operation_id,verified FROM pacingdb.m10_segment_receipts
           WHERE pacing_plan_id=? AND segment_index=?""",
        (target.pacing_plan_id, target.segment_index),
    ).fetchall()
    if len(receipt_rows) != 1 or (
        receipt_rows[0]["operation_id"] != target.operation_id
        or bool(receipt_rows[0]["verified"])
    ):
        raise SettlementRefused("SEND_SETTLEMENT_PACING_RECEIPT_MISMATCH")
    due_rows = connection.execute(
        """SELECT outbox_id,status,operation_id,payload_json
           FROM pacingdb.m10_due_outbox
           WHERE pacing_plan_id=? AND segment_index=?""",
        (target.pacing_plan_id, target.segment_index),
    ).fetchall()
    if len(due_rows) != 1 or due_rows[0]["status"] != "delivered":
        raise SettlementRefused("SEND_SETTLEMENT_DUE_OUTBOX_NOT_TERMINAL")
    due_operation = due_rows[0]["operation_id"]
    if due_operation not in {None, target.operation_id}:
        raise SettlementRefused("SEND_SETTLEMENT_DUE_OPERATION_MISMATCH")
    try:
        due_payload = json.loads(due_rows[0]["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise SettlementRefused("SEND_SETTLEMENT_DUE_PAYLOAD_INVALID") from exc
    if (
        due_payload.get("pacing_plan_id") != target.pacing_plan_id
        or due_payload.get("conversation_id") != target.conversation_id
        or int(due_payload.get("segment_index", -1)) != target.segment_index
        or due_payload.get("draft_id") != target.draft_id
        or due_payload.get("body_hash") != target.body_hash
    ):
        raise SettlementRefused("SEND_SETTLEMENT_DUE_PAYLOAD_MISMATCH")
    if _active_count(connection, target):
        raise SettlementRefused("SEND_SETTLEMENT_OTHER_WORK_ACTIVE")

    evidence: dict[str, object] = {
        "schema": "pmai-terminal-send-settlement-evidence-v2",
        "conversation_id": target.conversation_id,
        "pacing_plan_id": target.pacing_plan_id,
        "segment_index": target.segment_index,
        "operation_id": target.operation_id,
        "authorization_id": target.authorization_id,
        "draft_id": target.draft_id,
        "binding_id": target.binding_id,
        "binding_revision": target.binding_revision,
        "conversation_revision": target.conversation_revision,
        "body_hash": target.body_hash,
        "terminal_status": target.terminal_status,
        "artifact_status": final_artifact_status,
        "hub": {"status": hub["operation_status"], "commit_intent": 0},
        "hub_outboxes": hub_outboxes,
        "bridge": {
            "status": bridge["status"],
            "commit_intent": 0,
            "receipt_count": 0,
        },
        "pacing": {
            "status": pacing_plan["status"],
            "cancel_reason": pacing_plan["cancel_reason"],
            "receipt_verified": 0,
            "due_status": "delivered",
        },
        "legacy_due_operation_id_missing": due_operation is None,
        "other_active_work": 0,
        "runtime_global_control": {
            "paused": 1,
            "revision": int(global_control["revision"]),
            "reason_sha256": _sha256(str(global_control["reason"] or "")),
        },
    }
    return evidence, str(segment["status"]), str(artifact["status"])


def _assert_consistent_runtime_state(
    segment_status: str,
    artifact_status: str,
    target: TerminalSettlementTarget,
) -> None:
    """Require one of the two complete runtime states for first settlement."""

    if (segment_status, artifact_status) not in {
        ("authorized", "waiting"),
        (target.terminal_status, _TERMINAL[target.terminal_status]),
    }:
        raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_CAS_STATE_MISMATCH")


def _existing_certificate(
    connection: sqlite3.Connection, target: TerminalSettlementTarget
) -> sqlite3.Row | None:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_RUNTIME_AUDIT_TABLE,),
    ).fetchone()
    if table_exists is None:
        return None
    rows = connection.execute(
        """SELECT * FROM runtime_send_settlement_audit_v2
           WHERE settlement_id=? OR
                 (pacing_plan_id=? AND segment_index=? AND operation_id=?)""",
        (
            target.settlement_id,
            target.pacing_plan_id,
            target.segment_index,
            target.operation_id,
        ),
    ).fetchall()
    if len(rows) > 1:
        raise SettlementRefused("SEND_SETTLEMENT_CERTIFICATE_AMBIGUOUS")
    return rows[0] if rows else None


def _validate_existing_certificate(
    connection: sqlite3.Connection,
    existing: sqlite3.Row,
    target: TerminalSettlementTarget,
    *,
    evidence_json: str,
    evidence_sha256: str,
    segment_status: str,
    artifact_status: str,
) -> None:
    outbox_statuses = connection.execute(
        """SELECT status FROM hubdb.outbox WHERE outbox_id IN (?,?,?)""",
        (
            target.hub_send_outbox_id,
            target.hub_stable_outbox_id,
            target.hub_draft_outbox_id,
        ),
    ).fetchall()
    if len(outbox_statuses) != 3 or any(
        row["status"] != "cancelled" for row in outbox_statuses
    ):
        raise SettlementRefused("SEND_SETTLEMENT_CERTIFICATE_HUB_STATE_MISMATCH")
    if not _runtime_audit_guards_exist(connection):
        raise SettlementRefused("SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE")
    expected = {
        "settlement_id": target.settlement_id,
        "conversation_id": target.conversation_id,
        "pacing_plan_id": target.pacing_plan_id,
        "segment_index": target.segment_index,
        "operation_id": target.operation_id,
        "authorization_id": target.authorization_id,
        "draft_id": target.draft_id,
        "hub_send_outbox_id": target.hub_send_outbox_id,
        "hub_stable_outbox_id": target.hub_stable_outbox_id,
        "hub_draft_outbox_id": target.hub_draft_outbox_id,
        "binding_id": target.binding_id,
        "binding_revision": target.binding_revision,
        "conversation_revision": target.conversation_revision,
        "body_hash": target.body_hash,
        "terminal_status": target.terminal_status,
        "artifact_status": _TERMINAL[target.terminal_status],
        "operator_id": target.operator_id,
        "reason_code": target.reason_code,
        "evidence_sha256": evidence_sha256,
        "evidence_json": evidence_json,
    }
    existing_columns = set(existing.keys())
    if not set(expected).issubset(existing_columns):
        raise SettlementRefused("SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE")
    if any(existing[key] != value for key, value in expected.items()):
        raise SettlementRefused("SEND_SETTLEMENT_CERTIFICATE_MISMATCH")
    if (
        segment_status != target.terminal_status
        or artifact_status != _TERMINAL[target.terminal_status]
    ):
        raise SettlementRefused("SEND_SETTLEMENT_IDEMPOTENT_STATE_MISMATCH")


def _inspect_settlement_state(
    connection: sqlite3.Connection, target: TerminalSettlementTarget
) -> tuple[dict[str, object], str, str, str, str, sqlite3.Row | None]:
    evidence, segment_status, artifact_status = _collect_evidence(connection, target)
    evidence_json = _canonical(evidence)
    evidence_sha256 = _sha256(evidence_json)
    existing = _existing_certificate(connection, target)
    if existing is not None:
        _validate_existing_certificate(
            connection,
            existing,
            target,
            evidence_json=evidence_json,
            evidence_sha256=evidence_sha256,
            segment_status=segment_status,
            artifact_status=artifact_status,
        )
    else:
        _assert_consistent_runtime_state(segment_status, artifact_status, target)
    return (
        evidence,
        evidence_json,
        evidence_sha256,
        segment_status,
        artifact_status,
        existing,
    )


def settle_terminal_send(
    data_dir: str | Path,
    target: TerminalSettlementTarget,
    *,
    apply: bool = False,
) -> TerminalSettlementResult:
    """Inspect or CAS-settle one exact no-commit operation."""

    target.validate()
    root = Path(data_dir).resolve()
    runtime_path = root / "runtime.sqlite3"
    if any(
        not (root / filename).is_file()
        for filename in (
            "runtime.sqlite3",
            "hub.sqlite3",
            "qq-vm-bridge.sqlite3",
            "pacing.sqlite3",
        )
    ):
        raise SettlementRefused("SEND_SETTLEMENT_DATABASE_MISSING")
    expected_artifact = _TERMINAL[target.terminal_status]
    preflight = sqlite3.connect(runtime_path, isolation_level=None)
    preflight.row_factory = sqlite3.Row
    try:
        _attach(preflight, root)
        preflight.execute("BEGIN IMMEDIATE")
        (
            evidence,
            evidence_json,
            evidence_sha256,
            _segment_status,
            _artifact_status,
            existing,
        ) = _inspect_settlement_state(preflight, target)
        preflight.execute("ROLLBACK")
    except BaseException:
        if preflight.in_transaction:
            preflight.execute("ROLLBACK")
        raise
    finally:
        preflight.close()

    if existing is not None or not apply:
        return TerminalSettlementResult(
            False,
            existing is not None,
            evidence_sha256,
            target.terminal_status,
            expected_artifact,
            bool(evidence["legacy_due_operation_id_missing"]),
        )

    _prepare_atomic_journal_modes(root)
    connection = sqlite3.connect(runtime_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        _attach(connection, root)
        connection.execute("BEGIN IMMEDIATE")
        _assert_atomic_journal_modes(connection)
        (
            locked_evidence,
            locked_evidence_json,
            locked_evidence_sha256,
            locked_segment_status,
            locked_artifact_status,
            locked_existing,
        ) = _inspect_settlement_state(connection, target)
        if locked_existing is not None:
            connection.execute("ROLLBACK")
            return TerminalSettlementResult(
                False,
                True,
                locked_evidence_sha256,
                target.terminal_status,
                expected_artifact,
                bool(locked_evidence["legacy_due_operation_id_missing"]),
            )
        if locked_evidence_json != evidence_json:
            raise SettlementRefused("SEND_SETTLEMENT_PREFLIGHT_CHANGED")

        # All four databases use DELETE journals and remain under this one
        # attached BEGIN IMMEDIATE transaction. The three Hub cancellations,
        # both append-only audits, and the runtime projection commit together.
        _ensure_audit_schema(connection)
        _ensure_hub_outbox_audit_schema(connection, attached=True)
        _settle_hub_outboxes(connection, target, attached=True)
        post_evidence, post_segment_status, post_artifact_status = _collect_evidence(
            connection, target
        )
        if (
            _canonical(post_evidence) != locked_evidence_json
            or (post_segment_status, post_artifact_status)
            != (locked_segment_status, locked_artifact_status)
        ):
            raise SettlementRefused("SEND_SETTLEMENT_TRANSACTION_EVIDENCE_CHANGED")
        if (locked_segment_status, locked_artifact_status) == (
            "authorized",
            "waiting",
        ):
            changed_segment = connection.execute(
                """UPDATE runtime_segment_executions SET status=?
                   WHERE pacing_plan_id=? AND segment_index=? AND status='authorized'
                     AND operation_id=? AND authorization_id=?""",
                (
                    target.terminal_status,
                    target.pacing_plan_id,
                    target.segment_index,
                    target.operation_id,
                    target.authorization_id,
                ),
            ).rowcount
            changed_artifact = connection.execute(
                """UPDATE runtime_plan_artifacts SET status=?
                   WHERE pacing_plan_id=? AND conversation_id=? AND status='waiting'""",
                (expected_artifact, target.pacing_plan_id, target.conversation_id),
            ).rowcount
            if changed_segment != 1 or changed_artifact != 1:
                raise SettlementRefused("SEND_SETTLEMENT_RUNTIME_CAS_LOST")
        connection.execute(
            """INSERT INTO runtime_send_settlement_audit_v2(
                   settlement_id,conversation_id,pacing_plan_id,segment_index,
                   operation_id,authorization_id,draft_id,hub_send_outbox_id,
                   hub_stable_outbox_id,hub_draft_outbox_id,binding_id,
                   binding_revision,conversation_revision,body_hash,terminal_status,
                   artifact_status,operator_id,reason_code,evidence_sha256,
                   evidence_json,settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target.settlement_id,
                target.conversation_id,
                target.pacing_plan_id,
                target.segment_index,
                target.operation_id,
                target.authorization_id,
                target.draft_id,
                target.hub_send_outbox_id,
                target.hub_stable_outbox_id,
                target.hub_draft_outbox_id,
                target.binding_id,
                target.binding_revision,
                target.conversation_revision,
                target.body_hash,
                target.terminal_status,
                expected_artifact,
                target.operator_id,
                target.reason_code,
                locked_evidence_sha256,
                locked_evidence_json,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute("COMMIT")
        return TerminalSettlementResult(
            True,
            False,
            locked_evidence_sha256,
            target.terminal_status,
            expected_artifact,
            bool(locked_evidence["legacy_due_operation_id_missing"]),
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def verify_terminal_settlement(
    data_dir: str | Path,
    *,
    conversation_id: str,
    pacing_plan_id: str,
    segment_index: int,
    operation_id: str,
    terminal_status: str,
) -> bool:
    """Revalidate an existing certificate against all live authorities."""

    runtime_path = Path(data_dir).resolve() / "runtime.sqlite3"
    try:
        with sqlite3.connect(runtime_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT * FROM runtime_send_settlement_audit_v2
                   WHERE conversation_id=? AND pacing_plan_id=? AND segment_index=?
                     AND operation_id=? AND terminal_status=?""",
                (
                    conversation_id,
                    pacing_plan_id,
                    segment_index,
                    operation_id,
                    terminal_status,
                ),
            ).fetchone()
        if row is None:
            return False
        target = TerminalSettlementTarget(
            settlement_id=row["settlement_id"],
            operator_id=row["operator_id"],
            reason_code=row["reason_code"],
            conversation_id=row["conversation_id"],
            pacing_plan_id=row["pacing_plan_id"],
            segment_index=int(row["segment_index"]),
            operation_id=row["operation_id"],
            authorization_id=row["authorization_id"],
            draft_id=row["draft_id"],
            hub_send_outbox_id=row["hub_send_outbox_id"],
            hub_stable_outbox_id=row["hub_stable_outbox_id"],
            hub_draft_outbox_id=row["hub_draft_outbox_id"],
            binding_id=row["binding_id"],
            binding_revision=int(row["binding_revision"]),
            conversation_revision=int(row["conversation_revision"]),
            body_hash=row["body_hash"],
            terminal_status=row["terminal_status"],
            pacing_cancel_reason=json.loads(row["evidence_json"])["pacing"][
                "cancel_reason"
            ],
        )
        result = settle_terminal_send(data_dir, target, apply=False)
        return (
            result.evidence_sha256 == row["evidence_sha256"]
            and _canonical(json.loads(row["evidence_json"])) == row["evidence_json"]
        )
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        sqlite3.Error,
        SettlementRefused,
    ):
        return False


__all__ = [
    "SettlementRefused",
    "TerminalSettlementResult",
    "TerminalSettlementTarget",
    "settle_terminal_send",
    "verify_terminal_settlement",
]
