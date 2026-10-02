"""Reversible, owned draft preparation. A ticket is never send authorization."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from messenger_ai.domain import AuthorizedSendCommand, SendOperation, SendStatus


def source_keys_digest(keys: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class _PreparationScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reservation_id: UUID
    nonce: UUID
    outbox_id: int = Field(gt=0, strict=True)
    claim_token: str = Field(min_length=1, max_length=96)
    due_event_id: UUID
    one_shot_attempt_id: UUID | None = None
    account_id: str = Field(min_length=1, max_length=256)
    contact_id: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=256)
    binding_id: str = Field(min_length=1, max_length=256)
    binding_revision: int = Field(ge=1, strict=True)
    conversation_revision: int = Field(ge=1, strict=True)
    global_revision: int = Field(ge=1, strict=True)
    pacing_plan_id: UUID
    segment_index: int = Field(ge=0, le=2, strict=True)
    draft_id: UUID
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_message_keys: tuple[str, ...] = Field(min_length=1)
    source_keys_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_last_message_key: str = Field(min_length=1)
    original_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at: datetime
    deadline_at: datetime
    requested_monotonic_ns: int = Field(ge=0, strict=True)
    deadline_monotonic_ns: int = Field(gt=0, strict=True)

    @field_validator("requested_at", "deadline_at")
    @classmethod
    def _aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("staged times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _scope(self):
        if (self.requested_at >= self.deadline_at
                or self.requested_monotonic_ns >= self.deadline_monotonic_ns
                or any(not key for key in self.source_message_keys)
                or source_keys_digest(self.source_message_keys) != self.source_keys_digest
                or self.expected_last_message_key not in self.source_message_keys):
            raise ValueError("staged preparation scope is inconsistent")
        return self


class DraftPreparationRequest(_PreparationScope):
    body: str = Field(min_length=1)

    @model_validator(mode="after")
    def _body(self):
        if hashlib.sha256(self.body.encode()).hexdigest() != self.body_hash:
            raise ValueError("staged preparation body hash mismatch")
        return self


class PreparedDraftTicket(_PreparationScope):
    run_id: str = Field(min_length=1)
    session_epoch: str = Field(min_length=1)
    surface_epoch: str = Field(min_length=1)
    worker_epoch: str = Field(min_length=1)
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    process_started_at_100ns: int = Field(gt=0, strict=True)
    issued_at: datetime
    expires_at: datetime
    issued_monotonic_ns: int = Field(ge=0, strict=True)
    expires_monotonic_ns: int = Field(gt=0, strict=True)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_sequence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware_ticket(cls, value):
        return cls._aware(value)

    @model_validator(mode="after")
    def _life(self):
        if (not self.requested_at <= self.issued_at < self.expires_at <= self.deadline_at
                or not self.requested_monotonic_ns <= self.issued_monotonic_ns
                < self.expires_monotonic_ns <= self.deadline_monotonic_ns
                or (self.expires_at - self.issued_at).total_seconds() > 15
                or self.expires_monotonic_ns - self.issued_monotonic_ns > 15_000_000_000):
            raise ValueError("prepared ticket lifetime is inconsistent")
        return self

    def is_fresh(self, *, now: datetime, now_monotonic_ns: int) -> bool:
        return (self.issued_at <= now < self.expires_at
                and self.issued_monotonic_ns <= now_monotonic_ns < self.expires_monotonic_ns)


class DraftCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["cleaned", "cleanup_required", "not_owned"]
    reservation_id: UUID
    nonce: UUID
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$")


class StagedPreparationPort(Protocol):
    def binding_id_for(self, conversation_id: str) -> str: ...
    def has_cleanup_obligation(self, account_id: str) -> bool: ...
    def retain_cleanup_hold(self, owner: DraftPreparationRequest | PreparedDraftTicket, *, reason: str) -> None: ...
    async def prepare_draft(self, request: DraftPreparationRequest, *, cancel_event=None) -> PreparedDraftTicket: ...
    async def adopt_prepared(self, ticket: PreparedDraftTicket, command: AuthorizedSendCommand, *,
                             operation_id: UUID, segment_ref: str, binding_revision: int,
                             conversation_revision: int, cancel_event=None) -> SendOperation: ...
    async def abort_owned_ticket(self, owner: DraftPreparationRequest | PreparedDraftTicket, *,
                                 deadline_at: datetime) -> DraftCleanupResult: ...


class StagedPreparationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StagedCleanupRequired(StagedPreparationError):
    pass


_FAILURE_CODES = frozenset("""
    prepare_failure_unknown prepare_cancelled prepare_timeout
    staged_control_cancelled staged_prepare_timeout staged_ticket_invalid staged_ticket_scope_mismatch staged_ticket_expired
    staged_cleanup_timeout staged_cleanup_unproven
    hybrid_ui_action_failed hybrid_deadline_exhausted hybrid_deadline_invalid hybrid_revoked hybrid_request_future
    ui_reserved reservation_already_used source_snapshot_unproven unknown_binding operation_binding_mismatch stale_context
    identity_scope_mismatch identity_process_lifetime_mismatch identity_capture_order_invalid identity_chat_correlation_unproven
    identity_target_mismatch identity_frame_mismatch identity_mode_unproven identity_observation_mismatch
    identity_evidence_version_mismatch identity_profile_key_mismatch identity_chat_drift identity_deadline_exhausted
    identity_conversation_not_direct identity_group_probe_incomplete identity_message_tail_unproven
    identity_profile_acquisition_invalid identity_profile_scope_mismatch identity_profile_capture_failed
    identity_profile_capture_revoked identity_profile_evidence_invalid identity_profile_mismatch identity_profile_unproven
    identity_profile_capture_input_invalid identity_profile_capture_order_invalid identity_profile_evidence_unavailable
    identity_profile_expectation_missing identity_profile_report_invalid identity_profile_window_mismatch
    identity_profile_recovery_failed identity_evidence_stale identity_composer_not_empty
    prepare_write_budget_exhausted target_drift composer_drift composer_not_empty
    composer_has_no_readable_pattern send_input_incomplete composer_focus_or_scope_drift composer_focus_drift
    composer_scope_rejected composer_readback_mismatch composer_value_not_writable composer_clear_precondition_failed
    composer_clear_not_verified hybrid_input_fence_missing hybrid_input_boundary_missing hybrid_composer_metadata_mismatch
    hybrid_composer_scope_unproven hybrid_parent_scope_unproven hybrid_group_adjacency_unproven
    hybrid_guard_expired hybrid_guard_invalid hybrid_mode_invalid hybrid_paused hybrid_selection_metadata_invalid
    hybrid_snapshot_invalid hybrid_window_geometry_invalid direction_unknown
    hybrid_command_invalid hybrid_command_unsupported not_prepared send_pattern_unavailable
    prepared_evidence_mismatch prepared_ticket_mismatch prepared_ticket_already_adopted prepared_ticket_expired
    commit_already_attempted commit_not_attempted committed_cannot_abort outbound_receipt_not_unique
    hybrid_control_cancelled hybrid_preparation_scope_changed hybrid_cleanup_obligation hybrid_cursor_baseline_missing
    hybrid_cursor_source_unsettled hybrid_cursor_changed hybrid_reservation_replayed hybrid_prepared_proof_mismatch
    hybrid_reservation_held hybrid_prepare_unsettled hybrid_reservation_not_owned hybrid_reservation_scope_mismatch
    hybrid_owned_cleanup_unproven hybrid_commit_intent_cleanup_forbidden needs_manual_cleanup abort_not_owner
    hybrid_process_deadline_exhausted hybrid_process_revoked hybrid_process_reap_failed hybrid_process_request_invalid
    hybrid_process_request_too_large hybrid_process_reply_too_large hybrid_process_response_invalid
    hybrid_process_response_mismatch hybrid_process_round_required hybrid_process_scope_mismatch
    hybrid_process_supervision_unavailable hybrid_process_worker_exited hybrid_process_worker_failed
    hybrid_session_round_unavailable hybrid_session_deadline_exhausted hybrid_session_owner_unavailable
    hybrid_session_cleanup_required hybrid_session_closed hybrid_session_revoked hybrid_session_worker_failed
