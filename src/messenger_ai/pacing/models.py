"""Pure contracts for M10's deterministic, send-free pacing boundary."""

from __future__ import annotations

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
    conversation_id: str
    contact_id: str
    draft_id: UUID
    text_hash: str
    expected_last_message_key: str
    source_message_keys: tuple[str, ...]
    rule_version: str
    eligibility_id: str
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
