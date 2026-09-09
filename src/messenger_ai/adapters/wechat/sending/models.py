from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from messenger_ai.domain import AuthorizedSendCommand, ErrorCode
from messenger_ai.execution_guard import GuardErrorCode


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticBackend(StrEnum):
    UIA = "uia"
    MSAA = "msaa"
    WINDOW_MESSAGE = "window_message"


class SendRoute(FrozenModel):
    """Declared OS boundary for one driver; there is deliberately no fallback route."""

    backend: SemanticBackend
    capture_operation: str = "window.capture"
    select_operation: str
    compose_operation: str
    composer_read_operation: str
    commit_operation: str
    verify_operation: str
    requires_foreground: bool = False

    @property
    def prepare_operations(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.capture_operation,
                    self.select_operation,
                    self.compose_operation,
                    self.composer_read_operation,
                )
            )
        )

    @property
    def commit_operations(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.capture_operation, self.commit_operation)))

    @property
    def verify_operations(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.capture_operation, self.verify_operation)))


class TargetRef(FrozenModel):
    conversation_id: str = Field(min_length=1)
    conversation_session_id: str = Field(min_length=1)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapturedSendEvidence(FrozenModel):
    """Small structural boundary that an M4 observation can be normalized into."""

    conversation_id: str = Field(min_length=1)
    conversation_session_id: str = Field(min_length=1)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_inbound_message_key: str = Field(min_length=1)
    frame_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_captured_at: datetime
    frame_expires_at: datetime
    client_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    dpi_scale: float = Field(gt=0)
    outbound_sequence: int = Field(ge=0)

    @field_validator("frame_captured_at", "frame_expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def expiry_follows_capture(self) -> CapturedSendEvidence:
        if self.frame_expires_at <= self.frame_captured_at:
            raise ValueError("frame expiry must be later than capture time")
        return self

    @property
    def target(self) -> TargetRef:
        return TargetRef(
            conversation_id=self.conversation_id,
            conversation_session_id=self.conversation_session_id,
            identity_digest=self.identity_digest,
        )


class M4VisualObservation(Protocol):
    binding_id: str
    conversation_id: str
    frame_hash: str
    observed_at: datetime
    messages: tuple[Any, ...]
    evidence: Any
    publishable: bool


class M4WindowBinding(Protocol):
    handle: int
    process_id: int
    identity: str
    binding_id: str
    verified: bool
    environment: Any


def normalize_m4_evidence(
    observation: M4VisualObservation,
    binding: M4WindowBinding,
    *,
    environment_fingerprint: str,
    conversation_session_id: str,
    outbound_sequence: int,
) -> CapturedSendEvidence:
    """Normalize M4's public frozen evidence without importing its implementation."""

    if not observation.publishable:
        raise ValueError("M4 observation is not publishable")
    if not binding.verified or not binding.identity:
        raise ValueError("M4 window binding is not identity-verified")
    if (
        observation.binding_id != binding.binding_id
        or observation.evidence.binding_id != binding.binding_id
    ):
        raise ValueError("M4 binding identifiers differ")
    if (
        observation.frame_hash != observation.evidence.frame_hash
        or not observation.frame_hash
    ):
        raise ValueError("M4 frame hashes differ")
    inbound = [
        message
        for message in observation.messages
        if str(getattr(message, "direction", ""))
        in {"inbound", "MessageDirection.INBOUND"}
    ]
    if not inbound or not getattr(inbound[-1], "platform_message_key", ""):
        raise ValueError("M4 observation has no stable inbound message key")
    identity_digest = hashlib.sha256(binding.identity.encode("utf-8")).hexdigest()
    return CapturedSendEvidence(
        conversation_id=observation.conversation_id,
        conversation_session_id=conversation_session_id,
        identity_digest=identity_digest,
        last_inbound_message_key=inbound[-1].platform_message_key,
        frame_hash=observation.frame_hash,
        frame_captured_at=observation.observed_at,
        frame_expires_at=observation.evidence.expires_at,
        client_version=binding.environment.client_version,
        environment_fingerprint=environment_fingerprint,
        process_id=binding.process_id,
        window_handle=binding.handle,
        dpi_scale=binding.environment.dpi_scale,
        outbound_sequence=outbound_sequence,
    )


class AuthorizedWechatSend(FrozenModel):
    """M0 authorization plus the minimum body and M4 evidence needed by M5."""

    command: AuthorizedSendCommand
    body_text: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    observation: CapturedSendEvidence

    @model_validator(mode="after")
    def authorization_matches_payload(self) -> AuthorizedWechatSend:
        if (
            self.command.expires_at.tzinfo is None
            or self.command.expires_at.utcoffset() is None
        ):
            raise ValueError("authorization expiry must be timezone-aware")
        if text_digest(self.body_text) != self.command.text_hash:
            raise ValueError("body text does not match the authorized text hash")
        if self.command.conversation_id != self.observation.conversation_id:
            raise ValueError("authorization and observation conversations differ")
        if (
            self.command.expected_last_message_key
            != self.observation.last_inbound_message_key
        ):
            raise ValueError("authorization and observation message keys differ")
        return self


class PreparedSend(FrozenModel):
    operation_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=1)
    draft_id: UUID
    authorization_id: UUID
    conversation_id: str = Field(min_length=1)
    conversation_session_id: str = Field(min_length=1)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_last_message_key: str = Field(min_length=1)
    body_text: str = Field(min_length=1)
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    dpi_scale: float = Field(gt=0)
    prepared_frame_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_at: datetime
    expires_at: datetime
    baseline_outbound_sequence: int = Field(ge=0)
    route: SendRoute
    evidence_seal: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("prepared_at", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared timestamps must be timezone-aware")
        return value

    def seal_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"evidence_seal"})

    def seal_is_valid(self) -> bool:
        canonical = json.dumps(
            self.seal_payload(), sort_keys=True, separators=(",", ":")
        )
        return (
            hashlib.sha256(canonical.encode("utf-8")).hexdigest() == self.evidence_seal
        )


