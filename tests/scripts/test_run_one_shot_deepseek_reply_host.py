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
    / "run_one_shot_deepseek_reply_host.py"
)
SPEC = importlib.util.spec_from_file_location("run_one_shot_reply_host", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ATTEMPT = "11111111-1111-4111-8111-111111111111"
BINDING = "session-contact-2"
OPERATION = "22222222-2222-4222-8222-222222222222"


def _verified_report() -> dict[str, object]:
    return {
        "schema": MODULE.REPORT_SCHEMA,
        "attempt_id": ATTEMPT,
        "binding_id": BINDING,
        "status": "verified",
        "succeeded": True,
        "provider_called": True,
        "action_attempted": True,
        "intent_recorded": True,
        "active_worker_retired": True,
        "active_worker_exit_code": 0,
        "config_sha256": "a" * 64,
        "execution_config_sha256": "c" * 64,
        "source_key_hashes": ["b" * 64],
        "pacing_plan_id": "33333333-3333-4333-8333-333333333333",
        "operation_id": OPERATION,
        "send_status": "verified",
        "handoff": {
            "operation_id": OPERATION,
            "commit_worker_process_id": 101,
            "verify_worker_process_id": 202,
            "commit_worker_epoch": "44444444-4444-4444-8444-444444444444",
            "verify_health_worker_epoch": "55555555-5555-4555-8555-555555555555",
            "verify_worker_epoch": "55555555-5555-4555-8555-555555555555",
            "first_worker_retired": True,
            "verify_status": "ok",
        },
    }


def test_verified_report_requires_distinct_retired_worker_handoff() -> None:
    projected = MODULE._validate_guest_report(
        _verified_report(), binding_id=BINDING, attempt_id=ATTEMPT
    )
    assert projected["succeeded"] is True

    report = _verified_report()
    report["handoff"]["verify_worker_process_id"] = 101
    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


def test_no_new_inbound_report_proves_no_api_or_action() -> None:
    report = {
        "schema": MODULE.REPORT_SCHEMA,
        "attempt_id": ATTEMPT,
        "binding_id": BINDING,
        "status": "no_new_inbound",
        "succeeded": False,
        "provider_called": False,
        "action_attempted": False,
        "intent_recorded": True,
        "active_worker_retired": True,
        "active_worker_exit_code": 0,
        "config_sha256": "a" * 64,
        "execution_config_sha256": "c" * 64,
        "source_key_hashes": [],
    }
    projected = MODULE._validate_guest_report(
        report, binding_id=BINDING, attempt_id=ATTEMPT
    )
    assert projected["status"] == "no_new_inbound"


def test_report_rejects_unknown_content_bearing_field() -> None:
    report = _verified_report()
    report["reply_text"] = "must-not-export"
    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


def test_nonterminal_report_is_never_a_completed_result() -> None:
    report = {
        "schema": MODULE.REPORT_SCHEMA,
        "attempt_id": ATTEMPT,
        "binding_id": BINDING,
        "status": "running_once",
        "succeeded": False,
        "provider_called": False,
        "action_attempted": False,
        "intent_recorded": True,
        "active_worker_retired": False,
        "source_key_hashes": [],
    }
    with pytest.raises(RuntimeError, match="SCHEMA_INVALID"):
        MODULE._validate_guest_report(
            report, binding_id=BINDING, attempt_id=ATTEMPT
        )


def test_timeout_kills_exact_guest_process_tree() -> None:
    class Target:
        PID = 4123

        def WaitForArray(self, _events, _timeout):
            return 2

    class Killer:
        ExitCode = 0

        def __init__(self) -> None:
            self.calls = 0

        def WaitForArray(self, _events, _timeout):
            self.calls += 1
            return 1 if self.calls == 1 else 2

    class Guest:
        def __init__(self) -> None:
            self.created = None

        def ProcessCreate(self, *args):
            self.created = args
            return Killer()

    guest = Guest()
    MODULE._terminate_guest_process_tree(guest, Target())
    assert guest.created[0].endswith("taskkill.exe")
    assert guest.created[1][1:] == ["/PID", "4123", "/T", "/F"]


def test_ambiguous_normal_exit_is_killed_in_final_cleanup(monkeypatch) -> None:
    calls = []

    class Target:
        def WaitForArray(self, events, timeout):
            calls.append((events, timeout))
            return 4

    monkeypatch.setattr(
        MODULE,
        "_terminate_guest_process_tree",
        lambda guest, process: calls.append((guest, process)),
    )
    guest = object()
    target = Target()

    MODULE._ensure_process_tree_retired(guest, target)

    assert calls == [([2, 4], 1_000), (guest, target)]


def test_start_wait_failure_kills_exact_guest_process(monkeypatch) -> None:
    calls = []

    class Target:
        def WaitForArray(self, events, timeout):
            calls.append((events, timeout))
            return 4

    guest = object()
    target = Target()
    monkeypatch.setattr(
        MODULE,
        "_terminate_guest_process_tree",
        lambda actual_guest, actual_process: calls.append(
            (actual_guest, actual_process)
        ),
    )

    with pytest.raises(RuntimeError, match="DID_NOT_START"):
        MODULE._poll_process(target, timeout_seconds=1, guest=guest)

    assert calls == [([1], 30_000), (guest, target)]


def test_projection_does_not_emit_extra_fields() -> None:
    projected = MODULE._validate_guest_report(
        _verified_report(), binding_id=BINDING, attempt_id=ATTEMPT
    )
    assert "reply_text" not in json.dumps(projected)