""".split())


# Fixed codes emitted by profile_identity capture and its strict foreground
# parser. These are diagnostics only; public prepare/pause codes stay unchanged.
_HELPER_FAILURE_CODES = frozenset("""
    GUEST_HEADER_ENVIRONMENT_NOT_CERTIFIED ACTIVE_HEADER_AMBIGUOUS
    WINDOW_STATE_NOT_CERTIFIED GUEST_FOREGROUND_ENVIRONMENT_NOT_CERTIFIED
    QQ_FOREGROUND CHAT_SHELL_NOT_CERTIFIED TREE_TRUNCATED IDENTITY_STDIN_INVALID
    ACTIVE_HEADER_DIGEST_MISMATCH HEADER_INVOKE_UNAVAILABLE
    PROFILE_WINDOW_PRECONDITION_FAILED HEADER_INVOKE_FAILED
    PROFILE_VIEW_NOT_SEPARATE_WINDOW PROFILE_WINDOW_AMBIGUOUS
    PROFILE_UIA_ROOT_UNAVAILABLE RESTORATION_FAILED FOREGROUND_CHANGED
    QQ_WINDOW_NOT_RESTORED ORIGINAL_CONVERSATION_NOT_RESTORED PENDING_NO_STABLE_SIGNAL
    CAPTURE_INPUT_INVALID CAPTURE_CLOCK_INVALID CAPTURE_DEADLINE_EXHAUSTED
    IDENTITY_KEY_UNAVAILABLE IDENTITY_KEY_MISSING IDENTITY_KEY_INVALID
    HELPER_OUTPUT_LIMIT HELPER_TIMEOUT HELPER_START_FAILED HELPER_INVALID_OUTPUT HELPER_FAILED
    HEADER_SCOPE_MISMATCH GUEST_ENVIRONMENT_MISMATCH HEADER_PRIVACY_CONTRACT_FAILED
    HEADER_DIGEST_INVALID RIGHT_REGION_DIGEST_INVALID
    MISSING_FIELD INVALID_DIGEST INVALID_OUTPUT INVALID_SCHEMA PRIVACY_CONTRACT_FAILED
    RECOVERY_CONTRACT_FAILED VERSION_MISMATCH MODE_MISMATCH STATUS_MISMATCH
    HWND_OR_PROCESS_MISMATCH WINDOW_NOT_MAXIMIZED HEADER_MISMATCH ENVIRONMENT_MISMATCH
    SELECTOR_MISMATCH HEADER_NOT_UNIQUE PROFILE_CANDIDATE_NOT_UNIQUE EVIDENCE_TYPE_MISMATCH
    QQ_FOREGROUND_NOT_RESTORED RIGHT_REGION_MISMATCH
