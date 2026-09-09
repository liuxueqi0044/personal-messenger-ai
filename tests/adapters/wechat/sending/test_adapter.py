from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from messenger_ai.adapters.wechat.sending import (
    AuthorizedWechatSend,
    CapturedSendEvidence,
    CurrentPolicyVersions,
    OutboundBubble,
    SemanticBackend,
    SendResultStatus,
    SendRoute,
    WechatBackgroundSendAdapter,
    text_digest,
)
from messenger_ai.domain import (
    AuthorizationType,
    AuthorizedSendCommand,
    ErrorCode,
    Platform,
)
from messenger_ai.execution_guard import (
    ActionInterceptor,
    AdapterCapabilities,
    CapabilityRegistry,
    CircuitBreaker,
    DesktopState,
    EmergencyStop,
    EnvironmentFingerprint,
    EnvironmentFingerprinter,
    ExecutionGuard,
    PlatformMutex,
    SnapshotContentionMonitor,
    SupportLevel,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> datetime:
        self.value += timedelta(seconds=seconds)
        return self.value


class FakeEnvironmentSource:
    def __init__(self) -> None:
        self.values = {
            "client_version": "4.1.12.55",
            "windows_version": "test-windows",
            "dpi_scale": 1.25,
            "theme": "light",
            "window_mode": "normal",
            "window_signature": "window-2002",
            "process_signature": "process-202",
            "process_id": 202,
            "window_handle": 2002,
        }

    def read_environment(self, platform: Platform) -> dict:
        assert platform is Platform.WECHAT
        return dict(self.values)


class FakeDesktopReader:
    def __init__(self) -> None:
        self.state = DesktopState(
            foreground_window=777,
            keyboard_focus=778,
            pointer_position=(10, 20),
            clipboard_revision=1,
            window_state_digest="stable",
        )

    def read_state(self) -> DesktopState:
        return self.state.model_copy(deep=True)


class FakeCurrentPolicyVersions:
    def __init__(self) -> None:
        self.value: CurrentPolicyVersions | None = CurrentPolicyVersions(
            rule_version="rules-v1", policy_version="policy-v1"
        )
        self.error: Exception | None = None
        self.calls = 0

    async def current_versions(
        self, conversation_id: str
    ) -> CurrentPolicyVersions | None:
        self.calls += 1
        assert conversation_id == "conversation-1"
        if self.error is not None:
            raise self.error
        return self.value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


UIA_ROUTE = SendRoute(
    backend=SemanticBackend.UIA,
    select_operation="uia.selection_item_pattern.select",
    compose_operation="uia.value_pattern.set",
    composer_read_operation="uia.text_pattern.get",
    commit_operation="uia.invoke_pattern.invoke",
    verify_operation="uia.read",
)


class FakeDriver:
    def __init__(
        self,
        *,
        clock: FakeClock,
        environment_fingerprint: str,
        desktop: FakeDesktopReader,
        route: SendRoute = UIA_ROUTE,
    ) -> None:
        self._route = route
        self.clock = clock
        self.desktop = desktop
        self.selected = True
        self.selected_even_if_wrong = False
        self.composer = ""
        self.fail_commit = False
        self.fail_read = False
        self.change_desktop_on_compose = False
        self.change_desktop_on_commit = False
        self.auto_bubble = True
        self.commit_calls = 0
        self.compose_calls = 0
        self.commit_entered: asyncio.Event | None = None
        self.commit_release: asyncio.Event | None = None
        self.bubbles: list[OutboundBubble] = []
        self.evidence = CapturedSendEvidence(
            conversation_id="conversation-1",
            conversation_session_id="session-1",
            identity_digest=digest("bound-contact-1"),
            last_inbound_message_key="inbound-1",
            frame_hash=digest("frame-initial"),
            frame_captured_at=clock.now(),
            frame_expires_at=clock.now() + timedelta(seconds=60),
            client_version="4.1.12.55",
            environment_fingerprint=environment_fingerprint,
            process_id=202,
            window_handle=2002,
            dpi_scale=1.25,
            outbound_sequence=0,
        )

    @property
    def route(self) -> SendRoute:
        return self._route

    async def capture(self, target, token) -> CapturedSendEvidence:
        token.raise_if_cancelled()
        return self.evidence

    async def select_target(self, target, token) -> bool:
        token.raise_if_cancelled()
        if self.selected_even_if_wrong:
            return True
        return self.selected and (
            target.conversation_id == self.evidence.conversation_id
            and target.conversation_session_id == self.evidence.conversation_session_id
            and target.identity_digest == self.evidence.identity_digest
        )

    async def compose_text(self, target, text: str, token) -> None:
        token.raise_if_cancelled()
        self.compose_calls += 1
        self.composer = text
        if self.change_desktop_on_compose:
            self.desktop.state.pointer_position = (999, 999)
        self.evidence = self.evidence.model_copy(
            update={
                "frame_hash": digest(f"prepared:{text}"),
                "frame_captured_at": self.clock.now(),
                "frame_expires_at": self.clock.now() + timedelta(seconds=60),
            }
        )

    async def read_composer_hash(self, target, token) -> str:
        token.raise_if_cancelled()
        return text_digest(self.composer)

    async def commit(self, target, token) -> str:
        token.raise_if_cancelled()
        self.commit_calls += 1
        if self.commit_entered is not None:
            self.commit_entered.set()
        if self.commit_release is not None:
            await self.commit_release.wait()
        self.clock.advance(0.05)
        if self.fail_commit:
            raise OSError("simulated invoke failure")
        if self.change_desktop_on_commit:
            self.desktop.state.pointer_position = (999, 999)
        next_sequence = self.evidence.outbound_sequence + 1
        if self.auto_bubble:
            self.clock.advance(0.05)
            self.bubbles.append(
                OutboundBubble(
                    conversation_id=self.evidence.conversation_id,
                    conversation_session_id=self.evidence.conversation_session_id,
                    identity_digest=self.evidence.identity_digest,
                    direction="outbound",
                    body_hash=text_digest(self.composer),
                    observed_at=self.clock.now(),
                    sequence=next_sequence,
                )
            )
            self.evidence = self.evidence.model_copy(
                update={
                    "outbound_sequence": next_sequence,
                    "frame_hash": digest(f"sent:{next_sequence}"),
                    "frame_captured_at": self.clock.now(),
                    "frame_expires_at": self.clock.now() + timedelta(seconds=60),
                }
            )
        return f"commit-{self.commit_calls}"

    async def read_outbound_since(self, target, after_sequence: int, token):
        token.raise_if_cancelled()
        if self.fail_read:
            raise OSError("simulated post-commit read failure")
        return tuple(item for item in self.bubbles if item.sequence > after_sequence)


@dataclass
class Harness:
    clock: FakeClock
    environments: FakeEnvironmentSource
    desktop: FakeDesktopReader
    fingerprint: EnvironmentFingerprint
    guard: ExecutionGuard
    driver: FakeDriver
    policy_versions: FakeCurrentPolicyVersions
    adapter: WechatBackgroundSendAdapter

    def request(
        self, text: str = "hello", *, suffix: str = "1"
    ) -> AuthorizedWechatSend:
        command = AuthorizedSendCommand(
            draft_id=uuid4(),
            conversation_id=self.driver.evidence.conversation_id,
            expected_last_message_key=self.driver.evidence.last_inbound_message_key,
            text_hash=text_digest(text),
            idempotency_key=f"idempotency-{suffix}",
            authorization_type=AuthorizationType.HUMAN,
            authorization_id=uuid4(),
            policy_version="policy-v1",
            expires_at=self.clock.now() + timedelta(seconds=30),
        )
        return AuthorizedWechatSend(
            command=command,
            body_text=text,
            rule_version="rules-v1",
            observation=self.driver.evidence,
        )


def make_harness(*, route: SendRoute = UIA_ROUTE) -> Harness:
    clock = FakeClock()
    environments = FakeEnvironmentSource()
    desktop = FakeDesktopReader()
    fingerprint = EnvironmentFingerprint(
        platform=Platform.WECHAT, **environments.values
    )
    registry = CapabilityRegistry()
    registry.register(
        AdapterCapabilities(
            platform=Platform.WECHAT,
            capability_version="wechat-send-v1",
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            resolve_background=SupportLevel.SUPPORTED,
            compose_background=SupportLevel.SUPPORTED,
            send_background=SupportLevel.SUPPORTED,
            verify_background=SupportLevel.SUPPORTED,
            confidence=1,
            fixture_suite_version="synthetic-v1",
        ),
        fingerprint,
    )
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=EnvironmentFingerprinter(environments),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(desktop),
        platform_mutex=PlatformMutex(),
        circuit_breaker=CircuitBreaker(failure_threshold=3),
        emergency_stop=EmergencyStop(),
    )
    driver = FakeDriver(
        clock=clock,
        environment_fingerprint=fingerprint.digest,
        desktop=desktop,
        route=route,
    )
    policy_versions = FakeCurrentPolicyVersions()
    adapter = WechatBackgroundSendAdapter(
        guard=guard,
        driver=driver,
        clock=clock,
        current_policy_versions=policy_versions,
        capability_version="wechat-send-v1",
    )
    return Harness(
        clock,
        environments,
        desktop,
        fingerprint,
        guard,
        driver,
        policy_versions,
        adapter,
    )


