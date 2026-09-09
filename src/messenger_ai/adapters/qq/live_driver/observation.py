"""Q2 read-only visible-message observation for one exact bound QQ HWND."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identity import (
    IdentityBindingRegistry,
    IdentityDisposition,
    IdentityEvidenceSet,
)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Q2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisibleDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class ObservationDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED_OUTBOUND = "ignored_outbound"
    PENDING = "pending"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class ObservationReason(StrEnum):
    ACCEPTED = "observation_accepted"
    DUPLICATE_EVIDENCE = "duplicate_source_evidence"
    DUPLICATE_WATERMARK = "duplicate_message_watermark"
    OUTBOUND = "known_outbound_message"
    WINDOW_SCOPE_MISMATCH = "window_scope_mismatch"
    CAPTURE_SCOPE_UNSAFE = "capture_scope_unsafe"
    WINDOW_MINIMIZED = "window_minimized"
    STALE_FRAME = "stale_frame"
    FUTURE_FRAME = "future_frame"
    IDENTITY_PENDING = "identity_pending"
    IDENTITY_QUARANTINED = "identity_quarantined"
    DIRECTION_UNCERTAIN = "message_direction_uncertain"
    CONVERSATION_UNCERTAIN = "message_conversation_uncertain"
    TIME_UNCERTAIN = "message_time_uncertain"
    OBSERVER_CONFIDENCE_LOW = "observer_confidence_low"


class ExactHwndCaptureCapabilities(Q2Model):
    """A declaration checked before the observer invokes a capture boundary."""

    scope: Literal["exact_hwnd"] = "exact_hwnd"
    read_only: Literal[True] = True
    desktop_capture_supported: Literal[False] = False
    write_actions_supported: Literal[False] = False


class VisibleMessage(Q2Model):
    message_watermark: str = Field(min_length=1, max_length=512)
    source_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: VisibleDirection
    text: str = Field(max_length=100_000)
    observed_at: datetime | None
    observer_confidence: float = Field(ge=0, le=1)
    direction_confidence: float = Field(ge=0, le=1, default=1)
    time_confidence: float = Field(ge=0, le=1, default=1)

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware(value, "observed_at")


class VisibleConversationSnapshot(Q2Model):
    """Structured output of UIA/OCR limited to one exact QQ top-level HWND."""

    platform: Literal["qq"] = "qq"
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    capture_scope: Literal["exact_hwnd"] = "exact_hwnd"
    captured_at: datetime
    expires_at: datetime
    is_minimized: bool
    identity_evidence: IdentityEvidenceSet
    conversation_confidence: float = Field(ge=0, le=1)
    messages: tuple[VisibleMessage, ...]

    @field_validator("captured_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime, info: object) -> datetime:
        return _aware(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def same_exact_window(self) -> VisibleConversationSnapshot:
        if self.identity_evidence.window_handle != self.window_handle:
            raise ValueError(
                "identity evidence must be captured from the same exact HWND"
            )
        if self.expires_at <= self.captured_at:
            raise ValueError("snapshot expiry must follow capture time")
        return self


class BoundObservationScope(Q2Model):
    account_id: str = Field(min_length=1, max_length=512)
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector_pack_version: str = Field(min_length=1, max_length=256)


class NormalizedInboundEventCandidate(Q2Model):
    """Read-only candidate; downstream policy must decide what happens next."""

    platform: Literal["qq"] = "qq"
    read_only: Literal[True] = True
    conversation_binding_id: str = Field(min_length=1)
    local_contact_id: str = Field(min_length=1)
    hub_conversation_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    ui_identity_version: int = Field(ge=1)
    direction: Literal["inbound"] = "inbound"
    text: str
    observed_at: datetime
    message_watermark: str = Field(min_length=1)
    source_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observer_confidence: float = Field(ge=0, le=1)

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")


class ObservationIssue(Q2Model):
    disposition: ObservationDisposition
    reason: ObservationReason
    message_watermark: str | None = None
    source_evidence_hash: str | None = None


class ObservationBatch(Q2Model):
    candidates: tuple[NormalizedInboundEventCandidate, ...] = ()
    issues: tuple[ObservationIssue, ...] = ()

    @property
    def requires_human_attention(self) -> bool:
        return any(
            issue.disposition
            in {ObservationDisposition.PENDING, ObservationDisposition.QUARANTINED}
            for issue in self.issues
        )


class ExactHwndReadonlyCapturePort(Protocol):
    """Only a scoped read is expressible; no desktop or input method exists."""

    def capture_capabilities(self) -> ExactHwndCaptureCapabilities: ...

    def capture_visible_messages(
        self,
        window_handle: int,
    ) -> VisibleConversationSnapshot | Awaitable[VisibleConversationSnapshot]: ...


class UnsafeCapturePortError(RuntimeError):
    pass


class ObservationDedupPort(Protocol):
    """Atomic replay claim; production may back this with durable local state."""

    def claim(
        self,
        binding_id: str,
        source_evidence_hash: str,
        message_watermark: str,
    ) -> ObservationReason | None: ...


class InMemoryObservationDedupStore:
    """Repeatable atomic store; share an instance across observer restarts."""

    def __init__(self) -> None:
        self._seen_evidence: set[tuple[str, str]] = set()
        self._seen_watermarks: set[tuple[str, str]] = set()
        self._lock = RLock()

    def claim(
        self,
        binding_id: str,
        source_evidence_hash: str,
        message_watermark: str,
    ) -> ObservationReason | None:
        with self._lock:
            if (binding_id, source_evidence_hash) in self._seen_evidence:
                return ObservationReason.DUPLICATE_EVIDENCE
            if (binding_id, message_watermark) in self._seen_watermarks:
                return ObservationReason.DUPLICATE_WATERMARK
            self._seen_evidence.add((binding_id, source_evidence_hash))
            self._seen_watermarks.add((binding_id, message_watermark))
            return None


class ReadonlyMessageObserver:
    def __init__(
        self,
        *,
        registry: IdentityBindingRegistry,
        scope: BoundObservationScope,
        clock: Callable[[], datetime] | None = None,
        minimum_confidence: float = 0.95,
        maximum_frame_age: timedelta = timedelta(seconds=10),
        maximum_clock_skew: timedelta = timedelta(seconds=2),
        dedup_store: ObservationDedupPort | None = None,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between zero and one")
        if maximum_frame_age <= timedelta(0):
            raise ValueError("maximum_frame_age must be positive")
        if maximum_clock_skew < timedelta(0):
            raise ValueError("maximum_clock_skew cannot be negative")
        self._registry = registry
        self._scope = scope
        self._clock = clock or (lambda: datetime.now(UTC))
        self._minimum_confidence = minimum_confidence
        self._maximum_frame_age = maximum_frame_age
        self._maximum_clock_skew = maximum_clock_skew
        self._dedup_store = dedup_store or InMemoryObservationDedupStore()
        self._lock = RLock()

    async def capture_and_observe(
        self,
        port: ExactHwndReadonlyCapturePort,
    ) -> ObservationBatch:
        capabilities = port.capture_capabilities()
        if not isinstance(capabilities, ExactHwndCaptureCapabilities):
            raise UnsafeCapturePortError(
                "capture port must provide a validated capability declaration"
            )
        if (
            capabilities.scope != "exact_hwnd"
            or not capabilities.read_only
            or capabilities.desktop_capture_supported
            or capabilities.write_actions_supported
        ):
            raise UnsafeCapturePortError("capture port is not exact-HWND read-only")
        snapshot = port.capture_visible_messages(self._scope.window_handle)
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        if not isinstance(snapshot, VisibleConversationSnapshot):
            raise TypeError("capture port must return VisibleConversationSnapshot")
        return self.observe(snapshot)

    def observe(self, snapshot: VisibleConversationSnapshot) -> ObservationBatch:
        now = _aware(self._clock(), "clock")
        scope_issue = self._validate_snapshot(snapshot, now)
        if scope_issue is not None:
            return ObservationBatch(issues=(scope_issue,))

        resolution = self._registry.resolve(
            account_id=self._scope.account_id,
            evidence_set=snapshot.identity_evidence,
        )
        if resolution.disposition is IdentityDisposition.PENDING_HUMAN_BINDING:
            return self._all_messages(
                snapshot,
                ObservationDisposition.PENDING,
                ObservationReason.IDENTITY_PENDING,
            )
        if resolution.disposition is IdentityDisposition.QUARANTINED:
            return self._all_messages(
                snapshot,
                ObservationDisposition.QUARANTINED,
                ObservationReason.IDENTITY_QUARANTINED,
            )
        if snapshot.conversation_confidence < self._minimum_confidence:
            return self._all_messages(
                snapshot,
                ObservationDisposition.PENDING,
                ObservationReason.CONVERSATION_UNCERTAIN,
            )

        assert resolution.binding_id is not None
        assert resolution.local_contact_id is not None
        assert resolution.hub_conversation_id is not None
        assert resolution.ui_identity_version is not None
        candidates: list[NormalizedInboundEventCandidate] = []
        issues: list[ObservationIssue] = []
        binding_key = str(resolution.binding_id)
        with self._lock:
            for message in snapshot.messages:
                issue = self._message_issue(message)
                if issue is not None:
                    issues.append(issue)
                    continue
                duplicate = self._dedup_store.claim(
                    binding_key,
                    message.source_evidence_hash,
                    message.message_watermark,
                )
                if duplicate is not None:
                    issues.append(
                        ObservationIssue(
                            disposition=ObservationDisposition.DUPLICATE,
                            reason=duplicate,
                            message_watermark=message.message_watermark,
                            source_evidence_hash=message.source_evidence_hash,
                        )
                    )
                    continue
                assert message.observed_at is not None
                candidates.append(
                    NormalizedInboundEventCandidate(
                        conversation_binding_id=binding_key,
                        local_contact_id=resolution.local_contact_id,
                        hub_conversation_id=resolution.hub_conversation_id,
                        account_id=self._scope.account_id,
                        ui_identity_version=resolution.ui_identity_version,
                        text=message.text,
                        observed_at=message.observed_at,
                        message_watermark=message.message_watermark,
                        source_evidence_hash=message.source_evidence_hash,
                        observer_confidence=message.observer_confidence,
                    )
                )
        return ObservationBatch(candidates=tuple(candidates), issues=tuple(issues))

    def _validate_snapshot(
        self,
        snapshot: VisibleConversationSnapshot,
        now: datetime,
    ) -> ObservationIssue | None:
        if snapshot.capture_scope != "exact_hwnd":
            return ObservationIssue(
                disposition=ObservationDisposition.REJECTED,
                reason=ObservationReason.CAPTURE_SCOPE_UNSAFE,
            )
        evidence = snapshot.identity_evidence
        if (
            snapshot.window_handle != self._scope.window_handle
            or snapshot.process_id != self._scope.process_id
            or evidence.window_handle != self._scope.window_handle
            or evidence.environment_fingerprint != self._scope.environment_fingerprint
            or evidence.selector_pack_version != self._scope.selector_pack_version
        ):
            return ObservationIssue(
                disposition=ObservationDisposition.QUARANTINED,
                reason=ObservationReason.WINDOW_SCOPE_MISMATCH,
            )
        if snapshot.is_minimized:
            return ObservationIssue(
                disposition=ObservationDisposition.REJECTED,
                reason=ObservationReason.WINDOW_MINIMIZED,
            )
        if snapshot.captured_at > now + self._maximum_clock_skew:
            return ObservationIssue(
                disposition=ObservationDisposition.REJECTED,
                reason=ObservationReason.FUTURE_FRAME,
            )
        if (
            snapshot.expires_at <= now
            or now - snapshot.captured_at > self._maximum_frame_age
        ):
            return ObservationIssue(
                disposition=ObservationDisposition.REJECTED,
                reason=ObservationReason.STALE_FRAME,
            )
        return None

    def _message_issue(
        self,
        message: VisibleMessage,
    ) -> ObservationIssue | None:
        common = {
            "message_watermark": message.message_watermark,
            "source_evidence_hash": message.source_evidence_hash,
        }
        if message.direction is VisibleDirection.UNKNOWN or (
            message.direction_confidence < self._minimum_confidence
        ):
            return ObservationIssue(
                disposition=ObservationDisposition.PENDING,
                reason=ObservationReason.DIRECTION_UNCERTAIN,
                **common,
            )
        if message.direction is VisibleDirection.OUTBOUND:
            return ObservationIssue(
                disposition=ObservationDisposition.IGNORED_OUTBOUND,
                reason=ObservationReason.OUTBOUND,
                **common,
            )
        if (
            message.observed_at is None
            or message.time_confidence < self._minimum_confidence
        ):
            return ObservationIssue(
                disposition=ObservationDisposition.PENDING,
                reason=ObservationReason.TIME_UNCERTAIN,
                **common,
            )
        if message.observer_confidence < self._minimum_confidence:
            return ObservationIssue(
                disposition=ObservationDisposition.PENDING,
                reason=ObservationReason.OBSERVER_CONFIDENCE_LOW,
                **common,
            )
        return None

    @staticmethod
    def _all_messages(
        snapshot: VisibleConversationSnapshot,
        disposition: ObservationDisposition,
        reason: ObservationReason,
    ) -> ObservationBatch:
        if not snapshot.messages:
            return ObservationBatch(
                issues=(ObservationIssue(disposition=disposition, reason=reason),)
            )
        return ObservationBatch(
            issues=tuple(
                ObservationIssue(
                    disposition=disposition,
                    reason=reason,
                    message_watermark=message.message_watermark,
                    source_evidence_hash=message.source_evidence_hash,
                )
                for message in snapshot.messages
            )
        )


__all__ = [
    "BoundObservationScope",
    "ExactHwndCaptureCapabilities",
    "ExactHwndReadonlyCapturePort",
    "InMemoryObservationDedupStore",
    "NormalizedInboundEventCandidate",
    "ObservationBatch",
    "ObservationDedupPort",
    "ObservationDisposition",
    "ObservationIssue",
    "ObservationReason",
    "ReadonlyMessageObserver",
    "UnsafeCapturePortError",
    "VisibleConversationSnapshot",
    "VisibleDirection",
    "VisibleMessage",
]
