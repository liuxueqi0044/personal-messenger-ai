from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from messenger_ai.adapters.qq.live_driver.identity import (
    ConversationType,
    IdentityEvidenceSet,
)
from messenger_ai.adapters.qq.live_driver.uia_capture import (
    QQCurrentChatCapturePort,
    UiaCaptureError,
    snapshot_from_report,
)

HASH = "a" * 64
NOW = datetime(2026, 9, 8, 2, tzinfo=UTC)


def evidence(**changes: object) -> IdentityEvidenceSet:
    value: dict[str, object] = {
        "window_handle": 9001,
        "platform_conversation_id": "qq-current",
        "conversation_type": ConversationType.DIRECT,
        "structure_path": ("main_window", "conversation_item"),
        "participant_signature": "participant-digest",
        "environment_fingerprint": HASH,
        "selector_pack_version": "selector-v1",
        "confidence": 0.1,
    }
    value.update(changes)
    return IdentityEvidenceSet(**value)


def report(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-chat-v1",
        "mode": "current_chat_capture",
        "succeeded": True,
        "status": "CURRENT_CHAT_CAPTURED",
        "process_id": 1234,
        "window_handle": 9001,
        "captured_at": "2026-09-08T02:00:00Z",
        "active_header_digest": HASH,
        "right_region_structure_digest": "f" * 64,
        "is_minimized": False,
        "messages": [
            {
                "message_watermark": "wm-1",
                "source_evidence_hash": "b" * 64,
                "direction": "inbound",
                "text": "绝不能出现在 CLI 输出里的正文",
                "observer_confidence": 0.9,
                "direction_confidence": 0.8,
            },
            {
                "message_watermark": "wm-2",
                "source_evidence_hash": "c" * 64,
                "direction": "outbound",
                "text": "outbound",
            },
        ],
        "privacy": {
            "exact_hwnd": True,
            "desktop_capture_supported": False,
            "changed_window_state": False,
            "write_actions_supported": False,
            "emitted_chat_text": True,
        },
    }
    value.update(changes)
    return value


class FakeRunner:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((command, kwargs))
        import subprocess

        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(self.value), stderr=""
        )


def test_capture_port_constructs_exact_scope_snapshot_and_forces_unknown_time() -> None:
    fake = FakeRunner(report())
    port = QQCurrentChatCapturePort(
        ["fake-helper"], identity_evidence=evidence(), header_digest=HASH, runner=fake
    )
    snapshot = port.capture_visible_messages(9001)
    assert port.capture_capabilities().write_actions_supported is False
    assert snapshot.window_handle == 9001
    parsed = snapshot_from_report(
        report(),
        identity_evidence=evidence(),
        expected_header_digest=HASH,
        clock=lambda: NOW,
    )
    assert parsed.identity_evidence.environment_fingerprint == HASH
    assert parsed.identity_evidence.selector_pack_version == "selector-v1"
    assert all(message.observed_at is None for message in snapshot.messages)
    assert all(message.time_confidence == 0 for message in snapshot.messages)
    assert snapshot.messages[0].direction.value == "inbound"
    assert fake.calls[0][0] == [
        "fake-helper",
        "--capture-current-chat",
        "--capture-authorized",
    ]


def test_unknown_time_goes_pending_in_observer() -> None:
    snapshot = snapshot_from_report(
        report(),
        identity_evidence=evidence(),
        expected_header_digest=HASH,
        clock=lambda: NOW,
    )
    # The identity is intentionally not registered: even if registered later,
    # the zero time confidence prevents an accepted automatic event.
    assert all(message.time_confidence == 0 for message in snapshot.messages)


@pytest.mark.parametrize(
    "code",
    [
        "HWND_MISMATCH",
        "HEADER_MISMATCH",
    ],
)
def test_scope_and_header_mismatch_fail_closed(code: str) -> None:
    value = report()
    key, replacement = {
        "HWND_MISMATCH": ("window_handle", 1),
        "HEADER_MISMATCH": ("active_header_digest", "b" * 64),
    }[code]
    value[key] = replacement
    with pytest.raises(UiaCaptureError, match=code):
        snapshot_from_report(
            value,
            identity_evidence=evidence(),
            expected_header_digest=HASH,
            clock=lambda: NOW,
        )


def test_privacy_minimized_and_direction_ambiguity_fail_closed() -> None:
    minimized = report(is_minimized=True)
    with pytest.raises(UiaCaptureError, match="WINDOW_MINIMIZED"):
        snapshot_from_report(
            minimized,
            identity_evidence=evidence(),
            expected_header_digest=HASH,
        )
    value = report()
    value["privacy"] = {
        "exact_hwnd": True,
        "desktop_capture_supported": False,
        "changed_window_state": True,
        "write_actions_supported": False,
        "emitted_chat_text": True,
    }
    with pytest.raises(UiaCaptureError, match="PRIVACY_CONTRACT_FAILED"):
        snapshot_from_report(
            value, identity_evidence=evidence(), expected_header_digest=HASH
        )
    value = report()
    value["messages"][0]["direction"] = "ambiguous"
    with pytest.raises(UiaCaptureError, match="INVALID_DIRECTION"):
        snapshot_from_report(
            value, identity_evidence=evidence(), expected_header_digest=HASH
        )


def test_plaintext_never_appears_in_error() -> None:
    secret = "SECRET_CHAT_BODY"
    value = report()
    value["messages"][0]["text"] = secret
    value["privacy"]["emitted_chat_text"] = False
    with pytest.raises(UiaCaptureError) as caught:
        snapshot_from_report(
            value, identity_evidence=evidence(), expected_header_digest=HASH
        )
    assert secret not in str(caught.value)