async def full_send(harness: Harness, *, suffix: str = "1"):
    prepared_result = await harness.adapter.prepare_send(harness.request(suffix=suffix))
    assert prepared_result.status is SendResultStatus.PREPARED
    commit_result = await harness.adapter.commit_send(prepared_result.prepared)
    assert commit_result.status is SendResultStatus.COMMITTED_PENDING_VERIFY
    return await harness.adapter.verify_send(commit_result.receipt)


def test_one_hundred_synthetic_sends_are_verified_without_duplicates() -> None:
    async def scenario() -> None:
        harness = make_harness()
        for index in range(100):
            result = await full_send(harness, suffix=str(index))
            assert result.status is SendResultStatus.SENT_VERIFIED
        assert harness.driver.commit_calls == 100
        assert len(harness.driver.bubbles) == 100
        assert harness.desktop.state.pointer_position == (10, 20)
        assert harness.desktop.state.clipboard_revision == 1

    asyncio.run(scenario())


def test_wrong_target_is_rejected_before_composition() -> None:
    async def scenario() -> None:
        harness = make_harness()
        request = harness.request()
        harness.driver.selected_even_if_wrong = True
        harness.driver.evidence = harness.driver.evidence.model_copy(
            update={"conversation_id": "conversation-2"}
        )
        result = await harness.adapter.prepare_send(request)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.driver.composer == ""
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