""".split())


class PreparationFailureDiagnostic(BaseModel):
    """Content-free observation only; no permission or cleanup proof."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["staged_prepare_failure_v1"] = "staged_prepare_failure_v1"
    reservation_id: UUID
    nonce: UUID
    code: str
    helper_code: str | None = Field(default=None, strict=True)
    helper_stage: Literal["header", "profile"] | None = None
    stage: Literal["admission", "identity", "ownership", "before_write", "write", "after_write", "ticket",
                   "bridge_worker", "bridge_ticket", "bridge_proof", "bridge_persist", "bridge_scope",
                   "bridge_cursor", "controller_wait", "cleanup_abort", "cleanup_hold"]
    requested_at: datetime
    deadline_at: datetime
    requested_monotonic_ns: int = Field(ge=0, strict=True)
    deadline_monotonic_ns: int = Field(gt=0, strict=True)
    observed_at: datetime
    observed_monotonic_ns: int = Field(ge=0, strict=True)
    elapsed_utc_seconds: float = Field(allow_inf_nan=False)
    remaining_utc_seconds: float = Field(allow_inf_nan=False)
    elapsed_monotonic_ns: int = Field(strict=True)
    remaining_monotonic_ns: int = Field(strict=True)
    input_attempted: bool | None = Field(default=None, strict=True)
    ticket_known: bool = Field(strict=True)

    @field_validator("code")
    @classmethod
    def _finite_code(cls, value):
        if value not in _FAILURE_CODES:
            raise ValueError("unknown preparation diagnostic code")
        return value

    @field_validator("helper_code")
    @classmethod
    def _finite_helper_code(cls, value):
        if value is not None and value not in _HELPER_FAILURE_CODES:
            raise ValueError("unknown preparation helper diagnostic code")
        return value

    @field_validator("requested_at", "deadline_at", "observed_at")
    @classmethod
    def _aware_times(cls, value):
        return _PreparationScope._aware(value)

    @model_validator(mode="after")
    def _timing(self):
        if self.helper_stage is not None and self.helper_code is None:
            raise ValueError("helper diagnostic stage requires a known helper code")
        if (self.elapsed_utc_seconds != (self.observed_at - self.requested_at).total_seconds()
                or self.remaining_utc_seconds != (self.deadline_at - self.observed_at).total_seconds()
                or self.elapsed_monotonic_ns != self.observed_monotonic_ns - self.requested_monotonic_ns
                or self.remaining_monotonic_ns != self.deadline_monotonic_ns - self.observed_monotonic_ns):
            raise ValueError("preparation diagnostic timing is inconsistent")
        return self


