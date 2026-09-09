from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from messenger_ai.adapters.qq import (
    BubbleDirection,
    QQAdapter,
    QQBubble,
    QQConversation,
    QQIdentityBinding,
    QQSelector,
    QQSelectorPack,
    QQWindow,
)
from messenger_ai.domain import (
    AuthorizationType,
    AuthorizedSendCommand,
    ErrorCode,
    Platform,
    SendStatus,
)
from messenger_ai.execution_guard import (
    ActionInterceptor,
    AdapterCapabilities,
    CapabilityRegistry,
    DesktopState,
    EnvironmentFingerprint,
    EnvironmentFingerprinter,
    ExecutionGuard,
    SnapshotContentionMonitor,
    SupportLevel,
)


class FakeEnvironment:
    def __init__(self) -> None:
        self.values = {
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


class FakeDesktop:
    def read_state(self) -> DesktopState:
        return DesktopState(
            foreground_window=7,
            keyboard_focus=8,
            pointer_position=(1, 2),
            clipboard_revision=3,
            window_state_digest="unchanged",
        )


class FakeQQAccessibility:
    def __init__(self) -> None:
        self.window = QQWindow(process_id=101, window_handle=1001, class_name="QQNT")
        self.digest = "tree-v1"
        self.conversations = [
            QQConversation(
                internal_id="qq-conv-1",
                display_name="Alex",
                participant_signature="contact-proof-1",
                last_message_key="incoming-1",
                tree_digest=self.digest,
            )
        ]
        self.composer = ""
        self.bubbles: list[QQBubble] = []
        self.calls: list[str] = []
        self.emit_receipt = True

    def find_main_windows(self, selector):
        self.calls.append("find")
        return [self.window]

    def tree_digest(self, window):
        self.calls.append("tree")
        return self.digest

    def list_conversations(self, window, selector):
        self.calls.append("conversations")
        return [
            item.model_copy(update={"tree_digest": self.digest})
            for item in self.conversations
        ]

    def select_conversation(self, window, conversation, selector):
        self.calls.append("select")

    def write_composer(self, window, text, selector):
        self.calls.append("write-composer")
        self.composer = text

    def read_composer(self, window, selector):
        self.calls.append("read-composer")
        return self.composer

    def invoke_send(self, window, selector):
        self.calls.append("invoke-send")
        if self.emit_receipt:
            self.bubbles.append(
                QQBubble(
                    conversation_internal_id="qq-conv-1",
                    message_key=f"out-{len(self.bubbles)}",
                    direction=BubbleDirection.OUTBOUND,
                    text=self.composer,
                    observed_at=datetime.now(UTC) + timedelta(seconds=1),
                    tree_digest=self.digest,
                )
            )

    def list_bubbles(self, window, selector):
        self.calls.append("bubbles")
        return list(self.bubbles)


def _adapter(
    *, accessibility: FakeQQAccessibility | None = None
) -> tuple[QQAdapter, FakeQQAccessibility]:
    environment = FakeEnvironment()
    fingerprinter = EnvironmentFingerprinter(environment)
    fingerprint = EnvironmentFingerprint(platform=Platform.QQ, **environment.values)
    registry = CapabilityRegistry()
    registry.register(
        AdapterCapabilities(
            platform=Platform.QQ,
            capability_version="qq-cap-v1",
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            resolve_background=SupportLevel.SUPPORTED,
            compose_background=SupportLevel.SUPPORTED,
            send_background=SupportLevel.SUPPORTED,
            verify_background=SupportLevel.SUPPORTED,
            confidence=1,
            fixture_suite_version="qq-fake-v1",
        ),
        fingerprint,
    )
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=fingerprinter,
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(FakeDesktop()),
    )
    pack = QQSelectorPack(
        client_version=fingerprint.client_version,
        environment_fingerprint=fingerprint.digest,
        selectors=tuple(
            QQSelector(name=name, automation_id=f"id-{name}", control_type="Pane")
            for name in (
                "main_window",
                "conversations",
                "conversation_item",
                "composer",
                "send",
                "bubbles",
            )
        ),
        last_verified_at=datetime.now(UTC),
        fixture_suite_version="qq-fake-v1",
    )
    fake = accessibility or FakeQQAccessibility()
    return (
        QQAdapter(
            accessibility=fake,
            guard=guard,
            selector_pack=pack,
            environment_fingerprint=fingerprint.digest,
            capability_version="qq-cap-v1",
            bindings=(
                QQIdentityBinding(
                    hub_conversation_id="hub-conv-1",
                    contact_id="contact-1",
                    account_id="account-1",
                    platform_conversation_id="qq-conv-1",
                    participant_signature="contact-proof-1",
                    binding_id="approved-binding-1",
                ),
            ),
            text_provider=lambda _command: "你好，收到。",
        ),
        fake,
    )


