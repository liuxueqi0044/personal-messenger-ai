from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from uuid import UUID

from .contracts import Direction, ObservationBatch, ObservedMessage


class RuntimeState:
    """Durable V5 revisions and cross-service event bridge."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS runtime_conversations(
          conversation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL, conversation_revision INTEGER NOT NULL DEFAULT 1,
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
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS runtime_planning_jobs(
          conversation_id TEXT PRIMARY KEY, conversation_revision INTEGER NOT NULL,
          status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
          error_code TEXT, updated_at TEXT NOT NULL, source_keys_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS runtime_plan_artifacts(
          pacing_plan_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          conversation_revision INTEGER NOT NULL, binding_revision INTEGER NOT NULL,
          global_revision INTEGER NOT NULL, eligibility_json TEXT NOT NULL,
          planner_json TEXT NOT NULL, rule_version TEXT NOT NULL,
          account_id TEXT NOT NULL, contact_id TEXT NOT NULL,
          source_keys_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
          segment_draft_ids_json TEXT NOT NULL DEFAULT '[]'
        );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_plan_artifacts)")}
        if "segment_draft_ids_json" not in columns:
            self.connection.execute(
                "ALTER TABLE runtime_plan_artifacts ADD COLUMN segment_draft_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        job_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_planning_jobs)")}
        if "source_keys_json" not in job_columns:
            self.connection.execute("ALTER TABLE runtime_planning_jobs ADD COLUMN source_keys_json TEXT NOT NULL DEFAULT '[]'")

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

    def register(self, *, account_id: str, contact_id: str, conversation_id: str, binding_revision: int) -> None:
        with self.uow() as db:
            db.execute(
                """INSERT INTO runtime_conversations(conversation_id,account_id,contact_id,binding_revision)
                   VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                   binding_revision=excluded.binding_revision,
                   conversation_revision=runtime_conversations.conversation_revision+1,
                   paused=1,pause_reason='binding_changed'""",
                (conversation_id, account_id, contact_id, binding_revision),
            )

    def apply_observation(self, batch: ObservationBatch) -> tuple[str, ...]:
        """Persist cursor facts before exposing invalidation events to other services."""
        emitted: list[str] = []
        with self.uow() as db:
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
            if row["paused"] and row["pause_reason"] not in ("history_gap", "direction_unknown"):
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
                            "SELECT 1 FROM runtime_segment_executions WHERE operation_id=? AND conversation_id=? AND status='verified'",
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
                        "INSERT OR IGNORE INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                        (dedupe, event_type, batch.conversation_id, message.model_dump_json(), datetime.now(UTC).isoformat()),
                    )
                    emitted.append(event_type)
            db.execute(
                "UPDATE runtime_conversations SET conversation_revision=?,last_observed_at=?,paused=?,pause_reason=? WHERE conversation_id=?",
                (revision, datetime.now(UTC).isoformat(), int(pause_reason is not None), pause_reason, batch.conversation_id),
            )
            if "new_message" in emitted:
                source_keys = [
                    f"qq-uia/{batch.conversation_id}/{message.local_message_key}"
                    for message in batch.messages if message.direction is Direction.INBOUND
                ]
                db.execute(
                    """INSERT INTO runtime_planning_jobs(
                       conversation_id,conversation_revision,status,attempt_count,error_code,updated_at,source_keys_json)
                       VALUES(?,?, 'pending',0,NULL,?,?)
                       ON CONFLICT(conversation_id) DO UPDATE SET
                       conversation_revision=excluded.conversation_revision,status='pending',
                       attempt_count=0,error_code=NULL,updated_at=excluded.updated_at,
                       source_keys_json=excluded.source_keys_json""",
                    (batch.conversation_id, revision, datetime.now(UTC).isoformat(), json.dumps(source_keys)),
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

    def global_control(self) -> tuple[int, bool, str | None]:
        row = self.connection.execute("SELECT revision,paused,reason FROM runtime_global_control WHERE singleton=1").fetchone()
        return int(row["revision"]), bool(row["paused"]), row["reason"]

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
        row = self.connection.execute(
            "SELECT binding_revision,conversation_revision,paused FROM runtime_conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        global_revision, global_paused, _ = self.global_control()
        return (int(row["binding_revision"]), int(row["conversation_revision"]),
                bool(row["paused"]), global_revision, global_paused)

    def claim_events(self, *, limit: int = 100) -> list[sqlite3.Row]:
        with self.uow() as db:
            rows = db.execute(
                "SELECT e.*,c.account_id,c.contact_id FROM runtime_event_outbox e LEFT JOIN runtime_conversations c ON c.conversation_id=e.aggregate_id WHERE e.status='pending' ORDER BY e.event_id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE runtime_event_outbox SET status='dispatching' WHERE event_id=? AND status='pending'", (row["event_id"],))
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
                """SELECT j.*,c.account_id,c.contact_id,c.binding_revision,c.paused
                   FROM runtime_planning_jobs j JOIN runtime_conversations c USING(conversation_id)
                   WHERE j.status='pending' AND NOT EXISTS (
                     SELECT 1 FROM runtime_event_outbox e
                     WHERE e.aggregate_id=j.conversation_id AND e.status!='delivered'
                   ) ORDER BY j.updated_at LIMIT ?""", (limit,)
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE runtime_planning_jobs SET status='running',attempt_count=attempt_count+1,updated_at=? WHERE conversation_id=? AND conversation_revision=? AND status='pending'",
                    (datetime.now(UTC).isoformat(), row["conversation_id"], row["conversation_revision"]),
                )
            return rows

    def complete_planning_job(self, conversation_id: str, revision: int, *, outcome: str,
                              error_code: str | None = None) -> bool:
        if outcome not in {"completed", "ignored", "failed", "stale"}:
            raise ValueError("invalid planning outcome")
        with self.uow() as db:
            return bool(db.execute(
                "UPDATE runtime_planning_jobs SET status=?,error_code=?,updated_at=? WHERE conversation_id=? AND conversation_revision=? AND status='running'",
                (outcome, error_code, datetime.now(UTC).isoformat(), conversation_id, revision),
            ).rowcount)

    def recover_planning_jobs(self) -> int:
        """Retry only jobs never handed to a provider; running calls stay failed-safe."""
        with self.uow() as db:
            return db.execute(
                "UPDATE runtime_planning_jobs SET status='failed',error_code='provider_outcome_unknown',updated_at=? WHERE status='running'",
                (datetime.now(UTC).isoformat(),),
            ).rowcount

    def save_plan_artifact(self, *, pacing_plan_id: UUID, conversation_id: str,
                           conversation_revision: int, binding_revision: int,
                           global_revision: int, eligibility_json: str, planner_json: str,
                           rule_version: str, account_id: str, contact_id: str,
                           source_keys: tuple[str, ...], segment_draft_ids: tuple[str, ...] = ()) -> None:
        with self.uow() as db:
            db.execute(
                """INSERT INTO runtime_plan_artifacts(
                   pacing_plan_id,conversation_id,conversation_revision,binding_revision,
                   global_revision,eligibility_json,planner_json,rule_version,account_id,
                   contact_id,source_keys_json,status,segment_draft_ids_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting',?)""",
                (str(pacing_plan_id), conversation_id, conversation_revision, binding_revision,
                 global_revision, eligibility_json, planner_json, rule_version, account_id,
                 contact_id, json.dumps(source_keys), json.dumps(segment_draft_ids)),
            )

    def plan_artifact(self, pacing_plan_id: UUID | str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM runtime_plan_artifacts WHERE pacing_plan_id=?", (str(pacing_plan_id),)
        ).fetchone()
        if row is None:
            raise KeyError(str(pacing_plan_id))
        return row

    def create_segment_execution(self, *, pacing_plan_id: UUID, segment_index: int, conversation_id: str, body_hash: str,
                                 binding_revision: int, conversation_revision: int) -> bool:
        with self.uow() as db:
            return bool(db.execute(
                "INSERT OR IGNORE INTO runtime_segment_executions VALUES(?,?,?,?,?,?,?,?,?)",
                (str(pacing_plan_id), segment_index, conversation_id, body_hash, None, None, "due",
                 binding_revision, conversation_revision),
            ).rowcount)

    def bind_verified_operation(self, *, pacing_plan_id: UUID, segment_index: int,
                                operation_id: UUID, authorization_id: UUID | str) -> bool:
        with self.uow() as db:
            return bool(db.execute(
                "UPDATE runtime_segment_executions SET operation_id=?,authorization_id=?,status='verified' WHERE pacing_plan_id=? AND segment_index=? AND status!='verified'",
                (str(operation_id), str(authorization_id), str(pacing_plan_id), segment_index),
            ).rowcount)

    def bind_segment_operation(self, *, pacing_plan_id: UUID, segment_index: int,
                               authorization_id: str, operation_id: UUID) -> bool:
        with self.uow() as db:
            return bool(db.execute(
                "UPDATE runtime_segment_executions SET authorization_id=?,operation_id=?,status='authorized' WHERE pacing_plan_id=? AND segment_index=? AND operation_id IS NULL",
                (authorization_id, str(operation_id), str(pacing_plan_id), segment_index),
            ).rowcount)

    def close(self) -> None:
        self.connection.close()
