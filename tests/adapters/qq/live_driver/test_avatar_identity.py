from __future__ import annotations

import pytest

from messenger_ai.adapters.qq.live_driver.avatar_identity import (
    AvatarIdentityError,
    assess_pending_application,
    build_avatar_evidence,
    parse_avatar_report,
)

HASH = "a" * 64


def report(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-avatar-v1",
        "mode": "current_chat_avatar_capture",
        "succeeded": True,
        "status": "AVATAR_CAPTURED",
        "process_id": 1234,
        "window_handle": 9001,
        "is_maximized": True,
        "is_foreground_before": False,
        "is_foreground_after": False,
        "active_header_digest": HASH,
        "candidate_row_count": 3,
        "stable_match_count": 2,
        "avatar_hmac": "b" * 64,
        "structure_digest": "c" * 64,
        "capture_api": "PrintWindow",
        "privacy": {
            "exact_hwnd": True,
            "desktop_capture": False,
            "image_bytes_emitted": False,
            "emitted_chat_text": False,
            "emitted_control_names": False,
            "navigation_performed": False,
            "foreground_changed": False,
            "write_actions_supported": False,
            "mouse_input_used": False,
            "keyboard_input_used": False,
            "clipboard_used": False,
            "foreground_requested": False,
            "composer_or_send_accessed": False,
        },
    }
    value.update(changes)
    return value


def application() -> dict[str, object]:
    return {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "pending_human_binding",
        "binding_created": False,
        "automatic_eligible": False,
        "environment_fingerprint": HASH,
        "selector_pack_version": "selector-v1",
        "right_region_evidence": {"active_header_digest": HASH},
    }


def parsed() -> dict[str, object]:
    return parse_avatar_report(
        report(), window_handle=9001, process_id=1234, expected_header_digest=HASH
    ) | {"environment_fingerprint": HASH, "selector_pack_version": "selector-v1"}


def test_avatar_requires_two_consistent_rows_and_projects_keyed_hash() -> None:
    value = parsed()
    evidence = build_avatar_evidence(value, application())
    assert evidence.avatar_hash == "b" * 64
    assert evidence.display_name == ""
    assert evidence.structure_path[-1] == "avatar_identity"
    assert evidence.platform_conversation_id.startswith("qq-conversation-hmac:")
    updated = assess_pending_application(
        application(), value, identity_evidence=evidence
    )
    assert updated["status"] == "pending_human_binding"
    assert updated["binding_created"] is False
    assert updated["automatic_eligible"] is False
    assert updated["identity_evidence"]["avatar_hash"] == "b" * 64


@pytest.mark.parametrize(
    "capture_api", ["PrintWindow", "WindowsGraphicsCapture", "ExactHwndBitBlt"]
)
def test_supported_capture_api_keeps_the_same_privacy_gate(capture_api: str) -> None:
    parsed_value = parse_avatar_report(
        report(capture_api=capture_api),
        window_handle=9001,
        process_id=1234,
        expected_header_digest=HASH,
    )
    assert parsed_value["avatar_hmac"] == "b" * 64


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"candidate_row_count": 1}, "AVATAR_ROWS_NOT_STABLE"),
        ({"stable_match_count": 1}, "AVATAR_ROWS_NOT_STABLE"),
        ({"stable_match_count": 4}, "AVATAR_ROWS_NOT_STABLE"),
        ({"avatar_hmac": "not-a-hash"}, "INVALID_DIGEST"),
        ({"structure_digest": "not-a-hash"}, "INVALID_DIGEST"),
        ({"status": "AVATAR_CAPTURE_SOLID"}, "STATUS_MISMATCH"),
        ({"is_foreground_before": True}, "QQ_FOREGROUND"),
        ({"is_foreground_after": True}, "QQ_FOREGROUND"),
        ({"is_maximized": False}, "WINDOW_NOT_MAXIMIZED"),
        ({"active_header_digest": "d" * 64}, "HEADER_MISMATCH"),
        ({"capture_api": "DesktopScreenshot"}, "CAPTURE_API_MISMATCH"),
        ({"process_id": 9999}, "HWND_OR_PROCESS_MISMATCH"),
        ({"privacy": {"image_bytes_emitted": True}}, "PRIVACY_CONTRACT_FAILED"),
        ({"raw_image": "SECRET"}, "PRIVACY_CONTRACT_FAILED"),
    ],
)
def test_avatar_gate_fails_closed(changes: dict[str, object], code: str) -> None:
    with pytest.raises(AvatarIdentityError, match=code):
        parse_avatar_report(
            report(**changes),
            window_handle=9001,
            process_id=1234,
            expected_header_digest=HASH,
        )


def test_active_application_is_never_updated() -> None:
    with pytest.raises(AvatarIdentityError, match="APPLICATION_NOT_PENDING"):
        assess_pending_application(
            {**application(), "status": "active"},
            parsed(),
            identity_evidence=build_avatar_evidence(parsed(), application()),
        )
