"""SQLite-backed deterministic pacing scheduler.

This module intentionally stops at ``DueForRevalidation``.  It neither imports
adapters/outbox/authorization code nor has a send API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

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
    segment_gap_seconds,
)

ACTIVE = (PacingStatus.WAITING.value,)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


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
                  due_emitted_at TEXT, cancel_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
            conversation_id=request.draft.conversation_id,
            contact_id=request.draft.contact_id,
            draft_id=request.draft.draft_id,
            text_hash=request.draft.text_hash,
            expected_last_message_key=request.draft.expected_last_message_key,
            source_message_keys=request.source_message_keys,
            rule_version=request.draft.rule_version,
            pacing_rule_version=request.draft.rule_version,
            eligibility_id=request.draft.eligibility_id,
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
                "INSERT INTO m10_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(plan.pacing_plan_id),
                    plan.conversation_id,
                    plan.contact_id,
                    plan.status.value,
                    _stamp(plan.earliest_send_at),
                    _stamp(plan.expires_at),
                    plan.model_dump_json(),
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
    ) -> None:
        now = self.now()
        changed = conn.execute(
            "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=? AND status IN ('waiting','due_for_revalidation')",
            (PacingStatus.CANCELLED.value, reason.value, _stamp(now), plan_id),
        ).rowcount
        if changed:
            self._audit(conn, plan_id, "plan_cancelled", reason.value, {}, now)

    def due_for_revalidation(self) -> list[DueForRevalidation]:
        """Atomically claim due events once.  Calling this cannot authorize or send."""
        now = self.now()
        due_events: list[DueForRevalidation] = []
        with self._uow() as conn:
            self._handle_clock_jump(conn, now)
            rows = conn.execute(
                "SELECT * FROM m10_plans WHERE status='waiting' AND earliest_send_at<=? ORDER BY earliest_send_at,pacing_plan_id",
                (_stamp(now),),
            ).fetchall()
            for row in rows:
                if _parse(row["expires_at"]) <= now:
                    conn.execute(
                        "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=?",
                        (
                            PacingStatus.EXPIRED.value,
                            CancellationReason.EXPIRED.value,
                            _stamp(now),
                            row["pacing_plan_id"],
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
                    "UPDATE m10_plans SET status=?,due_emitted_at=?,updated_at=? WHERE pacing_plan_id=? AND status='waiting'",
                    (
                        PacingStatus.DUE_FOR_REVALIDATION.value,
                        _stamp(now),
                        _stamp(now),
                        row["pacing_plan_id"],
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
                    conversation_id=plan.conversation_id,
                    contact_id=plan.contact_id,
                    draft_id=plan.draft_id,
                    text_hash=plan.text_hash,
                    expected_last_message_key=plan.expected_last_message_key,
                    rule_version=plan.rule_version,
                    pacing_rule_version=plan.pacing_rule_version,
                    eligibility_id=plan.eligibility_id,
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
                    "INSERT OR IGNORE INTO m10_due_outbox(pacing_plan_id,segment_index,payload_json,created_at) VALUES(?,?,?,?)",
                    (str(plan.pacing_plan_id), plan.segment_index, due.model_dump_json(), _stamp(now)),
                )
                due_events.append(due)
        return due_events

    def claim_due_outbox(self, *, limit: int = 100) -> list[tuple[int, DueForRevalidation]]:
        """Claim durable due events for runtime delivery.

        Claims survive process failure: ``recover_due_outbox`` returns abandoned
        claims to pending without creating a second segment event.
        """
        now = self.now()
        with self._uow() as conn:
            rows = conn.execute(
                "SELECT outbox_id,payload_json FROM m10_due_outbox WHERE status='pending' ORDER BY outbox_id LIMIT ?",
                (limit,),
            ).fetchall()
            result: list[tuple[int, DueForRevalidation]] = []
            for row in rows:
                changed = conn.execute(
                    "UPDATE m10_due_outbox SET status='dispatching',claimed_at=? WHERE outbox_id=? AND status='pending'",
                    (_stamp(now), row["outbox_id"]),
                ).rowcount
                if changed:
                    result.append((row["outbox_id"], DueForRevalidation.model_validate_json(row["payload_json"])))
            return result

    def complete_due_outbox(self, outbox_id: int) -> bool:
        with self._uow() as conn:
            changed = conn.execute(
                "UPDATE m10_due_outbox SET status='delivered',delivered_at=? WHERE outbox_id=? AND status='dispatching'",
                (_stamp(self.now()), outbox_id),
            ).rowcount
            return bool(changed)

    def recover_due_outbox(self) -> int:
        with self._uow() as conn:
            return conn.execute(
                "UPDATE m10_due_outbox SET status='pending',claimed_at=NULL WHERE status='dispatching'"
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
            row = conn.execute(
                "SELECT * FROM m10_plans WHERE pacing_plan_id=?", (str(pacing_plan_id),)
            ).fetchone()
            if row is None:
                return None
            plan = PacingPlanRecord.model_validate_json(row["payload_json"])
            expected_index = plan.segment_index
            if operation_id is not None:
                receipt = conn.execute(
                    "SELECT pacing_plan_id,segment_index,verified FROM m10_segment_receipts WHERE operation_id=?",
                    (str(operation_id),),
                ).fetchone()
                if receipt is not None:
                    # Exact replay is idempotent; a cross-segment reuse is rejected.
                    if receipt["pacing_plan_id"] != str(pacing_plan_id) or (
                        segment_index is not None and receipt["segment_index"] != segment_index
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
                    "INSERT INTO m10_segment_receipts VALUES(?,?,?,?,?)",
                    (str(pacing_plan_id), expected_index, str(operation_id), int(segment_sent_and_verified), _stamp(now)),
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
                    "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=?",
                    (
                        status.value,
                        reason.value if hasattr(reason, "value") else reason,
                        _stamp(now),
                        str(pacing_plan_id),
                    ),
                )
                self._audit(
                    conn,
                    str(pacing_plan_id),
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
                    "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=?",
                    (
                        PacingStatus.EXPIRED.value,
                        CancellationReason.EXPIRED.value,
                        _stamp(now),
                        str(pacing_plan_id),
                    ),
                )
                self._audit(
                    conn,
                    str(pacing_plan_id),
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
                "UPDATE m10_plans SET status=?,earliest_send_at=?,payload_json=?,updated_at=? WHERE pacing_plan_id=?",
                (
                    PacingStatus.WAITING.value,
                    _stamp(next_due),
                    next_plan.model_dump_json(),
                    _stamp(now),
                    str(pacing_plan_id),
                ),
            )
            self._audit(
                conn,
                str(pacing_plan_id),
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
        self, conn: sqlite3.Connection, now: datetime, *, tolerance: float = 300
    ) -> list[str]:
        row = conn.execute(
            "SELECT value FROM m10_metadata WHERE key='last_clock' "
        ).fetchone()
        conn.execute(
            "INSERT INTO m10_metadata(key,value) VALUES('last_clock',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_stamp(now),),
        )
        if row is None:
            return []
        delta = (now - _parse(row["value"])).total_seconds()
        if 0 <= delta <= tolerance:
            return []
        plans = conn.execute(
            "SELECT pacing_plan_id FROM m10_plans WHERE status='waiting'"
        ).fetchall()
        ids = [item["pacing_plan_id"] for item in plans]
        for plan_id in ids:
            conn.execute(
                "UPDATE m10_plans SET status=?,cancel_reason=?,updated_at=? WHERE pacing_plan_id=? AND status='waiting'",
                (
                    PacingStatus.HOLD_REPLAN.value,
                    CancellationReason.CLOCK_JUMP.value,
                    _stamp(now),
                    plan_id,
                ),
            )
            self._audit(
                conn,
                plan_id,
                "plan_held",
                CancellationReason.CLOCK_JUMP.value,
                {"clock_delta_seconds": delta},
                now,
            )
        return ids

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
        conn.execute(
            "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,occurred_at) VALUES(?,?,?,?,?)",
            (
                plan_id,
                action,
                reason,
                json.dumps(payload, sort_keys=True, default=str),
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
