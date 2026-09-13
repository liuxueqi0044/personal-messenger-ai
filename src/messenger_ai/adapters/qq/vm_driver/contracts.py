from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

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
    """One-use, HMAC-authenticated authority to verify a predecessor's target.

    The token is a *capability*: possession is meant to prove that a specific
    correlated predecessor outcome was minted for this exact successor command.
    Every security-relevant field -- including the successor ``deadline``, a
    ``PREPARE`` successor's ``text_sha256`` / ``segment_ref`` payload binding,
    and a ``VERIFY`` successor's prepared-evidence digest -- is covered by
    ``auth_tag`` so a hand-crafted or tampered token cannot pass
    :func:`verify_selection_handoff_auth`.
    """

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
    successor_worker_epoch: UUID
    target_runtime_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    successor_deadline: datetime
    text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    segment_ref: str | None = Field(default=None, min_length=1)
    prepared_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    auth_tag: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("expires_at", "successor_deadline")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection handoff times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def capability_invariants(self) -> SelectionHandoff:
        if (
            self.predecessor_worker_epoch == UUID(int=0)
            or self.successor_worker_epoch == UUID(int=0)
            or self.predecessor_worker_epoch == self.successor_worker_epoch
        ):
            raise ValueError(
                "selection handoff requires distinct nonzero worker epochs"
            )
        if self.expires_at > self.successor_deadline:
            raise ValueError(
                "selection handoff expiry must not exceed the successor deadline"
            )
        if self.target_kind is WorkerKind.PREPARE:
            if self.text_sha256 is None or self.segment_ref is None:
                raise ValueError(
                    "prepare handoff must bind the successor text and segment"
                )
        elif self.text_sha256 is not None or self.segment_ref is not None:
            raise ValueError(
                "non-prepare handoff must not bind a text hash or segment ref"
            )
        if self.target_kind is WorkerKind.VERIFY:
            if self.prepared_evidence_sha256 is None:
                raise ValueError(
                    "verify handoff must bind the prepared evidence"
                )
        elif self.prepared_evidence_sha256 is not None:
            raise ValueError(
                "non-verify handoff must not bind prepared evidence"
            )
        return self


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
            WorkerKind.ABORT,
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


MIN_SELECTION_HANDOFF_SIGNING_KEY_BYTES = 32


def _require_signing_key(signing_key: bytes) -> bytes:
    """Return ``signing_key`` as bytes or refuse a too-weak / non-bytes key."""

    if not isinstance(signing_key, (bytes, bytearray, memoryview)):
        raise TypeError("selection handoff signing key must be bytes")
    key = bytes(signing_key)
    if len(key) < MIN_SELECTION_HANDOFF_SIGNING_KEY_BYTES:
        raise ValueError(
            "selection handoff signing key must be at least "
            f"{MIN_SELECTION_HANDOFF_SIGNING_KEY_BYTES} bytes"
        )
    return key


def _utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _canonical_handoff_payload(handoff: SelectionHandoff) -> dict[str, object]:
    """Canonical, stable, UTC-normalised projection of every security field.

    ``auth_tag`` is deliberately excluded so the payload can be re-derived from
    a (possibly tampered) token and compared against the presented tag.
    """

    return {
        "handoff_id": str(handoff.handoff_id),
        "source": str(handoff.source),
        "source_kind": handoff.source_kind.value,
        "target_kind": handoff.target_kind.value,
        "binding_id": handoff.binding_id,
        "binding_revision": handoff.binding_revision,
        "conversation_revision": handoff.conversation_revision,
        "operation_id": (
            None if handoff.operation_id is None else str(handoff.operation_id)
        ),
        "predecessor_request_id": str(handoff.predecessor_request_id),
        "successor_request_id": str(handoff.successor_request_id),
        "predecessor_worker_epoch": str(handoff.predecessor_worker_epoch),
        "successor_worker_epoch": str(handoff.successor_worker_epoch),
        "target_runtime_id_digest": handoff.target_runtime_id_digest,
        "expires_at": _utc_isoformat(handoff.expires_at),
        "successor_deadline": _utc_isoformat(handoff.successor_deadline),
        "text_sha256": handoff.text_sha256,
        "segment_ref": handoff.segment_ref,
        "prepared_evidence_sha256": handoff.prepared_evidence_sha256,
    }


