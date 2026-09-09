from __future__ import annotations

import json
import subprocess

import pytest

from messenger_ai.adapters.qq.live_driver.wgc_avatar import (
    WgcAvatarError,
    build_wgc_request,
    invoke_discovery_helper,
    invoke_wgc_sidecar,
    parse_discovery_report,
    parse_wgc_capture_report,
    require_discovery_unchanged,
    serialize_wgc_stdin,
)

HASH = "a" * 64


def discovery(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-avatar-discovery-v1",
        "mode": "current_chat_avatar_discovery",
        "succeeded": True,
        "status": "AVATAR_CANDIDATES_DISCOVERED",
        "process_id": 1234,
        "window_handle": 9001,
        "is_maximized": True,
        "is_foreground": False,
        "is_background": True,
        "active_header_digest": HASH,
        "candidate_count": 2,
        "structure_digest": "b" * 64,
        "candidates": [
            {
                "normalized_x": 0.3,
                "normalized_y": 0.2,
                "normalized_width": 0.04,
                "normalized_height": 0.04,
            },
            {
                "normalized_x": 0.3,
                "normalized_y": 0.4,
                "normalized_width": 0.04,
                "normalized_height": 0.04,
            },
        ],
        "privacy": {
            "exact_hwnd": True,
            "desktop_capture": False,
            "image_bytes_emitted": False,
            "emitted_chat_text": False,
            "emitted_control_names": False,
            "emitted_runtime_ids": False,
            "absolute_screen_coordinates_emitted": False,
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


def capture(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-avatar-v1",
        "mode": "current_chat_avatar_capture",
        "succeeded": True,
        "status": "CURRENT_AVATAR_CAPTURED",
        "process_id": 1234,
        "window_handle": 9001,
        "is_maximized": True,
        "is_foreground_before": False,
        "is_foreground_after": False,
        "active_header_digest": HASH,
        "candidate_row_count": 2,
        "stable_match_count": 2,
        "avatar_hmac": "c" * 64,
        "structure_digest": "b" * 64,
        "capture_api": "WindowsGraphicsCapture",
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


def test_real_terra_discovery_fixture_is_strict_and_stable() -> None:
    before = parse_discovery_report(discovery(), expected_header_digest=HASH)
    after = parse_discovery_report(discovery(), expected_header_digest=HASH)
    require_discovery_unchanged(before, after)
    request = build_wgc_request(before)
    payload = json.loads(serialize_wgc_stdin(request, b"k" * 32).splitlines()[1])
    assert payload["process_id"] == 1234
    assert payload["window_handle"] == 9001
    assert payload["structure_digest"] == "b" * 64
    assert len(payload["candidates"]) == 2
    assert "k" * 32 not in payload


def test_candidate_rectangle_fields_keep_explicit_order() -> None:
    value = discovery()
    value["candidates"] = [
        {
            "normalized_height": 0.61,
            "normalized_width": 0.53,
            "normalized_y": 0.27,
            "normalized_x": 0.13,
        },
        {
            "normalized_x": 0.21,
            "normalized_y": 0.42,
            "normalized_width": 0.17,
            "normalized_height": 0.29,
        },
    ]
    parsed = parse_discovery_report(value, expected_header_digest=HASH)
    first, second = parsed.candidates
    assert (first.normalized_x, first.normalized_y) == (0.13, 0.27)
    assert (first.normalized_width, first.normalized_height) == (0.53, 0.61)
    assert (second.normalized_x, second.normalized_y) == (0.21, 0.42)
    assert (second.normalized_width, second.normalized_height) == (0.17, 0.29)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"status": "INSUFFICIENT_AVATAR_ROWS"}, "DISCOVERY_STATUS_MISMATCH"),
        ({"is_background": False}, "QQ_FOREGROUND"),
        ({"candidate_count": 1}, "CANDIDATE_COUNT_INVALID"),
        ({"candidates": [{"normalized_x": 2}]}, "CANDIDATE_COUNT_INVALID"),
        ({"structure_digest": "bad"}, "INVALID_DIGEST"),
        ({"privacy": {"desktop_capture": True}}, "PRIVACY_CONTRACT_FAILED"),
    ],
)
def test_discovery_gate_fails_closed(changes: dict[str, object], code: str) -> None:
    with pytest.raises(WgcAvatarError, match=code):
        parse_discovery_report({**discovery(), **changes}, expected_header_digest=HASH)


def test_capture_report_reuses_avatar_contract() -> None:
    parsed = parse_wgc_capture_report(
        capture(),
        process_id=1234,
        window_handle=9001,
        expected_header_digest=HASH,
        expected_structure_digest="b" * 64,
        expected_candidate_count=2,
    )
    assert parsed["avatar_hmac"] == "c" * 64
    with pytest.raises(WgcAvatarError, match="CAPTURE_API_MISMATCH"):
        parse_wgc_capture_report(
            capture(capture_api="PrintWindow"),
            process_id=1234,
            window_handle=9001,
            expected_header_digest=HASH,
            expected_structure_digest="b" * 64,
            expected_candidate_count=2,
        )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"structure_digest": "d" * 64}, "STRUCTURE_DIGEST_MISMATCH"),
        ({"candidate_row_count": 3}, "CANDIDATE_COUNT_MISMATCH"),
    ],
)
def test_capture_cannot_cross_discovery_context(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(WgcAvatarError, match=code):
        parse_wgc_capture_report(
            capture(**changes),
            process_id=1234,
            window_handle=9001,
            expected_header_digest=HASH,
            expected_structure_digest="b" * 64,
            expected_candidate_count=2,
        )


def test_discovery_change_is_rejected() -> None:
    with pytest.raises(WgcAvatarError, match="DISCOVERY_CHANGED"):
        require_discovery_unchanged(
            parse_discovery_report(discovery(), expected_header_digest=HASH),
            parse_discovery_report(
                discovery(structure_digest="d" * 64), expected_header_digest=HASH
            ),
        )


def test_discovery_command_flags_and_sidecar_timeout_key_stdin_only() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        if any("discover-current-avatar" in part for part in command):
            stdout = json.dumps(discovery())
        else:
            stdout = json.dumps(capture())
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="secret")

    discovery_result = invoke_discovery_helper(["terra"], HASH, runner=fake_run)
    assert discovery_result["status"] == "AVATAR_CANDIDATES_DISCOVERED"
    request = build_wgc_request(
        parse_discovery_report(discovery_result, expected_header_digest=HASH)
    )
    invoke_wgc_sidecar(["native"], request, b"k" * 32, runner=fake_run)
    assert calls[0][0] == [
        "terra",
        "--discover-current-avatar",
        "--avatar-discovery-authorized",
    ]
    assert calls[0][1]["input"] == HASH + "\n"
    assert calls[0][1]["timeout"] == 60
    assert calls[1][0] == ["native"]
    assert calls[1][1]["timeout"] == 15
    assert "k" * 32 not in calls[1][0]