def preparation_failure(request, *, code, stage, now, tick, input_attempted=None, ticket_known=False,
                        helper_code=None, helper_stage=None):
    helper_code = helper_code if type(helper_code) is str and helper_code in _HELPER_FAILURE_CODES else None
    helper_stage = helper_stage if helper_code is not None and type(helper_stage) is str and helper_stage in {"header", "profile"} else None
    return PreparationFailureDiagnostic(
        reservation_id=request.reservation_id, nonce=request.nonce,
        code=code if type(code) is str and code in _FAILURE_CODES else "prepare_failure_unknown", stage=stage,
        helper_code=helper_code, helper_stage=helper_stage,
        requested_at=request.requested_at, deadline_at=request.deadline_at,
        requested_monotonic_ns=request.requested_monotonic_ns, deadline_monotonic_ns=request.deadline_monotonic_ns,
        observed_at=now, observed_monotonic_ns=tick,
        elapsed_utc_seconds=(now - request.requested_at).total_seconds(),
        remaining_utc_seconds=(request.deadline_at - now).total_seconds(),
        elapsed_monotonic_ns=tick - request.requested_monotonic_ns,
        remaining_monotonic_ns=request.deadline_monotonic_ns - tick,
        input_attempted=input_attempted, ticket_known=ticket_known)


def matching_prepare_failure(value, request):
    try:
        if type(value) is not PreparationFailureDiagnostic:
            return None
        value = PreparationFailureDiagnostic.model_validate(value.model_dump())
        if any(getattr(value, field) != getattr(request, field) for field in (
                "reservation_id", "nonce", "requested_at", "deadline_at",
                "requested_monotonic_ns", "deadline_monotonic_ns")):
            return None
        return value
    except BaseException:
        return None


def validate_ticket(ticket: PreparedDraftTicket, request: DraftPreparationRequest, *, now, tick) -> None:
    if not isinstance(ticket, PreparedDraftTicket):
        raise StagedPreparationError("staged_ticket_invalid")
    # Revalidate model_copy/update output as well as ordinary IPC parsing.
    try:
        PreparedDraftTicket.model_validate(ticket.model_dump())
    except ValueError as exc:
        raise StagedPreparationError("staged_ticket_invalid") from exc
    if any(getattr(ticket, key) != getattr(request, key) for key in _PreparationScope.model_fields):
        raise StagedPreparationError("staged_ticket_scope_mismatch")
    if not ticket.is_fresh(now=now, now_monotonic_ns=tick):
        raise StagedPreparationError("staged_ticket_expired")


