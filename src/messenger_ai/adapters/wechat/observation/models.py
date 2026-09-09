"""Value objects for the read-only WeChat visual observation boundary.

The module intentionally contains no operating-system, OCR, Docker, or window
automation code.  Concrete implementations are injected through ``ports``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class ObservationStatus(StrEnum):
    ACCEPTED = "accepted"
    HUMAN_REVIEW = "human_review"
    DROPPED = "dropped"
    STALE = "stale"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class VisualContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    STICKER = "sticker"
    RECALL = "recall"
    TIME_SEPARATOR = "time_separator"
    SYSTEM_NOTICE = "system_notice"
    UNSUPPORTED = "unsupported"


class WindowEnvironment(ObservationModel):
    """Environment dimensions are part of the compatibility key."""

    client_version: str
    windows_version: str
    graphics_backend: str
    dpi_scale: float = Field(gt=0)
    theme: str
    window_width: int = Field(gt=0)
    window_height: int = Field(gt=0)
    monitor_id: str = ""

    def fingerprint(self) -> str:
        material = {
            "client_version": self.client_version,
            "windows_version": self.windows_version,
            "graphics_backend": self.graphics_backend,
            "dpi_scale": self.dpi_scale,
            "theme": self.theme,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "monitor_id": self.monitor_id,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WindowDescriptor(ObservationModel):
    """A caller-supplied, already-discovered window descriptor.

    Discovery is intentionally outside this module.  ``handle`` must be a
    real target-window handle; zero and synthetic desktop handles are refused.
    """

    handle: int = Field(gt=0)
    process_id: int = Field(gt=0)
    executable: str = "Weixin.exe"
    title: str = ""
    environment: WindowEnvironment
    identity: str = ""
    identity_confidence: float = Field(ge=0, le=1, default=0)


class WindowBinding(ObservationModel):
    handle: int = Field(gt=0)
    process_id: int = Field(gt=0)
    executable: str
    identity: str
    identity_confidence: float = Field(ge=0, le=1)
    environment: WindowEnvironment
    bound_at: datetime
    binding_id: str
    verified: bool = False


class Rectangle(ObservationModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def contains(self, other: Rectangle) -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.x + other.width <= self.x + self.width
            and other.y + other.height <= self.y + self.height
        )


class CapturedFrame(ObservationModel):
    """Pixels returned by a capture provider for one bound window/ROI only."""

    frame_id: str
    captured_at: datetime
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    binding_id: str
    roi: Rectangle
    pixels: bytes = b""
    frame_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def populate_hash(self) -> CapturedFrame:
        if not self.frame_hash:
            object.__setattr__(
                self, "frame_hash", hashlib.sha256(self.pixels).hexdigest()
            )
        return self


class RegionOfInterest(ObservationModel):
    name: str
    rect: Rectangle
    purpose: str = ""


class LayoutClassification(ObservationModel):
    layout_version: str = "unknown"
    confidence: float = Field(ge=0, le=1)
    message_boundary_confidence: float = Field(ge=0, le=1, default=0)
    conversation_roi: RegionOfInterest | None = None
    message_roi: RegionOfInterest | None = None
    supported: bool = False
    reason: str = ""


class OCRToken(ObservationModel):
    text: str
    rect: Rectangle
    confidence: float = Field(ge=0, le=1, default=0)
    direction_confidence: float | None = Field(default=None, ge=0, le=1)
    kind: VisualContentType = VisualContentType.TEXT
    direction: MessageDirection = MessageDirection.UNKNOWN
    group_id: str = ""
    displayed_time: datetime | None = None


class OCRResult(ObservationModel):
    tokens: list[OCRToken] = Field(default_factory=list)
    engine: str = "local"
    confidence: float = Field(ge=0, le=1, default=0)
    source_frame_hash: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MessageCandidate(ObservationModel):
    platform_message_key: str
    text: str = ""
    direction: MessageDirection = MessageDirection.UNKNOWN
    content_type: VisualContentType = VisualContentType.TEXT
    confidence: float = Field(ge=0, le=1)
    boundary_confidence: float = Field(ge=0, le=1)
    displayed_time: datetime | None = None
    rect: Rectangle | None = None


class ConfidenceBreakdown(ObservationModel):
    conversation_identity_confidence: float = Field(ge=0, le=1)
    layout_version_confidence: float = Field(ge=0, le=1)
    message_boundary_confidence: float = Field(ge=0, le=1)
    ocr_text_confidence: float = Field(ge=0, le=1)
    direction_confidence: float = Field(ge=0, le=1)
    temporal_consistency_confidence: float = Field(ge=0, le=1)
    hard_identity_threshold: float = Field(ge=0, le=1, default=0.95)
    hard_layout_threshold: float = Field(ge=0, le=1, default=0.90)
    hard_direction_threshold: float = Field(ge=0, le=1, default=0.90)

    @property
    def overall(self) -> float:
        # A minimum is deliberately used: no average can hide a failed gate.
        return min(
            self.conversation_identity_confidence,
            self.layout_version_confidence,
            self.message_boundary_confidence,
            self.ocr_text_confidence,
            self.direction_confidence,
            self.temporal_consistency_confidence,
        )

    @property
    def hard_gates_pass(self) -> bool:
        return (
            self.conversation_identity_confidence >= self.hard_identity_threshold
            and self.layout_version_confidence >= self.hard_layout_threshold
            and self.direction_confidence >= self.hard_direction_threshold
        )


class ObservationEvidence(ObservationModel):
    """Short-lived, privacy-safe evidence; no raw pixels or message text."""

    evidence_id: str
    frame_hash: str
    binding_id: str
    regions: tuple[RegionOfInterest, ...] = ()
    redacted_summary: str = ""
    created_at: datetime
    expires_at: datetime
    redacted: bool = True
    raw_frame_ref: str | None = None

    @model_validator(mode="after")
    def enforce_privacy(self) -> ObservationEvidence:
        if not self.redacted:
            raise ValueError("observation evidence must be redacted")
        if self.raw_frame_ref is not None:
            raise ValueError("raw frame references are not allowed in normal evidence")
        if self.expires_at <= self.created_at:
            raise ValueError("evidence TTL must be positive")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class VisualObservation(ObservationModel):
    observation_id: str
    binding_id: str
    conversation_id: str = ""
    frame_hash: str
    observed_at: datetime
    messages: tuple[MessageCandidate, ...] = ()
    confidence: ConfidenceBreakdown
    evidence: ObservationEvidence
    status: ObservationStatus = ObservationStatus.ACCEPTED
    human_review_required: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def apply_hard_gates(self) -> VisualObservation:
        if not self.confidence.hard_gates_pass:
            object.__setattr__(self, "status", ObservationStatus.HUMAN_REVIEW)
            object.__setattr__(self, "human_review_required", True)
            if not self.reason:
                object.__setattr__(self, "reason", "hard_confidence_gate_failed")
        return self

    @property
    def publishable(self) -> bool:
        return (
            self.status == ObservationStatus.ACCEPTED and not self.human_review_required
        )


class NormalizedWechatEvent(ObservationModel):
    event_type: str
    platform: str = "wechat"
    conversation_id: str
    platform_message_key: str
    direction: MessageDirection
    text: str
    content_type: VisualContentType
    observed_at: datetime
    evidence_ref: str
    confidence: float = Field(ge=0, le=1)


class CompatibilityRecord(ObservationModel):
    fingerprint: str
    environment: WindowEnvironment
    status: str = "unknown"
    verified_at: datetime | None = None
    reason: str = ""


def utc_now() -> datetime:
    return datetime.now(UTC)


def evidence_expiry(created_at: datetime, ttl_seconds: float) -> datetime:
    return created_at + timedelta(seconds=max(0.001, ttl_seconds))
