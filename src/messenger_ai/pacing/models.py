"""Pure contracts for M10's deterministic, send-free pacing boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from messenger_ai.domain.models import PacingProfile, ReplyPlan
from messenger_ai.rules.models import PacingRules, RuleContext


class PacingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PacingStatus(StrEnum):
    WAITING = "waiting"
    DUE_FOR_REVALIDATION = "due_for_revalidation"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    HOLD_REPLAN = "hold_replan"
    COMPLETED = "completed"
    REJECTED = "rejected"


class CancellationReason(StrEnum):
    NEW_INBOUND = "new_inbound"
    USER_TAKEOVER = "user_takeover"
    DRAFT_CHANGED = "draft_changed"
    RULE_VERSION_CHANGED = "rule_version_changed"
    CAPABILITY_CHANGED = "capability_changed"
    HEALTH_CHANGED = "health_changed"
    PAUSED = "paused"
    EXPIRED = "expired"
    CLOCK_JUMP = "clock_jump"
    SUPERSEDED = "superseded"
    REVALIDATION_REJECTED = "revalidation_rejected"
    ONE_SHOT_TIMEOUT = "one_shot_timeout"


class AllowedTimeWindow(PacingModel):
    """A UTC daily time window.  A window can cross midnight."""

    start: time
    end: time

    @model_validator(mode="after")
    def non_empty(self) -> AllowedTimeWindow:
        if self.start == self.end:
            raise ValueError("allowed time window cannot cover zero/whole day")
        return self


class SchedulerLimits(PacingModel):
    """Rate limits apply to due/revalidation emissions, never to model output."""

    max_global_revalidations: int = Field(default=30, ge=1)
    max_contact_revalidations: int = Field(default=6, ge=1)
    window_seconds: int = Field(default=3600, ge=1)
    allowed_time_windows: tuple[AllowedTimeWindow, ...] = ()
    segment_gap_min_seconds: float = Field(default=2, ge=0)
    segment_gap_max_seconds: float = Field(default=8, ge=0)
    clock_jump_tolerance_seconds: float = Field(default=300, ge=1)
    auto_reply_max_chars: int = Field(default=120, ge=1)
    auto_reply_max_segments: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def gaps_are_ordered(self) -> SchedulerLimits:
        if self.segment_gap_max_seconds < self.segment_gap_min_seconds:
            raise ValueError("segment gap maximum must be >= minimum")
        return self


class PacingConfiguration(PacingModel):
    """The explicit, immutable bridge from an activated M7 RulePack to M10."""

    profile: PacingProfile
    limits: SchedulerLimits

    @classmethod
    def from_rule_context(cls, context: RuleContext) -> PacingConfiguration:
        rules = (
            context.contact_override.pacing
            if context.contact_override and context.contact_override.pacing
            else context.rulepack.normalized.pacing
        )
        return cls.from_rule_pacing(rules)

    @classmethod
    def from_rule_pacing(cls, rules: PacingRules) -> PacingConfiguration:
        return cls(
            profile=PacingProfile(
                hard_min_latency_seconds=rules.hard_min_latency_seconds,
                quiet_window_seconds=rules.quiet_window_seconds,
                reading_chars_per_second=rules.reading_chars_per_second,
                composition_chars_per_second=rules.composition_chars_per_second,
                min_inter_reply_gap_seconds=rules.min_inter_reply_gap_seconds,
                long_reply_threshold_chars=rules.long_reply_threshold_chars,
                long_reply_min_latency_seconds=rules.long_reply_min_latency_seconds,
            ),
            limits=SchedulerLimits(
                auto_reply_max_chars=rules.auto_reply_max_chars,
                auto_reply_max_segments=rules.auto_reply_max_segments,
            ),
        )


class DraftSnapshot(PacingModel):
    draft_id: UUID = Field(default_factory=uuid4)
    conversation_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    text: str
    text_hash: str = Field(min_length=1)
    expected_last_message_key: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    eligibility_id: str = Field(min_length=1)


class ScheduleRequest(PacingModel):
    """All facts that a planned due event is bound to at creation time."""

    draft: DraftSnapshot
    reply_plan: ReplyPlan
    source_message_keys: tuple[str, ...] = Field(min_length=1)
    first_inbound_at: datetime
    last_inbound_at: datetime
    inbound_text: str = ""
    last_outbound_at: datetime | None = None
    profile: PacingProfile = Field(default_factory=PacingProfile)
    limits: SchedulerLimits = Field(default_factory=SchedulerLimits)
    capability_healthy: bool = True
    paused: bool = False
    reserved_pacing_plan_id: UUID | None = None
    one_shot_attempt_id: UUID | None = None
    segment_eligibility_ids: tuple[str, ...] = ()
    segment_draft_ids: tuple[UUID, ...] = ()

    @field_validator("first_inbound_at", "last_inbound_at", "last_outbound_at")
    @classmethod
    def utc_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def temporal_order(self) -> ScheduleRequest:
        if self.last_inbound_at < self.first_inbound_at:
            raise ValueError("last inbound cannot precede first inbound")
        return self


class PacingPlanRecord(PacingModel):
    pacing_plan_id: UUID = Field(default_factory=uuid4)
    one_shot_attempt_id: UUID | None = None
    conversation_id: str
    contact_id: str
    draft_id: UUID
    text_hash: str
    expected_last_message_key: str
    source_message_keys: tuple[str, ...]
    rule_version: str
    eligibility_id: str
    segment_eligibility_ids: tuple[str, ...] = ()
    segment_draft_ids: tuple[UUID, ...] = ()
    pacing_rule_version: str
    created_at: datetime
    quiet_until: datetime
    earliest_send_at: datetime
    expires_at: datetime
    reading_delay_ms: int = Field(ge=0)
    composition_delay_ms: int = Field(ge=0)
    inter_message_gap_ms: int = Field(ge=0)
    segment_count: int = Field(ge=1, le=3)
    segment_index: int = Field(default=0, ge=0, le=2)
    segments: tuple[str, ...]
    status: PacingStatus = PacingStatus.WAITING
    cancellation_conditions: tuple[CancellationReason, ...]
    formula_inputs: dict[str, float | int | str] = Field(default_factory=dict)
    limits: SchedulerLimits = Field(default_factory=SchedulerLimits)


class DueForRevalidation(PacingModel):
    """The only M10 output at maturity; M9 must independently decide eligibility."""

    event_id: UUID = Field(default_factory=uuid4)
    pacing_plan_id: UUID
    one_shot_attempt_id: UUID | None = None
    conversation_id: str
    contact_id: str
    draft_id: UUID
    text_hash: str
    expected_last_message_key: str
    rule_version: str
    pacing_rule_version: str
    eligibility_id: str
    due_at: datetime
    segment_index: int = Field(ge=0, le=2)
    segment_count: int = Field(ge=1, le=3)
    body: str
    body_hash: str
    reason_code: Literal["pacing_due"] = "pacing_due"


class ScheduleOutcome(PacingModel):
    action: Literal["scheduled", "ignored", "manual_review", "blocked"]
    reason_code: str
    plan: PacingPlanRecord | None = None


class RecoveryReport(PacingModel):
    held_plan_ids: tuple[UUID, ...] = ()
    reason_code: str = "normal_restart"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def grapheme_count(text: str) -> int:
    """Small stdlib approximation: combining marks attach to their base character."""
    import unicodedata

    return sum(1 for char in text if not unicodedata.combining(char))


def segment_gap_seconds(previous_segment: str, limits: SchedulerLimits) -> float:
    # The formula is deterministic and bounded; it does not emulate typing.
    return clamp(
        grapheme_count(previous_segment) / 8,
        limits.segment_gap_min_seconds,
        limits.segment_gap_max_seconds,
    )


def in_window(moment: datetime, windows: tuple[AllowedTimeWindow, ...]) -> bool:
    if not windows:
        return True
    current = moment.astimezone(UTC).timetz().replace(tzinfo=None)
    for window in windows:
        if window.start < window.end and window.start <= current < window.end:
            return True
        if window.start > window.end and (
            current >= window.start or current < window.end
        ):
            return True
    return False


def next_window_start(
    moment: datetime, windows: tuple[AllowedTimeWindow, ...]
) -> datetime:
    if not windows or in_window(moment, windows):
        return moment
    base = moment.astimezone(UTC)
    candidates: list[datetime] = []
    for day_offset in range(3):
        day = (base + timedelta(days=day_offset)).date()
        for window in windows:
            candidate = datetime.combine(day, window.start, tzinfo=UTC)
            if candidate >= base:
                candidates.append(candidate)
    return min(candidates)


# --- m10_audit payload privacy (P1) -----------------------------------------
#
# ``m10_audit.payload_json`` is a diagnostic store, never a content store.  The
# sanitizer below is the single deterministic gate in front of every audit
# write and the in-place migration that repairs pre-existing rows.

AUDIT_DIGEST_PREFIX = "sha256:"
AUDIT_REDACTED_MARKER = "[redacted]"

_AUDIT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Free-text keys are replaced by deterministic digests; a companion ``*_hash``
# field is retained verbatim when the caller already provides one.
_AUDIT_TEXT_KEYS = frozenset(
    {
        "body",
        "bodies",
        "text",
        "texts",
        "content",
        "contents",
        "segment",
        "segments",
        "segment_body",
        "segment_bodies",
        "segment_text",
        "segment_texts",
        "segment_content",
        "segment_contents",
        "reply_body",
        "reply_bodies",
        "reply_text",
        "reply_texts",
        "reply_segment",
        "reply_segments",
        "draft_body",
        "draft_text",
        "inbound_text",
        "outbound_text",
        "input_text",
        "output_text",
        "message_body",
        "message_bodies",
        "message_text",
        "message_texts",
        "body_text",
        "raw_body",
        "raw_text",
        "plain_text",
        "plaintext",
    }
)
_AUDIT_TEXT_KEY_SUFFIXES = ("_text", "_body", "_content", "_plaintext")
_AUDIT_HASH_KEY_SUFFIXES = ("_hash", "_sha256", "_digest")

# Scalar leaves whose key is recognised metadata are safe to keep verbatim.
_AUDIT_SAFE_KEYS = frozenset(
    {
        "status",
        "action",
        "outcome",
        "reason_code",
        "rule_version",
        "pacing_rule_version",
        "expected_last_message_key",
        "source_message_keys",
        "cancellation_conditions",
        "quiet_until",
        "start",
        "end",
    }
)
_AUDIT_SAFE_KEY_SUFFIXES = (
    "_id",
    "_ids",
    "_at",
    "_version",
    "_code",
    *_AUDIT_HASH_KEY_SUFFIXES,
)


def _normalize_audit_key(key: object) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(key).strip().lower()).strip("_")


def _is_audit_text_key(normalized: str) -> bool:
    if normalized in _AUDIT_TEXT_KEYS:
        return True
    if normalized.endswith(_AUDIT_HASH_KEY_SUFFIXES):
        return False
    return normalized.endswith(_AUDIT_TEXT_KEY_SUFFIXES)


def _is_audit_safe_key(normalized: str) -> bool:
    return normalized in _AUDIT_SAFE_KEYS or normalized.endswith(
        _AUDIT_SAFE_KEY_SUFFIXES
    )


def _is_audit_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_AUDIT_DIGEST_PATTERN.match(value))


def audit_digest(value: object) -> str:
    """Return a stable, plaintext-free fingerprint for an audit-redacted value."""
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
    return AUDIT_DIGEST_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redact_audit_value(value: object) -> object:
    """Replace a free-text value with deterministic digests, preserving shape."""
    if _is_audit_digest(value):
        return value
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_redact_audit_value(item) for item in value]
    return audit_digest(value)


def _sanitize_audit_node(value: object, key: str | None) -> object:
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            normalized = _normalize_audit_key(raw_key)
            if _is_audit_text_key(normalized):
                sanitized[str(raw_key)] = _redact_audit_value(item)
            else:
                sanitized[str(raw_key)] = _sanitize_audit_node(item, normalized)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_sanitize_audit_node(item, key) for item in value]
    if isinstance(value, str):
        if key is not None and (_is_audit_safe_key(key) or _is_audit_digest(value)):
            return value
        # Unknown scalar plaintext never reaches audit storage.
        return AUDIT_REDACTED_MARKER
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return AUDIT_REDACTED_MARKER


def sanitize_audit_payload(payload: object) -> object:
    """Return a deterministic, plaintext-free copy for ``m10_audit.payload_json``.

    The pass is recursive: text/body/content keys are replaced by SHA-256
    digests (one digest per list element), IDs/statuses/counts/timestamps and
    existing ``*_hash`` values are retained, and any unexpected string leaf is
    replaced by a fixed marker.  Applying it twice is a no-op.
    """
    return _sanitize_audit_node(payload, None)
