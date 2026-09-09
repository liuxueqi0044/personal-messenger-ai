from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.qq_q3_avatar_assess import Q3AvatarCliError, invoke_avatar_helper, main

HASH = "a" * 64


def helper_report() -> dict[str, object]:
    return {
        "probe_version": "qq-uia-current-avatar-v1",
        "mode": "current_chat_avatar_capture",
        "succeeded": True,
        "status": "AVATAR_CAPTURED",
        "process_id": 1,
        "window_handle": 9001,
        "is_maximized": True,
        "is_foreground_before": False,
        "is_foreground_after": False,
        "active_header_digest": HASH,
        "candidate_row_count": 2,
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


def test_direct_script_entrypoint_is_available(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "qq_q3_avatar_assess.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert "--application" in completed.stdout


def test_avatar_key_is_stdin_only_and_flags_are_fixed() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps({"ok": True}), stderr=""
        )

    key = b"k" * 32
    result = invoke_avatar_helper(["fake-helper"], HASH, key, runner=fake_run)
    assert result["ok"] is True
    assert calls[0][0] == [
        "fake-helper",
        "--capture-current-avatar",
        "--avatar-authorized",
    ]
    assert key.hex() not in calls[0][0]
    assert calls[0][1]["input"] == HASH + "\n" + base64.b64encode(key).decode() + "\n"


def test_helper_failure_never_exposes_payload() -> None:
    def fake_run(command: list[str], **_kwargs: object):
        return subprocess.CompletedProcess(
            args=command,
            returncode=2,
            stdout=json.dumps(
                {"status": "AVATAR_CAPTURE_SOLID", "raw_image": "SECRET"}
            ),
            stderr="SECRET",
        )

    with pytest.raises(Q3AvatarCliError, match="AVATAR_CAPTURE_SOLID") as caught:
        invoke_avatar_helper(["fake-helper"], HASH, b"k" * 32, runner=fake_run)
    assert "SECRET" not in str(caught.value)


def test_main_updates_pending_application_without_printing_avatar_hash(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import qq_q3_avatar_assess as cli

    application = {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "pending_human_binding",
        "binding_created": False,
        "automatic_eligible": False,
        "environment_fingerprint": HASH,
        "selector_pack_version": "selector-v1",
        "right_region_evidence": {"active_header_digest": HASH},
    }
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(application), encoding="utf-8")

    class FakeStore:
        def __init__(self, _vault):
            pass

        def get_or_create_hmac_key(self, _name):
            return b"k" * 32

    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", FakeStore)
    monkeypatch.setattr(
        cli,
        "get_live_scope",
        lambda _profile: {
            "process_id": 1,
            "window_handle": 9001,
            "environment_fingerprint": HASH,
            "selector_pack_version": "selector-v1",
        },
    )
    monkeypatch.setattr(
        cli,
        "invoke_avatar_helper",
        lambda _command, _header, _key: helper_report(),
    )
    result = main(["--helper-command", "fake", "--application", str(path)])
    assert result == 0
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["status"] == "pending_human_binding_confirmation"
    assert updated["binding_created"] is False
    assert updated["automatic_eligible"] is False
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["application_updated"] is True
    assert "avatar_hmac" not in summary
    assert "b" * 64 not in output


def test_main_invalid_application_does_not_write(tmp_path: Path, monkeypatch) -> None:
    from scripts import qq_q3_avatar_assess as cli

    path = tmp_path / "pending.json"
    raw = json.dumps(
        {
            "schema_version": "qq-q3-binding-application-v1",
            "status": "active",
            "binding_created": True,
            "automatic_eligible": True,
        }
    )
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cli, "get_live_scope", lambda _profile: pytest.fail())
    assert main(["--helper-command", "fake", "--application", str(path)]) == 2
    assert path.read_text(encoding="utf-8") == raw


def test_main_unsupported_capture_api_does_not_write_pending_application(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import qq_q3_avatar_assess as cli

    application = {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "pending_human_binding",
        "binding_created": False,
        "automatic_eligible": False,
        "environment_fingerprint": HASH,
        "selector_pack_version": "selector-v1",
        "right_region_evidence": {"active_header_digest": HASH},
    }
    path = tmp_path / "pending.json"
    raw = json.dumps(application)
    path.write_text(raw, encoding="utf-8")

    class FakeStore:
        def __init__(self, _vault):
            pass

        def get_or_create_hmac_key(self, _name):
            return b"k" * 32

    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", FakeStore)
    monkeypatch.setattr(
        cli,
        "get_live_scope",
        lambda _profile: {
            "process_id": 1,
            "window_handle": 9001,
            "environment_fingerprint": HASH,
            "selector_pack_version": "selector-v1",
        },
    )
    monkeypatch.setattr(
        cli,
        "invoke_avatar_helper",
        lambda _command, _header, _key: (
            helper_report() | {"capture_api": "DesktopScreenshot"}
        ),
    )
    assert main(["--helper-command", "fake", "--application", str(path)]) == 2
    assert path.read_text(encoding="utf-8") == raw
