from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.live_driver.identity import (
    BindingAuditAction,
    BindingConflictError,
    BindingEvidenceReason,
    BindingStatus,
    ConfirmationAction,
    ConversationType,
    HumanBindingConfirmation,
    IdentityBindingRegistry,
    IdentityDisposition,
    IdentityEvidenceSet,
    IdentityReason,
    assess_binding_evidence,
)

NOW = datetime(2026, 9, 8, 8, tzinfo=UTC)
ENV = "a" * 64
AVATAR = "b" * 64


def evidence(**changes: object) -> IdentityEvidenceSet:
    values = {
        "window_handle": 9001,
        "platform_conversation_id": "qq-conversation-1",
        "conversation_type": ConversationType.DIRECT,
        "structure_path": ("main_window", "conversation_item"),
        "participant_signature": "participant-stable-1",
        "profile_id": "qq-profile-10001",
        "avatar_hash": AVATAR,
        "recent_message_anchor": "last-visible-message-8",
        "environment_fingerprint": ENV,
        "selector_pack_version": "qq-layout-v1",
        "confidence": 0.99,
        "display_name": "小雨",
        "list_position": 2,
    }
    values.update(changes)
    return IdentityEvidenceSet(**values)


def confirmation(
    action: ConfirmationAction, *, at: datetime = NOW
) -> HumanBindingConfirmation:
    return HumanBindingConfirmation(
        action=action,
        confirmed_by="local-user",
        confirmed_at=at,
        reason=f"explicit {action.value} in local workbench",
    )


def bind(registry: IdentityBindingRegistry, *, ev: IdentityEvidenceSet | None = None):
    return registry.bind(
        local_contact_id="contact-1",
        hub_conversation_id="hub-conversation-1",
        account_id="account-1",
        evidence_set=ev or evidence(),
        confirmation=confirmation(ConfirmationAction.BIND),
    )


def test_first_binding_requires_explicit_human_confirmation_action() -> None:
    registry = IdentityBindingRegistry()
    with pytest.raises(ValueError, match="must be bind"):
        registry.bind(
            local_contact_id="contact-1",
            hub_conversation_id="hub-1",
            account_id="account-1",
            evidence_set=evidence(),
            confirmation=confirmation(ConfirmationAction.REBIND),
        )
    with pytest.raises(ValidationError):
        HumanBindingConfirmation(
            action=ConfirmationAction.BIND,
            confirmed_by="local-user",
            confirmed_at=NOW,
            reason="clicked bind",
            approved=False,
        )


def test_unknown_or_weak_identity_cannot_be_bound() -> None:
    registry = IdentityBindingRegistry()
    with pytest.raises(BindingConflictError):
        bind(registry, ev=evidence(conversation_type=ConversationType.UNKNOWN))
    with pytest.raises(BindingConflictError):
        bind(registry, ev=evidence(confidence=0.4))


def test_display_name_and_list_position_are_not_identity_signals() -> None:
    registry = IdentityBindingRegistry()
    binding = bind(registry)
    renamed = evidence(display_name="新昵称", list_position=100)
    result = registry.resolve(account_id="account-1", evidence_set=renamed)
    assert result.disposition is IdentityDisposition.MATCHED
    assert result.binding_id == binding.binding_id
    assert result.automatic_eligible is True


def test_same_name_contacts_do_not_cross_bind() -> None:
    registry = IdentityBindingRegistry()
    first = bind(registry)
    second_evidence = evidence(
        platform_conversation_id="qq-conversation-2",
        participant_signature="participant-stable-2",
        profile_id="qq-profile-10002",
        display_name="小雨",
        list_position=0,
    )
    second = registry.bind(
        local_contact_id="contact-2",
        hub_conversation_id="hub-conversation-2",
        account_id="account-1",
        evidence_set=second_evidence,
        confirmation=confirmation(ConfirmationAction.BIND),
    )
    assert first.binding_id != second.binding_id
    assert (
        registry.resolve(
            account_id="account-1", evidence_set=second_evidence
        ).local_contact_id
        == "contact-2"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("participant_signature", "participant-attacker"),
        ("structure_path", ("main_window", "unexpected_item")),
        ("environment_fingerprint", "c" * 64),
        ("selector_pack_version", "qq-layout-v2"),
        ("avatar_hash", "d" * 64),
        ("profile_id", "qq-profile-other"),
    ],
)
def test_critical_conflict_quarantines_and_revokes_automatic_eligibility(
    field: str, value: object
) -> None:
    registry = IdentityBindingRegistry()
    binding = bind(registry)
    result = registry.resolve(
        account_id="account-1", evidence_set=evidence(**{field: value})
    )
    assert result.disposition is IdentityDisposition.QUARANTINED
    assert result.reason is IdentityReason.CRITICAL_EVIDENCE_CONFLICT
    assert field in result.conflicting_fields
    assert result.automatic_eligible is False
    assert registry.get(binding.binding_id).status is BindingStatus.QUARANTINED
    assert (
        registry.audit_entries()[-1].action is BindingAuditAction.CONFLICT_QUARANTINED
    )


