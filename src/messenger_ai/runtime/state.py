from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from .contracts import Direction, ObservationBatch


def _temporary_observation_pause(reason: str | None) -> bool:
    return bool(
        reason in {"history_gap", "direction_unknown"}
        or (reason and reason.startswith("driver_temporary:"))
        or reason in {
            "driver_quarantine:read_only_observe_timeout",
            "driver_quarantine:binding_changed_requires_release",
        }
    )


def _graceful_stop_pause(reason: str | None) -> bool:
    prefix = "runtime_control:graceful_stop:"
    if not reason or not reason.startswith(prefix):
        return False
    run_id = reason.removeprefix(prefix)
    try:
        return str(UUID(run_id)) == run_id
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class OneShotStopContext:
    attempt_id: UUID
    conversation_id: str
    global_revision: int
    graceful_stop_reason: str


@dataclass(frozen=True, slots=True)
class VerifiedSendStorePaths:
    """Durable authorities required before runtime may record ``verified``."""

    hub: Path
    pacing: Path


class RuntimeState:
    """Durable V5 revisions and cross-service event bridge."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        verified_send_stores: VerifiedSendStorePaths | None = None,
    ) -> None:
        self._path = str(path)
        self._verified_send_stores = verified_send_stores
        self.connection = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._one_shot_stop_context: ContextVar[OneShotStopContext | None] = (
            ContextVar(f"one_shot_stop_context_{id(self)}", default=None)
        )
        self.connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS runtime_conversations(
          conversation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL, conversation_revision INTEGER NOT NULL DEFAULT 1,
          conversation_type TEXT NOT NULL DEFAULT 'unknown',
          paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT, last_observed_at TEXT,
          UNIQUE(account_id, contact_id)
        );
        CREATE TABLE IF NOT EXISTS runtime_observations(
          conversation_id TEXT NOT NULL, local_message_key TEXT NOT NULL, direction TEXT NOT NULL,
          operation_id TEXT, payload_json TEXT NOT NULL, PRIMARY KEY(conversation_id,local_message_key)
        );
        CREATE TABLE IF NOT EXISTS runtime_event_outbox(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
          one_shot_attempt_id TEXT
        );
        CREATE TABLE IF NOT EXISTS runtime_global_control(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
          paused INTEGER NOT NULL, reason TEXT
        );
        INSERT OR IGNORE INTO runtime_global_control VALUES(1,1,0,NULL);
        CREATE TABLE IF NOT EXISTS runtime_segment_executions(
          pacing_plan_id TEXT NOT NULL, segment_index INTEGER NOT NULL, conversation_id TEXT NOT NULL, body_hash TEXT NOT NULL,
          authorization_id TEXT, operation_id TEXT UNIQUE, status TEXT NOT NULL,
          binding_revision INTEGER NOT NULL, conversation_revision INTEGER NOT NULL,
          PRIMARY KEY(pacing_plan_id,segment_index)
        );
        CREATE TABLE IF NOT EXISTS runtime_verified_send_proofs(
          pacing_plan_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
          conversation_id TEXT NOT NULL, authorization_id TEXT NOT NULL,
          operation_id TEXT NOT NULL UNIQUE, hub_commit_intent INTEGER NOT NULL,
          pacing_verified INTEGER NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(pacing_plan_id,segment_index)
        );
        CREATE TRIGGER IF NOT EXISTS runtime_verified_send_proofs_no_update
          BEFORE UPDATE ON runtime_verified_send_proofs
          BEGIN SELECT RAISE(ABORT, 'runtime_verified_send_proofs_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS runtime_verified_send_proofs_no_delete
          BEFORE DELETE ON runtime_verified_send_proofs
          BEGIN SELECT RAISE(ABORT, 'runtime_verified_send_proofs_append_only'); END;
        CREATE TABLE IF NOT EXISTS runtime_planning_jobs(
          conversation_id TEXT PRIMARY KEY, conversation_revision INTEGER NOT NULL,
          status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
          error_code TEXT, updated_at TEXT NOT NULL, source_keys_json TEXT NOT NULL DEFAULT '[]',
          reevaluation_id TEXT, one_shot_attempt_id TEXT
        );
        CREATE TABLE IF NOT EXISTS runtime_planner_decisions(
          request_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          conversation_revision INTEGER NOT NULL,
          binding_revision INTEGER NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('ignore','handoff')),
          selection_reason TEXT NOT NULL,
          model TEXT NOT NULL,
          latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_planner_evaluations(
          request_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          conversation_revision INTEGER NOT NULL,
          binding_revision INTEGER NOT NULL,
          global_revision INTEGER NOT NULL,
          rule_version TEXT NOT NULL,
          provider_request_json TEXT NOT NULL,
          plan_json TEXT NOT NULL,
          action TEXT NOT NULL,
          model TEXT NOT NULL,
          latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
          usage_json TEXT NOT NULL,
          outcome TEXT NOT NULL,
          decision_code TEXT,
          policy_requests_json TEXT NOT NULL DEFAULT '[]',
          policy_decisions_json TEXT NOT NULL DEFAULT '[]',
          policy_reason_codes_json TEXT NOT NULL DEFAULT '[]',
          policy_rule_ids_json TEXT NOT NULL DEFAULT '[]',
          policy_sensitive_categories_json TEXT NOT NULL DEFAULT '[]',
          content_policy_checks_enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_plan_artifacts(
          pacing_plan_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          conversation_revision INTEGER NOT NULL, binding_revision INTEGER NOT NULL,
          global_revision INTEGER NOT NULL, eligibility_json TEXT NOT NULL,
          planner_json TEXT NOT NULL, rule_version TEXT NOT NULL,
          account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
          source_keys_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
          segment_draft_ids_json TEXT NOT NULL DEFAULT '[]',
          one_shot_attempt_id TEXT
        );
        CREATE TABLE IF NOT EXISTS runtime_operator_reevaluations(
          reevaluation_id TEXT PRIMARY KEY,
          original_request_id TEXT NOT NULL UNIQUE,
          new_request_id TEXT UNIQUE,
          conversation_id TEXT NOT NULL,
          account_id TEXT NOT NULL,
          contact_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL,
          conversation_revision INTEGER NOT NULL,
          original_global_revision INTEGER NOT NULL,
          requested_current_global_revision INTEGER NOT NULL,
          source_keys_sha256 TEXT NOT NULL,
          operator_id TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          original_outcome TEXT NOT NULL,
          original_decision_code TEXT,
          status TEXT NOT NULL,
          cursor_unchanged INTEGER NOT NULL,
          observation_unchanged INTEGER NOT NULL,
          no_send_operations INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );
        """)
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(runtime_plan_artifacts)"
            )
        }
        if "segment_draft_ids_json" not in columns:
            self.connection.execute(
                "ALTER TABLE runtime_plan_artifacts ADD COLUMN segment_draft_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "one_shot_attempt_id" not in columns:
            self.connection.execute(
                "ALTER TABLE runtime_plan_artifacts "
                "ADD COLUMN one_shot_attempt_id TEXT"
            )
        event_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(runtime_event_outbox)"
            )
        }
        if "one_shot_attempt_id" not in event_columns:
            self.connection.execute(
                "ALTER TABLE runtime_event_outbox "
                "ADD COLUMN one_shot_attempt_id TEXT"
            )
        job_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(runtime_planning_jobs)"
            )
        }
        if "source_keys_json" not in job_columns:
            self.connection.execute("ALTER TABLE runtime_planning_jobs ADD COLUMN source_keys_json TEXT NOT NULL DEFAULT '[]'")
        if "reevaluation_id" not in job_columns:
            self.connection.execute(
                "ALTER TABLE runtime_planning_jobs ADD COLUMN reevaluation_id TEXT"
            )
        if "one_shot_attempt_id" not in job_columns:
            self.connection.execute(
                "ALTER TABLE runtime_planning_jobs "
                "ADD COLUMN one_shot_attempt_id TEXT"
            )
        conversation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_conversations)")}
        if "conversation_type" not in conversation_columns:
            self.connection.execute(
                "ALTER TABLE runtime_conversations ADD COLUMN conversation_type TEXT NOT NULL DEFAULT 'unknown'"
            )
        evaluation_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(runtime_planner_evaluations)"
            )
        }
        if "content_policy_checks_enabled" not in evaluation_columns:
            self.connection.execute(
                "ALTER TABLE runtime_planner_evaluations "
                "ADD COLUMN content_policy_checks_enabled INTEGER NOT NULL DEFAULT 1"
            )

    @contextmanager
    def uow(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def register(self, *, account_id: str, contact_id: str, conversation_id: str,
                 binding_revision: int, conversation_type: str | None = None) -> None:
        if conversation_type is not None and conversation_type not in {"direct", "group", "unknown"}:
            raise ValueError("invalid conversation type")
        with self.uow() as db:
            db.execute(
                """INSERT INTO runtime_conversations(conversation_id,account_id,contact_id,binding_revision,conversation_type)
                   VALUES(?,?,?,?,COALESCE(?, 'unknown')) ON CONFLICT(conversation_id) DO UPDATE SET
                   binding_revision=excluded.binding_revision,
                   conversation_type=CASE
                     WHEN ? IS NOT NULL THEN excluded.conversation_type
                     WHEN excluded.binding_revision=runtime_conversations.binding_revision
                       THEN runtime_conversations.conversation_type
                     ELSE 'unknown' END,
                   conversation_revision=runtime_conversations.conversation_revision+1,
                   paused=1,pause_reason='binding_changed'""",
                (conversation_id, account_id, contact_id, binding_revision,
                 conversation_type, conversation_type),
            )

    def apply_observation(self, batch: ObservationBatch) -> tuple[str, ...]:
        """Persist cursor facts before exposing invalidation events to other services."""
        emitted: list[str] = []
        with self.uow() as db:
            one_shot_context = self._one_shot_stop_context.get()
            if one_shot_context is not None and not self._one_shot_fence_is_current(
                db,
                conversation_id=batch.conversation_id,
                attempt_id=one_shot_context.attempt_id,
            ):
                raise RuntimeError("ONE_SHOT_GRACEFUL_STOP_FENCE_CHANGED")
            one_shot_attempt_id = (
                str(one_shot_context.attempt_id)
                if one_shot_context is not None
                else None
            )
            row = db.execute("SELECT * FROM runtime_conversations WHERE conversation_id=?", (batch.conversation_id,)).fetchone()
            if row is None or row["account_id"] != batch.account_id or row["contact_id"] != batch.contact_id:
                raise ValueError("observation does not match a registered local identity")
            if batch.binding_revision != int(row["binding_revision"]):
                db.execute(
                    "UPDATE runtime_conversations SET paused=1,pause_reason='binding_revision_changed' WHERE conversation_id=?",
                    (batch.conversation_id,),
                )
                return ("binding_changed",)
            pause_reason = None if batch.complete else (batch.gap_reason or "history_gap")
            if row["paused"] and not _temporary_observation_pause(row["pause_reason"]):
                pause_reason = row["pause_reason"]
            revision = int(row["conversation_revision"])
            for message in batch.messages:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO runtime_observations VALUES(?,?,?,?,?)",
                    (batch.conversation_id, message.local_message_key, message.direction.value,
                     str(message.operation_id) if message.operation_id else None, message.model_dump_json()),
                ).rowcount
                if not inserted:
                    continue
                event_type = None
                if message.direction is Direction.INBOUND:
                    event_type = "new_message"
                elif message.direction is Direction.UNKNOWN:
                    event_type, pause_reason = "direction_unknown", "direction_unknown"
                elif message.direction is Direction.OUTBOUND:
                    if message.operation_id is None:
                        event_type = "human_outbound"
                    else:
                        known = db.execute(
                            """SELECT 1 FROM runtime_segment_executions e
                               JOIN runtime_verified_send_proofs p
                                 ON p.pacing_plan_id=e.pacing_plan_id
                                AND p.segment_index=e.segment_index
                                AND p.operation_id=e.operation_id
                                AND p.conversation_id=e.conversation_id
                               WHERE e.operation_id=? AND e.conversation_id=?
                                 AND e.status='verified'""",
                            (str(message.operation_id), batch.conversation_id),
                        ).fetchone()
                        if known:
                            event_type = "bot_observed"
                        else:
                            event_type, pause_reason = "direction_unknown", "unmatched_operation_echo"
                if event_type:
                    if event_type != "bot_observed":
                        revision += 1
                    dedupe = f"observation:{batch.conversation_id}:{message.local_message_key}:{event_type}"
                    db.execute(
                        """INSERT OR IGNORE INTO runtime_event_outbox(
                           dedupe_key,event_type,aggregate_id,payload_json,created_at,
                           one_shot_attempt_id) VALUES(?,?,?,?,?,?)""",
                        (
                            dedupe,
                            event_type,
                            batch.conversation_id,
                            message.model_dump_json(),
                            datetime.now(UTC).isoformat(),
                            one_shot_attempt_id,
                        ),
                    )
                    emitted.append(event_type)
            db.execute(
                "UPDATE runtime_conversations SET conversation_revision=?,last_observed_at=?,paused=?,pause_reason=? WHERE conversation_id=?",
                (revision, datetime.now(UTC).isoformat(), int(pause_reason is not None), pause_reason, batch.conversation_id),
            )
            if "new_message" in emitted:
                prior_job = db.execute(
                    "SELECT reevaluation_id FROM runtime_planning_jobs WHERE conversation_id=?",
                    (batch.conversation_id,),
                ).fetchone()
                prior_reevaluation_id = (
                    prior_job["reevaluation_id"] if prior_job is not None else None
                )
                if prior_reevaluation_id is not None:
                    db.execute(
                        """UPDATE runtime_operator_reevaluations
                           SET status='superseded',completed_at=?
                           WHERE reevaluation_id=? AND status='prepared'""",
                        (datetime.now(UTC).isoformat(), prior_reevaluation_id),
                    )
                source_keys = [
                    f"qq-uia/{batch.conversation_id}/{message.local_message_key}"
                    for message in batch.messages if message.direction is Direction.INBOUND
                ]
                db.execute(
                    """INSERT INTO runtime_planning_jobs(
                       conversation_id,conversation_revision,status,attempt_count,error_code,
                       updated_at,source_keys_json,one_shot_attempt_id)
                       VALUES(?,?, 'pending',0,NULL,?,?,?)
                       ON CONFLICT(conversation_id) DO UPDATE SET
                       conversation_revision=excluded.conversation_revision,status='pending',
                       attempt_count=0,error_code=NULL,updated_at=excluded.updated_at,
                       source_keys_json=excluded.source_keys_json,reevaluation_id=NULL,
                       one_shot_attempt_id=excluded.one_shot_attempt_id""",
                    (
                        batch.conversation_id,
                        revision,
                        datetime.now(UTC).isoformat(),
                        json.dumps(source_keys),
                        one_shot_attempt_id,
                    ),
                )
        return tuple(emitted)

    def pause(self, conversation_id: str, reason: str = "manual_pause") -> None:
        with self.uow() as db:
            changed = db.execute(
                "UPDATE runtime_conversations SET paused=1,pause_reason=?,conversation_revision=conversation_revision+1 WHERE conversation_id=?",
                (reason, conversation_id),
            ).rowcount
            if not changed:
                raise KeyError(conversation_id)
            revision = db.execute("SELECT conversation_revision FROM runtime_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (f"contact_pause:{conversation_id}:{revision}", "contact_pause", conversation_id,
                 json.dumps({"reason": reason, "revision": revision}), datetime.now(UTC).isoformat()),
            )

    def resume(self, conversation_id: str, *, expected_revision: int) -> bool:
        with self.uow() as db:
            return bool(db.execute(
                "UPDATE runtime_conversations SET paused=0,pause_reason=NULL,conversation_revision=conversation_revision+1 WHERE conversation_id=? AND conversation_revision=? AND pause_reason='manual_pause'",
                (conversation_id, expected_revision),
            ).rowcount)

    @staticmethod
    def _control_matches_one_shot_context(
        row: sqlite3.Row | None, context: OneShotStopContext
    ) -> bool:
        return bool(
            row is not None
            and bool(row["paused"])
            and int(row["revision"]) == context.global_revision
            and row["reason"] == context.graceful_stop_reason
            and _graceful_stop_pause(row["reason"])
        )

    def _one_shot_fence_is_current(
        self,
        db: sqlite3.Connection,
        *,
        conversation_id: str,
        attempt_id: UUID,
    ) -> bool:
        context = self._one_shot_stop_context.get()
        if (
            context is None
            or context.attempt_id != attempt_id
            or context.conversation_id != conversation_id
        ):
            return False
        control = db.execute(
            "SELECT revision,paused,reason FROM runtime_global_control "
            "WHERE singleton=1"
        ).fetchone()
        return self._control_matches_one_shot_context(control, context)

    @contextmanager
    def one_shot_stopped_runtime_scope(
        self, *, attempt_id: UUID, conversation_id: str
    ) -> Iterator[OneShotStopContext]:
        """Mask one exact durable graceful stop for one attempt and target."""

        if not isinstance(attempt_id, UUID) or not conversation_id:
            raise ValueError("one-shot scope requires an attempt and conversation")
        with self._lock:
            row = self.connection.execute(
                "SELECT revision,paused,reason FROM runtime_global_control "
                "WHERE singleton=1"
            ).fetchone()
            target = self.connection.execute(
                "SELECT 1 FROM runtime_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if (
                row is None
                or target is None
                or not bool(row["paused"])
                or not _graceful_stop_pause(row["reason"])
                or self._one_shot_stop_context.get() is not None
            ):
                raise RuntimeError("ONE_SHOT_GRACEFUL_STOP_SCOPE_REJECTED")
            context = OneShotStopContext(
                attempt_id=attempt_id,
                conversation_id=conversation_id,
                global_revision=int(row["revision"]),
                graceful_stop_reason=str(row["reason"]),
            )
            token = self._one_shot_stop_context.set(context)
        try:
            yield context
        finally:
            self._one_shot_stop_context.reset(token)

    def global_control(self) -> tuple[int, bool, str | None]:
        with self._lock:
            row = self.connection.execute(
                "SELECT revision,paused,reason FROM runtime_global_control "
                "WHERE singleton=1"
            ).fetchone()
            context = self._one_shot_stop_context.get()
            paused = bool(row["paused"])
            if context is not None:
                paused = not self._control_matches_one_shot_context(row, context)
            return int(row["revision"]), paused, row["reason"]

    def set_global_pause(self, *, paused: bool, expected_revision: int, reason: str = "manual_global_pause") -> bool:
        with self.uow() as db:
            changed = db.execute(
                "UPDATE runtime_global_control SET paused=?,reason=?,revision=revision+1 WHERE singleton=1 AND revision=?",
                (int(paused), reason if paused else None, expected_revision),
            ).rowcount
            if not changed:
                return False
            revision = expected_revision + 1
            db.execute(
                "INSERT INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (f"global_pause:{revision}", "global_pause" if paused else "global_resume", "global",
                 json.dumps({"paused": paused, "reason": reason, "revision": revision}), datetime.now(UTC).isoformat()),
            )
            return True

    def revisions(self, conversation_id: str) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT binding_revision,conversation_revision FROM runtime_conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return int(row["binding_revision"]), int(row["conversation_revision"])

    def execution_state(self, conversation_id: str) -> tuple[int, int, bool, int, bool]:
        with self._lock:
            row = self.connection.execute(
                """SELECT binding_revision,conversation_revision,paused
                   FROM runtime_conversations WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            control = self.connection.execute(
                "SELECT revision,paused,reason FROM runtime_global_control "
                "WHERE singleton=1"
            ).fetchone()
            context = self._one_shot_stop_context.get()
            global_paused = bool(control["paused"])
            if context is not None:
                global_paused = not (
                    context.conversation_id == conversation_id
                    and self._control_matches_one_shot_context(control, context)
                )
            return (
                int(row["binding_revision"]),
                int(row["conversation_revision"]),
                bool(row["paused"]),
                int(control["revision"]),
                global_paused,
            )

    def one_shot_observation_execution_state(
        self, conversation_id: str
    ) -> tuple[int, int, bool, int, bool]:
        """Permit a fresh observation to heal only an observation-temporary pause."""

        with self._lock:
            row = self.connection.execute(
                """SELECT binding_revision,conversation_revision,paused,pause_reason
                   FROM runtime_conversations WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            control = self.connection.execute(
                "SELECT revision,paused,reason FROM runtime_global_control "
                "WHERE singleton=1"
            ).fetchone()
            context = self._one_shot_stop_context.get()
            global_paused = bool(control["paused"])
            if context is not None:
                global_paused = not (
                    context.conversation_id == conversation_id
                    and self._control_matches_one_shot_context(control, context)
                )
            contact_blocked = bool(
                bool(row["paused"])
                and not _temporary_observation_pause(row["pause_reason"])
            )
            return (
                int(row["binding_revision"]),
                int(row["conversation_revision"]),
                contact_blocked,
                int(control["revision"]),
                global_paused,
            )

    def claim_events(self, *, limit: int = 100) -> list[sqlite3.Row]:
        with self.uow() as db:
            rows = db.execute(
                """SELECT e.*,c.account_id,c.contact_id
                   FROM runtime_event_outbox e
                   LEFT JOIN runtime_conversations c
                     ON c.conversation_id=e.aggregate_id
                   WHERE e.status='pending' AND e.one_shot_attempt_id IS NULL
                   ORDER BY e.event_id LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE runtime_event_outbox SET status='dispatching'
                       WHERE event_id=? AND status='pending'
                         AND one_shot_attempt_id IS NULL""",
                    (row["event_id"],),
                )
            return rows

    def claim_events_for(
        self,
        conversation_id: str,
        *,
        one_shot_attempt_id: UUID,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        """Claim only one attempt's events for its exact one-shot target."""

        with self.uow() as db:
            if not self._one_shot_fence_is_current(
                db,
                conversation_id=conversation_id,
                attempt_id=one_shot_attempt_id,
            ):
                return []
            attempt_id = str(one_shot_attempt_id)
            rows = db.execute(
                """SELECT e.*,c.account_id,c.contact_id
                   FROM runtime_event_outbox e
                   LEFT JOIN runtime_conversations c
                     ON c.conversation_id=e.aggregate_id
                   WHERE e.status='pending' AND e.aggregate_id=?
                     AND e.one_shot_attempt_id=?
                   ORDER BY e.event_id LIMIT ?""",
                (conversation_id, attempt_id, limit),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE runtime_event_outbox SET status='dispatching'
                       WHERE event_id=? AND status='pending'
                         AND aggregate_id=? AND one_shot_attempt_id=?""",
                    (row["event_id"], conversation_id, attempt_id),
                )
            return rows

    def complete_event(self, event_id: int, *, delivered: bool) -> None:
        with self.uow() as db:
            db.execute(
                "UPDATE runtime_event_outbox SET status=? WHERE event_id=? AND status='dispatching'",
                ("delivered" if delivered else "pending", event_id),
            )

    def recover_events(self) -> int:
        with self.uow() as db:
            return db.execute("UPDATE runtime_event_outbox SET status='pending' WHERE status='dispatching'").rowcount

    def claim_planning_jobs(self, *, limit: int = 2) -> list[sqlite3.Row]:
        with self.uow() as db:
            rows = db.execute(
                """SELECT j.*,c.account_id,c.contact_id,c.binding_revision,c.conversation_type,c.paused
                   FROM runtime_planning_jobs j JOIN runtime_conversations c USING(conversation_id)
                   WHERE j.status='pending' AND j.one_shot_attempt_id IS NULL
                     AND NOT EXISTS (
                     SELECT 1 FROM runtime_event_outbox e
                     WHERE e.aggregate_id=j.conversation_id
                       AND e.one_shot_attempt_id IS NULL AND e.status!='delivered'
                   ) ORDER BY j.updated_at LIMIT ?""", (limit,)
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE runtime_planning_jobs
                       SET status='running',attempt_count=attempt_count+1,updated_at=?
                       WHERE conversation_id=? AND conversation_revision=?
                         AND status='pending' AND one_shot_attempt_id IS NULL""",
                    (datetime.now(UTC).isoformat(), row["conversation_id"], row["conversation_revision"]),
                )
            return rows

    def pending_planning_job_for(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        one_shot_attempt_id: UUID,
    ) -> sqlite3.Row | None:
        """Read exact scoped source metadata without claiming the job."""

        with self.uow() as db:
            if not self._one_shot_fence_is_current(
                db,
                conversation_id=conversation_id,
                attempt_id=one_shot_attempt_id,
            ):
                return None
            return db.execute(
                """SELECT j.*,c.account_id,c.contact_id,c.binding_revision,
                          c.conversation_type,c.paused
                   FROM runtime_planning_jobs j
                   JOIN runtime_conversations c USING(conversation_id)
                   WHERE j.conversation_id=? AND j.conversation_revision=?
                     AND j.status='pending' AND j.one_shot_attempt_id=?""",
                (
                    conversation_id,
                    expected_revision,
                    str(one_shot_attempt_id),
                ),
            ).fetchone()

    def claim_planning_job_for(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        one_shot_attempt_id: UUID,
    ) -> sqlite3.Row | None:
        """Claim one expected revision owned by the exact one-shot attempt."""

        with self.uow() as db:
            if not self._one_shot_fence_is_current(
                db,
                conversation_id=conversation_id,
                attempt_id=one_shot_attempt_id,
            ):
                return None
            attempt_id = str(one_shot_attempt_id)
            row = db.execute(
                """SELECT j.*,c.account_id,c.contact_id,c.binding_revision,
                          c.conversation_type,c.paused
                   FROM runtime_planning_jobs j
                   JOIN runtime_conversations c USING(conversation_id)
                   WHERE j.conversation_id=? AND j.conversation_revision=?
                     AND j.status='pending' AND j.one_shot_attempt_id=?
                     AND NOT EXISTS (
                       SELECT 1 FROM runtime_event_outbox e
                       WHERE e.aggregate_id=j.conversation_id
                         AND e.one_shot_attempt_id=? AND e.status!='delivered')""",
                (
                    conversation_id,
                    expected_revision,
                    attempt_id,
                    attempt_id,
                ),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                """UPDATE runtime_planning_jobs
                   SET status='running',attempt_count=attempt_count+1,updated_at=?
                   WHERE conversation_id=? AND conversation_revision=?
                     AND status='pending' AND one_shot_attempt_id=?""",
                (
                    datetime.now(UTC).isoformat(),
                    conversation_id,
                    expected_revision,
                    attempt_id,
                ),
            ).rowcount
            return row if changed == 1 else None

    def complete_planning_job(self, conversation_id: str, revision: int, *, outcome: str,
                              error_code: str | None = None) -> bool:
        if outcome not in {"completed", "ignored", "failed", "stale"}:
            raise ValueError("invalid planning outcome")
        with self.uow() as db:
            row = db.execute(
                """SELECT reevaluation_id FROM runtime_planning_jobs
                   WHERE conversation_id=? AND conversation_revision=? AND status='running'""",
                (conversation_id, revision),
            ).fetchone()
            changed = db.execute(
                "UPDATE runtime_planning_jobs SET status=?,error_code=?,updated_at=? WHERE conversation_id=? AND conversation_revision=? AND status='running'",
                (outcome, error_code, datetime.now(UTC).isoformat(), conversation_id, revision),
            ).rowcount
            if changed and row is not None and row["reevaluation_id"] is not None:
                db.execute(
                    """UPDATE runtime_operator_reevaluations
                       SET status=?,completed_at=?
                       WHERE reevaluation_id=? AND status='prepared'""",
                    (outcome, datetime.now(UTC).isoformat(), row["reevaluation_id"]),
                )
            return bool(changed)

    def complete_non_send_planning_decision(
        self,
        conversation_id: str,
        revision: int,
        *,
        binding_revision: int,
        action: str,
        selection_reason: str,
        request_id: str,
        model: str,
        latency_ms: int,
    ) -> bool:
        """Atomically finish a non-send job and retain the provider decision."""
        if action not in {"ignore", "handoff"}:
            raise ValueError("non-send decision must be ignore or handoff")
        if not request_id or not model or latency_ms < 0:
            raise ValueError("invalid provider decision metadata")
        now = datetime.now(UTC).isoformat()
        with self.uow() as db:
            changed = db.execute(
                """UPDATE runtime_planning_jobs
                   SET status='ignored',error_code=NULL,updated_at=?
                   WHERE conversation_id=? AND conversation_revision=? AND status='running'""",
                (now, conversation_id, revision),
            ).rowcount
            if not changed:
                return False
            db.execute(
                """INSERT INTO runtime_planner_decisions(
                   request_id,conversation_id,conversation_revision,binding_revision,
                   action,selection_reason,model,latency_ms,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (request_id, conversation_id, revision, binding_revision, action,
                 selection_reason, model, latency_ms, now),
            )
            return True

    def complete_planning_evaluation(
        self,
        conversation_id: str,
        revision: int,
        *,
        binding_revision: int,
        global_revision: int,
        rule_version: str,
        request_id: str,
        provider_request_json: str,
        plan_json: str,
        action: str,
        model: str,
        latency_ms: int,
        usage_json: str,
        audit_outcome: str,
        job_outcome: str,
        decision_code: str | None = None,
        policy_requests_json: str = "[]",
        policy_decisions_json: str = "[]",
        policy_reason_codes_json: str = "[]",
        policy_rule_ids_json: str = "[]",
        policy_sensitive_categories_json: str = "[]",
        content_policy_checks_enabled: bool = True,
        non_send_selection_reason: str | None = None,
        plan_artifact: dict[str, object] | None = None,
    ) -> bool:
        """Atomically retain a valid provider plan and finish its versioned decision exit.

        The audit row is append-only.  A lost job CAS is recorded as ``stale`` and
        cannot create a plan artifact or a legacy non-send decision.
        """
        if job_outcome not in {"completed", "ignored", "failed", "stale"}:
            raise ValueError("invalid planning outcome")
        if not all((request_id, rule_version, provider_request_json, plan_json, action, model)):
            raise ValueError("provider evaluation metadata is incomplete")
        if latency_ms < 0:
            raise ValueError("provider evaluation latency is invalid")
        if not isinstance(content_policy_checks_enabled, bool):
            raise TypeError("content_policy_checks_enabled must be a boolean")
        for value in (
            provider_request_json,
            plan_json,
            usage_json,
            policy_requests_json,
            policy_decisions_json,
            policy_reason_codes_json,
            policy_rule_ids_json,
            policy_sensitive_categories_json,
        ):
            json.loads(value)
        now = datetime.now(UTC).isoformat()
        with self.uow() as db:
            planning_job = db.execute(
                """SELECT reevaluation_id,one_shot_attempt_id
                   FROM runtime_planning_jobs
                   WHERE conversation_id=? AND conversation_revision=? AND status='running'""",
                (conversation_id, revision),
            ).fetchone()
            reevaluation_id = (
                planning_job["reevaluation_id"] if planning_job is not None else None
            )
            job_attempt_id = (
                planning_job["one_shot_attempt_id"]
                if planning_job is not None
                else None
            )
            conversation = db.execute(
                """SELECT binding_revision,conversation_revision,paused
                   FROM runtime_conversations WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            control = db.execute(
                "SELECT revision,paused,reason FROM runtime_global_control "
                "WHERE singleton=1"
            ).fetchone()
            context = self._one_shot_stop_context.get()
            normal_global_current = bool(
                job_attempt_id is None
                and control is not None
                and int(control["revision"]) == global_revision
                and not bool(control["paused"])
            )
            one_shot_global_current = bool(
                job_attempt_id is not None
                and context is not None
                and str(context.attempt_id) == job_attempt_id
                and context.conversation_id == conversation_id
                and context.global_revision == global_revision
                and self._control_matches_one_shot_context(control, context)
            )
            current = bool(
                planning_job is not None
                and conversation is not None
                and int(conversation["binding_revision"]) == binding_revision
                and int(conversation["conversation_revision"]) == revision
                and not bool(conversation["paused"])
                and (normal_global_current or one_shot_global_current)
            )
            requested_change = 0
            if current:
                requested_change = db.execute(
                    """UPDATE runtime_planning_jobs SET status=?,error_code=?,updated_at=?
                       WHERE conversation_id=? AND conversation_revision=? AND status='running'""",
                    (job_outcome, decision_code, now, conversation_id, revision),
                ).rowcount
            if not requested_change:
                db.execute(
                    """UPDATE runtime_planning_jobs SET status='stale',error_code='stale',updated_at=?
                       WHERE conversation_id=? AND conversation_revision=? AND status='running'""",
                    (now, conversation_id, revision),
                )
            effective_outcome = audit_outcome if requested_change else "stale"
            effective_decision_code = decision_code if requested_change else "stale"
            db.execute(
                """INSERT INTO runtime_planner_evaluations(
                   request_id,conversation_id,conversation_revision,binding_revision,
                   global_revision,rule_version,provider_request_json,plan_json,action,model,
                   latency_ms,usage_json,outcome,decision_code,policy_decisions_json,
                   policy_requests_json,policy_reason_codes_json,policy_rule_ids_json,
                   policy_sensitive_categories_json,content_policy_checks_enabled,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, conversation_id, revision, binding_revision,
                 global_revision, rule_version, provider_request_json, plan_json,
                 action, model, latency_ms, usage_json, effective_outcome, effective_decision_code,
                 policy_decisions_json, policy_requests_json, policy_reason_codes_json,
                 policy_rule_ids_json, policy_sensitive_categories_json,
                 int(content_policy_checks_enabled), now),
            )
            if reevaluation_id is not None:
                db.execute(
                    """UPDATE runtime_operator_reevaluations
                       SET new_request_id=?,status=?,completed_at=?
                       WHERE reevaluation_id=? AND status='prepared'
                         AND new_request_id IS NULL""",
                    (
                        request_id,
                        effective_outcome,
                        now,
                        reevaluation_id,
                    ),
                )
            if not requested_change:
                return False
            if non_send_selection_reason is not None:
                if action not in {"ignore", "handoff"}:
                    raise ValueError("non-send audit requires ignore or handoff")
                db.execute(
                    """INSERT INTO runtime_planner_decisions(
                       request_id,conversation_id,conversation_revision,binding_revision,
                       action,selection_reason,model,latency_ms,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (request_id, conversation_id, revision, binding_revision, action,
                     non_send_selection_reason, model, latency_ms, now),
                )
            if plan_artifact is not None:
                db.execute(
                    """INSERT INTO runtime_plan_artifacts(
                       pacing_plan_id,conversation_id,conversation_revision,binding_revision,
                       global_revision,eligibility_json,planner_json,rule_version,account_id,
                       contact_id,source_keys_json,status,segment_draft_ids_json,
                       one_shot_attempt_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?)""",
                    (
                        plan_artifact["pacing_plan_id"], conversation_id, revision,
                        binding_revision, global_revision,
                        plan_artifact["eligibility_json"], plan_json, rule_version,
                        plan_artifact["account_id"], plan_artifact["contact_id"],
                        plan_artifact["source_keys_json"],
                        plan_artifact["segment_draft_ids_json"],
                        job_attempt_id,
                    ),
                )
            return True

    def recover_planning_jobs(self) -> int:
        """Retry only jobs never handed to a provider; running calls stay failed-safe."""
        with self.uow() as db:
            now = datetime.now(UTC).isoformat()
            pending = db.execute(
                """SELECT reevaluation_id FROM runtime_planning_jobs
                   WHERE status='running' AND reevaluation_id IS NOT NULL"""
            ).fetchall()
            changed = db.execute(
                "UPDATE runtime_planning_jobs SET status='failed',error_code='provider_outcome_unknown',updated_at=? WHERE status='running'",
                (now,),
            ).rowcount
            for row in pending:
                db.execute(
                    """UPDATE runtime_operator_reevaluations
                       SET status='provider_outcome_unknown',completed_at=?
                       WHERE reevaluation_id=? AND status='prepared'""",
                    (now, row["reevaluation_id"]),
                )
            return changed

    def save_plan_artifact(self, *, pacing_plan_id: UUID, conversation_id: str,
                           conversation_revision: int, binding_revision: int,
                           global_revision: int, eligibility_json: str, planner_json: str,
                           rule_version: str, account_id: str, contact_id: str,
                           source_keys: tuple[str, ...], segment_draft_ids: tuple[str, ...] = (),
                           one_shot_attempt_id: UUID | None = None) -> None:
        with self.uow() as db:
            if one_shot_attempt_id is not None and not self._one_shot_fence_is_current(
                db,
                conversation_id=conversation_id,
                attempt_id=one_shot_attempt_id,
            ):
                raise RuntimeError("ONE_SHOT_GRACEFUL_STOP_FENCE_CHANGED")
            db.execute(
                """INSERT INTO runtime_plan_artifacts(
                   pacing_plan_id,conversation_id,conversation_revision,binding_revision,
                   global_revision,eligibility_json,planner_json,rule_version,account_id,
                   contact_id,source_keys_json,status,segment_draft_ids_json,
                   one_shot_attempt_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?)""",
                (str(pacing_plan_id), conversation_id, conversation_revision, binding_revision,
                 global_revision, eligibility_json, planner_json, rule_version, account_id,
                 contact_id, json.dumps(source_keys), json.dumps(segment_draft_ids),
                 str(one_shot_attempt_id) if one_shot_attempt_id is not None else None),
            )

    def plan_artifact(self, pacing_plan_id: UUID | str) -> sqlite3.Row:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM runtime_plan_artifacts WHERE pacing_plan_id=?",
                (str(pacing_plan_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(pacing_plan_id))
            if row["one_shot_attempt_id"] is not None:
                try:
                    attempt_id = UUID(str(row["one_shot_attempt_id"]))
                except ValueError as exc:
                    raise RuntimeError(
                        "ONE_SHOT_PLAN_PROVENANCE_INVALID"
                    ) from exc
                if not self._one_shot_fence_is_current(
                    self.connection,
                    conversation_id=str(row["conversation_id"]),
                    attempt_id=attempt_id,
                ):
                    raise RuntimeError("ONE_SHOT_GRACEFUL_STOP_FENCE_CHANGED")
            return row

    def plan_artifact_for_revision(
        self,
        conversation_id: str,
        conversation_revision: int,
        *,
        one_shot_attempt_id: UUID | None = None,
    ) -> sqlite3.Row:
        with self._lock:
            if one_shot_attempt_id is None:
                rows = self.connection.execute(
                    """SELECT * FROM runtime_plan_artifacts
                       WHERE conversation_id=? AND conversation_revision=?
                         AND one_shot_attempt_id IS NULL""",
                    (conversation_id, conversation_revision),
                ).fetchall()
            else:
                if not self._one_shot_fence_is_current(
                    self.connection,
                    conversation_id=conversation_id,
                    attempt_id=one_shot_attempt_id,
                ):
                    raise RuntimeError("ONE_SHOT_GRACEFUL_STOP_FENCE_CHANGED")
                rows = self.connection.execute(
                    """SELECT * FROM runtime_plan_artifacts
                       WHERE conversation_id=? AND conversation_revision=?
                         AND one_shot_attempt_id=?""",
                    (
                        conversation_id,
                        conversation_revision,
                        str(one_shot_attempt_id),
                    ),
                ).fetchall()
            if len(rows) != 1:
                raise KeyError(f"{conversation_id}:{conversation_revision}")
            return rows[0]

    def create_segment_execution(self, *, pacing_plan_id: UUID, segment_index: int,
                                 conversation_id: str, body_hash: str,
                                 binding_revision: int,
                                 conversation_revision: int) -> bool:
        """Create, or exactly replay, one due segment without widening ownership."""

        plan_id = str(pacing_plan_id)
        expected = (
            plan_id,
            segment_index,
            conversation_id,
            body_hash,
            binding_revision,
            conversation_revision,
        )
        with self.uow() as db:
            changed = db.execute(
                """INSERT OR IGNORE INTO runtime_segment_executions(
                       pacing_plan_id,segment_index,conversation_id,body_hash,
                       authorization_id,operation_id,status,binding_revision,
                       conversation_revision)
                   VALUES(?,?,?,?,NULL,NULL,'due',?,?)""",
                expected,
            ).rowcount
            if changed == 1:
                return True
            row = db.execute(
                """SELECT pacing_plan_id,segment_index,conversation_id,body_hash,
                          binding_revision,conversation_revision
                   FROM runtime_segment_executions
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (plan_id, segment_index),
            ).fetchone()
            return row is not None and tuple(row) == expected

    def bind_verified_operation(
        self,
        *,
        pacing_plan_id: UUID,
        segment_index: int,
        operation_id: UUID,
        authorization_id: UUID | str,
        receipt: object | None = None,
    ) -> bool:
        """Legacy bind seam constrained to ``authorized`` only.

        No caller-supplied object is accepted as proof of an external send.
        ``verified`` is reachable only through the dispatcher/Hub VERIFY
        settlement path; persisted bridge and pacing receipts are checked again
        before cursor reanchor.  The optional argument remains solely to fail
        closed for older callers that attempt the retired behavior.
        """

        plan_id = str(pacing_plan_id)
        operation = str(operation_id)
        authorization = str(authorization_id)
        with self.uow() as db:
            row = db.execute(
                """SELECT conversation_id,authorization_id,operation_id,status
                   FROM runtime_segment_executions
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (plan_id, segment_index),
            ).fetchone()
            if row is None:
                return False
            if receipt is not None:
                return False
            if (
                row["authorization_id"] == authorization
                and row["operation_id"] == operation
                and row["status"] == "authorized"
            ):
                return True
            if (
                row["status"] != "due"
                or row["operation_id"] is not None
                or row["authorization_id"] is not None
            ):
                return False
            changed = db.execute(
                """UPDATE runtime_segment_executions
                   SET operation_id=?,authorization_id=?,status=?
                   WHERE pacing_plan_id=? AND segment_index=? AND status='due'
                     AND operation_id IS NULL AND authorization_id IS NULL""",
                (operation, authorization, "authorized", plan_id, segment_index),
            ).rowcount
            return changed == 1

    def bind_segment_operation(self, *, pacing_plan_id: UUID, segment_index: int,
                               authorization_id: str, operation_id: UUID) -> bool:
        plan_id = str(pacing_plan_id)
        operation = str(operation_id)
        authorization = str(authorization_id)
        with self.uow() as db:
            row = db.execute(
                """SELECT authorization_id,operation_id,status
                   FROM runtime_segment_executions
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (plan_id, segment_index),
            ).fetchone()
            if row is None:
                return False
            if (
                row["authorization_id"] == authorization
                and row["operation_id"] == operation
                and row["status"] in {
                    "authorized",
                    "verified",
                    "failed",
                    "cancelled",
                    "send_uncertain",
                }
            ):
                return True
            if (
                row["status"] != "due"
                or row["operation_id"] is not None
                or row["authorization_id"] is not None
            ):
                return False
            duplicate = db.execute(
                """SELECT 1 FROM runtime_segment_executions
                   WHERE operation_id=? LIMIT 1""",
                (operation,),
            ).fetchone()
            if duplicate is not None:
                return False
            changed = db.execute(
                """UPDATE runtime_segment_executions
                   SET authorization_id=?,operation_id=?,status='authorized'
                   WHERE pacing_plan_id=? AND segment_index=? AND status='due'
                     AND operation_id IS NULL AND authorization_id IS NULL""",
                (authorization, operation, plan_id, segment_index),
            ).rowcount
            return changed == 1

    def settle_segment_operation(
        self,
        *,
        pacing_plan_id: UUID,
        segment_index: int,
        authorization_id: UUID | str,
        operation_id: UUID,
        operation_status: str,
    ) -> bool:
        """CAS an authorized operation to its conservative runtime terminal state."""

        terminal = {
            "failed": "failed",
            "cancelled": "cancelled",
            "send_uncertain": "send_uncertain",
            # A returned non-terminal or committed-unverified operation can no
            # longer be safely replayed by this lane.
            "pending": "send_uncertain",
            "prepared": "send_uncertain",
            "committed": "send_uncertain",
        }.get(operation_status)
        if terminal is None:
            raise ValueError("unsupported send operation status")
        plan_id = str(pacing_plan_id)
        operation = str(operation_id)
        authorization = str(authorization_id)
        with self.uow() as db:
            changed = db.execute(
                """UPDATE runtime_segment_executions SET status=?
                   WHERE pacing_plan_id=? AND segment_index=? AND status='authorized'
                     AND operation_id=? AND authorization_id=?""",
                (terminal, plan_id, segment_index, operation, authorization),
            ).rowcount
            if changed == 1:
                self._advance_artifact_status(db, plan_id)
                return True
            row = db.execute(
                """SELECT authorization_id,operation_id,status
                   FROM runtime_segment_executions
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (plan_id, segment_index),
            ).fetchone()
            return row is not None and tuple(row) == (
                authorization,
                operation,
                terminal,
            )

    def settle_verified_segment(
        self,
        *,
        pacing_plan_id: UUID,
        segment_index: int,
        authorization_id: UUID | str,
        operation_id: UUID,
    ) -> bool:
        """Record verified only from exact durable Hub and pacing evidence."""

        stores = self._verified_send_stores
        if stores is None or self._path == ":memory:":
            return False
        paths = (Path(stores.hub).resolve(), Path(stores.pacing).resolve())
        if not all(path.is_file() for path in paths):
            return False
        plan_id = str(pacing_plan_id)
        operation = str(operation_id)
        authorization = str(authorization_id)
        with self._lock:
            db = sqlite3.connect(self._path, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("ATTACH DATABASE ? AS verified_hub", (str(paths[0]),))
                db.execute("ATTACH DATABASE ? AS verified_pacing", (str(paths[1]),))
                db.execute("BEGIN IMMEDIATE")
                runtime_row = db.execute(
                    """SELECT conversation_id,authorization_id,operation_id,status
                       FROM runtime_segment_executions
                       WHERE pacing_plan_id=? AND segment_index=?""",
                    (plan_id, segment_index),
                ).fetchone()
                if runtime_row is None:
                    db.execute("ROLLBACK")
                    return False
                conversation_id = str(runtime_row["conversation_id"])
                hub_rows = db.execute(
                    """SELECT s.status,s.commit_intent,s.authorization_id,
                              d.conversation_id
                       FROM verified_hub.send_operations s
                       JOIN verified_hub.drafts d ON d.draft_id=s.draft_id
                       WHERE s.operation_id=?""",
                    (operation,),
                ).fetchall()
                pacing_rows = db.execute(
                    """SELECT operation_id,verified
                       FROM verified_pacing.m10_segment_receipts
                       WHERE pacing_plan_id=? AND segment_index=?""",
                    (plan_id, segment_index),
                ).fetchall()
                proven = (
                    runtime_row["authorization_id"] == authorization
                    and runtime_row["operation_id"] == operation
                    and runtime_row["status"] in {"authorized", "verified"}
                    and len(hub_rows) == 1
                    and hub_rows[0]["status"] == "verified"
                    and int(hub_rows[0]["commit_intent"]) == 1
                    and hub_rows[0]["authorization_id"] == authorization
                    and hub_rows[0]["conversation_id"] == conversation_id
                    and len(pacing_rows) == 1
                    and pacing_rows[0]["operation_id"] == operation
                    and int(pacing_rows[0]["verified"]) == 1
                )
                if not proven:
                    db.execute("ROLLBACK")
                    return False
                existing = db.execute(
                    """SELECT conversation_id,authorization_id,operation_id,
                              hub_commit_intent,pacing_verified
                       FROM runtime_verified_send_proofs
                       WHERE pacing_plan_id=? AND segment_index=?""",
                    (plan_id, segment_index),
                ).fetchone()
                expected = (conversation_id, authorization, operation, 1, 1)
                if existing is not None and tuple(existing) != expected:
                    db.execute("ROLLBACK")
                    return False
                if existing is None:
                    db.execute(
                        """INSERT INTO runtime_verified_send_proofs(
                               pacing_plan_id,segment_index,conversation_id,
                               authorization_id,operation_id,hub_commit_intent,
                               pacing_verified,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            plan_id,
                            segment_index,
                            conversation_id,
                            authorization,
                            operation,
                            1,
                            1,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                if runtime_row["status"] == "authorized":
                    changed = db.execute(
                        """UPDATE runtime_segment_executions SET status='verified'
                           WHERE pacing_plan_id=? AND segment_index=?
                             AND status='authorized' AND operation_id=?
                             AND authorization_id=?""",
                        (plan_id, segment_index, operation, authorization),
                    ).rowcount
                    if changed != 1:
                        db.execute("ROLLBACK")
                        return False
                    self._advance_artifact_status(db, plan_id)
                db.execute("COMMIT")
                return True
            except (sqlite3.Error, TypeError, ValueError):
                if db.in_transaction:
                    db.execute("ROLLBACK")
                return False
            finally:
                db.close()

    def recover_verified_segments(self) -> int:
        """Finish the safe crash window after durable Hub+pacing proof exists."""

        with self._lock:
            rows = self.connection.execute(
                """SELECT pacing_plan_id,segment_index,authorization_id,operation_id
                   FROM runtime_segment_executions
                   WHERE authorization_id IS NOT NULL AND operation_id IS NOT NULL
                     AND (status='authorized' OR (
                       status='verified' AND NOT EXISTS (
                         SELECT 1 FROM runtime_verified_send_proofs p
                         WHERE p.pacing_plan_id=runtime_segment_executions.pacing_plan_id
                           AND p.segment_index=runtime_segment_executions.segment_index
                           AND p.operation_id=runtime_segment_executions.operation_id
                       )
                     ))"""
            ).fetchall()
        return sum(
            self.settle_verified_segment(
                pacing_plan_id=UUID(row["pacing_plan_id"]),
                segment_index=int(row["segment_index"]),
                authorization_id=row["authorization_id"],
                operation_id=UUID(row["operation_id"]),
            )
            for row in rows
        )

    def reject_due_segment(
        self,
        *,
        pacing_plan_id: UUID,
        segment_index: int,
        conversation_id: str,
        body_hash: str,
        binding_revision: int,
        conversation_revision: int,
    ) -> bool:
        """Terminalize an M9 denial which intentionally has no operation id."""

        if not self.create_segment_execution(
            pacing_plan_id=pacing_plan_id,
            segment_index=segment_index,
            conversation_id=conversation_id,
            body_hash=body_hash,
            binding_revision=binding_revision,
            conversation_revision=conversation_revision,
        ):
            return False
        plan_id = str(pacing_plan_id)
        with self.uow() as db:
            changed = db.execute(
                """UPDATE runtime_segment_executions SET status='rejected'
                   WHERE pacing_plan_id=? AND segment_index=? AND status='due'
                     AND operation_id IS NULL AND authorization_id IS NULL""",
                (plan_id, segment_index),
            ).rowcount
            if changed == 1:
                self._advance_artifact_status(db, plan_id)
                return True
            row = db.execute(
                """SELECT operation_id,authorization_id,status
                   FROM runtime_segment_executions
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (plan_id, segment_index),
            ).fetchone()
            return row is not None and tuple(row) == (None, None, "rejected")

    @staticmethod
    def _artifact_segment_count(artifact: sqlite3.Row) -> int:
        for column in ("segment_draft_ids_json", "eligibility_json"):
            try:
                value = json.loads(artifact[column])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if isinstance(value, list) and value:
                return len(value)
        try:
            planner = json.loads(artifact["planner_json"])
        except (json.JSONDecodeError, TypeError):
            return 0
        segments = planner.get("reply_segments") if isinstance(planner, dict) else None
        return len(segments) if isinstance(segments, list) else 0

    def _advance_artifact_status(self, db: sqlite3.Connection, plan_id: str) -> None:
        artifact = db.execute(
            "SELECT * FROM runtime_plan_artifacts WHERE pacing_plan_id=?",
            (plan_id,),
        ).fetchone()
        if artifact is None or artifact["status"] != "waiting":
            return
        statuses = [
            str(row["status"])
            for row in db.execute(
                """SELECT status FROM runtime_segment_executions
                   WHERE pacing_plan_id=? ORDER BY segment_index""",
                (plan_id,),
            ).fetchall()
        ]
        target: str | None = None
        if "send_uncertain" in statuses:
            target = "send_uncertain"
        elif "cancelled" in statuses:
            target = "cancelled"
        elif any(status in {"failed", "rejected"} for status in statuses):
            target = "rejected"
        else:
            expected = self._artifact_segment_count(artifact)
            if expected > 0 and len(statuses) == expected and all(
                status == "verified" for status in statuses
            ):
                target = "completed"
        if target is not None:
            db.execute(
                """UPDATE runtime_plan_artifacts SET status=?
                   WHERE pacing_plan_id=? AND status='waiting'""",
                (target, plan_id),
            )

    def close(self) -> None:
        self.connection.close()