def _command() -> AuthorizedSendCommand:
    text = "你好，收到。"
    return AuthorizedSendCommand(
        draft_id=uuid4(),
        conversation_id="hub-conv-1",
        expected_last_message_key="incoming-1",
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
        idempotency_key="qq-send-1",
        authorization_type=AuthorizationType.HUMAN,
        authorization_id=uuid4(),
        policy_version="v1",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


async def test_prepare_commit_verify_uses_only_guarded_background_patterns() -> None:
    adapter, fake = _adapter()
    operation = await adapter.prepare_send(_command())
    assert operation.status is SendStatus.PREPARED
    operation = await adapter.commit_send(operation)
    assert operation.status is SendStatus.COMMITTED
    operation = await adapter.verify_send(operation)
    assert operation.status is SendStatus.VERIFIED
    assert {"select", "write-composer", "read-composer", "invoke-send"} <= set(
        fake.calls
    )


async def test_same_idempotency_key_never_retries_an_uncertain_commit() -> None:
    adapter, fake = _adapter()
    fake.emit_receipt = False
    command = _command()
    operation = await adapter.prepare_send(command)
    operation = await adapter.commit_send(operation)
    assert operation.status is SendStatus.COMMITTED
    operation = await adapter.verify_send(operation)
    assert operation.status is SendStatus.UNCERTAIN
    again = await adapter.prepare_send(command)
    assert again is operation
    assert fake.calls.count("invoke-send") == 1


async def test_ambiguous_identity_fails_closed_before_composer_write() -> None:
    adapter, fake = _adapter()
    fake.conversations.append(fake.conversations[0].model_copy(deep=True))
    operation = await adapter.prepare_send(_command())
    assert operation.status is SendStatus.FAILED
    assert operation.error_code == ErrorCode.IDENTITY_AMBIGUOUS.value
    assert "write-composer" not in fake.calls


async def test_expired_authorization_and_stale_tree_never_invoke_send() -> None:
    adapter, fake = _adapter()
    expired = _command().model_copy(update={"expires_at": datetime.now(UTC)})
    assert (
        await adapter.prepare_send(expired)
    ).error_code == ErrorCode.STALE_CONTEXT.value

    adapter, fake = _adapter()
    operation = await adapter.prepare_send(_command())
    fake.digest = "tree-v2"
    operation = await adapter.commit_send(operation)
    assert operation.status is SendStatus.FAILED
    assert operation.error_code == ErrorCode.ADAPTER_QUARANTINED.value
    assert "invoke-send" not in fake.calls


async def test_inbound_normalization_deduplicates_and_never_promotes_outbound() -> None:
    adapter, fake = _adapter()
    message = QQBubble(
        conversation_internal_id="qq-conv-1",
        message_key="in-1",
        direction=BubbleDirection.INBOUND,
        text="在吗？",
        observed_at=datetime.now(UTC),
        tree_digest=fake.digest,
    )
    fake.bubbles = [message, message.model_copy(deep=True)]
    assert len(await adapter.poll_events()) == 1
    assert await adapter.poll_events() == []


async def test_platform_contention_is_reported_by_guard_and_prevents_completion() -> (
    None
):
    adapter, _fake = _adapter()
    first, second = await asyncio.gather(
        adapter.prepare_send(_command()), adapter.prepare_send(_command())
    )
    assert first.status is SendStatus.PREPARED
    assert second is first
