from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from messenger_ai.domain import DomainModel


class WorkerKind(StrEnum):
    HEALTH = "health"
    SELECT_ONLY = "select_only"
    VERIFY_SELECTION_ONLY = "verify_selection_only"
    OBSERVE = "observe"
    PREPARE = "prepare"
    COMMIT = "commit"
    VERIFY = "verify"
    ABORT = "abort"
    STOP = "stop"


class WorkerStatus(StrEnum):
    OK = "ok"
    FAILED_SAFE = "failed_safe"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"


class PreparedBubbleAnchor(DomainModel):
    direction: Literal["inbound", "outbound", "unknown"]
    message_key: str = Field(min_length=1)
    conversation_internal_id: str = Field(min_length=1)
    text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreparedTargetIdentity(DomainModel):
    binding_id: str = Field(min_length=1)
    participant_signature: str = Field(min_length=1)
    conversation_type: Literal["direct", "unknown"]
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)


class PreparedVerificationEvidence(DomainModel):
    """Content-free PREPARE baseline that a fresh VERIFY worker may consume."""

    owner_binding_id: str = Field(min_length=1)
    target_identity: PreparedTargetIdentity
    before_bubbles: tuple[PreparedBubbleAnchor, ...]
    text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_ref: str = Field(min_length=1)


class SelectionHandoff(DomainModel):
    """One-use authority for a fresh worker to verify a predecessor's target."""

    handoff_id: UUID = Field(default_factory=uuid4)
    source: Literal["selection_refresh", "commit_success"]
    source_kind: WorkerKind
    target_kind: WorkerKind
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=0)
    conversation_revision: int = Field(ge=0)
    operation_id: UUID | None = None
    predecessor_request_id: UUID
    successor_request_id: UUID
    predecessor_worker_epoch: UUID
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection handoff expiry must be timezone-aware")
        return value


# Observation failures in this set prove that the registered counterparty or
# the certified QQ process/session changed.  The runtime must keep these
# conversations paused until a fresh human rebind, even if a later UI query
# happens to succeed.
IDENTITY_GUARD_ERROR_CODES = frozenset(
    {
        "binding_proof_drift_after_selection",
        "conversation_type_evidence_version_drift",
        "group_marker_detected",
        "identity_scope_drift",
        "profile_identity_mismatch",
        "session_identity_header_drift",
        "session_identity_not_registered",
        "session_identity_process_restarted",
        "session_identity_scope_drift",
    }
)


def observation_pause_reason(status: WorkerStatus, error_code: str | None) -> str:
    """Map worker failures onto the runtime's recoverability contract."""

    code = error_code or status.value
    if code == "direction_unknown":
        return code
    if code in IDENTITY_GUARD_ERROR_CODES:
        return f"identity_guard:{code}"
    return f"driver_temporary:{code}"


class WorkerCommand(DomainModel):
    """A closed, auditable request vocabulary for the guest UI process.

    The main process allocates operation_id and durable message keys.  No request
    accepts a process command, a window handle, or a free-form recipient lookup.
    """

    request_id: UUID = Field(default_factory=uuid4)
    kind: WorkerKind
    binding_id: str | None = None
    binding_revision: int = Field(default=0, ge=0)
    conversation_revision: int = Field(default=0, ge=0)
    operation_id: UUID | None = None
    segment_ref: str | None = None
    text: str | None = None
    prepared_evidence: PreparedVerificationEvidence | None = None
    selection_handoff: SelectionHandoff | None = None
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline must be timezone-aware")
        return value

    def requires_binding(self) -> bool:
        return self.kind in {
            WorkerKind.SELECT_ONLY,
            WorkerKind.VERIFY_SELECTION_ONLY,
            WorkerKind.OBSERVE,
            WorkerKind.PREPARE,
            WorkerKind.COMMIT,
            WorkerKind.VERIFY,
        }


class WorkerResult(DomainModel):
    request_id: UUID
    kind: WorkerKind
    status: WorkerStatus
    worker_epoch: UUID
    operation_id: UUID | None = None
    binding_id: str | None = None
    binding_revision: int = Field(default=0, ge=0)
    conversation_revision: int = Field(default=0, ge=0)
    evidence: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


def mint_selection_handoff(
    *,
    predecessor_command: WorkerCommand,
    predecessor_result: WorkerResult,
    successor_command: WorkerCommand,
    source: Literal["selection_refresh", "commit_success"],
    expires_at: datetime,
) -> SelectionHandoff:
    """Mint only from an exact correlated predecessor outcome."""

    correlated = (
        predecessor_result.request_id == predecessor_command.request_id
        and predecessor_result.kind is predecessor_command.kind
        and predecessor_result.binding_id == predecessor_command.binding_id
        and predecessor_result.binding_revision
        == predecessor_command.binding_revision
        and predecessor_result.conversation_revision
        == predecessor_command.conversation_revision
        and predecessor_result.operation_id == predecessor_command.operation_id
        and predecessor_result.worker_epoch != UUID(int=0)
    )
    same_target = (
        successor_command.binding_id == predecessor_command.binding_id
        and successor_command.binding_revision
        == predecessor_command.binding_revision
        and successor_command.conversation_revision
        == predecessor_command.conversation_revision
        and successor_command.operation_id == predecessor_command.operation_id
        and successor_command.selection_handoff is None
    )
    if not correlated or not same_target:
        raise ValueError("selection handoff correlation mismatch")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("selection handoff expiry must be timezone-aware")
    if expires_at <= datetime.now(UTC):
        raise ValueError("selection handoff expiry must be in the future")
    if source == "selection_refresh":
        valid_outcome = (
            predecessor_command.kind in {WorkerKind.OBSERVE, WorkerKind.PREPARE}
            and successor_command.kind is predecessor_command.kind
            and predecessor_result.status is WorkerStatus.FAILED_SAFE
            and predecessor_result.error_code
            == "selection_process_refresh_required"
        )
    else:
        valid_outcome = (
            predecessor_command.kind is WorkerKind.COMMIT
            and successor_command.kind is WorkerKind.VERIFY
            and predecessor_command.operation_id is not None
            and predecessor_result.status is WorkerStatus.OK
            and predecessor_result.error_code is None
        )
    if not valid_outcome:
        raise ValueError("selection handoff predecessor is not authoritative")
    return SelectionHandoff(
        source=source,
        source_kind=predecessor_command.kind,
        target_kind=successor_command.kind,
        binding_id=predecessor_command.binding_id or "",
        binding_revision=predecessor_command.binding_revision,
        conversation_revision=predecessor_command.conversation_revision,
        operation_id=predecessor_command.operation_id,
        predecessor_request_id=predecessor_command.request_id,
        successor_request_id=successor_command.request_id,
        predecessor_worker_epoch=predecessor_result.worker_epoch,
        expires_at=expires_at,
    )
