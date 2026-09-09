from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qq_q3_wgc_avatar_assess import main

HASH = "a" * 64


def discovery() -> dict[str, object]:
    return {
        "probe_version": "qq-uia-current-avatar-discovery-v1",
        "mode": "current_chat_avatar_discovery",
        "succeeded": True,
        "status": "AVATAR_CANDIDATES_DISCOVERED",
        "process_id": 1,
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


def capture() -> dict[str, object]:
    return {
        "probe_version": "qq-uia-current-avatar-v1",
        "mode": "current_chat_avatar_capture",
        "succeeded": True,
        "status": "CURRENT_AVATAR_CAPTURED",
        "process_id": 1,
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


def test_main_calls_discovery_sidecar_discovery_and_updates_atomically(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import qq_q3_wgc_avatar_assess as cli

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

    class Store:
        def __init__(self, _vault):
            pass

        def get_or_create_hmac_key(self, _name):
            return b"k" * 32

    calls: list[str] = []
    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", Store)
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

    def fake_discovery(*_args):
        calls.append("discovery")
        return discovery()

    def fake_sidecar(*_args):
        calls.append("sidecar")
        return capture()

    monkeypatch.setattr(cli, "invoke_discovery_helper", fake_discovery)
    monkeypatch.setattr(cli, "invoke_wgc_sidecar", fake_sidecar)
    assert (
        main(
            [
                "--discovery-command",
                "terra",
                "--sidecar-command",
                "native",
                "--application",
                str(path),
            ]
        )
        == 0
    )
    assert calls == ["discovery", "sidecar", "discovery"]
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["status"] == "pending_human_binding_confirmation"
    assert updated["binding_created"] is False
    assert updated["automatic_eligible"] is False
    output = capsys.readouterr().out
    assert "avatar_hmac" not in output
    assert "c" * 64 not in output


def test_discovery_drift_does_not_write_application(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import qq_q3_wgc_avatar_assess as cli

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
    calls = 0
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

    def fake_discovery(*_args):
        nonlocal calls
        calls += 1
        value = discovery()
        if calls == 2:
            value["structure_digest"] = "d" * 64
        return value

    monkeypatch.setattr(cli, "invoke_discovery_helper", fake_discovery)
    monkeypatch.setattr(cli, "invoke_wgc_sidecar", lambda *_args: capture())
    assert (
        main(
            [
                "--discovery-command",
                "terra",
                "--sidecar-command",
                "native",
                "--application",
                str(path),
            ]
        )
        == 2
    )
    assert calls == 2
    assert path.read_text(encoding="utf-8") == raw


def test_default_sidecar_points_to_project_release_skeleton() -> None:
    from scripts.qq_q3_wgc_avatar_assess import _default_sidecar_command

    command = _default_sidecar_command()
    assert command[0] == "dotnet"
    assert command[1].endswith(
        "scripts\\qq_wgc_avatar_sidecar_managed\\bin\\Release\\net10.0-windows10.0.26100.0\\QQ.WgcAvatar.dll"
    ) or command[1].endswith(
        "scripts/qq_wgc_avatar_sidecar_managed/bin/Release/net10.0-windows10.0.26100.0/QQ.WgcAvatar.dll"
    )


@pytest.mark.parametrize(
    "capture_update",
    [{"structure_digest": "d" * 64}, {"candidate_row_count": 3}],
)
def test_capture_context_mismatch_does_not_write_application(
    tmp_path: Path, monkeypatch, capture_update: dict[str, object]
) -> None:
    from scripts import qq_q3_wgc_avatar_assess as cli

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

    class Store:
        def __init__(self, _vault):
            pass

        def get_or_create_hmac_key(self, _name):
            return b"k" * 32

    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", Store)
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
    monkeypatch.setattr(cli, "invoke_discovery_helper", lambda *_args: discovery())
    monkeypatch.setattr(
        cli,
        "invoke_wgc_sidecar",
        lambda *_args: capture() | capture_update,
    )
    assert (
        main(
            [
                "--discovery-command",
                "terra",
                "--sidecar-command",
                "native",
                "--application",
                str(path),
            ]
        )
        == 2
    )
    assert path.read_text(encoding="utf-8") == raw
