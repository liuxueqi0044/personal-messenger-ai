"""Q3 human-confirmed QQ conversation identity bindings.

Display names and list positions are presentation data only.  A binding is
created, replaced, revoked, or rolled back only after an explicit local human
confirmation.  Runtime resolution fails closed when stable identity evidence
is missing or changes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Q3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    UNKNOWN = "unknown"


class ConfirmationAction(StrEnum):
    BIND = "bind"
    REBIND = "rebind"
    REVOKE = "revoke"
    ROLLBACK = "rollback"


class BindingStatus(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


class IdentityDisposition(StrEnum):
    MATCHED = "matched"
    PENDING_HUMAN_BINDING = "pending_human_binding"
    QUARANTINED = "quarantined"


class IdentityReason(StrEnum):
    MATCHED = "identity_matched"
    UNBOUND = "identity_unbound"
    LOW_CONFIDENCE = "identity_low_confidence"
    UNKNOWN_CONVERSATION_TYPE = "identity_conversation_type_unknown"
    STABLE_SECOND_SIGNAL_REQUIRED = "identity_stable_second_signal_required"
    CRITICAL_EVIDENCE_CONFLICT = "identity_critical_evidence_conflict"
    BINDING_QUARANTINED = "identity_binding_quarantined"


class BindingAuditAction(StrEnum):
    BOUND = "binding_created"
    REBOUND = "binding_rebound"
    REVOKED = "binding_revoked"
    CONFLICT_QUARANTINED = "binding_conflict_quarantined"
    ROLLED_BACK = "binding_rolled_back"


class BindingEvidenceReason(StrEnum):
    READY = "binding_evidence_ready"
    UNKNOWN_CONVERSATION_TYPE = "binding_conversation_type_unknown"
    LOW_CONFIDENCE = "binding_evidence_low_confidence"
    STABLE_SECOND_SIGNAL_REQUIRED = "binding_stable_second_signal_required"


class HumanBindingConfirmation(Q3Model):
    """An explicit local human act, not an inference or a model decision."""

    confirmation_id: UUID = Field(default_factory=uuid4)
    action: ConfirmationAction
    confirmed_by: str = Field(min_length=1, max_length=256)
    confirmed_at: datetime
    reason: str = Field(min_length=1, max_length=1000)
    approved: Literal[True] = True

    @field_validator("confirmed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _aware(value, "confirmed_at")


class IdentityEvidenceSet(Q3Model):
    """PII-minimized, multi-signal evidence for one visible QQ conversation."""

    platform: Literal["qq"] = "qq"
    window_handle: int = Field(gt=0)
    platform_conversation_id: str = Field(min_length=1, max_length=512)
    conversation_type: ConversationType
    structure_path: tuple[str, ...] = Field(min_length=2)
    participant_signature: str = Field(min_length=1, max_length=512)
    profile_id: str | None = Field(default=None, min_length=1, max_length=512)
    avatar_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recent_message_anchor: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector_pack_version: str = Field(min_length=1, max_length=256)
    confidence: float = Field(ge=0, le=1)
    display_name: str = Field(default="", max_length=512)
    list_position: int | None = Field(default=None, ge=0)

    @field_validator("structure_path")
    @classmethod
    def semantic_path_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(segment.strip().lower() for segment in value)
        if any(not segment for segment in cleaned):
            raise ValueError("structure_path cannot contain empty segments")
        if any(segment.isdecimal() for segment in cleaned):
            raise ValueError("structure_path cannot depend on list positions")
        return cleaned

    @property
    def evidence_hash(self) -> str:
        """Hash stable identity signals; presentation fields are excluded."""

        return _digest(self.critical_payload)

    @property
    def critical_payload(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "window_handle": self.window_handle,
            "platform_conversation_id": self.platform_conversation_id,
            "conversation_type": self.conversation_type.value,
            "structure_path": self.structure_path,
            "participant_signature": self.participant_signature,
            "profile_id": self.profile_id,
            "avatar_hash": self.avatar_hash,
            "environment_fingerprint": self.environment_fingerprint,
            "selector_pack_version": self.selector_pack_version,
        }


class BindingEvidenceAssessment(Q3Model):
    bindable: bool
    automatic_eligible: bool
    reasons: tuple[BindingEvidenceReason, ...]


def assess_binding_evidence(
    evidence: IdentityEvidenceSet,
    *,
    minimum_confidence: float = 0.95,
) -> BindingEvidenceAssessment:
    """Assess whether evidence is strong enough for an active Q3 binding.

    A user-selected header and recent-message anchor prove which UI was meant
    during this one interaction, but they do not safely distinguish two
    contacts with the same display name on future observations.  At least one
    independent stable signal is therefore mandatory before automatic
    eligibility can be granted.
    """

    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum_confidence must be between zero and one")
    reasons: list[BindingEvidenceReason] = []
    if evidence.conversation_type is ConversationType.UNKNOWN:
        reasons.append(BindingEvidenceReason.UNKNOWN_CONVERSATION_TYPE)
    if evidence.confidence < minimum_confidence:
        reasons.append(BindingEvidenceReason.LOW_CONFIDENCE)
    if evidence.profile_id is None and evidence.avatar_hash is None:
        reasons.append(BindingEvidenceReason.STABLE_SECOND_SIGNAL_REQUIRED)
    if reasons:
        return BindingEvidenceAssessment(
            bindable=False,
            automatic_eligible=False,
            reasons=tuple(reasons),
        )
    return BindingEvidenceAssessment(
        bindable=True,
        automatic_eligible=True,
        reasons=(BindingEvidenceReason.READY,),
    )


class ConversationBinding(Q3Model):
    binding_id: UUID = Field(default_factory=uuid4)
    local_contact_id: str = Field(min_length=1, max_length=512)
    hub_conversation_id: str = Field(min_length=1, max_length=512)
    account_id: str = Field(min_length=1, max_length=512)
    platform: Literal["qq"] = "qq"
    ui_identity_version: int = Field(ge=1)
    evidence_set: IdentityEvidenceSet
    confidence: float = Field(ge=0, le=1)
    confirmed_by_user_at: datetime
    selector_pack_version: str = Field(min_length=1, max_length=256)
    confirmation_id: UUID
    status: BindingStatus = BindingStatus.ACTIVE
    automatic_eligible: bool = True
    supersedes_binding_id: UUID | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    @field_validator("confirmed_by_user_at", "revoked_at")
    @classmethod
    def normalize_times(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "timestamp")
        return _aware(value, field_name)

    @model_validator(mode="after")
    def coherent_state(self) -> ConversationBinding:
        if self.selector_pack_version != self.evidence_set.selector_pack_version:
            raise ValueError("selector version must match evidence")
        if self.status is BindingStatus.ACTIVE and not self.automatic_eligible:
            raise ValueError("active binding must retain automatic eligibility")
        if self.status is not BindingStatus.ACTIVE and self.automatic_eligible:
            raise ValueError("non-active binding cannot retain automatic eligibility")
        if self.status is BindingStatus.REVOKED and self.revoked_at is None:
            raise ValueError("revoked binding requires revoked_at")
        return self


class BindingAuditEntry(Q3Model):
    audit_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    action: BindingAuditAction
    binding_id: UUID
    prior_binding_id: UUID | None = None
    contact_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    occurred_at: datetime
    reason: str = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")


class IdentityResolution(Q3Model):
    disposition: IdentityDisposition
    reason: IdentityReason
    binding_id: UUID | None = None
    local_contact_id: str | None = None
    hub_conversation_id: str | None = None
    account_id: str | None = None
    ui_identity_version: int | None = None
    automatic_eligible: bool = False
    conflicting_fields: tuple[str, ...] = ()


class BindingConflictError(ValueError):
    """Raised when a requested human binding would cross an existing identity."""


class IdentityBindingRegistry:
    """Thread-safe, versioned Q3 state machine with append-only audit records."""

    def __init__(
        self,
        *,
        minimum_confidence: float = 0.95,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between zero and one")
        self._minimum_confidence = minimum_confidence
        self._clock = clock or (lambda: datetime.now(UTC))
        self._bindings: dict[UUID, ConversationBinding] = {}
        self._current_by_platform: dict[tuple[str, str], UUID] = {}
        self._current_by_contact: dict[tuple[str, str], UUID] = {}
        self._current_by_hub: dict[tuple[str, str], UUID] = {}
        self._audits: list[BindingAuditEntry] = []
        self._lock = RLock()

    def bind(
        self,
        *,
        local_contact_id: str,
        hub_conversation_id: str,
        account_id: str,
        evidence_set: IdentityEvidenceSet,
        confirmation: HumanBindingConfirmation,
    ) -> ConversationBinding:
        """Create a first binding after an explicit human confirmation."""

        self._require_confirmation(confirmation, ConfirmationAction.BIND)
        self._require_bindable(evidence_set)
        platform_key = (account_id, evidence_set.platform_conversation_id)
        contact_key = (account_id, local_contact_id)
        with self._lock:
            self._assert_available(
                platform_key,
                contact_key,
                (account_id, hub_conversation_id),
            )
            binding = ConversationBinding(
                local_contact_id=local_contact_id,
                hub_conversation_id=hub_conversation_id,
                account_id=account_id,
                ui_identity_version=1,
                evidence_set=evidence_set,
                confidence=evidence_set.confidence,
                confirmed_by_user_at=confirmation.confirmed_at,
                selector_pack_version=evidence_set.selector_pack_version,
                confirmation_id=confirmation.confirmation_id,
            )
            self._install_current(binding)
            self._audit(
                BindingAuditAction.BOUND,
                binding,
                confirmation.confirmed_by,
                confirmation.confirmed_at,
                confirmation.reason,
            )
            return binding

    create_binding = bind

    def resolve(
        self, *, account_id: str, evidence_set: IdentityEvidenceSet
    ) -> IdentityResolution:
        """Resolve visible evidence, quarantining any critical mismatch."""

        key = (account_id, evidence_set.platform_conversation_id)
        with self._lock:
            binding_id = self._current_by_platform.get(key)
            if binding_id is None:
                return IdentityResolution(
                    disposition=IdentityDisposition.PENDING_HUMAN_BINDING,
                    reason=IdentityReason.UNBOUND,
                )
            binding = self._bindings[binding_id]
            if binding.status is BindingStatus.QUARANTINED:
                return self._resolution(
                    binding,
                    IdentityDisposition.QUARANTINED,
                    IdentityReason.BINDING_QUARANTINED,
                )
            if evidence_set.conversation_type is ConversationType.UNKNOWN:
                return self._resolution(
                    binding,
                    IdentityDisposition.PENDING_HUMAN_BINDING,
                    IdentityReason.UNKNOWN_CONVERSATION_TYPE,
                )
            if evidence_set.confidence < self._minimum_confidence:
                return self._resolution(
                    binding,
                    IdentityDisposition.PENDING_HUMAN_BINDING,
                    IdentityReason.LOW_CONFIDENCE,
                )
            strength = assess_binding_evidence(
                evidence_set, minimum_confidence=self._minimum_confidence
            )
            if not strength.bindable:
                return self._resolution(
                    binding,
                    IdentityDisposition.PENDING_HUMAN_BINDING,
                    IdentityReason.STABLE_SECOND_SIGNAL_REQUIRED,
                )
            conflicts = self._critical_conflicts(binding.evidence_set, evidence_set)
            if conflicts:
                quarantined = binding.model_copy(
                    update={
                        "status": BindingStatus.QUARANTINED,
                        "automatic_eligible": False,
                        "revocation_reason": "critical identity evidence conflict",
                    }
                )
                self._bindings[binding.binding_id] = quarantined
                self._audit(
                    BindingAuditAction.CONFLICT_QUARANTINED,
                    quarantined,
                    "system",
                    _aware(self._clock(), "clock"),
                    "critical identity evidence conflict: " + ",".join(conflicts),
                )
                return self._resolution(
                    quarantined,
                    IdentityDisposition.QUARANTINED,
                    IdentityReason.CRITICAL_EVIDENCE_CONFLICT,
                    conflicts,
                )
            return self._resolution(
                binding,
                IdentityDisposition.MATCHED,
                IdentityReason.MATCHED,
            )

    def rebind(
        self,
        binding_id: UUID,
        *,
        evidence_set: IdentityEvidenceSet,
        confirmation: HumanBindingConfirmation,
    ) -> ConversationBinding:
        self._require_confirmation(confirmation, ConfirmationAction.REBIND)
        self._require_bindable(evidence_set)
        with self._lock:
            current = self._require_current(binding_id)
            platform_key = (current.account_id, evidence_set.platform_conversation_id)
            owner = self._current_by_platform.get(platform_key)
            if owner is not None and owner != binding_id:
                raise BindingConflictError(
                    "platform conversation is bound to another contact"
                )
            self._retire(current, confirmation.confirmed_at, "superseded by rebind")
            replacement = ConversationBinding(
                local_contact_id=current.local_contact_id,
                hub_conversation_id=current.hub_conversation_id,
                account_id=current.account_id,
                ui_identity_version=current.ui_identity_version + 1,
                evidence_set=evidence_set,
                confidence=evidence_set.confidence,
                confirmed_by_user_at=confirmation.confirmed_at,
                selector_pack_version=evidence_set.selector_pack_version,
                confirmation_id=confirmation.confirmation_id,
                supersedes_binding_id=current.binding_id,
            )
            self._install_current(replacement)
            self._audit(
                BindingAuditAction.REBOUND,
                replacement,
                confirmation.confirmed_by,
                confirmation.confirmed_at,
                confirmation.reason,
                prior=current.binding_id,
            )
            return replacement

    def revoke(
        self,
        binding_id: UUID,
        *,
        confirmation: HumanBindingConfirmation,
    ) -> ConversationBinding:
        self._require_confirmation(confirmation, ConfirmationAction.REVOKE)
        with self._lock:
            current = self._require_current(binding_id)
            revoked = self._retire(
                current, confirmation.confirmed_at, confirmation.reason
            )
            self._current_by_platform.pop(
                (current.account_id, current.evidence_set.platform_conversation_id),
                None,
            )
            self._current_by_contact.pop(
                (current.account_id, current.local_contact_id),
                None,
            )
            self._current_by_hub.pop(
                (current.account_id, current.hub_conversation_id),
                None,
            )
            self._audit(
                BindingAuditAction.REVOKED,
                revoked,
                confirmation.confirmed_by,
                confirmation.confirmed_at,
                confirmation.reason,
            )
            return revoked

    def rollback(
        self,
        binding_id: UUID,
        *,
        confirmation: HumanBindingConfirmation,
    ) -> ConversationBinding:
        """Undo a rebind/revocation as a new, explicitly approved version."""

        self._require_confirmation(confirmation, ConfirmationAction.ROLLBACK)
        with self._lock:
            current = self._require_latest(binding_id)
            if current.status is BindingStatus.ACTIVE:
                if current.supersedes_binding_id is None:
                    raise BindingConflictError(
                        "binding has no prior version to restore"
                    )
                source = self._bindings[current.supersedes_binding_id]
                self._retire(
                    current,
                    confirmation.confirmed_at,
                    "superseded by rollback",
                )
            else:
                # Undoing a revoke/quarantine restores the last human-approved
                # evidence, never the conflicting observation that caused it.
                source = current
                self._current_by_platform.pop(
                    (
                        current.account_id,
                        current.evidence_set.platform_conversation_id,
                    ),
                    None,
                )
                self._current_by_contact.pop(
                    (current.account_id, current.local_contact_id),
                    None,
                )
                self._current_by_hub.pop(
                    (current.account_id, current.hub_conversation_id),
                    None,
                )
            replacement = ConversationBinding(
                local_contact_id=current.local_contact_id,
                hub_conversation_id=current.hub_conversation_id,
                account_id=current.account_id,
                ui_identity_version=current.ui_identity_version + 1,
                evidence_set=source.evidence_set,
                confidence=source.confidence,
                confirmed_by_user_at=confirmation.confirmed_at,
                selector_pack_version=source.selector_pack_version,
                confirmation_id=confirmation.confirmation_id,
                supersedes_binding_id=current.binding_id,
            )
            self._install_current(replacement)
            self._audit(
                BindingAuditAction.ROLLED_BACK,
                replacement,
                confirmation.confirmed_by,
                confirmation.confirmed_at,
                confirmation.reason,
                prior=current.binding_id,
            )
            return replacement

    def get(self, binding_id: UUID) -> ConversationBinding:
        with self._lock:
            return self._bindings[binding_id]

    def audit_entries(self) -> tuple[BindingAuditEntry, ...]:
        with self._lock:
            return tuple(self._audits)

    def history(
        self, *, account_id: str, local_contact_id: str
    ) -> tuple[ConversationBinding, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._bindings.values()
                        if item.account_id == account_id
                        and item.local_contact_id == local_contact_id
                    ),
                    key=lambda item: item.ui_identity_version,
                )
            )

    def _assert_available(
        self,
        platform_key: tuple[str, str],
        contact_key: tuple[str, str],
        hub_key: tuple[str, str],
    ) -> None:
        if platform_key in self._current_by_platform:
            raise BindingConflictError("platform conversation already has a binding")
        if contact_key in self._current_by_contact:
            raise BindingConflictError(
                "contact already has a current QQ binding; use rebind"
            )
        if hub_key in self._current_by_hub:
            raise BindingConflictError(
                "hub conversation already has a current QQ binding"
            )

    def _install_current(self, binding: ConversationBinding) -> None:
        self._bindings[binding.binding_id] = binding
        self._current_by_platform[
            (binding.account_id, binding.evidence_set.platform_conversation_id)
        ] = binding.binding_id
        self._current_by_contact[(binding.account_id, binding.local_contact_id)] = (
            binding.binding_id
        )
        self._current_by_hub[(binding.account_id, binding.hub_conversation_id)] = (
            binding.binding_id
        )

    def _retire(
        self,
        binding: ConversationBinding,
        at: datetime,
        reason: str,
    ) -> ConversationBinding:
        retired = binding.model_copy(
            update={
                "status": BindingStatus.REVOKED,
                "automatic_eligible": False,
                "revoked_at": _aware(at, "revoked_at"),
                "revocation_reason": reason,
            }
        )
        self._bindings[binding.binding_id] = retired
        self._current_by_platform.pop(
            (binding.account_id, binding.evidence_set.platform_conversation_id),
            None,
        )
        self._current_by_contact.pop(
            (binding.account_id, binding.local_contact_id),
            None,
        )
        self._current_by_hub.pop(
            (binding.account_id, binding.hub_conversation_id),
            None,
        )
        return retired

    def _require_current(self, binding_id: UUID) -> ConversationBinding:
        try:
            binding = self._bindings[binding_id]
        except KeyError as exc:
            raise KeyError("unknown binding") from exc
        current_id = self._current_by_contact.get(
            (binding.account_id, binding.local_contact_id)
        )
        if current_id != binding_id:
            raise BindingConflictError("operation requires the current binding version")
        return binding

    def _require_latest(self, binding_id: UUID) -> ConversationBinding:
        try:
            binding = self._bindings[binding_id]
        except KeyError as exc:
            raise KeyError("unknown binding") from exc
        latest = max(
            (
                item
                for item in self._bindings.values()
                if item.account_id == binding.account_id
                and item.local_contact_id == binding.local_contact_id
            ),
            key=lambda item: item.ui_identity_version,
        )
        if latest.binding_id != binding_id:
            raise BindingConflictError("rollback requires the latest binding version")
        current_id = self._current_by_contact.get(
            (binding.account_id, binding.local_contact_id)
        )
        if current_id not in {None, binding_id}:
            raise BindingConflictError("another binding version is current")
        return binding

    def _require_bindable(self, evidence: IdentityEvidenceSet) -> None:
        assessment = assess_binding_evidence(
            evidence, minimum_confidence=self._minimum_confidence
        )
        if not assessment.bindable:
            raise BindingConflictError(
                "identity evidence is not strong enough for an active binding"
            )

    @staticmethod
    def _require_confirmation(
        confirmation: HumanBindingConfirmation,
        expected: ConfirmationAction,
    ) -> None:
        if confirmation.action is not expected:
            raise ValueError(f"human confirmation action must be {expected.value}")

    @staticmethod
    def _critical_conflicts(
        expected: IdentityEvidenceSet,
        observed: IdentityEvidenceSet,
    ) -> tuple[str, ...]:
        fields = (
            "window_handle",
            "platform_conversation_id",
            "conversation_type",
            "structure_path",
            "participant_signature",
            "environment_fingerprint",
            "selector_pack_version",
        )
        conflicts = [
            field
            for field in fields
            if getattr(expected, field) != getattr(observed, field)
        ]
        for optional in ("profile_id", "avatar_hash"):
            left = getattr(expected, optional)
            right = getattr(observed, optional)
            if left is not None and right is not None and left != right:
                conflicts.append(optional)
        return tuple(conflicts)

    @staticmethod
    def _resolution(
        binding: ConversationBinding,
        disposition: IdentityDisposition,
        reason: IdentityReason,
        conflicts: tuple[str, ...] = (),
    ) -> IdentityResolution:
        return IdentityResolution(
            disposition=disposition,
            reason=reason,
            binding_id=binding.binding_id,
            local_contact_id=binding.local_contact_id,
            hub_conversation_id=binding.hub_conversation_id,
            account_id=binding.account_id,
            ui_identity_version=binding.ui_identity_version,
            automatic_eligible=binding.automatic_eligible
            and disposition is IdentityDisposition.MATCHED,
            conflicting_fields=conflicts,
        )

    def _audit(
        self,
        action: BindingAuditAction,
        binding: ConversationBinding,
        actor: str,
        occurred_at: datetime,
        reason: str,
        *,
        prior: UUID | None = None,
    ) -> None:
        self._audits.append(
            BindingAuditEntry(
                sequence=len(self._audits) + 1,
                action=action,
                binding_id=binding.binding_id,
                prior_binding_id=prior,
                contact_id=binding.local_contact_id,
                actor=actor,
                occurred_at=occurred_at,
                reason=reason,
                evidence_hash=binding.evidence_set.evidence_hash,
            )
        )


__all__ = [
    "BindingAuditAction",
    "BindingAuditEntry",
    "BindingConflictError",
    "BindingEvidenceAssessment",
    "BindingEvidenceReason",
    "BindingStatus",
    "ConfirmationAction",
    "ConversationBinding",
    "ConversationType",
    "HumanBindingConfirmation",
    "IdentityBindingRegistry",
    "IdentityDisposition",
    "IdentityEvidenceSet",
    "IdentityReason",
    "IdentityResolution",
    "assess_binding_evidence",
]
