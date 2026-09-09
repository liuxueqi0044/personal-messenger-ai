from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from messenger_ai.adapters.qq.live_driver.identity import (
    ConfirmationAction,
    ConversationType,
    HumanBindingConfirmation,
    IdentityBindingRegistry,
    IdentityEvidenceSet,
)
from messenger_ai.adapters.qq.live_driver.observation import (
    BoundObservationScope,
    ExactHwndCaptureCapabilities,
    InMemoryObservationDedupStore,
    NormalizedInboundEventCandidate,
    ObservationDisposition,
    ObservationReason,
    ReadonlyMessageObserver,
    VisibleConversationSnapshot,
    VisibleDirection,
    VisibleMessage,
)

NOW = datetime(2026, 9, 8, 9, tzinfo=UTC)
ENV = "a" * 64


def identity(**changes: object) -> IdentityEvidenceSet:
    values = {
        "window_handle": 9001,
        "platform_conversation_id": "qq-conversation-1",
        "conversation_type": ConversationType.DIRECT,
        "structure_path": ("main_window", "conversation_item"),
        "participant_signature": "participant-stable-1",
        "profile_id": "qq-profile-1",
        "avatar_hash": "b" * 64,
        "recent_message_anchor": "anchor-1",
        "environment_fingerprint": ENV,
        "selector_pack_version": "qq-layout-v1",
        "confidence": 0.99,
        "display_name": "同名",
        "list_position": 5,
    }
    values.update(changes)
    return IdentityEvidenceSet(**values)


def message(index: int = 1, **changes: object) -> VisibleMessage:
    values = {
        "message_watermark": f"watermark-{index}",
        "source_evidence_hash": f"{index:064x}",
        "direction": VisibleDirection.INBOUND,
        "text": f"message {index}",
        "observed_at": NOW,
        "observer_confidence": 0.99,
        "direction_confidence": 0.99,
        "time_confidence": 0.99,
    }
    values.update(changes)
    return VisibleMessage(**values)


def snapshot(
    *messages: VisibleMessage, **changes: object
) -> VisibleConversationSnapshot:
    values = {
        "process_id": 42,
        "window_handle": 9001,
        "captured_at": NOW,
        "expires_at": NOW + timedelta(seconds=5),
        "is_minimized": False,
        "identity_evidence": identity(),
        "conversation_confidence": 0.99,
        "messages": messages or (message(),),
    }
    values.update(changes)
    return VisibleConversationSnapshot(**values)