def test_missing_stable_second_signal_stays_pending() -> None:
    registry = IdentityBindingRegistry()
    binding = bind(registry)
    result = registry.resolve(
        account_id="account-1",
        evidence_set=evidence(profile_id=None, avatar_hash=None),
    )
    assert result.disposition is IdentityDisposition.PENDING_HUMAN_BINDING
    assert result.reason is IdentityReason.STABLE_SECOND_SIGNAL_REQUIRED
    assert result.binding_id == binding.binding_id


def test_header_and_recent_anchor_alone_cannot_create_active_binding() -> None:
    weak = evidence(profile_id=None, avatar_hash=None)
    assessment = assess_binding_evidence(weak)

    assert assessment.bindable is False
    assert assessment.automatic_eligible is False
    assert assessment.reasons == (BindingEvidenceReason.STABLE_SECOND_SIGNAL_REQUIRED,)
    with pytest.raises(BindingConflictError, match="not strong enough"):
        bind(IdentityBindingRegistry(), ev=weak)


def test_unbound_and_low_confidence_resolution_stay_pending() -> None:
    registry = IdentityBindingRegistry()
    unbound = registry.resolve(account_id="account-1", evidence_set=evidence())
    assert unbound.disposition is IdentityDisposition.PENDING_HUMAN_BINDING
    bind(registry)
    weak = registry.resolve(
        account_id="account-1", evidence_set=evidence(confidence=0.5)
    )
    assert weak.disposition is IdentityDisposition.PENDING_HUMAN_BINDING
    assert weak.automatic_eligible is False


def test_rebind_and_rollback_create_versions_and_append_audit() -> None:
    registry = IdentityBindingRegistry()
    first = bind(registry)
    changed = evidence(
        participant_signature="participant-stable-v2",
        selector_pack_version="qq-layout-v2",
    )
    second = registry.rebind(
        first.binding_id,
        evidence_set=changed,
        confirmation=confirmation(
            ConfirmationAction.REBIND, at=NOW + timedelta(minutes=1)
        ),
    )
    restored = registry.rollback(
        second.binding_id,
        confirmation=confirmation(
            ConfirmationAction.ROLLBACK, at=NOW + timedelta(minutes=2)
        ),
    )
    assert [
        item.ui_identity_version
        for item in registry.history(
            account_id="account-1", local_contact_id="contact-1"
        )
    ] == [1, 2, 3]
    assert registry.get(first.binding_id).status is BindingStatus.REVOKED
    assert registry.get(second.binding_id).status is BindingStatus.REVOKED
    assert restored.evidence_set.participant_signature == "participant-stable-1"
    assert [item.action for item in registry.audit_entries()] == [
        BindingAuditAction.BOUND,
        BindingAuditAction.REBOUND,
        BindingAuditAction.ROLLED_BACK,
    ]


def test_revoke_is_human_confirmed_and_audited() -> None:
    registry = IdentityBindingRegistry()
    binding = bind(registry)
    revoked = registry.revoke(
        binding.binding_id,
        confirmation=confirmation(ConfirmationAction.REVOKE),
    )
    assert revoked.status is BindingStatus.REVOKED
    assert revoked.automatic_eligible is False
    assert (
        registry.resolve(account_id="account-1", evidence_set=evidence()).reason
        is IdentityReason.UNBOUND
    )
    assert registry.audit_entries()[-1].action is BindingAuditAction.REVOKED


def test_revocation_can_be_rolled_back_only_with_a_new_human_confirmation() -> None:
    registry = IdentityBindingRegistry()
    binding = bind(registry)
    revoked = registry.revoke(
        binding.binding_id,
        confirmation=confirmation(ConfirmationAction.REVOKE),
    )
    restored = registry.rollback(
        revoked.binding_id,
        confirmation=confirmation(
            ConfirmationAction.ROLLBACK,
            at=NOW + timedelta(minutes=1),
        ),
    )
    assert restored.ui_identity_version == 2
    assert restored.status is BindingStatus.ACTIVE
    assert (
        registry.resolve(account_id="account-1", evidence_set=evidence()).binding_id
        == restored.binding_id
    )
    assert registry.audit_entries()[-1].action is BindingAuditAction.ROLLED_BACK


def test_cross_binding_existing_platform_or_contact_is_rejected() -> None:
    registry = IdentityBindingRegistry()
    bind(registry)
    with pytest.raises(BindingConflictError):
        registry.bind(
            local_contact_id="contact-other",
            hub_conversation_id="hub-other",
            account_id="account-1",
            evidence_set=evidence(),
            confirmation=confirmation(ConfirmationAction.BIND),
        )
    with pytest.raises(BindingConflictError):
        registry.bind(
            local_contact_id="contact-1",
            hub_conversation_id="hub-other",
            account_id="account-1",
            evidence_set=evidence(
                platform_conversation_id="qq-conversation-other",
                participant_signature="participant-other",
            ),
            confirmation=confirmation(ConfirmationAction.BIND),
        )


def test_structure_path_forbids_list_position_as_identity() -> None:
    with pytest.raises(ValidationError, match="list positions"):
        evidence(structure_path=("conversation_list", "3"))
