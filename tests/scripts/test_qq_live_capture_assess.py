from __future__ import annotations

import json
import subprocess

import pytest

from scripts.qq_live_capture_assess import CaptureAssessmentError, verify_report

HASH = "a" * 64


def report(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-chat-v1",
        "mode": "current_chat_capture",
        "succeeded": True,
        "captured_at": "2026-09-08T02:00:00Z",
        "is_minimized": False,
        "active_header_digest": HASH,
        "right_region_structure_digest": "b" * 64,
        "messages": [
            {
                "direction": "inbound",
                "source_evidence_hash": HASH,
                "message_watermark": "w1",
                "text": "private body must not be printed",
            },
            {
                "direction": "unknown",
                "source_evidence_hash": "b" * 64,
                "message_watermark": "w2",
                "text": "another body",
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


def test_verify_only_returns_counts_digests_directions_without_text() -> None:
    result = verify_report(json.dumps(report()))
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["verify_only"] is True
    assert result["message_count"] == 2
    assert result["directions"] == {"inbound": 1, "outbound": 0, "unknown": 1}
    assert "private body" not in encoded


def test_verify_rejects_dirty_privacy_and_ambiguous_direction() -> None:
    dirty = report()
    dirty["privacy"]["write_actions_supported"] = True
    with pytest.raises(CaptureAssessmentError, match="PRIVACY_CONTRACT_FAILED"):
        verify_report(json.dumps(dirty))
    ambiguous = report()
    ambiguous["messages"][0]["direction"] = "ambiguous"
    with pytest.raises(CaptureAssessmentError, match="INVALID_DIRECTION"):
        verify_report(json.dumps(ambiguous))


def test_verify_records_unknown_time_without_accepting_it() -> None:
    value = report()
    value["messages"][0]["observed_at"] = "2026-09-08T02:00:00Z"
    result = verify_report(json.dumps(value))
    assert result["pending_time_baseline"] is True
    assert "observed_time_ignored" in result["issues"]


def test_main_style_subprocess_failure_never_surfaces_body(monkeypatch, capsys) -> None:
    from scripts import qq_live_capture_assess as cli

    secret = "SECRET_CHAT_BODY"
    value = report()
    value["messages"][0]["text"] = secret

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=json.dumps(value), stderr=secret
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["--helper-command", "fake"]) == 0
    assert secret not in capsys.readouterr().out
