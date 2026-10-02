"""SQLite-backed deterministic pacing scheduler.

This module intentionally stops at ``DueForRevalidation``.  It neither imports
adapters/outbox/authorization code nor has a send API.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from messenger_ai.domain.clock import ClockPort, SystemClock

from .models import (
    CancellationReason,
    DueForRevalidation,
    PacingPlanRecord,
    PacingStatus,
    RecoveryReport,
    ScheduleOutcome,
    ScheduleRequest,
    clamp,
    grapheme_count,
    next_window_start,
    sanitize_audit_payload,
    segment_gap_seconds,
)

ACTIVE = (PacingStatus.WAITING.value,)

# Migration marker recorded in ``m10_metadata`` once pre-existing audit rows
# have been redacted.  The value is deterministic so replays are cheap no-ops.
AUDIT_REDACTION_MIGRATION_KEY = "m10_audit_payload_redaction"
AUDIT_REDACTION_MIGRATION_VERSION = "redacted-v1"


def migrate_audit_payload_redaction(connection: sqlite3.Connection) -> int:
    """Redact legacy audit payloads without running scheduler schema migrations."""

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not {"m10_audit", "m10_metadata"}.issubset(tables):
        raise RuntimeError("PACING_AUDIT_MIGRATION_SCHEMA_MISSING")
    marker = connection.execute(
        "SELECT value FROM m10_metadata WHERE key=?",
        (AUDIT_REDACTION_MIGRATION_KEY,),
    ).fetchone()
    if marker is not None:
        if marker[0] != AUDIT_REDACTION_MIGRATION_VERSION:
            raise RuntimeError("PACING_AUDIT_MIGRATION_VERSION_MISMATCH")
        return 0
    connection.execute("BEGIN IMMEDIATE")
    rewritten = 0
    try:
        rows = connection.execute(
            "SELECT audit_id,payload_json FROM m10_audit"
        ).fetchall()
        for row in rows:
            try:
                stored = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                stored = row["payload_json"]
            sanitized = json.dumps(
                sanitize_audit_payload(stored), sort_keys=True, default=str
            )
            if sanitized != row["payload_json"]:
                rewritten += connection.execute(
                    "UPDATE m10_audit SET payload_json=? WHERE audit_id=?",
                    (sanitized, row["audit_id"]),
                ).rowcount
        connection.execute(
            "INSERT INTO m10_metadata(key,value) VALUES(?,?)",
            (AUDIT_REDACTION_MIGRATION_KEY, AUDIT_REDACTION_MIGRATION_VERSION),
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
    return rewritten


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ClaimedDueOutbox:
    outbox_id: int
    due: DueForRevalidation
    claim_token: str
    claimed_at: datetime
    recoverable: bool


@dataclass(frozen=True, slots=True)
class DueClaimState:
    eligible: bool
    error_code: str | None = None
    expires_at: datetime | None = None
    plan_status: str | None = None
    operation_id: str | None = None


class PacingScheduler:
    """Transactional plan store with an idempotent, pull-based due queue."""

    def __init__(
        self, path: str | Path = ":memory:", *, clock: ClockPort | None = None
    ) -> None:
        self.path = str(path)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS m10_plans (
                  pacing_plan_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  contact_id TEXT NOT NULL, status TEXT NOT NULL, earliest_send_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                  one_shot_attempt_id TEXT, due_emitted_at TEXT, cancel_reason TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS m10_one_active_conversation
                  ON m10_plans(conversation_id) WHERE status='waiting';
                CREATE INDEX IF NOT EXISTS m10_due ON m10_plans(status, earliest_send_at);
                CREATE TABLE IF NOT EXISTS m10_audit (
                  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, action TEXT NOT NULL,
                  reason_code TEXT NOT NULL, payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m10_rate_events (
                  pacing_plan_id TEXT NOT NULL, contact_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
                  PRIMARY KEY(pacing_plan_id, occurred_at)
                );
                CREATE TABLE IF NOT EXISTS m10_due_outbox (
                  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  pacing_plan_id TEXT NOT NULL,
                  segment_index INTEGER NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  operation_id TEXT,
                  claimed_at TEXT,
                  delivered_at TEXT,
                  created_at TEXT NOT NULL,
                  one_shot_attempt_id TEXT,
                  UNIQUE(pacing_plan_id, segment_index)
                );
                CREATE TABLE IF NOT EXISTS m10_segment_receipts (
                  pacing_plan_id TEXT NOT NULL,
                  segment_index INTEGER NOT NULL,
                  operation_id TEXT NOT NULL,
                  verified INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(pacing_plan_id, segment_index),
                  UNIQUE(operation_id)
                );
                CREATE TABLE IF NOT EXISTS m10_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            plan_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(m10_plans)")
            }
            if "one_shot_attempt_id" not in plan_columns:
                self.connection.execute(
                    "ALTER TABLE m10_plans ADD COLUMN one_shot_attempt_id TEXT"
                )
            outbox_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(m10_due_outbox)")
            }
            if "one_shot_attempt_id" not in outbox_columns:
                self.connection.execute(
                    "ALTER TABLE m10_due_outbox ADD COLUMN one_shot_attempt_id TEXT"
                )
            for name, declaration in (
                ("not_before", "TEXT"), ("claim_token", "TEXT"), ("defer_reason", "TEXT"),
            ):
                if name not in outbox_columns:
                    self.connection.execute(f"ALTER TABLE m10_due_outbox ADD COLUMN {name} {declaration}")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS m10_due_provenance "
                "ON m10_plans(one_shot_attempt_id, status, earliest_send_at)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS m10_due_outbox_provenance "
                "ON m10_due_outbox(one_shot_attempt_id, status, outbox_id)"
            )
            migrate_audit_payload_redaction(self.connection)

    @contextmanager
    def _uow(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def now(self) -> datetime:
        return _utc(self.clock.now())

    @staticmethod
    def _reply_segments(request: ScheduleRequest) -> tuple[str, ...]:
        if request.reply_plan.action == "ignore":
            return ()
        supplied = tuple(item for item in request.reply_plan.reply_segments if item)
        if supplied:
            return supplied
        return (request.reply_plan.reply_text,) if request.reply_plan.reply_text else ()

    def schedule(self, request: ScheduleRequest) -> ScheduleOutcome:
        """Persist a plan.  A zero-segment / ignore decision is deliberately no-op."""
        if request.paused:
            return ScheduleOutcome(action="blocked", reason_code="PAUSED")
        if not request.capability_healthy:
            return ScheduleOutcome(action="blocked", reason_code="CAPABILITY_UNHEALTHY")
        segments = self._reply_segments(request)
        if not segments:
            return ScheduleOutcome(action="ignored", reason_code="REPLYPLAN_IGNORE")
        if len(segments) > request.limits.auto_reply_max_segments:
            return ScheduleOutcome(
                action="manual_review", reason_code="TOO_MANY_SEGMENTS"
            )
        all_text = "".join(segments)
        if grapheme_count(all_text) > request.limits.auto_reply_max_chars:
            return ScheduleOutcome(
                action="manual_review", reason_code="AUTO_REPLY_TOO_LONG"
            )

        profile = request.profile
        reading = clamp(
            grapheme_count(request.inbound_text) / profile.reading_chars_per_second,
            profile.min_reading_seconds,
            profile.max_reading_seconds,
        )
        composition = clamp(
            grapheme_count(all_text) / profile.composition_chars_per_second,
            profile.min_composition_seconds,
            profile.max_composition_seconds,
        )
        quiet_until = request.last_inbound_at + timedelta(
            seconds=profile.quiet_window_seconds
        )
        candidates = [
            quiet_until,
            request.first_inbound_at
            + timedelta(seconds=profile.hard_min_latency_seconds),
            request.first_inbound_at + timedelta(seconds=reading + composition),
        ]
        if grapheme_count(all_text) >= profile.long_reply_threshold_chars:
            candidates.append(
                request.first_inbound_at
                + timedelta(seconds=profile.long_reply_min_latency_seconds)
            )
        if request.last_outbound_at is not None:
            candidates.append(
                request.last_outbound_at
                + timedelta(seconds=profile.min_inter_reply_gap_seconds)
            )
        base_due = max(candidates)
        due = next_window_start(base_due, request.limits.allowed_time_windows)
        expires = request.first_inbound_at + timedelta(seconds=profile.plan_ttl_seconds)
        if due >= expires:
            return ScheduleOutcome(
                action="manual_review", reason_code="WINDOW_AFTER_EXPIRY"
            )
        gaps = [
            segment_gap_seconds(segment, request.limits) for segment in segments[:-1]
        ]
        plan = PacingPlanRecord(
            pacing_plan_id=request.reserved_pacing_plan_id or uuid4(),
            one_shot_attempt_id=request.one_shot_attempt_id,
            conversation_id=request.draft.conversation_id,
            contact_id=request.draft.contact_id,
            draft_id=request.draft.draft_id,
            text_hash=request.draft.text_hash,
            expected_last_message_key=request.draft.expected_last_message_key,
            source_message_keys=request.source_message_keys,
            rule_version=request.draft.rule_version,
            pacing_rule_version=request.draft.rule_version,
            eligibility_id=request.draft.eligibility_id,
            segment_eligibility_ids=request.segment_eligibility_ids
            or (request.draft.eligibility_id,) * len(segments),
            segment_draft_ids=request.segment_draft_ids
            or (request.draft.draft_id,) * len(segments),
            created_at=self.now(),
            quiet_until=quiet_until,
            earliest_send_at=due,
            expires_at=expires,
            reading_delay_ms=round(reading * 1000),
            composition_delay_ms=round(composition * 1000),
            inter_message_gap_ms=round((gaps[0] if gaps else 0) * 1000),
            segment_count=len(segments),
            segments=segments,
            cancellation_conditions=tuple(CancellationReason),
            formula_inputs={
                "inbound_graphemes": grapheme_count(request.inbound_text),
                "reply_graphemes": grapheme_count(all_text),
                "hard_min_seconds": profile.hard_min_latency_seconds,
                "long_reply_threshold_chars": profile.long_reply_threshold_chars,
                "long_reply_min_latency_seconds": profile.long_reply_min_latency_seconds,
            },
            limits=request.limits,
        )
        with self._uow() as conn:
            self._cancel_active(
                conn, plan.conversation_id, CancellationReason.SUPERSEDED
            )
            now = self.now()
            conn.execute(
                """INSERT INTO m10_plans(
                       pacing_plan_id, conversation_id, contact_id, status,
                       earliest_send_at, expires_at, payload_json,
                       one_shot_attempt_id, due_emitted_at, cancel_reason,
                       created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(plan.pacing_plan_id),
                    plan.conversation_id,
                    plan.contact_id,
                    plan.status.value,
                    _stamp(plan.earliest_send_at),
                    _stamp(plan.expires_at),
                    plan.model_dump_json(),
                    str(plan.one_shot_attempt_id)
                    if plan.one_shot_attempt_id is not None
                    else None,
                    None,
                    None,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            self._audit(
                conn,
                str(plan.pacing_plan_id),
                "plan_created",
                "PACED",
                plan.model_dump(mode="json"),
                now,
            )
        return ScheduleOutcome(action="scheduled", reason_code="PACED", plan=plan)

    def cancel(self, conversation_id: str, reason: CancellationReason) -> int:
        """Cancels each currently valid plan in one conversation with a reason code."""
        with self._uow() as conn:
            return self._cancel_active(conn, conversation_id, reason)

    def cancel_plan(
        self, pacing_plan_id: UUID | str, reason: CancellationReason
    ) -> int:
        """Cancel only the named active plan, returning whether it changed."""
        with self._uow() as conn:
            return int(self._cancel_id(conn, str(pacing_plan_id), reason))

    def invalidate_rule_version(self, rule_version: str) -> int:
        return self._cancel_where(
            "rule_version_changed", "RULE_VERSION_CHANGED", rule_version=rule_version
        )

    def on_new_inbound(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.NEW_INBOUND)

    def on_user_takeover(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.USER_TAKEOVER)

    def on_draft_changed(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.DRAFT_CHANGED)

    def on_capability_changed(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.CAPABILITY_CHANGED)

    def on_health_changed(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.HEALTH_CHANGED)

    def on_paused(self, conversation_id: str) -> int:
        return self.cancel(conversation_id, CancellationReason.PAUSED)

    def invalidate_contact(self, contact_id: str, reason: CancellationReason) -> int:
        with self._uow() as conn:
            rows = conn.execute(
                "SELECT pacing_plan_id FROM m10_plans WHERE contact_id=? AND status='waiting'",
                (contact_id,),
            ).fetchall()
            for row in rows:
                self._cancel_id(conn, row["pacing_plan_id"], reason)
            return len(rows)

    def _cancel_where(self, action: str, reason_code: str, *, rule_version: str) -> int:
        with self._uow() as conn:
            rows = conn.execute(
                "SELECT pacing_plan_id,payload_json FROM m10_plans WHERE status='waiting'"
            ).fetchall()
            selected = [
                row
                for row in rows
                if json.loads(row["payload_json"])["rule_version"] == rule_version
            ]
            for row in selected:
                self._cancel_id(
                    conn, row["pacing_plan_id"], CancellationReason.RULE_VERSION_CHANGED
                )
            return len(selected)

    def _cancel_active(
        self, conn: sqlite3.Connection, conversation_id: str, reason: CancellationReason
    ) -> int:
        rows = conn.execute(
            "SELECT pacing_plan_id FROM m10_plans WHERE conversation_id=? AND status IN ('waiting','due_for_revalidation')",
            (conversation_id,),
        ).fetchall()
        for row in rows:
            self._cancel_id(conn, row["pacing_plan_id"], reason)
        return len(rows)

    def _cancel_id(
        self, conn: sqlite3.Connection, plan_id: str, reason: CancellationReason
    ) -> bool:
        now = self.now()
        changed = conn.execute(
            "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=? AND status IN ('waiting','due_for_revalidation')",
            (PacingStatus.CANCELLED.value, reason.value, _stamp(now), plan_id),
        ).rowcount
        if changed:
            self._audit(conn, plan_id, "plan_cancelled", reason.value, {}, now)
        return bool(changed)

    def due_for_revalidation(
        self,
        *,
        pacing_plan_id: UUID | str | None = None,
        one_shot_attempt_id: UUID | str | None = None,
    ) -> list[DueForRevalidation]:
        """Atomically emit due events once, optionally for one exact plan only.

        Calling this cannot authorize or send.
        """
        if one_shot_attempt_id is not None and pacing_plan_id is None:
            raise ValueError("one_shot_attempt_id requires pacing_plan_id")
        now = self.now()
        due_events: list[DueForRevalidation] = []
        with self._uow() as conn:
            exact_plan_id = str(pacing_plan_id) if pacing_plan_id is not None else None
            exact_attempt_id = (
                str(one_shot_attempt_id) if one_shot_attempt_id is not None else None
            )
            attempt_clause, attempt_parameters = self._attempt_filter(exact_attempt_id)
            self._handle_clock_jump(
                conn,
                now,
                pacing_plan_id=exact_plan_id,
                one_shot_attempt_id=exact_attempt_id,
            )
            if exact_plan_id is None:
                rows = conn.execute(
                    "SELECT * FROM m10_plans WHERE status='waiting' "
                    "AND earliest_send_at<=? AND one_shot_attempt_id IS NULL "
                    "ORDER BY earliest_send_at,pacing_plan_id",
                    (_stamp(now),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM m10_plans WHERE pacing_plan_id=? "
                    "AND status='waiting' AND earliest_send_at<=? AND "
                    + attempt_clause,
                    (exact_plan_id, _stamp(now), *attempt_parameters),
                ).fetchall()
            for row in rows:
                if _parse(row["expires_at"]) <= now:
                    conn.execute(
                        "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? "
                        "WHERE pacing_plan_id=? AND status='waiting' AND "
                        + attempt_clause,
                        (
                            PacingStatus.EXPIRED.value,
                            CancellationReason.EXPIRED.value,
                            _stamp(now),
                            row["pacing_plan_id"],
                            *attempt_parameters,
                        ),
                    )
                    self._audit(
                        conn,
                        row["pacing_plan_id"],
                        "plan_expired",
                        CancellationReason.EXPIRED.value,
                        {},
                        now,
                    )
                    continue
                plan = PacingPlanRecord.model_validate_json(row["payload_json"])
                if not self._rate_available(conn, plan, now):
                    # Never stretch a stale automatic message beyond its expiry just to fit a quota.
                    self._cancel_id(
                        conn, row["pacing_plan_id"], CancellationReason.EXPIRED
                    )
                    continue
                changed = conn.execute(
                    "UPDATE m10_plans SET status=?,due_emitted_at=?,updated_at=? "
                    "WHERE pacing_plan_id=? AND status='waiting' AND " + attempt_clause,
                    (
                        PacingStatus.DUE_FOR_REVALIDATION.value,
                        _stamp(now),
                        _stamp(now),
                        row["pacing_plan_id"],
                        *attempt_parameters,
                    ),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    "INSERT INTO m10_rate_events VALUES(?,?,?)",
                    (row["pacing_plan_id"], plan.contact_id, _stamp(now)),
                )
                due = DueForRevalidation(
                    pacing_plan_id=plan.pacing_plan_id,
                    one_shot_attempt_id=plan.one_shot_attempt_id,
                    conversation_id=plan.conversation_id,
                    contact_id=plan.contact_id,
                    draft_id=plan.segment_draft_ids[plan.segment_index],
                    text_hash=plan.text_hash,
                    expected_last_message_key=plan.expected_last_message_key,
                    rule_version=plan.rule_version,
                    pacing_rule_version=plan.pacing_rule_version,
                    eligibility_id=plan.segment_eligibility_ids[plan.segment_index],
                    due_at=now,
                    segment_index=plan.segment_index,
                    segment_count=plan.segment_count,
                    body=plan.segments[plan.segment_index],
                    body_hash=hashlib.sha256(
                        plan.segments[plan.segment_index].encode()
                    ).hexdigest(),
                )
                self._audit(
                    conn,
                    row["pacing_plan_id"],
                    "due_for_revalidation",
                    "PACING_DUE",
                    due.model_dump(mode="json"),
                    now,
                )
                conn.execute(
                    """INSERT OR IGNORE INTO m10_due_outbox(
                           pacing_plan_id, segment_index, payload_json, created_at,
                           one_shot_attempt_id
                       ) VALUES(?,?,?,?,?)""",
                    (
                        str(plan.pacing_plan_id),
                        plan.segment_index,
                        due.model_dump_json(),
                        _stamp(now),
                        str(plan.one_shot_attempt_id)
                        if plan.one_shot_attempt_id is not None
                        else None,
                    ),
                )
                due_events.append(due)
        return due_events

    def claim_due_outbox(
        self,
        *,
        limit: int = 100,
        pacing_plan_id: UUID | str | None = None,
        segment_index: int | None = None,
        one_shot_attempt_id: UUID | str | None = None,
        recoverable: bool = True,
    ) -> list[tuple[int, DueForRevalidation]]:
        return [(claim.outbox_id, claim.due) for claim in self.claim_due_outbox_with_tokens(
            limit=limit, pacing_plan_id=pacing_plan_id, segment_index=segment_index,
            one_shot_attempt_id=one_shot_attempt_id, recoverable=recoverable,
        )]

    def claim_due_outbox_with_tokens(
        self,
        *,
        limit: int = 100,
        pacing_plan_id: UUID | str | None = None,
        segment_index: int | None = None,
        one_shot_attempt_id: UUID | str | None = None,
        recoverable: bool = True,
    ) -> list[ClaimedDueOutbox]:
        """Claim durable due events for runtime delivery.

        Claims survive process failure: ``recover_due_outbox`` returns abandoned
        claims to pending without creating a second segment event.  Supplying a
        plan and segment restricts the transaction to that exact outbox row.
        """
        if (pacing_plan_id is None) != (segment_index is None):
            raise ValueError(
                "pacing_plan_id and segment_index must be supplied together"
            )
        if one_shot_attempt_id is not None and pacing_plan_id is None:
            raise ValueError(
                "one_shot_attempt_id requires pacing_plan_id and segment_index"
            )
        if not isinstance(recoverable, bool):
            raise TypeError("recoverable must be a boolean")
        now = self.now()
        with self._uow() as conn:
            predicates = ["status='pending'", "(not_before IS NULL OR not_before<=?)"]
            parameters: list[object] = [_stamp(now)]
            exact_attempt_id = (
                str(one_shot_attempt_id) if one_shot_attempt_id is not None else None
            )
            attempt_clause, attempt_parameters = self._attempt_filter(exact_attempt_id)
            if pacing_plan_id is None:
                predicates.append("one_shot_attempt_id IS NULL")
            else:
                predicates.append("pacing_plan_id=?")
                parameters.append(str(pacing_plan_id))
                predicates.append("segment_index=?")
                parameters.append(segment_index)
                predicates.append(attempt_clause)
                parameters.extend(attempt_parameters)
            parameters.append(limit)
            rows = conn.execute(
                "SELECT outbox_id,payload_json FROM m10_due_outbox WHERE "
                + " AND ".join(predicates)
                + " ORDER BY outbox_id LIMIT ?",
                parameters,
            ).fetchall()
            result: list[ClaimedDueOutbox] = []
            dispatch_status = (
                "dispatching" if recoverable else "dispatching_nonrecoverable"
            )
            for row in rows:
                claim_token = str(uuid4())
                changed = conn.execute(
                    "UPDATE m10_due_outbox SET status=?,claimed_at=?,claim_token=? "
                    "WHERE outbox_id=? AND status='pending' AND " + attempt_clause,
                    (
                        dispatch_status,
                        _stamp(now),
                        claim_token,
                        row["outbox_id"],
                        *attempt_parameters,
                    ),
                ).rowcount
                if changed:
                    result.append(ClaimedDueOutbox(
                        outbox_id=row["outbox_id"],
                        due=DueForRevalidation.model_validate_json(row["payload_json"]),
                        claim_token=claim_token, claimed_at=now, recoverable=recoverable,
                    ))
            return result

    @staticmethod
    def _claim_parameters(claim: ClaimedDueOutbox) -> tuple[object, ...]:
        return (claim.outbox_id, str(claim.due.pacing_plan_id), claim.due.segment_index,
                claim.claim_token, "dispatching" if claim.recoverable else "dispatching_nonrecoverable")

    def due_claim_state(self, claim: ClaimedDueOutbox) -> DueClaimState:
        """Read real plan status and immutable due membership, without renewing it."""
        with self._lock:
            row = self.connection.execute(
                "SELECT o.payload_json AS due_json,o.operation_id,p.status,p.expires_at,p.payload_json "
                "FROM m10_due_outbox o JOIN m10_plans p ON p.pacing_plan_id=o.pacing_plan_id "
                "WHERE o.outbox_id=? AND o.pacing_plan_id=? AND o.segment_index=? "
                "AND o.claim_token=? AND o.status=?",
                self._claim_parameters(claim),
            ).fetchone()
        if row is None:
            return DueClaimState(False, "due_claim_stale")
        due = DueForRevalidation.model_validate_json(row["due_json"])
        plan = PacingPlanRecord.model_validate_json(row["payload_json"])
        if due != claim.due or plan.one_shot_attempt_id != due.one_shot_attempt_id:
            return DueClaimState(False, "due_claim_provenance_mismatch")
        expires = _parse(row["expires_at"])
        state = dict(expires_at=expires, plan_status=row["status"], operation_id=row["operation_id"])
        if (plan.conversation_id != due.conversation_id or plan.contact_id != due.contact_id
                or plan.segment_index != due.segment_index or plan.segment_count != due.segment_count
                or due.segment_index < 0 or due.segment_index >= len(plan.segment_draft_ids)
                or due.segment_index >= len(plan.segments)
                or plan.segment_draft_ids[due.segment_index] != due.draft_id
                or plan.segments[due.segment_index] != due.body
                or hashlib.sha256(due.body.encode()).hexdigest() != due.body_hash
                or plan.expected_last_message_key != due.expected_last_message_key
                or plan.text_hash != due.text_hash or plan.rule_version != due.rule_version
                or plan.pacing_rule_version != due.pacing_rule_version
                or (plan.segment_eligibility_ids and (
                    due.segment_index >= len(plan.segment_eligibility_ids)
                    or plan.segment_eligibility_ids[due.segment_index] != due.eligibility_id))
                or (not plan.segment_eligibility_ids and plan.eligibility_id != due.eligibility_id)):
            return DueClaimState(False, "due_plan_membership_mismatch", **state)
        if plan.expires_at != expires:
            return DueClaimState(False, "due_plan_expiry_mismatch", **state)
        if row["operation_id"] is not None:
            return DueClaimState(False, "due_operation_exists", **state)
        if row["status"] != PacingStatus.DUE_FOR_REVALIDATION.value:
            return DueClaimState(False, "due_plan_not_eligible", **state)
        if expires <= self.now():
            return DueClaimState(False, "due_plan_expired", **state)
        return DueClaimState(True, **state)

    def is_due_claim_current(self, claim: ClaimedDueOutbox) -> bool:
        """Fence a result independently of plan expiry or existing operation state."""
        attempt_clause, attempt_parameters = self._attempt_filter(
            str(claim.due.one_shot_attempt_id) if claim.due.one_shot_attempt_id else None,
        )
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM m10_due_outbox WHERE outbox_id=? "
                "AND pacing_plan_id=? AND segment_index=? AND claim_token=? AND status=? AND "
                + attempt_clause,
                (*self._claim_parameters(claim), *attempt_parameters),
            ).fetchone()
        return row is not None and DueForRevalidation.model_validate_json(row["payload_json"]) == claim.due

    def defer_due_outbox(self, claim: ClaimedDueOutbox, *, not_before: datetime | None,
                         reason: str, needs_attention: bool = False) -> bool:
        """Return one unsent claim to waiting, or hold it for explicit attention.

        The claim nonce fences stale results after recovery/reclaim. This never
        emits another due, changes a plan/segment/expiry, or touches send facts.
        """
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason) is None:
            raise ValueError("defer reason must be a bounded local code")
        now = self.now()
        retry = max(_utc(not_before) if not_before else now, now + timedelta(seconds=10))
        attempt_clause, attempt_parameters = self._attempt_filter(
            str(claim.due.one_shot_attempt_id) if claim.due.one_shot_attempt_id else None,
        )
        with self._uow() as conn:
            changed = conn.execute(
                "UPDATE m10_due_outbox SET status=?,claimed_at=NULL,not_before=?,defer_reason=? "
                "WHERE outbox_id=? AND pacing_plan_id=? AND segment_index=? AND claim_token=? "
                "AND status=? AND operation_id IS NULL AND " + attempt_clause,
                ("navigation_attention" if needs_attention else "pending", _stamp(retry), reason,
                 *self._claim_parameters(claim), *attempt_parameters),
            ).rowcount
            if changed:
                self._audit(conn, str(claim.due.pacing_plan_id), "due_navigation_held" if needs_attention
                            else "due_navigation_deferred", reason, {"segment_index": claim.due.segment_index}, now)
            return changed == 1

    def release_navigation_hold(self, outbox_id: int, *, expected_claim_token: str,
                                reason: str = "navigation_operator_released") -> bool:
        """Explicitly release a navigation hold; expired/cancelled work stays held."""
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason) is None:
            raise ValueError("release reason must be a bounded local code")
        now = self.now()
        with self._uow() as conn:
            changed = conn.execute(
                "UPDATE m10_due_outbox SET status='pending',not_before=?,defer_reason=? "
                "WHERE outbox_id=? AND claim_token=? AND status='navigation_attention' "
                "AND operation_id IS NULL AND EXISTS(SELECT 1 FROM m10_plans p "
                "WHERE p.pacing_plan_id=m10_due_outbox.pacing_plan_id AND p.status=? AND p.expires_at>?)",
                (_stamp(now + timedelta(seconds=10)), reason, outbox_id, expected_claim_token,
                 PacingStatus.DUE_FOR_REVALIDATION.value, _stamp(now)),
            ).rowcount
            return changed == 1

    def hold_due_outbox_for_recovery(self, claim: ClaimedDueOutbox, *,
                                     operation_id: UUID | str | None,
                                     reason: str = "navigation_existing_operation") -> bool:
        """Keep an existing operation out of the normal navigation/prepare queue."""
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason) is None:
            raise ValueError("recovery reason must be a bounded local code")
        operation = str(operation_id) if operation_id is not None else None
        attempt_clause, attempt_parameters = self._attempt_filter(
            str(claim.due.one_shot_attempt_id) if claim.due.one_shot_attempt_id else None,
        )
        with self._uow() as conn:
            return conn.execute(
                "UPDATE m10_due_outbox SET status='operation_recovery_hold',claimed_at=NULL,defer_reason=?,operation_id=? "
                "WHERE outbox_id=? AND pacing_plan_id=? AND segment_index=? AND claim_token=? AND status=? "
                "AND (operation_id IS NULL OR operation_id IS ?) AND " + attempt_clause,
                (reason, operation, *self._claim_parameters(claim), operation, *attempt_parameters),
            ).rowcount == 1

    def complete_due_outbox(self, outbox_id: int) -> bool:
        with self._uow() as conn:
            changed = conn.execute(
                """UPDATE m10_due_outbox SET status='delivered',delivered_at=?
                   WHERE outbox_id=? AND status IN ('dispatching','dispatching_nonrecoverable')""",
                (_stamp(self.now()), outbox_id),
            ).rowcount
            return bool(changed)

    def recover_due_outbox(self) -> int:
        with self._uow() as conn:
            return conn.execute(
                "UPDATE m10_due_outbox SET status='pending',claimed_at=NULL,claim_token=NULL WHERE status='dispatching'"
            ).rowcount

    def record_revalidation_result(
        self,
        pacing_plan_id: UUID,
        *,
        segment_sent_and_verified: bool,
        segment_index: int | None = None,
        operation_id: UUID | str | None = None,
    ) -> PacingPlanRecord | None:
        """Advance only a verified semantic segment.

        M9/Outbox calls this after its own authorization and send verification.
        A denial, cancellation, or uncertain outcome stops every remaining
        segment.  This is acknowledgement bookkeeping, never a send command.
        """
        with self._uow() as conn:
            return self._record_revalidation_result(
                conn,
                pacing_plan_id=str(pacing_plan_id),
                segment_sent_and_verified=segment_sent_and_verified,
                segment_index=segment_index,
                operation_id=str(operation_id) if operation_id is not None else None,
            )

    def record_revalidation_result_and_complete_due_outbox(
        self,
        outbox_id: int,
        pacing_plan_id: UUID | str,
        *,
        segment_sent_and_verified: bool,
        segment_index: int,
        operation_id: UUID | str | None = None,
        one_shot_attempt_id: UUID | str | None = None,
        expected_claim_token: str | None = None,
    ) -> PacingPlanRecord | None:
        """Record an exact result and deliver its claimed outbox row atomically."""
        plan_id = str(pacing_plan_id)
        exact_operation_id = str(operation_id) if operation_id is not None else None
        exact_attempt_id = (
            str(one_shot_attempt_id) if one_shot_attempt_id is not None else None
        )
        attempt_clause, attempt_parameters = self._attempt_filter(exact_attempt_id)
        with self._uow() as conn:
            outbox = conn.execute(
                "SELECT status,operation_id,claim_token FROM m10_due_outbox "
                "WHERE outbox_id=? AND pacing_plan_id=? AND segment_index=? AND "
                + attempt_clause,
                (
                    outbox_id,
                    plan_id,
                    segment_index,
                    *attempt_parameters,
                ),
            ).fetchone()
            # Optional navigation claims fence *all* acknowledgement effects,
            # including the plan update/receipt, in this same transaction.
            # Legacy callers retain their original idempotent completion path.
            if expected_claim_token is not None and (
                outbox is None or outbox["claim_token"] != expected_claim_token
            ):
                return None
            if outbox is None or outbox["status"] not in {
                "dispatching",
                "dispatching_nonrecoverable",
                "delivered",
            }:
                return None
            if outbox["operation_id"] not in {None, exact_operation_id}:
                return None
            result = self._record_revalidation_result(
                conn,
                pacing_plan_id=plan_id,
                segment_sent_and_verified=segment_sent_and_verified,
                segment_index=segment_index,
                operation_id=exact_operation_id,
                one_shot_attempt_id=exact_attempt_id,
                enforce_one_shot_provenance=True,
            )
            if result is None:
                return None
            if outbox["status"] == "delivered":
                return result
            changed = conn.execute(
                "UPDATE m10_due_outbox SET status='delivered',operation_id=?,"
                "delivered_at=? WHERE outbox_id=? AND pacing_plan_id=? "
                "AND segment_index=? AND status IN "
                "('dispatching','dispatching_nonrecoverable') AND "
                + attempt_clause
                + " AND (operation_id IS NULL OR operation_id=?)",
                (
                    exact_operation_id,
                    _stamp(self.now()),
                    outbox_id,
                    plan_id,
                    segment_index,
                    *attempt_parameters,
                    exact_operation_id,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("exact due outbox completion lost its claim")
            return result

    def _record_revalidation_result(
        self,
        conn: sqlite3.Connection,
        *,
        pacing_plan_id: str,
        segment_sent_and_verified: bool,
        segment_index: int | None,
        operation_id: str | None,
        one_shot_attempt_id: str | None = None,
        enforce_one_shot_provenance: bool = False,
    ) -> PacingPlanRecord | None:
        if enforce_one_shot_provenance:
            attempt_clause, attempt_parameters = self._attempt_filter(
                one_shot_attempt_id
            )
            row = conn.execute(
                "SELECT * FROM m10_plans WHERE pacing_plan_id=? AND " + attempt_clause,
                (pacing_plan_id, *attempt_parameters),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM m10_plans WHERE pacing_plan_id=?",
                (pacing_plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = PacingPlanRecord.model_validate_json(row["payload_json"])
        serialized_attempt_id = (
            str(plan.one_shot_attempt_id)
            if plan.one_shot_attempt_id is not None
            else None
        )
        if enforce_one_shot_provenance and serialized_attempt_id != one_shot_attempt_id:
            return None
        expected_index = plan.segment_index
        if operation_id is not None:
            receipt = conn.execute(
                "SELECT pacing_plan_id,segment_index,verified "
                "FROM m10_segment_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if receipt is not None:
                # Exact replay is idempotent; cross-segment/result reuse is rejected.
                if (
                    receipt["pacing_plan_id"] != pacing_plan_id
                    or (
                        segment_index is not None
                        and receipt["segment_index"] != segment_index
                    )
                    or bool(receipt["verified"]) != segment_sent_and_verified
                ):
                    return None
                return plan
        if segment_index is not None and segment_index != expected_index:
            return None
        if row["status"] != PacingStatus.DUE_FOR_REVALIDATION.value:
            return None
        now = self.now()
        if operation_id is not None:
            conn.execute(
                """INSERT INTO m10_segment_receipts(
                       pacing_plan_id, segment_index, operation_id, verified,
                       created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    pacing_plan_id,
                    expected_index,
                    operation_id,
                    int(segment_sent_and_verified),
                    _stamp(now),
                ),
            )
        if (
            not segment_sent_and_verified
            or plan.segment_index + 1 >= plan.segment_count
        ):
            status = (
                PacingStatus.REJECTED
                if not segment_sent_and_verified
                else PacingStatus.COMPLETED
            )
            reason = (
                CancellationReason.REVALIDATION_REJECTED
                if not segment_sent_and_verified
                else "COMPLETE"
            )
            conn.execute(
                "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? "
                "WHERE pacing_plan_id=?",
                (
                    status.value,
                    reason.value if hasattr(reason, "value") else reason,
                    _stamp(now),
                    pacing_plan_id,
                ),
            )
            self._audit(
                conn,
                pacing_plan_id,
                "remaining_segments_stopped"
                if not segment_sent_and_verified
                else "all_segments_complete",
                reason.value if hasattr(reason, "value") else reason,
                {},
                now,
            )
            return plan
        gap = segment_gap_seconds(plan.segments[plan.segment_index], plan.limits)
        next_due = now + timedelta(seconds=gap)
        if next_due >= plan.expires_at:
            conn.execute(
                "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? "
                "WHERE pacing_plan_id=?",
                (
                    PacingStatus.EXPIRED.value,
                    CancellationReason.EXPIRED.value,
                    _stamp(now),
                    pacing_plan_id,
                ),
            )
            self._audit(
                conn,
                pacing_plan_id,
                "plan_expired",
                CancellationReason.EXPIRED.value,
                {},
                now,
            )
            return plan
        next_plan = plan.model_copy(
            update={
                "segment_index": plan.segment_index + 1,
                "earliest_send_at": next_due,
                "status": PacingStatus.WAITING,
            }
        )
        conn.execute(
            "UPDATE m10_plans SET status=?,earliest_send_at=?,payload_json=?,"
            "updated_at=? WHERE pacing_plan_id=?",
            (
                PacingStatus.WAITING.value,
                _stamp(next_due),
                next_plan.model_dump_json(),
                _stamp(now),
                pacing_plan_id,
            ),
        )
        self._audit(
            conn,
            pacing_plan_id,
            "next_segment_scheduled",
            "SEMANTIC_SEGMENT_GAP",
            {"gap_seconds": gap, "segment_index": next_plan.segment_index},
            now,
        )
        return next_plan

    def recover(self, *, jump_tolerance_seconds: float = 300) -> RecoveryReport:
        """On a detected discontinuity, hold plans instead of emitting a burst."""
        now = self.now()
        with self._uow() as conn:
            held = self._handle_clock_jump(conn, now, tolerance=jump_tolerance_seconds)
        return RecoveryReport(
            held_plan_ids=tuple(UUID(item) for item in held),
            reason_code="clock_jump" if held else "normal_restart",
        )

    def _handle_clock_jump(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        *,
        tolerance: float = 300,
        pacing_plan_id: str | None = None,
        one_shot_attempt_id: str | None = None,
    ) -> list[str]:
        attempt_clause, attempt_parameters = self._attempt_filter(one_shot_attempt_id)
        plans: list[sqlite3.Row] | None = None
        if pacing_plan_id is not None:
            plans = conn.execute(
                "SELECT pacing_plan_id FROM m10_plans WHERE pacing_plan_id=? "
                "AND status='waiting' AND " + attempt_clause,
                (pacing_plan_id, *attempt_parameters),
            ).fetchall()
            if not plans:
                return []
        metadata_key = (
            "last_clock"
            if pacing_plan_id is None
            else f"last_clock_exact:{pacing_plan_id}:{one_shot_attempt_id or 'regular'}"
        )
        row = conn.execute(
            "SELECT value FROM m10_metadata WHERE key=?", (metadata_key,)
        ).fetchone()
        if row is None and pacing_plan_id is not None:
            row = conn.execute(
                "SELECT value FROM m10_metadata WHERE key='last_clock'"
            ).fetchone()
        conn.execute(
            "INSERT INTO m10_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (metadata_key, _stamp(now)),
        )
        if row is None:
            return []
        delta = (now - _parse(row["value"])).total_seconds()
        if 0 <= delta <= tolerance:
            return []
        if plans is None:
            plans = conn.execute(
                "SELECT pacing_plan_id FROM m10_plans WHERE status='waiting' "
                "AND one_shot_attempt_id IS NULL"
            ).fetchall()
        ids: list[str] = []
        for plan_row in plans:
            plan_id = plan_row["pacing_plan_id"]
            changed = conn.execute(
                "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? "
                "WHERE pacing_plan_id=? AND status='waiting' AND " + attempt_clause,
                (
                    PacingStatus.HOLD_REPLAN.value,
                    CancellationReason.CLOCK_JUMP.value,
                    _stamp(now),
                    plan_id,
                    *attempt_parameters,
                ),
            ).rowcount
            if not changed:
                continue
            ids.append(plan_id)
            self._audit(
                conn,
                plan_id,
                "plan_held",
                CancellationReason.CLOCK_JUMP.value,
                {"clock_delta_seconds": delta},
                now,
            )
        return ids

    @staticmethod
    def _attempt_filter(one_shot_attempt_id: str | None) -> tuple[str, tuple[str, ...]]:
        if one_shot_attempt_id is None:
            return "one_shot_attempt_id IS NULL", ()
        return "one_shot_attempt_id=?", (one_shot_attempt_id,)

    def _rate_available(
        self, conn: sqlite3.Connection, plan: PacingPlanRecord, now: datetime
    ) -> bool:
        # Limits are stored in the due plan payload so its original RulePack remains auditable.
        limits = plan.limits
        window_seconds = limits.window_seconds
        global_limit = limits.max_global_revalidations
        contact_limit = limits.max_contact_revalidations
        cutoff = _stamp(now - timedelta(seconds=window_seconds))
        global_count = conn.execute(
            "SELECT COUNT(*) FROM m10_rate_events WHERE occurred_at>=?", (cutoff,)
        ).fetchone()[0]
        contact_count = conn.execute(
            "SELECT COUNT(*) FROM m10_rate_events WHERE contact_id=? AND occurred_at>=?",
            (plan.contact_id, cutoff),
        ).fetchone()[0]
        # Conservative base values; policies may gate further downstream in M9.
        return global_count < global_limit and contact_count < contact_limit

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        plan_id: str,
        action: str,
        reason: str,
        payload: object,
        now: datetime,
    ) -> None:
        # Single choke point: every m10_audit payload is redacted before it can
        # reach storage, so body/segment/plaintext never lands in payload_json.
        sanitized = json.dumps(
            sanitize_audit_payload(payload), sort_keys=True, default=str
        )
        conn.execute(
            "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,occurred_at) VALUES(?,?,?,?,?)",
            (
                plan_id,
                action,
                reason,
                sanitized,
                _stamp(now),
            ),
        )

    def get_plan(self, plan_id: UUID) -> PacingPlanRecord | None:
        row = self.connection.execute(
            "SELECT payload_json FROM m10_plans WHERE pacing_plan_id=?", (str(plan_id),)
        ).fetchone()
        return (
            PacingPlanRecord.model_validate_json(row["payload_json"]) if row else None
        )

    def audit_rows(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM m10_audit ORDER BY audit_id"
        ).fetchall()
