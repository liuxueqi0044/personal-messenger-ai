import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Platform(StrEnum):
    QQ = "qq"
    WECHAT = "wechat"


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE_NOTICE = "image_notice"
    UNSUPPORTED = "unsupported"


class DraftStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    SENT = "sent"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SendStatus(StrEnum):
    PENDING = "pending"
    PREPARED = "prepared"
    COMMITTED = "committed"
    VERIFIED = "verified"
    FAILED = "failed"
    UNCERTAIN = "send_uncertain"
    CANCELLED = "cancelled"


class AuthorizationType(StrEnum):
    HUMAN = "human"
    POLICY = "policy"


class PlanStatus(StrEnum):
    PLANNED = "planned"
    WAITING = "waiting"
    AUTHORIZED = "authorized"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Message(DomainModel):
    event_id: UUID = Field(default_factory=uuid4)
    platform: Platform
    account_id: str
    conversation_id: str
    contact_id: str
    platform_message_key: str
    observed_at: datetime
    text: str = ""
    content_type: ContentType = ContentType.TEXT
    displayed_time: datetime | None = None
    extraction_confidence: float = Field(ge=0, le=1, default=1)
    evidence_ref: str | None = None
    adapter_fingerprint: str = "fake"


class InboundMessage(Message):
    pass


class ReplyPlan(DomainModel):
    action: str = "draft"
    reply_text: str = ""
    reply_segments: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    policy_tags: list[str] = Field(default_factory=list)
    must_rule_results: list[str] = Field(default_factory=list)
    prohibited_rule_results: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=1)
    assumptions: list[str] = Field(default_factory=list)
    requires_fresh_context: bool = False
    expires_at: datetime | None = None


class Draft(DomainModel):
    draft_id: UUID = Field(default_factory=uuid4)
    conversation_id: str
    contact_id: str
    text: str
    source_message_keys: tuple[str, ...] = ()
    rule_version: str
    status: DraftStatus = DraftStatus.CREATED
    text_hash: str = ""
    _locked: bool = False

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_locked", False) and name in {
            "text",
            "contact_id",
            "conversation_id",
            "source_message_keys",
            "rule_version",
            "text_hash",
        }:
            raise ValueError("authorized draft is immutable")
        super().__setattr__(name, value)

    def model_post_init(self, __context: Any, /) -> None:
        if not self.text_hash:
            object.__setattr__(
                self, "text_hash", hashlib.sha256(self.text.encode()).hexdigest()
            )

    def authorize(self) -> "Draft":
        object.__setattr__(self, "status", DraftStatus.AUTHORIZED)
        object.__setattr__(self, "_locked", True)
        return self

    def update_text(self, text: str) -> "Draft":
        if self._locked:
            raise ValueError("authorized draft is immutable")
        self.text = text
        self.text_hash = hashlib.sha256(text.encode()).hexdigest()
        return self


class Authorization(DomainModel):
    authorization_id: UUID = Field(default_factory=uuid4)
    draft_id: UUID
    conversation_id: str
    expected_last_message_key: str
    text_hash: str
    idempotency_key: str
    authorization_type: AuthorizationType
    policy_version: str
    expires_at: datetime
    consumed: bool = False


class PacingPlan(DomainModel):
    pacing_plan_id: UUID = Field(default_factory=uuid4)
    conversation_id: str
    source_message_keys: tuple[str, ...]
    quiet_until: datetime
    earliest_send_at: datetime
    expires_at: datetime
    reading_delay_ms: int = Field(ge=0)
    composition_delay_ms: int = Field(ge=0)
    inter_message_gap_ms: int = Field(ge=0)
    segment_count: int = Field(ge=1, le=3, default=1)
    cancellation_conditions: tuple[str, ...] = ()
    pacing_rule_version: str
    status: PlanStatus = PlanStatus.PLANNED


class AuthorizedSendCommand(DomainModel):
    draft_id: UUID
    conversation_id: str
    expected_last_message_key: str
    text_hash: str
    idempotency_key: str
    authorization_type: AuthorizationType
    authorization_id: UUID
    policy_version: str
    expires_at: datetime


class SendOperation(DomainModel):
    operation_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str
    draft_id: UUID
    status: SendStatus = SendStatus.PENDING
    error_code: str | None = None


class RulePack(DomainModel):
    rulepack_id: str
    version: str
    source_hash: str
    identity_profile: dict[str, Any] = Field(default_factory=dict)
    tone_and_style: dict[str, Any] = Field(default_factory=dict)
    required_behaviors: list[str] = Field(default_factory=list)
    prohibited_behaviors: list[str] = Field(default_factory=list)


class PacingProfile(DomainModel):
    hard_min_latency_seconds: float = 8
    quiet_window_seconds: float = 6
    reading_chars_per_second: float = 8
    composition_chars_per_second: float = 5
    min_reading_seconds: float = 2
    max_reading_seconds: float = 30
    min_composition_seconds: float = 4
    max_composition_seconds: float = 90
    min_inter_reply_gap_seconds: float = 15
    long_reply_threshold_chars: int = 60
    long_reply_min_latency_seconds: float = 30
    plan_ttl_seconds: float = 300
