from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from messenger_ai.adapters.qq import (
    BubbleDirection,
    QQAdapter,
    QQBubble,
    QQIdentityBinding,
    QQSelector,
    QQSelectorPack,
    QQWindow,
)
from messenger_ai.adapters.wechat.observation import (
    ConfidenceBreakdown,
    HumanEvidenceQueue,
    LayoutClassification,
    MessageCandidate,
    MessageDirection,
    ObservationEvidence,
    SyntheticCaptureProvider,
    VisualContentType,
    VisualObservation,
    WeChatEventNormalizer,
    WeChatObservationAdapter,
    WindowBinding,
    WindowEnvironment,
)
from messenger_ai.domain import EventEnvelope, InboundMessage, Platform
from messenger_ai.execution_guard import (
    ActionInterceptor,
    ActionPhase,
    AdapterCapabilities,
    CapabilityRegistry,
    DesktopState,
    EnvironmentFingerprint,
    EnvironmentFingerprinter,
    ExecutionGuard,
    GuardedAction,
    GuardedActionType,
    SnapshotContentionMonitor,
    SupportLevel,
)
from messenger_ai.hub import HubService, SQLiteHubStore
from messenger_ai.memory import (
    Contact,
    HumanApproval,
    IdentityBinding,
    MemoryService,
    SQLiteMemoryStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 2, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class Environment:
    def __init__(self) -> None:
        self.values: Mapping[str, object] = {
            "client_version": "9.9.26.44343",
            "windows_version": "test-windows",
            "dpi_scale": 1.25,
            "theme": "light",
            "window_mode": "normal",
            "window_signature": "qq-window",
            "process_signature": "qq-process",
            "process_id": 101,
            "window_handle": 1001,
        }

    def read_environment(self, platform: Platform) -> dict:
        assert platform is Platform.QQ
        return dict(self.values)


class StableDesktop:
    def read_state(self) -> DesktopState:
        return DesktopState(
            foreground_window=1,
            keyboard_focus=2,
            pointer_position=(3, 4),
            clipboard_revision=1,
            window_state_digest="stable",
        )


class FakeQQTree:
    def __init__(self, now: datetime) -> None:
        self.window = QQWindow(process_id=101, window_handle=1001, class_name="QQNT")
        self.digest = "qq-tree-v1"
        self.bubbles = [
            QQBubble(
                conversation_internal_id="qq-conversation-a",
                message_key="qq-message-1",
                direction=BubbleDirection.INBOUND,
                text="来自 QQ 的消息",
                observed_at=now,
                tree_digest=self.digest,
            )
        ]

    def find_main_windows(self, selector):
        return [self.window]

    def tree_digest(self, window):
        return self.digest

    def list_bubbles(self, window, selector):
        return self.bubbles


class UnsupportedLayout:
    def classify(self, frame, environment):
        return LayoutClassification(confidence=0.1, supported=False, reason="unknown")


def approval(clock: Clock) -> HumanApproval:
    return HumanApproval(
        verified_by="owner", verified_at=clock.now(), reason="reviewed"
    )


def envelope(message: InboundMessage, event_id=None) -> EventEnvelope[InboundMessage]:
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type="message.observed",
        occurred_at=message.observed_at,
        observed_at=message.observed_at,
        aggregate_type="conversation",
        aggregate_id=message.conversation_id,
        payload=message,
        producer="integration.fake",
    )


def bridge(
    hub: HubService, memory: MemoryService, event: EventEnvelope[InboundMessage]
):
    result = hub.ingest(event.payload, envelope=event)
    consumed = memory.consume_inbound(event) if result.accepted else False
    return result, consumed


def build_qq_adapter(clock: Clock, contact_id: str) -> QQAdapter:
    environment = Environment()
    fingerprint = EnvironmentFingerprint(platform=Platform.QQ, **environment.values)
    registry = CapabilityRegistry()
    registry.register(
        AdapterCapabilities(
            platform=Platform.QQ,
            capability_version="qq-integration-v1",
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            confidence=1,
            fixture_suite_version="integration-v1",
        ),
        fingerprint,
    )
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=EnvironmentFingerprinter(environment),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(StableDesktop()),
    )
    selectors = tuple(
        QQSelector(name=name, control_type="Pane", automation_id=f"{name}-id")
        for name in ("main_window", "bubbles")
    )
    return QQAdapter(
        accessibility=FakeQQTree(clock.now()),
        guard=guard,
        selector_pack=QQSelectorPack(
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            selectors=selectors,
            last_verified_at=clock.now(),
            fixture_suite_version="integration-v1",
        ),
        environment_fingerprint=fingerprint.digest,
        capability_version="qq-integration-v1",
        bindings=(
            QQIdentityBinding(
                hub_conversation_id="qq-conversation-a",
                contact_id=contact_id,
                account_id="qq-account",
                platform_conversation_id="qq-conversation-a",
                participant_signature="approved-qq-proof",
                binding_id="qq-binding",
            ),
        ),
    )


def register_contact(
    memory: MemoryService,
    clock: Clock,
    *,
    contact_id: str,
    platform: Platform,
    account_id: str,
    conversation_id: str,
) -> None:
    memory.create_contact(
        Contact(contact_id=contact_id, display_name="Alex", created_at=clock.now())
    )
    memory.bind_identity(
        IdentityBinding(
            contact_id=contact_id,
            platform=platform,
            account_id=account_id,
            conversation_id=conversation_id,
            platform_evidence_hash=hashlib.sha256(
                f"{platform.value}:{conversation_id}".encode()
            ).hexdigest(),
            approval=approval(clock),
        )
    )


