"""Strict M7 source, compiled-rule, report, and activation models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionMode(StrEnum):
    PROMPT_STYLE = "PROMPT_STYLE"
    OUTPUT_VALIDATOR = "OUTPUT_VALIDATOR"
    POLICY_GUARD = "POLICY_GUARD"
    PACING_RULE = "PACING_RULE"
    CONTEXT_RULE = "CONTEXT_RULE"
    MANUAL_ONLY = "MANUAL_ONLY"


class RulePriority(StrEnum):
    SYSTEM = "system"
    GLOBAL_PROHIBITION = "global_prohibition"
    PLATFORM = "platform"
    CONTACT = "contact"
    TEMPORARY = "temporary"
    STYLE = "style"


class SourceFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    DOCX = "docx"
    YAML = "yaml"


class RuleKind(StrEnum):
    REQUIRED = "required"
    PROHIBITED = "prohibited"
    ESCALATION = "escalation"
    STYLE = "style"
    CONTEXT = "context"
    PACING = "pacing"


class PersonaProfile(RuleModel):
    identity: str = "待用户文件补充"
    language: str = "zh-CN"
    tone: tuple[str, ...] = ()
    preferred_length: Literal["concise", "normal", "detailed"] = "concise"


class PacingRules(RuleModel):
    hard_min_latency_seconds: float = Field(default=8, ge=0)
    quiet_window_seconds: float = Field(default=6, ge=0)
    reading_chars_per_second: float = Field(default=8, gt=0)
    composition_chars_per_second: float = Field(default=5, gt=0)
    min_inter_reply_gap_seconds: float = Field(default=15, ge=0)
    long_reply_threshold_chars: int = Field(default=60, ge=1)
    long_reply_min_latency_seconds: float = Field(default=30, ge=30)
    auto_reply_max_chars: int = Field(default=120, ge=1)
    auto_reply_max_segments: int = Field(default=3, ge=1, le=3)


class Rule(RuleModel):
    rule_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    kind: RuleKind
    enforcement: tuple[ExecutionMode, ...] = ()
    priority: RulePriority = RulePriority.STYLE
    source_location: str = ""
    enabled: bool = True

    @field_validator("enforcement")
    @classmethod
    def unique_enforcement(
        cls, value: tuple[ExecutionMode, ...]
    ) -> tuple[ExecutionMode, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate enforcement modes")
        return value

    @model_validator(mode="after")
    def prohibited_is_enforceable(self) -> Rule:
        hard = {
            ExecutionMode.POLICY_GUARD,
            ExecutionMode.OUTPUT_VALIDATOR,
            ExecutionMode.MANUAL_ONLY,
        }
        if self.kind == RuleKind.PROHIBITED and not hard.intersection(self.enforcement):
            raise ValueError("prohibited behavior must map to a hard enforcement mode")
        return self


class ContactOverride(RuleModel):
    contact_id: str = Field(min_length=2, max_length=128)
    required_behaviors: tuple[Rule, ...] = ()
    prohibited_behaviors: tuple[Rule, ...] = ()
    tone: tuple[str, ...] = ()
    pacing: PacingRules | None = None

    @field_validator("contact_id")
    @classmethod
    def stable_contact_id(cls, value: str) -> str:
        if value != value.strip() or any(char.isspace() for char in value):
            raise ValueError(
                "contact override must use stable contact_id, not a display name"
            )
        if value.casefold() in {"name", "nickname", "display_name", "unknown"}:
            raise ValueError("nickname-like contact keys are not stable IDs")
        if not value.startswith(
            ("contact-", "contact_", "contact:", "contact/")
        ) and not any(char.isdigit() for char in value):
            raise ValueError(
                "contact override must use a stable contact_id, not a nickname"
            )
        return value


class NormalizedRuleSource(RuleModel):
    schema_version: Literal[1] = 1
    rulepack_id: str = Field(min_length=1, max_length=128)
    persona: PersonaProfile = Field(default_factory=PersonaProfile)
    required_behaviors: tuple[Rule, ...] = ()
    prohibited_behaviors: tuple[Rule, ...] = ()
    escalation_rules: tuple[Rule, ...] = ()
    pacing: PacingRules = Field(default_factory=PacingRules)
    contacts: tuple[ContactOverride, ...] = ()
    examples_positive: tuple[str, ...] = ()
    examples_negative: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_contacts(self) -> NormalizedRuleSource:
        contact_ids = [item.contact_id for item in self.contacts]
        if len(set(contact_ids)) != len(contact_ids):
            raise ValueError("duplicate contact_id override")
        return self


class RuleSource(RuleModel):
    name: str = Field(min_length=1)
    content: bytes
    format: SourceFormat | None = None

    @model_validator(mode="after")
    def nonempty(self) -> RuleSource:
        if not self.content:
            raise ValueError("rule source is empty")
        return self


class ConflictReport(RuleModel):
    conflict_id: str
    rule_ids: tuple[str, ...]
    excerpts: tuple[str, ...]
    priorities: tuple[RulePriority, ...]
    suggestion: str
    affected_test_ids: tuple[str, ...] = ()


class AmbiguityReport(RuleModel):
    ambiguity_id: str
    rule_id: str
    excerpt: str
    reason: str
    suggestion: str


class RuleTestCase(RuleModel):
    test_id: str
    rule_id: str
    kind: Literal["positive", "negative", "boundary"]
    input_text: str
    expected: str
    enforcement: tuple[ExecutionMode, ...]


class RulePackReport(RuleModel):
    valid: bool
    errors: tuple[str, ...] = ()
    conflicts: tuple[ConflictReport, ...] = ()
    ambiguities: tuple[AmbiguityReport, ...] = ()
    warnings: tuple[str, ...] = ()
    test_cases: tuple[RuleTestCase, ...] = ()

    @property
    def requires_human_review(self) -> bool:
        return bool(self.conflicts or self.ambiguities or self.errors)


class RuleEvalReport(RuleModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed_test_ids: tuple[str, ...] = ()
    rulepack_version: str = ""

    @model_validator(mode="after")
    def counts(self) -> RuleEvalReport:
        if self.passed > self.total:
            raise ValueError("passed tests cannot exceed total")
        return self


class RulePackDraft(RuleModel):
    draft_id: str = Field(default_factory=lambda: str(uuid4()))
    rulepack_id: str
    version: str
    source_name: str
    source_format: SourceFormat
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized: NormalizedRuleSource
    report: RulePackReport
    compiler_version: str = "m7-1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ImmutableRulePack(RuleModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rulepack_id: str
    version: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized: NormalizedRuleSource
    compiler_version: str
    activated_at: datetime


PersonaRulePack = ImmutableRulePack


class HumanApproval(RuleModel):
    approver_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    approved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuleContext(RuleModel):
    rulepack: ImmutableRulePack
    contact_override: ContactOverride | None = None
    effective_required: tuple[Rule, ...] = ()
    effective_prohibited: tuple[Rule, ...] = ()


class ActivationEvent(RuleModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: Literal["rulepack.activated", "rulepack.rolled_back"]
    rulepack_id: str
    new_version: str
    previous_version: str | None = None
    source_hash: str
