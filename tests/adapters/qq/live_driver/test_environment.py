from __future__ import annotations

import asyncio
from pathlib import Path

from messenger_ai.adapters.qq.live_driver.environment import (
    CertifiedQQProfile,
    EnvironmentFingerprint,
    ModalState,
    QQEnvironmentSentinel,
    RuntimeDecisionReason,
    RuntimeSnapshot,
    WindowBounds,
    WindowPresentation,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def fingerprint(**changes: object) -> EnvironmentFingerprint:
    value = {
        "executable_path": "C:/Program Files/Tencent/QQ/QQ.exe",
        "executable_signature": HASH_A,
        "process_signature": HASH_B,
        "client_version": "9.9.26.44343",
        "windows_version": "10.0.26100",
        "dpi_scale": 1.0,
        "monitor_id": "monitor-primary",
        "monitor_topology_digest": HASH_C,
        "theme": "dark",
        "window_class": "TXGuiFoundation",
        "presentation": WindowPresentation.MAXIMIZED,
    }
    value.update(changes)
    return EnvironmentFingerprint(**value)


def profile() -> CertifiedQQProfile:
    expected = fingerprint()
    return CertifiedQQProfile(
        profile_id="qq-certified-v1",
        allowed_executable_paths=(expected.executable_path,),
        allowed_executable_signatures=(HASH_A,),
        allowed_process_signatures=(HASH_B,),
        allowed_window_classes=(expected.window_class,),
        certified_fingerprint=expected,
    )


def snapshot(**changes: object) -> RuntimeSnapshot:
    value = fingerprint().model_dump()
    value.update(
        {
            "process_id": 42,
            "window_handle": 9001,
            "bounds": WindowBounds(left=0, top=0, right=1920, bottom=1080),
            "is_foreground": False,
            "is_occluded": True,
            "is_logged_in": True,
            "modal_state": ModalState.NONE,
        }
    )
    value.update(changes)
    return RuntimeSnapshot(**value)


def test_maximized_occluded_qq_is_observable_and_committable_when_certified() -> None:
    envelope = QQEnvironmentSentinel(StaticPort(snapshot()), profile()).assess(
        snapshot()
    )
    assert envelope.snapshot.is_occluded is True
    assert envelope.decision.observe_allowed is True
    assert envelope.decision.commit_allowed is True
    assert envelope.decision.reasons == (RuntimeDecisionReason.READY,)


def test_foreground_qq_remains_observable_but_commit_is_forbidden() -> None:
    envelope = QQEnvironmentSentinel(
        StaticPort(snapshot(is_foreground=True)), profile()
    ).assess(snapshot(is_foreground=True))
    assert envelope.decision.observe_allowed is True
    assert envelope.decision.commit_allowed is False
    assert envelope.decision.reasons == (RuntimeDecisionReason.QQ_FOREGROUND,)


def test_minimized_normal_and_unknown_states_fail_closed() -> None:
    sentinel = QQEnvironmentSentinel(StaticPort(snapshot()), profile())
    expected = {
        WindowPresentation.MINIMIZED: RuntimeDecisionReason.WINDOW_MINIMIZED,
        WindowPresentation.NORMAL: RuntimeDecisionReason.WINDOW_NOT_MAXIMIZED,
        WindowPresentation.UNKNOWN: RuntimeDecisionReason.WINDOW_STATE_UNKNOWN,
    }
    for state, reason in expected.items():
        result = sentinel.assess(snapshot(presentation=state)).decision
        assert result.observe_allowed is False and result.commit_allowed is False
        assert reason in result.reasons


def test_version_dpi_monitor_theme_topology_and_class_drift_fail_closed() -> None:
    sentinel = QQEnvironmentSentinel(StaticPort(snapshot()), profile())
    changes = {
        "client_version": ("9.9.27", RuntimeDecisionReason.CLIENT_VERSION_DRIFT),
        "dpi_scale": (1.25, RuntimeDecisionReason.DPI_DRIFT),
        "monitor_id": ("monitor-secondary", RuntimeDecisionReason.MONITOR_DRIFT),
        "theme": ("light", RuntimeDecisionReason.THEME_DRIFT),
        "monitor_topology_digest": ("d" * 64, RuntimeDecisionReason.TOPOLOGY_DRIFT),
        "window_class": ("UnknownClass", RuntimeDecisionReason.WINDOW_CLASS_DRIFT),
    }
    for field, (value, reason) in changes.items():
        decision = sentinel.assess(snapshot(**{field: value})).decision
        assert decision.observe_allowed is False and decision.commit_allowed is False
        assert reason in decision.reasons


def test_unknown_modal_and_login_state_fail_closed() -> None:
    sentinel = QQEnvironmentSentinel(StaticPort(snapshot()), profile())
    modal = sentinel.assess(snapshot(modal_state=ModalState.UNKNOWN)).decision
    logged_out = sentinel.assess(snapshot(is_logged_in=False)).decision
    assert RuntimeDecisionReason.MODAL_PRESENT in modal.reasons
    assert RuntimeDecisionReason.NOT_LOGGED_IN in logged_out.reasons
    assert not modal.observe_allowed and not logged_out.commit_allowed


def test_fingerprint_digest_is_deterministic_and_excludes_ephemeral_handles() -> None:
    first = snapshot(process_id=42, window_handle=100).fingerprint
    second = snapshot(process_id=99, window_handle=200).fingerprint
    assert first == second
    assert first.digest == second.digest
    assert first.digest == fingerprint().digest


class StaticPort:
    def __init__(self, value: RuntimeSnapshot) -> None:
        self.value = value

    def read_snapshot(self) -> RuntimeSnapshot:
        return self.value


class AsyncPort(StaticPort):
    async def read_snapshot(self) -> RuntimeSnapshot:
        return self.value


def test_inspect_accepts_sync_and_async_readonly_ports() -> None:
    assert asyncio.run(
        QQEnvironmentSentinel(StaticPort(snapshot()), profile()).inspect()
    ).decision.commit_allowed
    assert asyncio.run(
        QQEnvironmentSentinel(AsyncPort(snapshot()), profile()).inspect()
    ).decision.commit_allowed


def test_q0_source_has_no_desktop_mutation_api() -> None:
    source = (
        Path(__file__).parents[4]
        / "src/messenger_ai/adapters/qq/live_driver/environment.py"
    )
    body = source.read_text(encoding="utf-8")
    forbidden = (
        "Set" + "ForegroundWindow",
        "Show" + "Window",
        "Send" + "Input",
        "mouse" + "_event",
        "keybd" + "_event",
    )
    assert not any(token in body for token in forbidden)
