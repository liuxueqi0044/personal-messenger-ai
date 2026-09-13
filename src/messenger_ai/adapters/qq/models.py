from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
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
    # Q1 structural selectors.  These fields are optional so deployed v1 packs
    # continue to validate while newer packs avoid relying on AutomationId.
    required_patterns: tuple[str, ...] = ()
    class_name_tokens: tuple[str, ...] = ()
    ancestor_control_types: tuple[str, ...] = ()
    selected_class_name_token: str | None = None
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


class QQCertifiedDirectIdentity(DomainModel):
    """Fresh proof for the currently selected one-to-one conversation."""

    profile_id_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")
    conversation_type: Literal["direct"]
    client_version: str = Field(min_length=1)
    selector_pack_version: str = Field(min_length=1)
    group_marker_probe_complete: Literal[True]
    group_marker_count: Literal[0]
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    header_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    right_region_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def participant_signature(self) -> str:
        return f"qq-profile-hmac:{self.profile_id_hmac}"


class QQSessionObservedDirectIdentity(DomainModel):
    """Operator-observed direct identity with separate stable and locator proofs."""

    binding_id: str = Field(min_length=1)
    conversation_type: Literal["direct"]
    type_evidence_source: Literal["operator_observed_direct"]
    client_version: str = Field(min_length=1)
    selector_pack_version: str = Field(min_length=1)
    group_marker_probe_complete: Literal[True]
    group_marker_count: Literal[0]
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    process_started_at_100ns: int = Field(gt=0)
    vm_environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_row_runtime_id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    header_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def participant_signature(self) -> str:
        """Stable contact proof, deliberately independent of QQ window lifetime.

        The PID/HWND/start time and selected runtime-id remain available as
        same-session locators, but cannot be part of a participant identity:
        QQ replaces all of them after a restart or window recreation.
        """
        stable = {
            "binding_id": self.binding_id,
            "conversation_type": self.conversation_type,
            "header_digest": self.header_digest,
            "type_evidence_source": self.type_evidence_source,
        }
        digest = hashlib.sha256(json.dumps(stable, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
        return f"qq-session-observed:{digest}"

    @property
    def legacy_participant_signature(self) -> str:
        """The pre-restart-stability digest, retained solely for migration."""
        legacy = {
            "binding_id": self.binding_id,
            "client_version": self.client_version,
            "conversation_type": self.conversation_type,
            "header_digest": self.header_digest,
            "process_id": self.process_id,
            "process_started_at_100ns": self.process_started_at_100ns,
            "selected_row_runtime_id_hash": self.selected_row_runtime_id_hash,
            "selector_pack_version": self.selector_pack_version,
            "type_evidence_source": self.type_evidence_source,
            "vm_environment_fingerprint": self.vm_environment_fingerprint,
            "window_handle": self.window_handle,
        }
        digest = hashlib.sha256(json.dumps(legacy, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
        return f"qq-session-observed:{digest}"


class QQIdentityBinding(DomainModel):
    """Human-approved binding; display_name is presentation-only evidence."""

    hub_conversation_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    platform_conversation_id: str = Field(min_length=1)
    participant_signature: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    # Legacy construction stays fail-closed. Production requires the explicit
    # all-direct scope; temporary one-to-one sessions are included by consent.
    conversation_type: Literal["unknown", "direct", "group"] = "unknown"
    friendship_verified: bool = False
    authorization_scope: Literal["legacy_explicit_contacts", "all_direct_including_temporary"] = "legacy_explicit_contacts"

    def matches(self, conversation: QQConversation) -> bool:
        return (
            not conversation.participant_signature.startswith("uncertified:")
            and self.platform_conversation_id == conversation.internal_id
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