def test_expired_frame_is_rejected_without_driver_action() -> None:
    async def scenario() -> None:
        harness = make_harness()
        request = harness.request()
        harness.clock.advance(61)
        result = await harness.adapter.prepare_send(request)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.driver.composer == ""

    asyncio.run(scenario())


def test_new_message_between_prepare_and_commit_stales_prepared_send() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.driver.evidence = harness.driver.evidence.model_copy(
            update={
                "last_inbound_message_key": "inbound-raced",
                "frame_hash": digest("frame-raced"),
            }
        )
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("rule_version", "policy_version"),
    (("rules-v2", "policy-v1"), ("rules-v1", "policy-v2")),
)
def test_policy_or_rule_version_change_alone_stales_before_driver_commit(
    rule_version: str, policy_version: str
) -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        unchanged_evidence = harness.driver.evidence
        harness.policy_versions.value = CurrentPolicyVersions(
            rule_version=rule_version, policy_version=policy_version
        )
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.policy_versions.calls == 1
        assert harness.driver.commit_calls == 0
        assert harness.driver.evidence == unchanged_evidence

    asyncio.run(scenario())


def test_unavailable_current_policy_versions_stale_before_driver_commit() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.policy_versions.value = None
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.policy_versions.calls == 1
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


def test_current_policy_version_port_error_stales_before_driver_commit() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.policy_versions.error = OSError("policy store unavailable")
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.policy_versions.calls == 1
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value"), (("dpi_scale", 1.5), ("client_version", "4.2.0"))
)
def test_environment_or_client_change_is_rejected(field: str, value) -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.environments.values[field] = value
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


def test_post_commit_read_failure_is_uncertain_and_never_retried() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        receipt = (await harness.adapter.commit_send(prepared)).receipt
        harness.driver.fail_read = True
        result = await harness.adapter.verify_send(receipt)
        assert result.error_code is ErrorCode.SEND_UNCERTAIN
        assert result.automatic_retry_allowed is False
        again = await harness.adapter.verify_send(receipt)
        assert again.error_code is ErrorCode.SEND_UNCERTAIN
        assert harness.driver.commit_calls == 1

    asyncio.run(scenario())


def test_commit_exception_is_uncertain_and_single_attempt() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.driver.fail_commit = True
        first = await harness.adapter.commit_send(prepared)
        second = await harness.adapter.commit_send(prepared)
        assert first.error_code is ErrorCode.SEND_UNCERTAIN
        assert second.error_code is ErrorCode.SEND_UNCERTAIN
        assert harness.driver.commit_calls == 1
        assert not harness.guard.platform_mutex.locked(Platform.WECHAT)

    asyncio.run(scenario())


def test_old_or_wrong_body_bubble_does_not_verify() -> None:
    async def scenario() -> None:
        harness = make_harness()
        harness.driver.auto_bubble = False
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        receipt = (await harness.adapter.commit_send(prepared)).receipt
        harness.driver.bubbles.append(
            OutboundBubble(
                conversation_id=receipt.conversation_id,
                conversation_session_id=receipt.conversation_session_id,
                identity_digest=receipt.identity_digest,
                direction="outbound",
                body_hash=digest("wrong"),
                observed_at=receipt.commit_started_at - timedelta(seconds=1),
                sequence=receipt.baseline_outbound_sequence + 1,
            )
        )
        result = await harness.adapter.verify_send(receipt)
        assert result.error_code is ErrorCode.SEND_UNCERTAIN

    asyncio.run(scenario())


