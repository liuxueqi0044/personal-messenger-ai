from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from messenger_ai.adapters.wechat.observation import (
    ConfidenceBreakdown,
    MessageCandidate,
    MessageDirection,
    ObservationEvidence,
    VisualObservation,
    WindowBinding,
    WindowEnvironment,
)
from messenger_ai.adapters.wechat.sending import (
    WechatReadOnlyCapabilityProbe,
    normalize_m4_evidence,
)
from messenger_ai.domain import ErrorCode, Platform
from messenger_ai.execution_guard import (
    ActionInterceptor,
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


class Source:
    def __init__(self) -> None:
        self.values = {
            "client_version": "4.1.12.55",
            "windows_version": "test",
            "dpi_scale": 1.25,
            "theme": "light",
            "window_mode": "normal",
            "window_signature": "wechat-window",
            "process_signature": "wechat-process",
            "process_id": 202,
            "window_handle": 2002,
        }

    def read_environment(self, platform):
        return self.values


class Desktop:
    def read_state(self):
        return DesktopState(
            foreground_window=1,
            keyboard_focus=2,
            pointer_position=(3, 4),
            clipboard_revision=5,
            window_state_digest="stable",
        )


class ReadOnlyDriver:
    def __init__(self, *, foreground: bool = False) -> None:
        self.foreground = foreground
        self.calls: list[str] = []

    async def query_process(self, token):
        self.calls.append("process")
        return {"found": True, "version": "4.1.12.55"}

    async def query_window(self, process, token):
        self.calls.append("window")
        return {
            "handle": 2002,
            "readable": True,
            "requires_foreground": self.foreground,
        }

    async def inspect_uia(self, window, token):
        self.calls.append("uia")
        return {"semantic_send_chain_proven": False}

    async def inspect_msaa(self, window, token):
        self.calls.append("msaa")
        return {"semantic_send_chain_proven": False}


def make_probe(foreground: bool = False):
    source = Source()
    fingerprint = EnvironmentFingerprint(platform=Platform.WECHAT, **source.values)
    guard = ExecutionGuard(
        registry=CapabilityRegistry(),
        fingerprinter=EnvironmentFingerprinter(source),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(Desktop()),
        platform_mutex=PlatformMutex(),
        circuit_breaker=CircuitBreaker(),
        emergency_stop=EmergencyStop(),
    )
    driver = ReadOnlyDriver(foreground=foreground)
    probe = WechatReadOnlyCapabilityProbe(
        guard=guard,
        driver=driver,
        capability_version="wechat-send-v1",
        fixture_suite_version="read-only-v1",
    )
    return fingerprint, guard, driver, probe


def test_read_only_probe_never_claims_send_support() -> None:
    async def scenario() -> None:
        fingerprint, guard, driver, probe = make_probe()
        decision = await probe.run()
        assert decision.error_code is ErrorCode.CAPABILITY_UNSUPPORTED
        assert decision.real_send_attempted is False
        assert decision.capabilities.send_background is SupportLevel.UNSUPPORTED
        assert decision.capabilities.verify_background is SupportLevel.UNSUPPORTED
        assert driver.calls == ["process", "window", "uia", "msaa"]
        registered = await guard.capabilities(
            Platform.WECHAT, "wechat-send-v1", fingerprint.digest
        )
        assert registered == decision.capabilities

    asyncio.run(scenario())


def test_probe_reports_foreground_required_without_trying_it() -> None:
    async def scenario() -> None:
        _, _, _, probe = make_probe(foreground=True)
        decision = await probe.run()
        assert decision.error_code is ErrorCode.FOREGROUND_REQUIRED
        assert decision.real_send_attempted is False

    asyncio.run(scenario())


def test_m4_public_evidence_normalizes_structurally() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    environment = WindowEnvironment(
        client_version="4.1.12.55",
        windows_version="test",
        graphics_backend="d3d11",
        dpi_scale=1.25,
        theme="light",
        window_width=1200,
        window_height=900,
    )
    binding = WindowBinding(
        handle=2002,
        process_id=202,
        executable="Weixin.exe",
        identity="bound-contact-1",
        identity_confidence=1,
        environment=environment,
        bound_at=now,
        binding_id="binding-1",
        verified=True,
    )
    evidence = ObservationEvidence(
        evidence_id="evidence-1",
        frame_hash=hashlib.sha256(b"frame").hexdigest(),
        binding_id="binding-1",
        created_at=now,
        expires_at=now + timedelta(seconds=5),
    )
    confidence = ConfidenceBreakdown(
        conversation_identity_confidence=1,
        layout_version_confidence=1,
        message_boundary_confidence=1,
        ocr_text_confidence=1,
        direction_confidence=1,
        temporal_consistency_confidence=1,
    )
    observation = VisualObservation(
        observation_id="observation-1",
        binding_id="binding-1",
        conversation_id="conversation-1",
        frame_hash=evidence.frame_hash,
        observed_at=now,
        messages=(
            MessageCandidate(
                platform_message_key="inbound-1",
                text="redacted at boundary",
                direction=MessageDirection.INBOUND,
                confidence=1,
                boundary_confidence=1,
            ),
        ),
        confidence=confidence,
        evidence=evidence,
    )
    normalized = normalize_m4_evidence(
        observation,
        binding,
        environment_fingerprint="a" * 64,
        conversation_session_id="session-1",
        outbound_sequence=3,
    )
    assert normalized.last_inbound_message_key == "inbound-1"
    assert normalized.window_handle == 2002
    assert normalized.environment_fingerprint == "a" * 64
