"""Strict, side-effect-free contracts for deterministic policy decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def sha256_text(value: str) -> str:
    """Hash the exact UTF-8 body; whitespace is intentionally significant."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value: BaseModel | dict[str, object]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyOutcome(StrEnum):
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review"
    AUTO_ELIGIBLE = "auto_eligible"
    HUMAN_ELIGIBLE = "human_eligible"


class PolicyPhase(StrEnum):
    ELIGIBILITY = "eligibility"
    DUE_REVALIDATION = "due_revalidation"
    EXECUTION_REVALIDATION = "execution_revalidation"


class AuthorizationKind(StrEnum):
    HUMAN = "human"
    POLICY = "policy"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class ReplyAction(StrEnum):
    IGNORE = "ignore"
    DRAFT = "draft"
    AUTO_REPLY_CANDIDATE = "auto_reply_candidate"
    HANDOFF = "handoff"


class ConversationType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    UNKNOWN = "unknown"


class SupportLevel(StrEnum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class PolicyReason(StrEnum):
    ELIGIBLE_LOW_RISK = "ELIGIBLE_LOW_RISK"
    ELIGIBLE_HUMAN_APPROVED = "ELIGIBLE_HUMAN_APPROVED"
    NO_REPLY_REQUESTED = "NO_REPLY_REQUESTED"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    POLICY_PAUSED = "POLICY_PAUSED"
    AUTO_MODE_DISABLED = "AUTO_MODE_DISABLED"
    MANUAL_TAKEOVER = "MANUAL_TAKEOVER"
    CONTACT_REQUESTED_STOP = "CONTACT_REQUESTED_STOP"
    OUTBOUND_WITHOUT_REPLY_LIMIT = "OUTBOUND_WITHOUT_REPLY_LIMIT"
    DRAFT_EXPIRED = "DRAFT_EXPIRED"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    STALE_ELIGIBILITY = "STALE_ELIGIBILITY"
    STALE_DRAFT = "STALE_DRAFT"
    STALE_LAST_MESSAGE = "STALE_LAST_MESSAGE"
    STALE_RULEPACK = "STALE_RULEPACK"
    STALE_PACING_RULE = "STALE_PACING_RULE"
    STALE_CAPABILITY = "STALE_CAPABILITY"
    STALE_POLICY_STATE = "STALE_POLICY_STATE"
    TOO_EARLY = "TOO_EARLY"
    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    NEW_CONTACT = "NEW_CONTACT"
    NOT_WHITELISTED = "NOT_WHITELISTED"
    UNSUPPORTED_CONVERSATION = "UNSUPPORTED_CONVERSATION"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    NO_FRESH_INBOUND = "NO_FRESH_INBOUND"
    ADAPTER_UNHEALTHY = "ADAPTER_UNHEALTHY"
    BACKGROUND_SEND_UNSUPPORTED = "BACKGROUND_SEND_UNSUPPORTED"
    BACKGROUND_VERIFY_UNSUPPORTED = "BACKGROUND_VERIFY_UNSUPPORTED"
    OUTSIDE_ALLOWED_WINDOW = "OUTSIDE_ALLOWED_WINDOW"
    QUIET_WINDOW_ACTIVE = "QUIET_WINDOW_ACTIVE"
    RATE_LIMITED = "RATE_LIMITED"
    LOW_MODEL_CONFIDENCE = "LOW_MODEL_CONFIDENCE"
    MODEL_RISK_REVIEW = "MODEL_RISK_REVIEW"
    MODEL_RISK_BLOCKED = "MODEL_RISK_BLOCKED"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
    PROHIBITED_RULE_HIT = "PROHIBITED_RULE_HIT"
    MANUAL_RULE_HIT = "MANUAL_RULE_HIT"
    SENSITIVE_TOPIC = "SENSITIVE_TOPIC"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    PROHIBITED_OUTPUT = "PROHIBITED_OUTPUT"
    MINOR_SAFETY = "MINOR_SAFETY"
    APPROVAL_MISSING = "APPROVAL_MISSING"
    APPROVAL_STALE = "APPROVAL_STALE"
    AUTHORIZATION_ISSUED = "AUTHORIZATION_ISSUED"
    AUTHORIZATION_INVALID = "AUTHORIZATION_INVALID"
    AUTHORIZATION_TAMPERED = "AUTHORIZATION_TAMPERED"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    AUTHORIZATION_CONSUMED = "AUTHORIZATION_CONSUMED"
    AUTHORIZATION_REPLAYED = "AUTHORIZATION_REPLAYED"
    AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"
    EXECUTION_BINDING_MISMATCH = "EXECUTION_BINDING_MISMATCH"


class SensitiveCategory(StrEnum):
    MONEY = "money"
    CREDENTIALS = "credentials"
    PRIVACY = "privacy"
    OFFLINE_ACTION = "offline_action"
    LEGAL_MEDICAL = "legal_medical"
    CONFLICT_CRISIS = "conflict_crisis"
    SEXUAL_CONTENT = "sexual_content"
    MINOR = "minor"
    IDENTITY_AUTOMATION = "identity_automation"
    EXTERNAL_CONTENT = "external_content"


class ExecutionMode(StrEnum):
    BACKGROUND = "background"
    GUEST_FOREGROUND = "guest_foreground"


class CapabilitySnapshot(PolicyModel):
    capability_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(min_length=1)
    send_background: SupportLevel
    verify_background: SupportLevel
    send_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    verify_guest_foreground: SupportLevel = SupportLevel.UNSUPPORTED
    healthy: bool
    client_version: str = Field(min_length=1)
    execution_mode: ExecutionMode = ExecutionMode.BACKGROUND
    binding_revision: int = Field(default=1, ge=1)

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self)