def observer(*, bind: bool = True) -> ReadonlyMessageObserver:
    registry = IdentityBindingRegistry()
    if bind:
        registry.bind(
            local_contact_id="contact-1",
            hub_conversation_id="hub-conversation-1",
            account_id="account-1",
            evidence_set=identity(),
            confirmation=HumanBindingConfirmation(
                action=ConfirmationAction.BIND,
                confirmed_by="local-user",
                confirmed_at=NOW - timedelta(seconds=10),
                reason="confirmed in local workbench",
            ),
        )
    return ReadonlyMessageObserver(
        registry=registry,
        scope=BoundObservationScope(
            account_id="account-1",
            process_id=42,
            window_handle=9001,
            environment_fingerprint=ENV,
            selector_pack_version="qq-layout-v1",
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )


def test_bound_inbound_message_becomes_read_only_candidate_only() -> None:
    result = observer().observe(snapshot(message(1)))
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.read_only is True
    assert candidate.direction == "inbound"
    assert candidate.local_contact_id == "contact-1"
    assert candidate.hub_conversation_id == "hub-conversation-1"
    assert "send" not in NormalizedInboundEventCandidate.model_fields
    assert "reply" not in NormalizedInboundEventCandidate.model_fields


def test_unbound_conversation_stays_pending_and_never_emits_candidate() -> None:
    result = observer(bind=False).observe(snapshot(message(1)))
    assert result.candidates == ()
    assert result.requires_human_attention is True
    assert result.issues[0].reason is ObservationReason.IDENTITY_PENDING


def test_identity_conflict_quarantines_without_candidate() -> None:
    changed = identity(participant_signature="wrong-participant")
    result = observer().observe(snapshot(message(1), identity_evidence=changed))
    assert result.candidates == ()
    assert result.issues[0].disposition is ObservationDisposition.QUARANTINED
    assert result.issues[0].reason is ObservationReason.IDENTITY_QUARANTINED


def test_duplicate_evidence_and_watermark_are_each_suppressed() -> None:
    service = observer()
    assert len(service.observe(snapshot(message(1))).candidates) == 1
    same_evidence = message(2, source_evidence_hash=f"{1:064x}")
    same_watermark = message(3, message_watermark="watermark-1")
    result = service.observe(snapshot(same_evidence, same_watermark))
    assert result.candidates == ()
    assert [issue.reason for issue in result.issues] == [
        ObservationReason.DUPLICATE_EVIDENCE,
        ObservationReason.DUPLICATE_WATERMARK,
    ]


def test_pending_items_are_not_consumed_by_deduplication() -> None:
    service = observer()
    uncertain = message(1, direction=VisibleDirection.UNKNOWN)
    first = service.observe(snapshot(uncertain))
    assert first.issues[0].reason is ObservationReason.DIRECTION_UNCERTAIN
    corrected = message(1, direction=VisibleDirection.INBOUND)
    second = service.observe(snapshot(corrected))
    assert len(second.candidates) == 1


def test_shared_atomic_dedup_store_survives_observer_recreation() -> None:
    registry = IdentityBindingRegistry()
    registry.bind(
        local_contact_id="contact-1",
        hub_conversation_id="hub-conversation-1",
        account_id="account-1",
        evidence_set=identity(),
        confirmation=HumanBindingConfirmation(
            action=ConfirmationAction.BIND,
            confirmed_by="local-user",
            confirmed_at=NOW - timedelta(seconds=10),
            reason="confirmed in local workbench",
        ),
    )
    scope = BoundObservationScope(
        account_id="account-1",
        process_id=42,
        window_handle=9001,
        environment_fingerprint=ENV,
        selector_pack_version="qq-layout-v1",
    )
    store = InMemoryObservationDedupStore()
    first = ReadonlyMessageObserver(
        registry=registry,
        scope=scope,
        clock=lambda: NOW + timedelta(seconds=1),
        dedup_store=store,
    )
    restarted = ReadonlyMessageObserver(
        registry=registry,
        scope=scope,
        clock=lambda: NOW + timedelta(seconds=1),
        dedup_store=store,
    )
    assert len(first.observe(snapshot()).candidates) == 1
    duplicate = restarted.observe(snapshot())
    assert duplicate.candidates == ()
    assert duplicate.issues[0].reason is ObservationReason.DUPLICATE_EVIDENCE


def test_uncertain_direction_time_conversation_and_confidence_stay_pending() -> None:
    service = observer()
    cases = [
        snapshot(message(1, direction=VisibleDirection.UNKNOWN)),
        snapshot(message(2, observed_at=None)),
        snapshot(message(3, observer_confidence=0.4)),
        snapshot(message(4), conversation_confidence=0.4),
    ]
    expected = [
        ObservationReason.DIRECTION_UNCERTAIN,
        ObservationReason.TIME_UNCERTAIN,
        ObservationReason.OBSERVER_CONFIDENCE_LOW,
        ObservationReason.CONVERSATION_UNCERTAIN,
    ]
    for item, reason in zip(cases, expected, strict=True):
        result = service.observe(item)
        assert result.candidates == ()
        assert result.issues[0].reason is reason


def test_known_outbound_is_ignored_not_reinterpreted_as_inbound() -> None:
    result = observer().observe(
        snapshot(message(1, direction=VisibleDirection.OUTBOUND))
    )
    assert result.candidates == ()
    assert result.issues[0].disposition is ObservationDisposition.IGNORED_OUTBOUND


def test_minimized_and_stale_frames_are_rejected() -> None:
    service = observer()
    minimized = service.observe(snapshot(is_minimized=True))
    stale = service.observe(
        snapshot(
            captured_at=NOW - timedelta(minutes=1),
            expires_at=NOW - timedelta(seconds=1),
        )
    )
    assert minimized.issues[0].reason is ObservationReason.WINDOW_MINIMIZED
    assert stale.issues[0].reason is ObservationReason.STALE_FRAME
    assert minimized.candidates == stale.candidates == ()


def test_wrong_hwnd_process_environment_or_selector_is_quarantined() -> None:
    service = observer()
    cases = [
        snapshot(
            window_handle=9002,
            identity_evidence=identity(window_handle=9002),
        ),
        snapshot(process_id=99),
        snapshot(identity_evidence=identity(environment_fingerprint="c" * 64)),
        snapshot(identity_evidence=identity(selector_pack_version="qq-layout-v2")),
    ]
    for item in cases:
        result = service.observe(item)
        assert result.candidates == ()
        assert result.issues[0].reason is ObservationReason.WINDOW_SCOPE_MISMATCH


class ExactFakePort:
    def __init__(self, value: VisibleConversationSnapshot) -> None:
        self.value = value
        self.requested_handles: list[int] = []

    def capture_capabilities(self) -> ExactHwndCaptureCapabilities:
        return ExactHwndCaptureCapabilities()

    async def capture_visible_messages(
        self, window_handle: int
    ) -> VisibleConversationSnapshot:
        self.requested_handles.append(window_handle)
        return self.value


def test_capture_port_is_called_only_with_the_bound_exact_hwnd() -> None:
    service = observer()
    port = ExactFakePort(snapshot())
    result = asyncio.run(service.capture_and_observe(port))
    assert port.requested_handles == [9001]
    assert len(result.candidates) == 1