def _canonical_handoff_bytes(handoff: SelectionHandoff) -> bytes:
    return json.dumps(
        _canonical_handoff_payload(handoff),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _selection_handoff_auth_tag(
    handoff: SelectionHandoff, signing_key: bytes,
) -> str:
    return hmac.new(
        signing_key, _canonical_handoff_bytes(handoff), hashlib.sha256
    ).hexdigest()


def verify_selection_handoff_auth(
    handoff: SelectionHandoff, signing_key: bytes,
) -> bool:
    """Constant-time proof that ``handoff`` carries a valid ``auth_tag``.

    A token built by any path other than :func:`mint_selection_handoff` -- or a
    minted token with *any* field mutated afterwards -- fails this check because
    the canonical payload no longer matches the tag.
    """

    if not isinstance(handoff, SelectionHandoff):
        return False
    key = _require_signing_key(signing_key)
    expected = _selection_handoff_auth_tag(handoff, key)
    return hmac.compare_digest(handoff.auth_tag, expected)


def mint_selection_handoff(
    *,
    predecessor_command: WorkerCommand,
    predecessor_result: WorkerResult,
    successor_command: WorkerCommand,
    source: Literal["selection_refresh", "commit_success"],
    successor_worker_epoch: UUID,
    target_runtime_id_digest: str,
    expires_at: datetime,
    signing_key: bytes,
) -> SelectionHandoff:
    """Mint the HMAC-authenticated capability from an exact correlated outcome.

    ``signing_key`` must be at least 32 bytes.  The returned token binds the
    successor command's ``deadline`` and action payload: exact ``text`` digest
    and ``segment_ref`` for ``PREPARE``, or the prepared-evidence digest for
    ``VERIFY``.  The tag is computed over the canonical JSON of every security
    field except ``auth_tag`` itself.
    """

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
    signing_key_bytes = _require_signing_key(signing_key)
    successor_deadline = successor_command.deadline
    if successor_deadline is None:
        raise ValueError("selection handoff requires a successor deadline")
    if (
        not isinstance(successor_deadline, datetime)
        or successor_deadline.tzinfo is None
        or successor_deadline.utcoffset() is None
    ):
        raise ValueError(
            "selection handoff successor deadline must be timezone-aware"
        )
    if (
        not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise ValueError("selection handoff expiry must be timezone-aware")
    if expires_at <= datetime.now(UTC):
        raise ValueError("selection handoff expiry must be in the future")
    if expires_at > successor_deadline:
        raise ValueError(
            "selection handoff expiry must not exceed the successor deadline"
        )
    if (
        successor_worker_epoch == UUID(int=0)
        or successor_worker_epoch == predecessor_result.worker_epoch
    ):
        raise ValueError("selection handoff successor worker is not fresh")
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
    if successor_command.kind is WorkerKind.PREPARE:
        if not successor_command.text:
            raise ValueError(
                "prepare handoff requires the successor text to bind"
            )
        if not successor_command.segment_ref:
            raise ValueError(
                "prepare handoff requires the successor segment reference"
            )
        text_sha256 = hashlib.sha256(
            successor_command.text.encode("utf-8")
        ).hexdigest()
        segment_ref: str | None = successor_command.segment_ref
    else:
        text_sha256 = None
        segment_ref = None
    if successor_command.kind is WorkerKind.VERIFY:
        if successor_command.prepared_evidence is None:
            raise ValueError(
                "verify handoff requires prepared evidence to bind"
            )
        prepared_evidence_sha256 = hashlib.sha256(
            json.dumps(
                successor_command.prepared_evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
    else:
        prepared_evidence_sha256 = None
    handoff = SelectionHandoff(
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
        successor_worker_epoch=successor_worker_epoch,
        target_runtime_id_digest=target_runtime_id_digest,
        expires_at=expires_at,
        successor_deadline=successor_deadline,
        text_sha256=text_sha256,
        segment_ref=segment_ref,
        prepared_evidence_sha256=prepared_evidence_sha256,
        auth_tag="0" * 64,
    )
    return handoff.model_copy(
        update={
            "auth_tag": _selection_handoff_auth_tag(handoff, signing_key_bytes),
        }
    )
