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


_PRE_PROVIDER_EVIDENCE_STATES = frozenset(
    {
        "claimed",
        "events_dispatched",
        "planning_claimed",
        "pre_provider_timeout",
        "pre_provider_failed",
    }
)
_SAFE_RECLAIM_STATES = frozenset(
    {"pre_provider_timeout", "pre_provider_failed"}
)


class _OneShotDeadlineExpired(TimeoutError):
    """The total one-shot budget elapsed before or during one async boundary."""

    def __init__(self, *, started: bool) -> None:
        super().__init__("one-shot deadline expired")
        self.started = started


def _safe_pre_provider_evidence(phase: str) -> str:
    """Serialize the only evidence that permits a later source-claim release.

    These records are written only by an explicit return path that stopped
    before the provider boundary.  In-progress records can have the same
    static values but do not prove their process is dead, so they are never
    reclaimable.  Old rows without this evidence are also not reclaimable.
    """

    return json.dumps(
        {
            "phase": phase,
            "provider_started": False,
            "composer_written": False,
            "send_triggered": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _ambiguous_evidence(phase: str) -> str:
    """Record that the provider/send side-effect boundary may have been crossed."""

    return json.dumps(
        {
            "phase": phase,
            "provider_started": True,
            "composer_written": None,
            "send_triggered": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _evidence_proves_pre_provider_safe(value: object) -> bool:
    try:
        evidence = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(evidence, dict)
        and evidence.get("provider_started") is False
        and evidence.get("composer_written") is False
        and evidence.get("send_triggered") is False
    )


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
    """Durably fences source batches across bounded one-shot attempts.

    A source batch is normally single-use.  The sole automatic recovery
    exception is an exact replacement attempt for a batch in an explicit,
    terminal pre-provider state.  In-progress, missing, legacy, and ambiguous
    records remain duplicate refusals; crash recovery needs an external death
    witness and remains a manual operation.
    """

    def __init__(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
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
              updated_at TEXT NOT NULL,
              evidence_json TEXT NOT NULL DEFAULT '{}'
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
        columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(one_shot_attempts)"
            ).fetchall()
        }
        if "evidence_json" not in columns:
            self.connection.execute(
                "ALTER TABLE one_shot_attempts "
                "ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
            )

    def claim(
        self, *, attempt_id: UUID, binding_id: str, source_keys: tuple[str, ...]
    ) -> bool:
        if not source_keys or len(set(source_keys)) != len(source_keys):
            raise ValueError("one-shot source keys must be non-empty and unique")
        now = datetime.now(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in source_keys)
            claimed_rows = self.connection.execute(
                """SELECT source_message_key,attempt_id FROM one_shot_sources
                   WHERE binding_id=? AND source_message_key IN ("""
                + placeholders
                + ")",
                (binding_id, *source_keys),
            ).fetchall()
            if claimed_rows:
                owner_ids = {str(row["attempt_id"]) for row in claimed_rows}
                if (
                    len(claimed_rows) != len(source_keys)
                    or len(owner_ids) != 1
                    or str(attempt_id) in owner_ids
                ):
                    self.connection.execute("ROLLBACK")
                    return False
                owner_id = owner_ids.pop()
                owner_sources = self.connection.execute(
                    """SELECT source_message_key FROM one_shot_sources
                       WHERE binding_id=? AND attempt_id=?""",
                    (binding_id, owner_id),
                ).fetchall()
                owner = self.connection.execute(
                    """SELECT state,evidence_json FROM one_shot_attempts
                       WHERE attempt_id=? AND binding_id=?""",
                    (owner_id, binding_id),
                ).fetchone()
                if (
                    owner is None
                    or {str(row["source_message_key"]) for row in owner_sources}
                    != set(source_keys)
                    or str(owner["state"]) not in _SAFE_RECLAIM_STATES
                    or not _evidence_proves_pre_provider_safe(
                        owner["evidence_json"]
                    )
                ):
                    self.connection.execute("ROLLBACK")
                    return False
                release_evidence = json.dumps(
                    {
                        "phase": "released_safe",
                        "provider_started": False,
                        "composer_written": False,
                        "send_triggered": False,
                        "replaced_by_attempt_id": str(attempt_id),
                        "prior_state": str(owner["state"]),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.connection.execute(
                    """UPDATE one_shot_attempts
                       SET state='released_safe',evidence_json=?,updated_at=?
                       WHERE attempt_id=? AND binding_id=?""",
                    (release_evidence, now, owner_id, binding_id),
                )
                self.connection.execute(
                    "DELETE FROM one_shot_sources WHERE binding_id=? AND attempt_id=?",
                    (binding_id, owner_id),
                )
            self.connection.execute(
                """INSERT INTO one_shot_attempts(
                   attempt_id,binding_id,state,operation_id,created_at,updated_at,
                   evidence_json) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(attempt_id),
                    binding_id,
                    "claimed",
                    None,
                    now,
                    now,
                    _safe_pre_provider_evidence("claimed"),
                ),
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
        self,
        *,
        attempt_id: UUID,
        state: str,
        operation_id: UUID | None = None,
        evidence_json: str | None = None,
    ) -> None:
        if evidence_json is None:
            evidence_json = (
                _safe_pre_provider_evidence(state)
                if state in _PRE_PROVIDER_EVIDENCE_STATES
                else _ambiguous_evidence(state)
            )
        changed = self.connection.execute(
            """UPDATE one_shot_attempts
               SET state=?,operation_id=COALESCE(?,operation_id),updated_at=?,
                   evidence_json=?
               WHERE attempt_id=?""",
            (
                state,
                str(operation_id) if operation_id is not None else None,
                datetime.now(UTC).isoformat(),
                evidence_json,
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


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _await_before_deadline(factory, *, deadline: float):
    """Run one cancellable async boundary within the one-shot total budget.

    ``factory`` is intentionally lazy: if no budget remains, it is never
    invoked, so no coroutine or downstream action can be started by accident.
    A timeout after it starts is explicitly distinguishable by the caller and
    must be treated as an ambiguous side-effect boundary.
    """

    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        raise _OneShotDeadlineExpired(started=False)
    try:
        return await asyncio.wait_for(factory(), timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise _OneShotDeadlineExpired(started=True) from exc


def _cancel_plan(app, pacing_plan_id: UUID) -> bool:
    """Require durable confirmation that the exact plan changed to cancelled."""

    try:
        changed = app.pacing.cancel_plan(
            pacing_plan_id, CancellationReason.ONE_SHOT_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - caller quarantines a failed cleanup
        return False
    return changed is True or (type(changed) is int and changed == 1)


def _planned_pacing_plan_id(
    app,
    *,
    conversation_id: str,
    conversation_revision: int,
    attempt_id: UUID,
) -> UUID | None:
    """Read an exact attempt-owned plan without widening a timeout cleanup."""

    try:
        artifact = app.state.plan_artifact_for_revision(
            conversation_id,
            conversation_revision,
            one_shot_attempt_id=attempt_id,
        )
        return UUID(str(artifact["pacing_plan_id"]))
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return None


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

    if max_wait_seconds <= 0:
        raise ValueError("one-shot max_wait_seconds must be positive")
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

    try:
        emitted = await _await_before_deadline(
            lambda: app.coordinator.observe_driver(
                app.driver, binding.hub_conversation_id
            ),
            deadline=deadline,
        )
    except _OneShotDeadlineExpired:
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            error_code="ONE_SHOT_OBSERVE_TIMEOUT",
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
    if _remaining_seconds(deadline) <= 0:
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            error_code="ONE_SHOT_PRE_PROVIDER_TIMEOUT",
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

    def pre_provider_timeout() -> OneShotReplyResult:
        ledger.update(attempt_id=attempt_id, state="pre_provider_timeout")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            source_key_hashes=source_hashes,
            error_code="ONE_SHOT_PRE_PROVIDER_TIMEOUT",
        )

    def cancel_after_deadline(
        pacing_plan_id: UUID,
    ) -> OneShotReplyResult:
        if _cancel_plan(app, pacing_plan_id):
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
        ledger.update(attempt_id=attempt_id, state="timeout_cleanup_failed")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="uncertain",
            provider_called=True,
            action_attempted=False,
            source_key_hashes=source_hashes,
            pacing_plan_id=pacing_plan_id,
            error_code="ONE_SHOT_TIMEOUT_CLEANUP_FAILED",
        )

    if _remaining_seconds(deadline) <= 0:
        return pre_provider_timeout()
    app.coordinator.dispatch_events(
        conversation_id=binding.hub_conversation_id,
        one_shot_attempt_id=attempt_id,
    )
    ledger.update(attempt_id=attempt_id, state="events_dispatched")
    if _remaining_seconds(deadline) <= 0:
        return pre_provider_timeout()
    job = app.state.claim_planning_job_for(
        binding.hub_conversation_id,
        expected_revision=conversation_revision,
        one_shot_attempt_id=attempt_id,
    )
    if job is None or int(job["binding_revision"]) != binding_revision:
        ledger.update(attempt_id=attempt_id, state="pre_provider_failed")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="failed",
            provider_called=False,
            action_attempted=False,
            source_key_hashes=source_hashes,
            error_code="ONE_SHOT_PLANNING_JOB_UNAVAILABLE",
        )

    ledger.update(attempt_id=attempt_id, state="planning_claimed")
    if _remaining_seconds(deadline) <= 0:
        return pre_provider_timeout()
    ledger.update(attempt_id=attempt_id, state="provider_pending")
    try:
        planning_outcome = await _await_before_deadline(
            lambda: app.planning.run_claimed(job, max_reply_segments=1),
            deadline=deadline,
        )
    except _OneShotDeadlineExpired as expired:
        if not expired.started:
            return pre_provider_timeout()
        pacing_plan_id = _planned_pacing_plan_id(
            app,
            conversation_id=binding.hub_conversation_id,
            conversation_revision=conversation_revision,
            attempt_id=attempt_id,
        )
        if pacing_plan_id is not None and not _cancel_plan(app, pacing_plan_id):
            ledger.update(attempt_id=attempt_id, state="timeout_cleanup_failed")
            return OneShotReplyResult(
                attempt_id=attempt_id,
                binding_id=binding.binding_id,
                state="uncertain",
                provider_called=True,
                action_attempted=False,
                source_key_hashes=source_hashes,
                pacing_plan_id=pacing_plan_id,
                error_code="ONE_SHOT_TIMEOUT_CLEANUP_FAILED",
            )
        ledger.update(attempt_id=attempt_id, state="provider_timeout_ambiguous")
        return OneShotReplyResult(
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            state="uncertain",
            provider_called=True,
            action_attempted=False,
            source_key_hashes=source_hashes,
            pacing_plan_id=pacing_plan_id,
            error_code="ONE_SHOT_PROVIDER_TIMEOUT",
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
    if _remaining_seconds(deadline) <= 0:
        return cancel_after_deadline(pacing_plan_id)
    planner = json.loads(artifact["planner_json"])
    if len(planner.get("reply_segments", [])) != 1:
        if not _cancel_plan(app, pacing_plan_id):
            ledger.update(attempt_id=attempt_id, state="timeout_cleanup_failed")
            return OneShotReplyResult(
                attempt_id=attempt_id,
                binding_id=binding.binding_id,
                state="uncertain",
                provider_called=True,
                action_attempted=False,
                source_key_hashes=source_hashes,
                pacing_plan_id=pacing_plan_id,
                error_code="ONE_SHOT_TIMEOUT_CLEANUP_FAILED",
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
    while _remaining_seconds(deadline) > 0:
        ledger.update(attempt_id=attempt_id, state="dispatch_pending")
        try:
            consumed, operation = await _await_before_deadline(
                lambda: app.due.dispatch_exact(
                    pacing_plan_id=pacing_plan_id,
                    conversation_id=binding.hub_conversation_id,
                    segment_index=0,
                    one_shot_attempt_id=attempt_id,
                ),
                deadline=deadline,
            )
        except _OneShotDeadlineExpired as expired:
            if expired.started:
                # ``dispatch_exact`` may be blocked in an adapter worker or a
                # non-cancellable provider thread.  Its action boundary has
                # been entered, so only an uncertainty quarantine is honest.
                cancelled = _cancel_plan(app, pacing_plan_id)
                ledger.update(attempt_id=attempt_id, state="dispatch_timeout_ambiguous")
                return OneShotReplyResult(
                    attempt_id=attempt_id,
                    binding_id=binding.binding_id,
                    state="uncertain",
                    provider_called=True,
                    action_attempted=True,
                    source_key_hashes=source_hashes,
                    pacing_plan_id=pacing_plan_id,
                    error_code=(
                        "ONE_SHOT_DISPATCH_TIMEOUT"
                        if cancelled
                        else "ONE_SHOT_DISPATCH_TIMEOUT_CLEANUP_FAILED"
                    ),
                )
            return cancel_after_deadline(pacing_plan_id)
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
        await asyncio.sleep(min(0.2, _remaining_seconds(deadline)))

    return cancel_after_deadline(pacing_plan_id)
