from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from messenger_ai.domain import DomainModel


class BubbleDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class QQWindow(DomainModel):
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    class_name: str = Field(min_length=1)
    title: str = ""


class QQSelector(DomainModel):
    name: str = Field(min_length=1)
    automation_id: str | None = None
    class_name: str | None = None
    control_type: str = Field(min_length=1)
    ancestor_automation_ids: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1, default=1)


class QQSelectorPack(DomainModel):
    """Selectors are deliberately non-portable across QQ builds or desktops."""

    client_version: str = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selectors: tuple[QQSelector, ...]
    last_verified_at: datetime
    fixture_suite_version: str = Field(min_length=1)

    @field_validator("last_verified_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_verified_at must be timezone-aware")
        return value

    def selector(self, name: str) -> QQSelector:
        matches = [candidate for candidate in self.selectors if candidate.name == name]
        if len(matches) != 1:
            raise ValueError(f"selector {name!r} is absent or ambiguous")
        return matches[0]


class QQConversation(DomainModel):
    internal_id: str = Field(min_length=1)
    display_name: str = ""
    participant_signature: str = Field(min_length=1)
    last_message_key: str = ""
    tree_digest: str = Field(min_length=1)


class QQIdentityBinding(DomainModel):
    """Human-approved binding; display_name is presentation-only evidence."""

    hub_conversation_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    platform_conversation_id: str = Field(min_length=1)
    participant_signature: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)

    def matches(self, conversation: QQConversation) -> bool:
        return (
            self.platform_conversation_id == conversation.internal_id
            and self.participant_signature == conversation.participant_signature
        )


class QQBubble(DomainModel):
    conversation_internal_id: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    direction: BubbleDirection
    text: str = ""
    observed_at: datetime
    tree_digest: str = Field(min_length=1)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class QQPreparedEvidence(DomainModel):
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    conversation_internal_id: str = Field(min_length=1)
    participant_signature: str = Field(min_length=1)
    last_message_key: str
    tree_digest: str = Field(min_length=1)
    text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_at: datetime

    @field_validator("bound_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bound_at must be timezone-aware")
        return value

    @property
    def digest(self) -> str:
        payload: dict[str, Any] = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QQPreparedSend(DomainModel):
    operation_id: UUID
    idempotency_key: str = Field(min_length=1)
    text: str
    evidence: QQPreparedEvidence


# Concise aliases make the public contract easy to discover without leaking UIA.
SelectorPack = QQSelectorPack
IdentityBinding = QQIdentityBinding
PreparedSend = QQPreparedSend