class ControlCancellation:
    """Sources are checked synchronously at every stage, including pause intent."""
    def __init__(self, control_cancelled: Callable[[], bool] | None = None):
        self.local = asyncio.Event()
        self.control_cancelled = control_cancelled or (lambda: False)

    def is_set(self):
        return self.local.is_set() or self.control_cancelled()

    def set(self):
        self.local.set()

    async def wait(self):
        while not self.is_set():
            await asyncio.sleep(.01)
        return True


def _discard(task):
    if not task.cancelled():
        task.exception()


class StagedPreparationController:
    def __init__(self, port: StagedPreparationPort, *, clock, monotonic_ns=time.monotonic_ns,
                 control_cancelled=None, cleanup_timeout_seconds=45.0):
        if (isinstance(cleanup_timeout_seconds, bool) or not isinstance(cleanup_timeout_seconds, (int, float))
                or not math.isfinite(cleanup_timeout_seconds) or not 0 < cleanup_timeout_seconds <= 45):
            raise ValueError("cleanup timeout must be finite, positive and at most 45 seconds")
        self.port, self.clock, self.monotonic_ns = port, clock, monotonic_ns
        self.cancel = ControlCancellation(control_cancelled)
        self.cleanup_timeout_seconds = cleanup_timeout_seconds

    async def prepare(self, request):
        if self.cancel.is_set():
            raise StagedPreparationError("staged_control_cancelled")
        task = asyncio.create_task(self.port.prepare_draft(request, cancel_event=self.cancel))
        cancellation = asyncio.create_task(self.cancel.wait())
        cancellation.add_done_callback(_discard)
        timeout = min((request.deadline_at - self.clock()).total_seconds(),
                      (request.deadline_monotonic_ns - self.monotonic_ns()) / 1e9)
        ticket_known = False
        try:
            done, _ = await asyncio.wait({task, cancellation}, timeout=max(0, timeout),
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.cancel.is_set():
                raise StagedPreparationError("staged_control_cancelled")
            if task not in done:
                raise StagedPreparationError("staged_prepare_timeout")
            ticket = task.result()
            ticket_known = isinstance(ticket, PreparedDraftTicket)
            validate_ticket(ticket, request, now=self.clock(), tick=self.monotonic_ns())
            return ticket
        except BaseException as exc:
            # Persist our first observed timeout/cancellation before revoking
            # the task; its later cancellation/cleanup must not erase it.
            try:
                recorder = getattr(self.port, "record_prepare_failure", None)
                if callable(recorder):
                    code = exc.code if isinstance(exc, StagedPreparationError) else (
                        "prepare_cancelled" if isinstance(exc, asyncio.CancelledError) else "prepare_failure_unknown")
                    recorder(request, preparation_failure(request, code=code, stage="controller_wait",
                        now=self.clock(), tick=self.monotonic_ns(), ticket_known=ticket_known))
            except BaseException:
                pass
            self.cancel.set()
            if not task.done():
                task.cancel()
            task.add_done_callback(_discard)
            raise
        finally:
            cancellation.cancel()

    async def abort(self, owner):
        # Cleanup is independent of the cancelled input capability. The port
        # may clear only this exact reservation, never an arbitrary composer.
        deadline = self.clock() + timedelta(seconds=self.cleanup_timeout_seconds)
        monotonic_deadline = self.monotonic_ns() + int(self.cleanup_timeout_seconds * 1e9)
        envelope_deadline = time.monotonic() + self.cleanup_timeout_seconds
        task = asyncio.create_task(self.port.abort_owned_ticket(owner, deadline_at=deadline))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.cleanup_timeout_seconds)
            if (task not in done or self.clock() >= deadline
                    or self.monotonic_ns() >= monotonic_deadline or time.monotonic() >= envelope_deadline):
                raise StagedCleanupRequired("staged_cleanup_timeout")
            result = task.result()
            if isinstance(result, DraftCleanupResult):
                result = DraftCleanupResult.model_validate(result.model_dump())
            if (not isinstance(result, DraftCleanupResult) or result.status != "cleaned" or result.error_code is not None
                    or result.reservation_id != owner.reservation_id or result.nonce != owner.nonce):
                raise StagedCleanupRequired("staged_cleanup_unproven")
            return result
        except BaseException as exc:
            if not task.done():
                task.cancel()
                task.add_done_callback(_discard)
            try:
                self.port.retain_cleanup_hold(owner, reason="staged_cleanup_unproven")
            except BaseException:
                # The port must not invent ownership when no reservation was
                # persisted. A failed hold write still leaves cleanup unproven;
                # the due coordinator must receive its stable hold contract.
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StagedCleanupRequired("staged_cleanup_unproven") from None


