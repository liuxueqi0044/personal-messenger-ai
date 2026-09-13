from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.domain import DomainModel, SendStatus
from messenger_ai.pacing.models import CancellationReason


class OneShotReplyResult(DomainModel):
    attempt_id: UUID
    binding_id: str = Field(min_length=1)
    state: Literal[
        "no_new_inbound",
        "duplicate_source",
        "ignored",
        "failed",
        "deferred_cancelled",
        "verified",
        "uncertain",
    ]
    provider_called: bool
    action_attempted: bool
    send_action_attempted: bool | None = None
    selection_action_attempted: bool | None = None
    source_key_hashes: tuple[str, ...] = ()
    pacing_plan_id: UUID | None = None
    operation_id: UUID | None = None
    send_status: str | None = None
    error_code: str | None = None
    handoff: dict[str, object] | None = None

    def model_post_init(self, __context: object, /) -> None:
        if self.send_action_attempted is None:
            self.send_action_attempted = self.action_attempted


class OneShotAttemptLedger:
    """Durably prevents a new attempt id from resending the same source key."""

    def __init__(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS one_shot_attempts(
              attempt_id TEXT PRIMARY KEY,
              binding_id TEXT NOT NULL,
              state TEXT NOT NULL,
              operation_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS one_shot_sources(
              binding_id TEXT NOT NULL,
              source_message_key TEXT NOT NULL,
              attempt_id TEXT NOT NULL,
              PRIMARY KEY(binding_id,source_message_key),
              FOREIGN KEY(attempt_id) REFERENCES one_shot_attempts(attempt_id)
            );
            """
        )

    def claim(
        self, *, attempt_id: UUID, binding_id: str, source_keys: tuple[str, ...]
    ) -> bool:
        if not source_keys or len(set(source_keys)) != len(source_keys):
            raise ValueError("one-shot source keys must be non-empty and unique")
        now = datetime.now(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO one_shot_attempts VALUES(?,?,?,?,?,?)",
                (str(attempt_id), binding_id, "claimed", None, now, now),
            )
            for source_key in source_keys:
                self.connection.execute(
                    "INSERT INTO one_shot_sources VALUES(?,?,?)",
                    (binding_id, source_key, str(attempt_id)),
                )
            self.connection.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.connection.execute("ROLLBACK")
            return False
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def update(
        self, *, attempt_id: UUID, state: str, operation_id: UUID | None = None
    ) -> None:
        changed = self.connection.execute(
            """UPDATE one_shot_attempts
               SET state=?,operation_id=COALESCE(?,operation_id),updated_at=?
               WHERE attempt_id=?""",
            (
                state,
                str(operation_id) if operation_id is not None else None,
                datetime.now(UTC).isoformat(),
                str(attempt_id),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("ONE_SHOT_ATTEMPT_NOT_CLAIMED")

    def close(self) -> None:
        self.connection.close()


def _source_hashes(source_keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(item.encode("utf-8")).hexdigest() for item in source_keys
    )


def _handoff_proves_fresh_verify(
    handoff: object, *, operation_id: UUID
) -> bool:
    if not isinstance(handoff, dict):
        return False
    first_pid = handoff.get("commit_worker_process_id")
    second_pid = handoff.get("verify_worker_process_id")
    try:
        commit_epoch = UUID(str(handoff.get("commit_worker_epoch")))
        health_epoch = UUID(str(handoff.get("verify_health_worker_epoch")))
        verify_epoch = UUID(str(handoff.get("verify_worker_epoch")))
    except ValueError:
        return False
    return bool(
        handoff.get("operation_id") == str(operation_id)
        and isinstance(first_pid, int)
        and not isinstance(first_pid, bool)
        and first_pid > 0
        and isinstance(second_pid, int)
        and not isinstance(second_pid, bool)
        and second_pid > 0
        and first_pid != second_pid
        and commit_epoch != verify_epoch
        and health_epoch == verify_epoch
        and handoff.get("first_worker_retired") is True
        and handoff.get("verify_status") == "ok"
    )


async def run_one_shot_reply(
    *,
    app,
    ledger: OneShotAttemptLedger,
    binding: QQIdentityBinding,
    attempt_id: UUID,
    max_wait_seconds: float,
) -> OneShotReplyResult:
    """Observe, plan once, and dispatch at most one exact target operation."""

    deadline = time.monotonic() + max_wait_seconds
    entry_state = getattr(
        app.state, "one_shot_observation_execution_state", None
    )
    binding_revision, _, paused, _, global_paused = (
        entry_state(binding.hub_conversation_id)
        if callable(entry_state)
        else app.state.execution_state(binding.hub_conversation_id)
    )
    if paused or global_paused or binding.conversation_type != "direct":
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            error_code="ONE_SHOT_TARGET_NOT_ACTIVE_DIRECT",
        )

    emitted = await app.coordinator.observe_driver(
        app.driver, binding.hub_conversation_id
    )
    current_binding_revision, _, current_paused, _, current_global_paused = (
        app.state.execution_state(binding.hub_conversation_id)
    )
    if (
        current_binding_revision != binding_revision
        or current_paused
        or current_global_paused
    ):
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            error_code="ONE_SHOT_OBSERVATION_INCOMPLETE",
        )
    if "new_message" not in emitted:
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="no_new_inbound",
            provider_called=False,
            action_attempted=False,
        )
    _, conversation_revision = app.state.revisions(binding.hub_conversation_id)
    pending_job = app.state.pending_planning_job_for(
        binding.hub_conversation_id,
        expected_revision=conversation_revision,
        one_shot_attempt_id=attempt_id,
    )
    if pending_job is None or int(pending_job["binding_revision"]) != binding_revision:
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            error_code="ONE_SHOT_PLANNING_JOB_UNAVAILABLE",
        )
    source_keys = tuple(json.loads(pending_job["source_keys_json"]))
    source_hashes = _source_hashes(source_keys)
    if not ledger.claim(
        attempt_id=attempt_id,
        binding_id=binding.binding_id,
        source_keys=source_keys,
    ):
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="duplicate_source",
            provider_called=False,
            action_attempted=False,
            source_key_hashes=source_hashes,
            error_code="ONE_SHOT_DUPLICATE_SOURCE",
        )

    app.coordinator.dispatch_events(
        conversation_id=binding.hub_conversation_id,
        one_shot_attempt_id=attempt_id,
    )
    job = app.state.claim_planning_job_for(
        binding.hub_conversation_id,
        expected_revision=conversation_revision,
        one_shot_attempt_id=attempt_id,
    )
    if job is None or int(job["binding_revision"]) != binding_revision:
        ledger.update(attempt_id=attempt_id, state="failed")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            source_key_hashes=source_hashes,
            error_code="ONE_SHOT_PLANNING_JOB_UNAVAILABLE",
        )

    ledger.update(attempt_id=attempt_id, state="provider_pending")
    planning_outcome = await app.planning.run_claimed(
        job, max_reply_segments=1
    )
    if planning_outcome != "scheduled":
        state = "ignored" if planning_outcome == "ignored" else "failed"
        ledger.update(attempt_id=attempt_id, state=state)
        job_state = app.state.connection.execute(
            """SELECT error_code FROM runtime_planning_jobs
               WHERE conversation_id=?""",
            (binding.hub_conversation_id,),
        ).fetchone()
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state=state,
            provider_called=True,
            action_attempted=False,
            source_key_hashes=source_hashes,
            error_code=(
                str(job_state["error_code"]).upper()
                if job_state is not None and job_state["error_code"]
                else None
            ),
        )

    artifact = app.state.plan_artifact_for_revision(
        binding.hub_conversation_id,
        conversation_revision,
        one_shot_attempt_id=attempt_id,
    )
    pacing_plan_id = UUID(str(artifact["pacing_plan_id"]))
    planner = json.loads(artifact["planner_json"])
    if len(planner.get("reply_segments", [])) != 1:
        app.pacing.cancel_plan(
            pacing_plan_id, CancellationReason.ONE_SHOT_TIMEOUT
        )
        ledger.update(attempt_id=attempt_id, state="failed")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=True,
            action_attempted=False,
            source_key_hashes=source_hashes,
            pacing_plan_id=pacing_plan_id,
            error_code="ONE_SHOT_SEGMENT_LIMIT",
        )

    ledger.update(attempt_id=attempt_id, state="waiting_due")
    while time.monotonic() < deadline:
        consumed, operation = await app.due.dispatch_exact(
            pacing_plan_id=pacing_plan_id,
            conversation_id=binding.hub_conversation_id,
            segment_index=0,
            one_shot_attempt_id=attempt_id,
        )
        if consumed:
            if operation is None:
                ledger.update(attempt_id=attempt_id, state="failed")
                return OneShotReplyResult(
                    attempt_id=attempt_id,
                    binding_id=binding.binding_id,
                    state="failed",
                    provider_called=True,
                    action_attempted=False,
                    source_key_hashes=source_hashes,
                    pacing_plan_id=pacing_plan_id,
                    error_code="ONE_SHOT_REVALIDATION_REJECTED",
                )
            handoff = app.driver.last_send_handoff()
            action_attempted = isinstance(handoff, dict)
            selection_action_attempted = (
                handoff.get("selection_action_attempted")
                if isinstance(handoff, dict)
                and isinstance(handoff.get("selection_action_attempted"), bool)
                else None
            )
            if (
                operation.status is SendStatus.VERIFIED
                and _handoff_proves_fresh_verify(
                    handoff, operation_id=operation.operation_id
                )
            ):
                state = "verified"
            elif operation.status is SendStatus.UNCERTAIN or action_attempted:
                state = "uncertain"
            else:
                state = "failed"
            ledger.update(
                attempt_id=attempt_id,
                state=state,
                operation_id=operation.operation_id,
            )
            return OneShotReplyResult(
                attempt_id=attempt_id,
                binding_id=binding.binding_id,
                state=state,
                provider_called=True,
                action_attempted=action_attempted,
                send_action_attempted=action_attempted,
                selection_action_attempted=selection_action_attempted,
                source_key_hashes=source_hashes,
                pacing_plan_id=pacing_plan_id,
                operation_id=operation.operation_id,
                send_status=operation.status.value,
                error_code=(
                    str(operation.error_code).upper()
                    if operation.error_code
                    else None
                ),
                handoff=handoff,
            )
        await asyncio.sleep(0.2)

    app.pacing.cancel_plan(pacing_plan_id, CancellationReason.ONE_SHOT_TIMEOUT)
    ledger.update(attempt_id=attempt_id, state="deferred_cancelled")
    return OneShotReplyResult(
        attempt_id=attempt_id,
        binding_id=binding.binding_id,
        state="deferred_cancelled",
        provider_called=True,
        action_attempted=False,
        source_key_hashes=source_hashes,
        pacing_plan_id=pacing_plan_id,
        error_code="ONE_SHOT_WAIT_TIMEOUT",
    )