def test_duplicate_matching_bubbles_are_uncertain() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        receipt = (await harness.adapter.commit_send(prepared)).receipt
        original = harness.driver.bubbles[0]
        harness.driver.bubbles.append(original.model_copy(update={"sequence": 2}))
        result = await harness.adapter.verify_send(receipt)
        assert result.error_code is ErrorCode.SEND_UNCERTAIN

    asyncio.run(scenario())


def test_tampered_prepared_send_fails_evidence_seal() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        tampered = prepared.model_copy(update={"window_handle": 9001})
        result = await harness.adapter.commit_send(tampered)
        assert result.error_code is ErrorCode.STALE_CONTEXT
        assert harness.driver.commit_calls == 0

    asyncio.run(scenario())


def test_emergency_stop_prevents_new_prepare() -> None:
    async def scenario() -> None:
        harness = make_harness()
        await harness.guard.emergency_stop("test stop")
        result = await harness.adapter.prepare_send(harness.request())
        assert result.error_code is ErrorCode.FAILED_SAFE
        assert harness.driver.composer == ""

    asyncio.run(scenario())


def test_emergency_stop_during_commit_is_uncertain_and_releases_lock() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.driver.commit_entered = asyncio.Event()
        harness.driver.commit_release = asyncio.Event()
        task = asyncio.create_task(harness.adapter.commit_send(prepared))
        await harness.driver.commit_entered.wait()
        await harness.guard.emergency_stop("stop during commit")
        result = await task
        assert result.error_code is ErrorCode.SEND_UNCERTAIN
        assert harness.driver.commit_calls == 1
        assert not harness.guard.platform_mutex.locked(Platform.WECHAT)

    asyncio.run(scenario())


def test_contention_is_failed_safe_and_platform_lock_is_released() -> None:
    async def scenario() -> None:
        harness = make_harness()
        harness.driver.change_desktop_on_compose = True
        result = await harness.adapter.prepare_send(harness.request())
        assert result.error_code is ErrorCode.FAILED_SAFE
        assert not harness.guard.platform_mutex.locked(Platform.WECHAT)

    asyncio.run(scenario())


def test_commit_contention_is_uncertain_and_never_retried() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        harness.driver.change_desktop_on_commit = True
        result = await harness.adapter.commit_send(prepared)
        assert result.error_code is ErrorCode.SEND_UNCERTAIN
        again = await harness.adapter.commit_send(prepared)
        assert again.error_code is ErrorCode.SEND_UNCERTAIN
        assert harness.driver.commit_calls == 1
        assert not harness.guard.platform_mutex.locked(Platform.WECHAT)

    asyncio.run(scenario())


def test_concurrent_commit_invokes_driver_once() -> None:
    async def scenario() -> None:
        harness = make_harness()
        prepared = (await harness.adapter.prepare_send(harness.request())).prepared
        one, two = await asyncio.gather(
            harness.adapter.commit_send(prepared),
            harness.adapter.commit_send(prepared),
        )
        assert harness.driver.commit_calls == 1
        statuses = {one.status, two.status}
        assert SendResultStatus.COMMITTED_PENDING_VERIFY in statuses
        assert SendResultStatus.REJECTED in statuses

    asyncio.run(scenario())


def test_concurrent_prepare_reserves_idempotency_before_composition() -> None:
    async def scenario() -> None:
        harness = make_harness()
        request = harness.request()
        one, two = await asyncio.gather(
            harness.adapter.prepare_send(request),
            harness.adapter.prepare_send(request),
        )
        assert harness.driver.compose_calls == 1
        statuses = {one.status, two.status}
        assert statuses == {SendResultStatus.PREPARED, SendResultStatus.REJECTED}

    asyncio.run(scenario())


def test_foreground_route_returns_foreground_required() -> None:
    async def scenario() -> None:
        route = UIA_ROUTE.model_copy(update={"requires_foreground": True})
        harness = make_harness(route=route)
        result = await harness.adapter.prepare_send(harness.request())
        assert result.error_code is ErrorCode.FOREGROUND_REQUIRED
        assert harness.driver.composer == ""

    asyncio.run(scenario())


def test_forbidden_route_is_rejected_at_construction() -> None:
    route = UIA_ROUTE.model_copy(update={"select_operation": "pyautogui.click"})
    with pytest.raises(ValueError, match="forbidden"):
        make_harness(route=route)


def test_authorized_body_hash_is_mandatory() -> None:
    harness = make_harness()
    request = harness.request()
    with pytest.raises(ValueError, match="authorized text hash"):
        AuthorizedWechatSend(
            command=request.command,
            body_text="changed",
            rule_version=request.rule_version,
            observation=request.observation,
        )