class DraftSnapshot(PolicyModel):
    draft_id: str = Field(min_length=1)
    platform: Literal["qq", "wechat"]
    account_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=4000)
    body_hash: str = ""
    expected_last_message_key: str = Field(min_length=1)
    source_message_keys: tuple[str, ...] = ()
    rulepack_version: str = Field(min_length=1)
    pacing_plan_id: str = Field(min_length=1)
    pacing_rule_version: str = Field(min_length=1)
    capability_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_state_version: str = Field(min_length=1)
    binding_revision: int = Field(default=1, ge=1)
    conversation_revision: int = Field(default=1, ge=1)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_snapshot(self) -> DraftSnapshot:
        calculated = sha256_text(self.body)
        if self.body_hash and self.body_hash != calculated:
            raise ValueError("body_hash does not match the exact draft body")
        if self.expires_at <= self.created_at:
            raise ValueError("draft expiry must be after creation")
        if (
            self.source_message_keys
            and self.expected_last_message_key not in self.source_message_keys
        ):
            raise ValueError("expected last message must be one of the source messages")
        if not self.body_hash:
            object.__setattr__(self, "body_hash", calculated)
        return self

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self)


class PlannerAssessment(PolicyModel):
    action: ReplyAction = ReplyAction.DRAFT
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: float = Field(default=1.0, ge=0, le=1)
    policy_tags: tuple[str, ...] = ()
    prohibited_rule_ids: tuple[str, ...] = ()
    manual_rule_ids: tuple[str, ...] = ()
    output_validated: bool = True
    marked_long_reply: bool = False


class HumanApproval(PolicyModel):
    approval_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    approver_id: str = Field(min_length=1)
    subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_at: datetime
    expires_at: datetime
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("approved_at", "expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> HumanApproval:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after approval")
        return self