class RawSendReceipt(FrozenModel):
    operation_id: UUID
    idempotency_key: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    conversation_session_id: str = Field(min_length=1)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_started_at: datetime
    commit_returned_at: datetime
    baseline_outbound_sequence: int = Field(ge=0)
    driver_commit_token: str = Field(min_length=1)

    @field_validator("commit_started_at", "commit_returned_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt timestamps must be timezone-aware")
        return value


class OutboundBubble(FrozenModel):
    conversation_id: str = Field(min_length=1)
    conversation_session_id: str = Field(min_length=1)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: str
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    sequence: int = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bubble timestamp must be timezone-aware")
        return value


class SendResultStatus(StrEnum):
    PREPARED = "PREPARED"
    COMMITTED_PENDING_VERIFY = "COMMITTED_PENDING_VERIFY"
    SENT_VERIFIED = "SENT_VERIFIED"
    REJECTED = "REJECTED"


class SendResult(FrozenModel):
    status: SendResultStatus
    error_code: ErrorCode | GuardErrorCode | None = None
    reason: str = ""
    prepared: PreparedSend | None = None
    receipt: RawSendReceipt | None = None
    verified_bubble_sequence: int | None = Field(default=None, ge=0)
    automatic_retry_allowed: bool = False

    @model_validator(mode="after")
    def payload_matches_status(self) -> SendResult:
        if self.status is SendResultStatus.PREPARED and self.prepared is None:
            raise ValueError("PREPARED result requires PreparedSend")
        if (
            self.status is SendResultStatus.COMMITTED_PENDING_VERIFY
            and self.receipt is None
        ):
            raise ValueError("commit result requires RawSendReceipt")
        if (
            self.status is SendResultStatus.SENT_VERIFIED
            and self.error_code is not None
        ):
            raise ValueError("verified result cannot carry an error")
        if self.error_code is ErrorCode.SEND_UNCERTAIN and self.automatic_retry_allowed:
            raise ValueError("SEND_UNCERTAIN can never be automatically retried")
        return self


def make_prepared_send(**values: Any) -> PreparedSend:
    values["prepared_at"] = values.get("prepared_at", datetime.now(UTC))
    provisional = PreparedSend(evidence_seal="0" * 64, **values)
    canonical = json.dumps(
        provisional.seal_payload(), sort_keys=True, separators=(",", ":")
    )
    seal = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return provisional.model_copy(update={"evidence_seal": seal})