def test_qq_and_wechat_normalized_events_cross_hub_then_memory_idempotently(
    tmp_path,
) -> None:
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    memory_store = SQLiteMemoryStore(tmp_path / "memory.sqlite")
    hub, memory = (
        HubService(hub_store, clock=clock),
        MemoryService(memory_store, clock=clock),
    )
    try:
        register_contact(
            memory,
            clock,
            contact_id="contact-a",
            platform=Platform.QQ,
            account_id="qq-account",
            conversation_id="qq-conversation-a",
        )
        register_contact(
            memory,
            clock,
            contact_id="contact-b",
            platform=Platform.WECHAT,
            account_id="wechat-account",
            conversation_id="wechat-conversation-b",
        )
        qq_message = asyncio.run(build_qq_adapter(clock, "contact-a").poll_events())[0]
        qq_event = envelope(qq_message)
        accepted, consumed = bridge(hub, memory, qq_event)
        assert accepted.accepted and consumed
        duplicate, duplicate_consumed = bridge(hub, memory, qq_event)
        assert duplicate.duplicate and not duplicate_consumed

        now = clock.now()
        observation = VisualObservation(
            observation_id="wechat-accepted",
            binding_id="wechat-binding",
            conversation_id="wechat-conversation-b",
            frame_hash="f" * 64,
            observed_at=now,
            messages=(
                MessageCandidate(
                    platform_message_key="wechat-message-1",
                    text="来自微信的消息",
                    direction=MessageDirection.INBOUND,
                    content_type=VisualContentType.TEXT,
                    confidence=0.99,
                    boundary_confidence=0.99,
                ),
            ),
            confidence=ConfidenceBreakdown(
                conversation_identity_confidence=0.99,
                layout_version_confidence=0.99,
                message_boundary_confidence=0.99,
                ocr_text_confidence=0.99,
                direction_confidence=0.99,
                temporal_consistency_confidence=1,
            ),
            evidence=ObservationEvidence(
                evidence_id="wechat-evidence",
                frame_hash="f" * 64,
                binding_id="wechat-binding",
                created_at=now,
                expires_at=now + timedelta(seconds=30),
            ),
        )
        wechat_message = WeChatEventNormalizer().normalize_messages(
            observation, account_id="wechat-account", contact_id="contact-b", now=now
        )[0]
        assert bridge(hub, memory, envelope(wechat_message))[0].accepted
        first = memory.context("contact-a", "qq-conversation-a", budget_chars=5000)
        second = memory.context("contact-b", "wechat-conversation-b", budget_chars=5000)
        assert [item.text for item in first.recent_messages] == ["来自 QQ 的消息"]
        assert [item.text for item in second.recent_messages] == ["来自微信的消息"]
    finally:
        hub_store.close()
        memory_store.close()


def test_low_confidence_wechat_is_queued_for_human_evidence_not_ingested(
    tmp_path,
) -> None:
    clock = Clock()
    queue = HumanEvidenceQueue()
    environment = WindowEnvironment(
        client_version="4.1.12.55",
        windows_version="test",
        graphics_backend="test",
        dpi_scale=1,
        theme="light",
        window_width=400,
        window_height=300,
    )
    binding = WindowBinding(
        handle=8,
        process_id=9,
        executable="Weixin.exe",
        identity="wechat-conversation",
        identity_confidence=0.1,
        environment=environment,
        bound_at=clock.now(),
        binding_id="low-confidence-binding",
        verified=True,
    )
    adapter = WeChatObservationAdapter(
        SyntheticCaptureProvider(),
        layout_classifier=UnsupportedLayout(),
        evidence_queue=queue,
    )
    observation = adapter.observe(binding, now=clock.now())
    assert observation is not None and observation.human_review_required
    assert queue.pending(clock.now()) == (observation,)
    assert (
        WeChatEventNormalizer().normalize_messages(observation, now=clock.now()) == ()
    )

    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    memory_store = SQLiteMemoryStore(tmp_path / "memory.sqlite")
    try:
        assert (
            hub_store.connection.execute("SELECT COUNT(1) FROM messages").fetchone()[0]
            == 0
        )
        assert (
            memory_store.connection.execute(
                "SELECT COUNT(1) FROM memory_messages"
            ).fetchone()[0]
            == 0
        )
    finally:
        hub_store.close()
        memory_store.close()


def test_m2_rejected_fallback_has_no_hub_or_memory_side_effect(tmp_path) -> None:
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    memory_store = SQLiteMemoryStore(tmp_path / "memory.sqlite")
    environment = Environment()
    fingerprint = EnvironmentFingerprint(platform=Platform.QQ, **environment.values)
    registry = CapabilityRegistry()
    registry.register(
        AdapterCapabilities(
            platform=Platform.QQ,
            capability_version="guard-v1",
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            confidence=1,
            fixture_suite_version="integration-v1",
        ),
        fingerprint,
    )
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=EnvironmentFingerprinter(environment),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(StableDesktop()),
    )
    request = GuardedAction(
        platform=Platform.QQ,
        action_type=GuardedActionType.OBSERVE,
        phase=ActionPhase.READ,
        requested_operations=("uia.read",),
        fallback_operations=("user32.SendInput",),
        capability_version="guard-v1",
        environment_fingerprint=fingerprint.digest,
        target_process_id=fingerprint.process_id,
        target_window_handle=fingerprint.window_handle,
    )
    invoked = False

    async def action(token):
        nonlocal invoked
        invoked = True

    try:
        result = asyncio.run(guard.run(request, action))
        assert not result.succeeded and not invoked
        assert (
            hub_store.connection.execute("SELECT COUNT(1) FROM outbox").fetchone()[0]
            == 0
        )
        assert (
            memory_store.connection.execute(
                "SELECT COUNT(1) FROM memory_messages"
            ).fetchone()[0]
            == 0
        )
    finally:
        hub_store.close()
        memory_store.close()
