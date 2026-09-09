from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.qq_q3_profile_assess import Q3CliError, invoke_profile_helper, main

HASH = "a" * 64


def test_direct_script_entrypoint_loads_project_modules(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "qq_q3_profile_assess.py"
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


def test_hmac_key_is_stdin_only_and_flags_are_fixed() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps({"ok": True}), stderr=""
        )

    key = b"k" * 32
    result = invoke_profile_helper(["fake-helper"], HASH, key, runner=fake_run)
    assert result["ok"] is True
    assert calls[0][0] == [
        "fake-helper",
        "--capture-current-identity",
        "--identity-authorized",
    ]
    assert key.hex() not in calls[0][0]
    assert calls[0][1]["input"] == HASH + "\n" + base64.b64encode(key).decode() + "\n"


def test_helper_failure_surfaces_only_safe_status_code() -> None:
    def fake_run(command: list[str], **_kwargs: object):
        return subprocess.CompletedProcess(
            args=command,
            returncode=2,
            stdout=json.dumps(
                {
                    "succeeded": False,
                    "status": "PENDING_NO_STABLE_SIGNAL",
                    "raw_profile_id": "SECRET",
                }
            ),
            stderr="SECRET",
        )

    with pytest.raises(Q3CliError, match="PENDING_NO_STABLE_SIGNAL") as caught:
        invoke_profile_helper(["fake-helper"], HASH, b"k" * 32, runner=fake_run)
    assert "SECRET" not in str(caught.value)


def test_main_updates_existing_pending_application_atomically(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import qq_q3_profile_assess as cli

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
        "invoke_profile_helper",
        lambda _command, _header, _key: {
            "probe_version": "qq-uia-current-identity-v1",
            "mode": "current_chat_identity",
            "succeeded": True,
            "status": "STABLE_IDENTITY_CAPTURED",
            "process_id": 1,
            "window_handle": 9001,
            "is_maximized": True,
            "is_foreground_before": False,
            "is_foreground_after": False,
            "active_header_digest": HASH,
            "right_region_structure_digest": "b" * 64,
            "header_candidate_count": 1,
            "identity_candidate_count": 1,
            "profile_id_hmac": "c" * 64,
            "identity_evidence_type": "explicit_labeled_qq_id",
            "profile_structure_digest": "d" * 64,
            "privacy": {
                "exact_hwnd": True,
                "raw_profile_id_emitted": False,
                "hmac_key_from_stdin_only": True,
                "emitted_chat_text": False,
                "emitted_control_names": False,
                "desktop_capture_supported": False,
                "mouse_input_used": False,
                "keyboard_input_used": False,
                "clipboard_used": False,
                "foreground_requested": False,
                "composer_send_attempted": False,
                "composer_or_send_accessed": False,
                "write_actions_supported": False,
                "hmac_key_emitted": False,
                "transient_navigation_performed": True,
            },
            "recovery": {
                "attempted": True,
                "original_view_restored": True,
                "foreground_changed": False,
            },
        },
    )
    result = main(
        [
            "--helper-command",
            "fake",
            "--application",
            str(path),
        ]
    )
    assert result == 0
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["status"] == "pending_human_binding_confirmation"
    assert updated["binding_created"] is False
    assert updated["automatic_eligible"] is False
    assert "profile_id" in updated
    summary = json.loads(capsys.readouterr().out)
    assert summary["application_updated"] is True
    assert "profile_id_hmac" not in summary


def test_main_validation_error_does_not_write_application(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import qq_q3_profile_assess as cli

    original = {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "active",
        "binding_created": True,
        "automatic_eligible": True,
    }
    path = tmp_path / "pending.json"
    raw = json.dumps(original)
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", lambda _vault: None)
    result = main(
        [
            "--helper-command",
            "fake",
            "--application",
            str(path),
        ]
    )
    assert result == 2
    assert path.read_text(encoding="utf-8") == raw
