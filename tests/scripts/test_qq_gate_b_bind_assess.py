from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from scripts.qq_gate_b_bind_assess import (
    GateBAssessmentError,
    assess_gate_b,
    build_binding_application,
    invoke_helper,
    main,
    parse_helper_output,
    read_selection_phrase,
)

PHRASE = "小雨"
COMMAND = ["fake-helper"]
HASH = "a" * 64


def payload(
    status: str = "MATCH_READY_DRY_RUN", **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-selection-v1",
        "mode": "conversation_selection",
        "succeeded": True,
        "status": status,
        "match_count": 1 if status != "CURRENT_RIGHT_REGION_MATCH" else 0,
        "selection_attempted": False,
        "match_evidence_digest": HASH,
        "right_region_evidence": None,
        "root_visible_text_match_count": 1,
        "left_visible_text_match_count": 1,
        "right_visible_text_match_count": 0,
        "privacy": {
            "target_from_stdin_only": True,
            "emitted_target": False,
            "emitted_control_names": False,
            "emitted_chat_text": False,
            "emitted_content_hashes_are_aggregate": True,
        },
    }
    value.update(changes)
    if status in {"CURRENT_RIGHT_REGION_MATCH", "SELECTED_WITH_POST_EVIDENCE"}:
        value["right_visible_text_match_count"] = 1
        value["right_region_evidence"] = {
            "available": True,
            "node_count": 12,
            "structure_digest": "b" * 64,
            "content_digest": "c" * 64,
            "header_candidate_count": 1,
            "active_header_digest": "d" * 64,
            "target_match_evidence_digest": "e" * 64,
        }
    if status == "SELECTED_WITH_POST_EVIDENCE":
        value["selection_attempted"] = True
    return value


class FakeRunner:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        response = self.responses.pop(0)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(response), stderr=""
        )


def test_real_helper_flags_and_phrase_stdin_only() -> None:
    fake = FakeRunner(payload())
    result = invoke_helper(COMMAND, PHRASE, runner=fake)
    command, kwargs = fake.calls[0]
    assert result.status == "MATCH_READY_DRY_RUN"
    assert command == ["fake-helper", "--match-from-stdin"]
    assert PHRASE not in command
    assert kwargs["input"] == PHRASE

    fake = FakeRunner(payload("SELECTED_WITH_POST_EVIDENCE"))
    invoke_helper(COMMAND, PHRASE, authorized=True, runner=fake)
    assert fake.calls[0][0] == [
        "fake-helper",
        "--match-from-stdin",
        "--select-authorized",
    ]


def test_dry_run_accepts_exact_match_or_current_right_region() -> None:
    ready = parse_helper_output(payload(), phase="dry-run")
    assert ready.match_count == 1 and not ready.selection_attempted
    current = parse_helper_output(
        payload("CURRENT_RIGHT_REGION_MATCH"), phase="dry-run"
    )
    assert current.match_count == 0
    assert current.right_visible_text_match_count > 0

    with pytest.raises(GateBAssessmentError, match="SELECTION_NOT_EXACTLY_ONE"):
        parse_helper_output(payload(match_count=2), phase="dry-run")


def test_current_region_creates_candidate_without_second_helper(tmp_path: Path) -> None:
    fake = FakeRunner(payload("CURRENT_RIGHT_REGION_MATCH"))
    output = tmp_path / "candidate.json"
    result = assess_gate_b(
        command=COMMAND,
        selection_phrase=PHRASE,
        authorized=True,
        confirmation_id="human-confirmation-1",
        output_path=output,
        runner=fake,
    )
    assert result["application_saved"] is True
    assert len(fake.calls) == 1
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "pending_human_binding"
    assert saved["binding_created"] is False
    assert saved["binding_evidence_assessment"] == {
        "bindable": False,
        "automatic_eligible": False,
        "reason_codes": ["binding_stable_second_signal_required"],
    }
    assert "local_contact_id" not in saved
    assert PHRASE not in output.read_text(encoding="utf-8")


def test_authorized_requires_post_evidence_and_preserves_match_digest(
    tmp_path: Path,
) -> None:
    fake = FakeRunner(payload(), payload("SELECTED_WITH_POST_EVIDENCE"))
    output = tmp_path / "candidate.json"
    result = assess_gate_b(
        command=COMMAND,
        selection_phrase=PHRASE,
        authorized=True,
        confirmation_id="human-confirmation-1",
        output_path=output,
        environment_fingerprint=HASH,
        selector_pack_version="qq-selector-v1",
        runner=fake,
    )
    assert result["status"] == "SELECTED_WITH_POST_EVIDENCE"
    assert len(fake.calls) == 2
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["match_evidence_digest"] == HASH
    assert saved["right_region_evidence"]["structure_digest"] == "b" * 64
    assert saved["environment_fingerprint"] == HASH
    assert saved["selector_pack_version"] == "qq-selector-v1"
    assert saved["human_confirmation"]["required"] is True
    assert saved["human_confirmation"]["approved"] is True
    assert saved["binding_created"] is False


def test_mismatched_evidence_and_dirty_privacy_fail_closed(tmp_path: Path) -> None:
    changed = payload("SELECTED_WITH_POST_EVIDENCE", match_evidence_digest="f" * 64)
    fake = FakeRunner(payload(), changed)
    with pytest.raises(GateBAssessmentError, match="EVIDENCE_CHANGED"):
        assess_gate_b(
            command=COMMAND,
            selection_phrase=PHRASE,
            authorized=True,
            confirmation_id="human-confirmation-1",
            output_path=tmp_path / "candidate.json",
            runner=fake,
        )
    assert not (tmp_path / "candidate.json").exists()

    dirty = payload(
        emitted_target=True,
        privacy={
            "target_from_stdin_only": True,
            "emitted_target": True,
            "emitted_control_names": False,
            "emitted_chat_text": False,
            "emitted_content_hashes_are_aggregate": True,
        },
    )
    with pytest.raises(GateBAssessmentError, match="PRIVACY_CONTRACT_FAILED"):
        parse_helper_output(dirty, phase="dry-run")


def test_confirmation_and_phrase_are_not_leaked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(GateBAssessmentError, match="EMPTY_SELECTION_PHRASE"):
        read_selection_phrase(io.StringIO("\n"))
    with pytest.raises(GateBAssessmentError, match="CONFIRMATION_REQUIRED"):
        assess_gate_b(command=COMMAND, selection_phrase=PHRASE, authorized=True)
    fake = FakeRunner(payload("SELECTED_WITH_POST_EVIDENCE"))
    with pytest.raises(GateBAssessmentError, match="SELECTION_PHRASE_LEAK"):
        invoke_helper([PHRASE], PHRASE, authorized=True, runner=fake)

    monkeypatch.setattr("sys.stdin", io.StringIO(PHRASE))
    assert main(["--helper-command", "fake-helper", "--select-authorized"]) == 2
    assert PHRASE not in capsys.readouterr().out


def test_candidate_builder_does_not_create_contact() -> None:
    result = parse_helper_output(payload(), phase="dry-run")
    candidate = build_binding_application(result, "human-confirmation-1")
    assert "local_contact_id" not in candidate
    assert candidate["automatic_eligible"] is False
    assert candidate["binding_created"] is False
