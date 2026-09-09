from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from messenger_ai.domain import DomainModel, Platform


class ContactStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class FactStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"


class SourceKind(StrEnum):
    MESSAGE = "message"
    USER_EDIT = "user_edit"


class HumanApproval(DomainModel):
    approval_id: UUID = Field(default_factory=uuid4)
    verified_by: str = Field(min_length=1)
    verified_at: datetime
    reason: str = Field(min_length=1)

    @field_validator("verified_at")
    @classmethod
    def requires_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return value.astimezone(UTC)


class Contact(DomainModel):
    contact_id: str = Field(min_length=1)
    display_name: str = ""
    relationship_tags: tuple[str, ...] = ()
    status: ContactStatus = ContactStatus.ACTIVE
    created_at: datetime


class IdentityBinding(DomainModel):
    binding_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    platform: Platform
    account_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    platform_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval: HumanApproval | None = None
    # Kept explicit for the stable M6 contract; approval adds the review reason/id.
    verified_by: str | None = None
    verified_at: datetime | None = None

    @model_validator(mode="after")
    def require_explicit_human_verification(self) -> IdentityBinding:
        if self.approval is None:
            if not self.verified_by or self.verified_at is None:
                raise ValueError(
                    "new identity bindings require HumanApproval or verifier"
                )
            object.__setattr__(
                self,
                "approval",
                HumanApproval(
                    verified_by=self.verified_by,
                    verified_at=self.verified_at,
                    reason="explicit verifier assertion",
                ),
            )
        assert self.approval is not None
        if (
            self.verified_by is not None
            and self.verified_by != self.approval.verified_by
        ):
            raise ValueError("verified_by conflicts with approval")
        if (
            self.verified_at is not None
            and self.verified_at != self.approval.verified_at
        ):
            raise ValueError("verified_at conflicts with approval")
        object.__setattr__(self, "verified_by", self.approval.verified_by)
        object.__setattr__(self, "verified_at", self.approval.verified_at)
        return self


class MemoryMessage(DomainModel):
    message_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    source_event_id: UUID
    platform_message_key: str = Field(min_length=1)
    text: str
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> MemoryMessage:
        if self.observed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("message timestamps must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("message expiry must be after observation")
        return self


class ConversationSummary(DomainModel):
    summary_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    text: str
    from_message_key: str = Field(min_length=1)
    to_message_key: str = Field(min_length=1)
    generation_version: str = Field(min_length=1)
    source_message_ids: tuple[UUID, ...]
    created_at: datetime


class SourceEvidence(DomainModel):
    kind: SourceKind
    source_message_id: UUID | None = None
    user_edit_id: str | None = None
    recorded_by: str = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def one_traceable_source(self) -> SourceEvidence:
        is_message = self.source_message_id is not None
        is_edit = self.user_edit_id is not None
        if is_message == is_edit:
            raise ValueError("source evidence requires exactly one source")
        if self.kind is SourceKind.MESSAGE and not is_message:
            raise ValueError("message evidence requires source_message_id")
        if self.kind is SourceKind.USER_EDIT and not is_edit:
            raise ValueError("user edit evidence requires user_edit_id")
        return self


class ContactFact(DomainModel):
    fact_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    value: str
    confidence: float = Field(ge=0, le=1)
    status: FactStatus
    evidence: SourceEvidence
    hard_rule: bool = False
    created_at: datetime


class ContactPreference(DomainModel):
    preference_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    value: str
    approval: HumanApproval
    hard_rule: bool = False


class RelationshipState(DomainModel):
    state_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    evidence: SourceEvidence
    approval: HumanApproval


class ContactContext(DomainModel):
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    hard_rules: tuple[ContactFact | ContactPreference, ...] = ()
    confirmed_preferences: tuple[ContactPreference, ...] = ()
    confirmed_facts: tuple[ContactFact, ...] = ()
    relationship_states: tuple[RelationshipState, ...] = ()
    summaries: tuple[ConversationSummary, ...] = ()
    recent_messages: tuple[MemoryMessage, ...] = ()
    source_message_ids: tuple[UUID, ...] = ()
    budget_chars: int = Field(gt=0)
    used_chars: int = Field(ge=0)
    truncated: bool = False


class DeletionReceipt(DomainModel):
    deletion_id: UUID = Field(default_factory=uuid4)
    contact_id: str = Field(min_length=1)
    deleted_by: str = Field(min_length=1)
    deleted_at: datetime
    recoverable_until: datetime
    external_cleanup_scopes: tuple[str, ...] = ("plans", "rule_overrides", "evidence")
