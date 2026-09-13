from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).parents[2].parent
    / "qq-vm"
    / "install"
    / "run_visual_selection_acceptance_host.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_visual_selection_acceptance_host", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


ATTEMPT = "11111111-1111-4111-8111-111111111111"
BINDING = "session-contact-2"


@pytest.mark.parametrize(
    "binding_id",
    ["session-contact-1", "session-contact-2", "session-contact-3"],
)
def test_rotation_binding_ids_are_explicit_and_well_formed(binding_id: str) -> None:
    assert MODULE._BINDING.fullmatch(binding_id) is not None


def _successful_report():
    return {
        "schema": MODULE.REPORT_SCHEMA,
        "attempt_id": ATTEMPT,
        "binding_id": BINDING,
        "status": "succeeded",
        "succeeded": True,
        "action_attempted": True,
        "fresh_process_verified": True,
        "first_worker_retired": True,
        "second_worker_retired": True,
        "intent_recorded": True,
        "first_worker_process_id": 301,
        "second_worker_process_id": 302,
        "config_sha256": "a" * 64,
        "stages": [
            {
                "stage": "first_health",
                "kind": "health",
                "status": "ok",
                "worker_epoch": "22222222-2222-4222-8222-222222222222",
                "process_id": 101,
                "window_handle": 202,
            },
            {
                "stage": "select_once",
                "kind": "select_only",
                "status": "failed_safe",
                "worker_epoch": "22222222-2222-4222-8222-222222222222",
                "error_code": "selection_process_refresh_required",
                "frame_sha256": "b" * 64,
                "model": "deepseek-v4-flash-vision-exp",
                "latency_ms": 41,
                "visual_decision": "match",
                "visual_reason": "exact_label",
                "visual_confidence": 0.99,
                "normalized_label_match": True,
                "target_label": "must-not-export",
            },
            {
                "stage": "second_health",
                "kind": "health",
                "status": "ok",
                "worker_epoch": "33333333-3333-4333-8333-333333333333",
                "process_id": 101,
                "window_handle": 202,
            },
            {
                "stage": "fresh_process_verify",
                "kind": "verify_selection_only",
                "status": "ok",
                "worker_epoch": "33333333-3333-4333-8333-333333333333",
                "selection_confirmed": True,
                "target_identity": {
                    "binding_id": BINDING,
                    "conversation_type": "direct",
                    "process_id": 101,
                    "window_handle": 202,
                    "selected_row_runtime_id_hash": "c" * 64,
                    "header_digest": "d" * 64,
                    "group_marker_count": 0,
                    "message_text": "must-not-export",
                },
            },
        ],
    }


def test_guest_report_projection_keeps_only_structural_evidence() -> None:
    projected = MODULE._validate_guest_report(
        _successful_report(), binding_id=BINDING, attempt_id=ATTEMPT
    )

    serialized = json.dumps(projected)
    assert projected["succeeded"] is True
    assert "must-not-export" not in serialized
    assert "target_label" not in serialized
    assert projected["stages"][1]["visual_decision"] == "match"
    assert projected["stages"][1]["visual_reason"] == "exact_label"
    assert projected["stages"][1]["visual_confidence"] == 0.99
    assert projected["stages"][1]["normalized_label_match"] is True
    assert projected["stages"][-1]["target_identity"]["group_marker_count"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_attempted", False),
        ("fresh_process_verified", False),
        ("first_worker_retired", False),
        ("second_worker_retired", False),
        ("intent_recorded", False),
    ],
)
def test_success_report_requires_the_complete_gate(field, value) -> None:
    report = _successful_report()
    report[field] = value

    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


@pytest.mark.parametrize("mutation", ["same_pid", "same_epoch", "wrong_order"])
def test_success_report_requires_distinct_retired_workers_and_exact_stages(
    mutation: str,
) -> None:
    report = _successful_report()
    if mutation == "same_pid":
        report["second_worker_process_id"] = report["first_worker_process_id"]
    elif mutation == "same_epoch":
        report["stages"][2]["worker_epoch"] = report["stages"][0]["worker_epoch"]
        report["stages"][3]["worker_epoch"] = report["stages"][0]["worker_epoch"]
    else:
        report["stages"][1], report["stages"][2] = (
            report["stages"][2],
            report["stages"][1],
        )

    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


def test_visual_labels_are_complete_cli_values_but_never_a_report_field() -> None:
    assert MODULE._parse_labels(["3=联系人丙", "1=联系人甲", "2=联系人乙"]) == [
        "1=联系人甲",
        "2=联系人乙",
        "3=联系人丙",
    ]
    with pytest.raises(ValueError, match="VISUAL_LABEL_INVALID"):
        MODULE._parse_labels(["2=联系人乙", "2=另一人"])
    with pytest.raises(ValueError, match="VISUAL_LABEL_INVALID"):
        MODULE._parse_labels(["2=联系人乙\n注入"])


def test_unknown_top_level_guest_field_is_rejected() -> None:
    report = _successful_report()
    report["api_key"] = "must-not-export"

    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


def test_timeout_kills_the_exact_guest_process_tree() -> None:
    class Target:
        PID = 4123

        def __init__(self) -> None:
            self.waits = []

        def WaitForArray(self, events, timeout):
            self.waits.append((events, timeout))
            return 2

    class Killer:
        ExitCode = 0

        def __init__(self) -> None:
            self.calls = 0

        def WaitForArray(self, events, _timeout):
            self.calls += 1
            return 1 if self.calls == 1 else 2

    class Guest:
        def __init__(self) -> None:
            self.created = None
            self.killer = Killer()

        def ProcessCreate(self, *args):
            self.created = args
            return self.killer

    target = Target()
    guest = Guest()

    MODULE._terminate_guest_process_tree(guest, target)

    assert guest.created[0].endswith("taskkill.exe")
    assert guest.created[1][1:] == ["/PID", "4123", "/T", "/F"]
    assert target.waits == [([2, 4], 10_000)]
