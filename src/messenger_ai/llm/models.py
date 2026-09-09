"""Strict contracts for the side-effect-free M8 reply planner.

The models in this module intentionally contain no platform, sending, or
authorization concepts.  They are the only data that may cross the provider
boundary.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    # ``extra=forbid`` is the important contract boundary.  Pydantic's normal
    # enum parsing keeps the Python API ergonomic while provider JSON is still
    # rejected structurally by the generated strict schema.
    model_config = ConfigDict(extra="forbid")


class ReplyAction(StrEnum):
    IGNORE = "ignore"
    DRAFT = "draft"
    AUTO_REPLY_CANDIDATE = "auto_reply_candidate"
    HANDOFF = "handoff"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    HUMAN_OUTBOUND = "human_outbound"
    BOT_OUTBOUND = "bot_outbound"


class InboundItem(StrictModel):
    """A minimum, already-isolated message projection.

    ``text`` is data, never instructions.  ``message_key`` is included in the
    request fingerprint so a late provider response can be discarded.
    """

    message_key: str = Field(min_length=1, max_length=256)
    text: str = Field(max_length=8000)
    observed_at: datetime | None = None
    direction: MessageDirection = MessageDirection.INBOUND


class ContextItem(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=2000)
    source: str = Field(min_length=1, max_length=256)
    confidence: float = Field(ge=0, le=1, default=1)


class ContactProjection(StrictModel):
    """Minimal per-contact context prepared by M6."""

    contact_id: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=256)
    relationship_stage: Literal["new", "familiar", "committed", "unknown"] = "unknown"
    facts: tuple[ContextItem, ...] = ()
    preferences: tuple[ContextItem, ...] = ()
    summaries: tuple[ContextItem, ...] = ()
    recent_messages: tuple[InboundItem, ...] = ()
    source_ids: tuple[str, ...] = ()


class RuleProjection(StrictModel):
    """Layered M7 projection; each field is a separate prompt layer."""

    rulepack_id: str = Field(min_length=1, max_length=256)
    rule_version: str = Field(min_length=1, max_length=128)
    source_hash: str = Field(min_length=1, max_length=128)
    system_safety: tuple[str, ...] = ()
    persona_style: tuple[str, ...] = ()
    persona_identity: str = ""
    language: str = "zh-CN"
    preferred_length: str = "concise"
    behavior: tuple[str, ...] = ()
    prohibited: tuple[str, ...] = ()
    escalation: tuple[str, ...] = ()
    examples_positive: tuple[str, ...] = ()
    examples_negative: tuple[str, ...] = ()
    pacing_note: str = "Planner does not choose send times"


class ReplyPlan(StrictModel):
    """The complete structured candidate emitted by a model provider."""

    action: ReplyAction
    reply_text: str = Field(default="", max_length=4000)
    reply_segments: list[str] = Field(default_factory=list, max_length=3)
    risk_level: RiskLevel = RiskLevel.LOW
    policy_tags: tuple[str, ...] = ()
    must_rule_results: tuple[str, ...] = ()
    prohibited_rule_results: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1, default=0)
    expires_at: datetime | None = None
    selection_reason: str = Field(default="", max_length=500)
    variation_seed: str = Field(default="", max_length=64)

    @field_validator("reply_text", mode="before")
    @classmethod
    def text_is_string(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise TypeError("reply_text must be a string")
        return value

    @field_validator("reply_segments", mode="before")
    @classmethod
    def segments_are_sequence(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise TypeError("reply_segments must be an array")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> ReplyPlan:
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware")
        if self.action in {ReplyAction.IGNORE, ReplyAction.HANDOFF}:
            if self.reply_text or self.reply_segments:
                raise ValueError("ignore/handoff plans cannot contain reply text")
        else:
            if not self.reply_text.strip():
                raise ValueError("reply candidates require reply_text")
            if not 1 <= len(self.reply_segments) <= 3:
                raise ValueError("reply candidates require one to three segments")
            if any(not item.strip() for item in self.reply_segments):
                raise ValueError("reply segments cannot be blank")
        return self

    def normalized(self) -> ReplyPlan:
        """Return a deterministic style normalization after strict validation."""

        def clean(value: str) -> str:
            value = " ".join(value.strip().split())
            while value and value[-1] in "。！？!?，,；;：:~～…":
                value = value[:-1].rstrip()
            return value

        text = clean(self.reply_text)
        segments = [clean(item) for item in self.reply_segments]
        return self.model_copy(update={"reply_text": text, "reply_segments": segments})


class ReplyPlanRequest(StrictModel):
    """Versioned input to a provider; all user-originated text is data."""

    request_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=256)
    contact: ContactProjection
    rules: RuleProjection
    inbound: tuple[InboundItem, ...] = Field(min_length=1, max_length=50)
    context_fingerprint: str = Field(min_length=1, max_length=128)
    created_at: datetime
    model_hint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def same_conversation(self) -> ReplyPlanRequest:
        if self.contact.recent_messages and any(
            item.message_key == "" for item in self.contact.recent_messages
        ):
            raise ValueError("context messages need stable keys")
        return self


class UsageRecord(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ProviderError(StrictModel):
    category: Literal[
        "rate_limited",
        "server_error",
        "timeout",
        "network",
        "schema",
        "rejected",
        "unknown",
    ]
    message: str = Field(max_length=500)
    retryable: bool = False
    status_code: int | None = Field(default=None, ge=100, le=599)


class ReplyPlanResult(StrictModel):
    """Auditable provider result.  A result is never a send instruction."""

    request_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=128)
    context_fingerprint: str = Field(min_length=1, max_length=128)
    plan: ReplyPlan | None = None
    model: str = Field(min_length=1, max_length=128)
    latency_ms: int = Field(ge=0)
    usage: UsageRecord = Field(default_factory=UsageRecord)
    error: ProviderError | None = None
    stale: bool = False

    @model_validator(mode="after")
    def valid_success_or_error(self) -> ReplyPlanResult:
        if (self.plan is None) == (self.error is None) and not self.stale:
            raise ValueError("result must contain exactly one plan or error")
        if self.stale and self.plan is not None:
            raise ValueError("stale result cannot contain a plan")
        return self


class PromptProjection(StrictModel):
    system_safety: str
    persona_style: str
    behavior: str
    contact_context: str
    current_inbound: str
    output_schema: dict[str, Any]
