from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator

from messenger_ai.domain import DomainModel, ErrorCode, Platform


class SupportLevel(StrEnum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class GuardedActionType(StrEnum):
    OBSERVE = "observe_background"
    RESOLVE = "resolve_background"
    COMPOSE = "compose_background"
    SEND = "send_background"
    VERIFY = "verify_background"
    OBSERVE_GUEST = "observe_guest_foreground"
    RESOLVE_GUEST = "resolve_guest_foreground"
    COMPOSE_GUEST = "compose_guest_foreground"
    SEND_GUEST = "send_guest_foreground"
    VERIFY_GUEST = "verify_guest_foreground"


class ActionPhase(StrEnum):
    READ = "read"
    PREPARE = "prepare"
    COMMIT = "commit"
    VERIFY = "verify"


class GuardResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CONTENDED = "contended"
    QUARANTINED = "quarantined"


class GuardErrorCode(StrEnum):
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    CAPABILITY_VERSION_MISMATCH = "CAPABILITY_VERSION_MISMATCH"
    ENVIRONMENT_FINGERPRINT_MISMATCH = "ENVIRONMENT_FINGERPRINT_MISMATCH"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    ACTION_TIMED_OUT = "ACTION_TIMED_OUT"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    DESKTOP_CONTENTION = "DESKTOP_CONTENTION"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"


class EnvironmentFingerprint(DomainModel):
    platform: Platform
    client_version: str
    windows_version: str
    dpi_scale: float = Field(gt=0)
    theme: str
    window_mode: str
    window_signature: str
    process_signature: str
    process_id: int | None = Field(default=None, gt=0)
    window_handle: int | None = Field(default=None, gt=0)

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=False)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AdapterCapabilities(DomainModel):
    platform: Platform
    capability_version: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observe_background: SupportLevel = SupportLevel.UNSUPPORTED
    resolve_background: SupportLevel = SupportLevel.UNSUPPORTED
    compose_background: SupportLevel = SupportLevel.UNSUPPORTED
    send_background: SupportLevel = SupportLevel.UNSUPPORTED
    verify_background: SupportLevel = SupportLevel.UNSUPPORTED
    observe_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    resolve_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    compose_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    send_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    verify_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    requires_visible_window: bool = False
    requires_unminimized: bool = False
    confidence: float = Field(ge=0, le=1)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fixture_suite_version: str = Field(min_length=1)

    @field_validator("verified_at")
    @classmethod
    def verified_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")
        return value

    def level_for(self, action_type: GuardedActionType) -> SupportLevel:
        return SupportLevel(getattr(self, action_type.value))

    @property
    def capability_hash(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GuardedAction(DomainModel):
    action_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    platform: Platform
    action_type: GuardedActionType
    phase: ActionPhase
    requested_operations: tuple[str, ...]
    fallback_operations: tuple[str, ...] = ()
    capability_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_process_id: int | None = Field(default=None, gt=0)
    target_window_handle: int | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    guest_session_id: str | None = None

    @field_validator("requested_operations")
    @classmethod
    def operations_must_be_declared(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one OS operation must be declared")
        return value


class GuardDecision(DomainModel):
    allowed: bool
    error_code: ErrorCode | GuardErrorCode | None = None
    reason: str = ""
    capability_hash: str | None = None


class ContentionEvent(DomainModel):
    resource: str
    before: Any = None
    after: Any = None


class GuardedResult(DomainModel):
    action_id: str
    status: GuardResultStatus
    value: Any = None
    error_code: ErrorCode | GuardErrorCode | None = None
    reason: str = ""
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    contention_events: tuple[ContentionEvent, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is GuardResultStatus.SUCCEEDED