class LivePolicyState(PolicyModel):
    observed_at: datetime
    last_message_key: str = Field(min_length=1)
    active_rulepack_version: str = Field(min_length=1)
    active_pacing_rule_version: str = Field(min_length=1)
    capability: CapabilitySnapshot
    policy_state_version: str = Field(min_length=1)
    binding_revision: int = Field(default=1, ge=1)
    conversation_revision: int = Field(default=1, ge=1)
    conversation_type: ConversationType = ConversationType.DIRECT
    inbound_content_type: Literal["text", "image_notice", "unsupported"] = "text"
    outbound_content_type: Literal["text", "unsupported"] = "text"
    identity_unique: bool = True
    context_complete: bool = True
    is_new_contact: bool = False
    contact_whitelisted: bool = False
    counterparty_initiated: bool = True
    source_inbound_unhandled: bool = True
    automation_enabled: bool = False
    global_paused: bool = False
    platform_paused: bool = False
    contact_paused: bool = False
    manual_takeover: bool = False
    contact_requested_stop: bool = False
    consecutive_unanswered_outbound: int = Field(default=0, ge=0)
    known_or_suspected_minor: bool = False
    quiet_window_complete: bool = True
    within_allowed_window: bool = True
    rate_limit_available: bool = True

    @field_validator("observed_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class PolicyRequest(PolicyModel):
    draft: DraftSnapshot
    inbound_text: str = Field(default="", max_length=12000)
    assessment: PlannerAssessment = Field(default_factory=PlannerAssessment)
    state: LivePolicyState
    scheduled_due_at: datetime
    plan_expires_at: datetime
    approval: HumanApproval | None = None

    @field_validator("scheduled_due_at", "plan_expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("schedule timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_schedule(self) -> PolicyRequest:
        if self.plan_expires_at <= self.scheduled_due_at:
            raise ValueError("plan expiry must be after due time")
        return self


class PolicyDecision(PolicyModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    phase: PolicyPhase
    outcome: PolicyOutcome
    subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_kind: AuthorizationKind | None = None
    reason_codes: tuple[PolicyReason, ...]
    rule_ids: tuple[str, ...] = ()
    sensitive_categories: tuple[SensitiveCategory, ...] = ()
    rulepack_version: str
    evaluated_at: datetime
    valid_until: datetime
    audit_summary: str
    approval_id: str | None = None

    @field_validator("evaluated_at", "valid_until")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision timestamps must be timezone-aware")
        return value

    @property
    def may_schedule(self) -> bool:
        return self.outcome in {
            PolicyOutcome.AUTO_ELIGIBLE,
            PolicyOutcome.HUMAN_ELIGIBLE,
        }

    @property
    def may_authorize(self) -> bool:
        return self.phase is PolicyPhase.DUE_REVALIDATION and self.may_schedule


class AuthorizationBinding(PolicyModel):
    eligibility_id: str = Field(min_length=1)
    authorization_kind: AuthorizationKind
    draft_id: str = Field(min_length=1)
    platform: Literal["qq", "wechat"]
    account_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_last_message_key: str = Field(min_length=1)
    rulepack_version: str = Field(min_length=1)
    pacing_plan_id: str = Field(min_length=1)
    pacing_rule_version: str = Field(min_length=1)
    capability_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_state_version: str = Field(min_length=1)
    execution_mode: ExecutionMode = ExecutionMode.BACKGROUND
    binding_revision: int = Field(default=1, ge=1)
    conversation_revision: int = Field(default=1, ge=1)
    approval_id: str | None = None

    @property
    def binding_hash(self) -> str:
        return canonical_hash(self)


class AuthorizationEnvelope(PolicyModel):
    authorization_id: str
    token: str
    binding: AuthorizationBinding
    issued_at: datetime
    expires_at: datetime


class AuthorizationConsumeResult(PolicyModel):
    accepted: bool
    authorization_id: str | None = None
    reason: PolicyReason
    consumed_at: datetime | None = None
    audit_summary: str


class InvalidationKind(StrEnum):
    GLOBAL_PAUSE = "global_pause"
    PLATFORM_PAUSE = "platform_pause"
    CONTACT_PAUSE = "contact_pause"
    AUTO_MODE_DISABLED = "auto_mode_disabled"
    DRAFT_CHANGED = "draft_changed"
    NEW_MESSAGE = "new_message"
    RULE_CHANGED = "rule_changed"
    PACING_CHANGED = "pacing_changed"
    CAPABILITY_CHANGED = "capability_changed"
    BINDING_CHANGED = "binding_changed"
    HUMAN_OUTBOUND = "human_outbound"


class InvalidationEvent(PolicyModel):
    kind: InvalidationKind
    platform: Literal["qq", "wechat"] | None = None
    conversation_id: str | None = None
    contact_id: str | None = None
    draft_id: str | None = None
    rulepack_version: str | None = None
    pacing_rule_version: str | None = None
    capability_snapshot_hash: str | None = None
    reason: str = Field(default="state changed", min_length=1, max_length=500)
