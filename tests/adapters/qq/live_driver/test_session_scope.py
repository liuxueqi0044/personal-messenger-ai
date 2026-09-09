from __future__ import annotations

import json
import subprocess

import pytest

from messenger_ai.adapters.qq.live_driver.session_scope import (
    CurrentSessionInspectionError,
    invoke_current_session_helper,
    parse_current_session_report,
)

HASH = "a" * 64


def report(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-session-v1",
        "mode": "current_session_inspection",
        "succeeded": True,
        "status": "CURRENT_SESSION_INSPECTED",
        "process_id": 41,
        "window_handle": 9001,
        "process_started_at": "2026-09-09T01:02:03.0000000Z",
        "is_maximized": True,
        "is_foreground": False,
        "header_candidate_count": 1,
        "active_header_digest": HASH,
        "structure_digest": "b" * 64,
        "privacy": {
            "exact_hwnd": True,
            "emitted_chat_text": False,
            "emitted_control_names": False,
            "emitted_contact_identifier": False,
            "desktop_capture_supported": False,
            "navigation_performed": False,
            "foreground_changed": False,
            "write_actions_supported": False,
            "mouse_input_used": False,
            "keyboard_input_used": False,
            "clipboard_used": False,
            "composer_or_send_accessed": False,
        },
    }
    value.update(changes)
    return value


def test_parser_projects_only_redacted_scope_metadata() -> None:
    parsed = parse_current_session_report(report(), expected_header_digest=HASH)

    assert parsed.process_id == 41
    assert parsed.process_started_at.utcoffset().total_seconds() == 0
    assert not hasattr(parsed, "header_text")
    assert not hasattr(parsed, "messages")


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"active_header_digest": "c" * 64}, "HEADER_MISMATCH"),
        ({"structure_digest": "bad"}, "INSPECTION_DATA_INVALID"),
        ({"is_maximized": False}, "WINDOW_STATE_NOT_CERTIFIED"),
        ({"header_candidate_count": 2}, "ACTIVE_HEADER_AMBIGUOUS"),
        ({"unexpected": "field"}, "INSPECTION_SCHEMA_INVALID"),
    ],
)
def test_parser_fails_closed_on_scope_or_schema_drift(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(CurrentSessionInspectionError) as failure:
        parse_current_session_report(report(**changes), expected_header_digest=HASH)

    assert failure.value.code == code


def test_parser_enforces_no_content_and_no_input_privacy_contract() -> None:
    value = report()
    value["privacy"]["emitted_chat_text"] = True  # type: ignore[index]

    with pytest.raises(CurrentSessionInspectionError) as failure:
        parse_current_session_report(value, expected_header_digest=HASH)

    assert failure.value.code == "PRIVACY_CONTRACT_FAILED"


def test_invoker_passes_digest_on_stdin_and_uses_readonly_flags() -> None:
    observed: dict[str, object] = {}

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, json.dumps(report()), "")

    result = invoke_current_session_helper(["helper"], HASH, runner=runner)

    assert result["status"] == "CURRENT_SESSION_INSPECTED"
    assert observed["command"] == [
        "helper",
        "--inspect-current-session",
        "--session-inspection-authorized",
    ]
    assert observed["input"] == HASH + "\n"
    assert "QQ" not in repr(observed)


def test_invoker_surfaces_only_safe_helper_status() -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            2,
            json.dumps(report(succeeded=False, status="CHAT_SHELL_NOT_CERTIFIED")),
            "private detail",
        )

    with pytest.raises(CurrentSessionInspectionError) as failure:
        invoke_current_session_helper(["helper"], HASH, runner=runner)

    assert failure.value.code == "CHAT_SHELL_NOT_CERTIFIED"
    assert "private detail" not in str(failure.value)