class StagedPrepareAdapter:
    """Only fast adoption of this ticket; no fallback to UI preparation."""
    def __init__(self, port, ticket, *, clock, monotonic_ns=time.monotonic_ns, cancel=None,
                 cleanup_timeout_seconds=45.0, claim_current=None):
        self.port, self.ticket, self.clock, self.monotonic_ns = port, ticket, clock, monotonic_ns
        self.cancel = cancel or ControlCancellation()
        self.adopted = False
        self.confirmed = False
        self.claim_current = claim_current or (lambda: True)
        self.cleanup_timeout_seconds = cleanup_timeout_seconds

    def current(self):
        return (not self.cancel.is_set() and self.claim_current()
                and self.ticket.is_fresh(now=self.clock(), now_monotonic_ns=self.monotonic_ns()))

    async def abort(self):
        controller = StagedPreparationController(self.port, clock=self.clock,
            monotonic_ns=self.monotonic_ns, cleanup_timeout_seconds=self.cleanup_timeout_seconds)
        return await controller.abort(self.ticket)

    async def prepare_send(self, command, *, operation_id, segment_ref, binding_revision, conversation_revision):
        ticket = self.ticket
        if (self.adopted or not self.current()
                or command.draft_id != ticket.draft_id or command.conversation_id != ticket.conversation_id
                or command.text_hash != ticket.body_hash or command.expected_last_message_key != ticket.expected_last_message_key
                or command.idempotency_key != f"m10:{ticket.pacing_plan_id}:{ticket.segment_index}"
                or segment_ref != f"{ticket.pacing_plan_id}:{ticket.segment_index}"
                or (binding_revision, conversation_revision) != (ticket.binding_revision, ticket.conversation_revision)):
            raise StagedPreparationError("staged_adoption_scope_mismatch")
        # Mark locally before IPC; a lost acknowledgement cannot trigger a
        # second adoption or a slow PREPARE of the same reservation.
        self.adopted = True
        task = asyncio.create_task(self.port.adopt_prepared(ticket, command, operation_id=operation_id,
            segment_ref=segment_ref, binding_revision=binding_revision,
            conversation_revision=conversation_revision, cancel_event=self.cancel))
        cancellation = asyncio.create_task(self.cancel.wait())
        cancellation.add_done_callback(_discard)
        timeout = min((ticket.expires_at - self.clock()).total_seconds(),
                      (ticket.expires_monotonic_ns - self.monotonic_ns()) / 1e9)
        try:
            done, _ = await asyncio.wait({task, cancellation}, timeout=max(0, timeout),
                                         return_when=asyncio.FIRST_COMPLETED)
            if task not in done or not self.current():
                raise StagedPreparationError("staged_adoption_expired_or_cancelled")
            result = task.result()
            if (not isinstance(result, SendOperation) or result.status is not SendStatus.PREPARED
                    or result.operation_id != operation_id or result.draft_id != command.draft_id
                    or result.idempotency_key != command.idempotency_key):
                raise StagedPreparationError("staged_adoption_unproven")
            self.confirmed = True
            return result
        except BaseException:
            self.cancel.set()
            if not task.done():
                task.cancel()
                task.add_done_callback(_discard)
            raise
        finally:
            cancellation.cancel()
