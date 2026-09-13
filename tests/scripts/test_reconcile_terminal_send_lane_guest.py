from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "reconcile_terminal_send_lane_guest.py"
)
SPEC = importlib.util.spec_from_file_location(
    "reconcile_terminal_send_lane_guest", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OPERATOR_ID = "codex-test"
REASON_CODE = "FAILED_SAFE_NO_COMMIT"
BODY_HASH = "a" * 64
SECRET = "private message body"

# Mirror of the host's exact REPORT_FIELDS contract for a validated result v2.
HOST_REPORT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "mode",
        "settlement_id",
        "conversation_id",
        "pacing_plan_id",
        "segment_index",
        "operation_id",
        "authorization_id",
        "draft_id",
        "hub_send_outbox_id",
        "hub_stable_outbox_id",
        "hub_draft_outbox_id",
        "binding_id",
        "binding_revision",
        "conversation_revision",
        "body_hash",
        "terminal_status",
        "pacing_cancel_reason",
        "artifact_status",
        "applied",
        "idempotent",
        "evidence_sha256",
        "legacy_due_operation_id_missing",
    }
)
REJECTED_FIELDS = frozenset({"schema", "status", "mode", "error_code"})
TARGET_VALUES: dict[str, object] = {
    "mode": "dry_run",
    "settlement_id": "settlement-1",
    "conversation_id": "conversation-1",
    "pacing_plan_id": "plan-1",
    "segment_index": 0,
    "operation_id": "operation-1",
    "authorization_id": "authorization-1",
    "draft_id": "draft-1",
    "hub_send_outbox_id": "send-outbox-1",
    "hub_stable_outbox_id": "stable-outbox-1",
    "hub_draft_outbox_id": "draft-outbox-1",
    "binding_id": "binding-1",
    "binding_revision": 1,
    "conversation_revision": 3,
    "body_hash": BODY_HASH,
    "terminal_status": "failed",
    "pacing_cancel_reason": "revalidation_rejected",
}


class _Owner:
    acquired = False
    closed = False

    def acquire(self) -> None:
        type(self).acquired = True

    def close(self) -> None:
        type(self).closed = True


def _arguments(report: Path) -> list[str]:
    return [
        "reconcile_terminal_send_lane_guest.py",
        "--data-dir",
        str(report.parent),
        "--settlement-id",
        "settlement-1",
        "--operator-id",
        OPERATOR_ID,
        "--reason-code",
        REASON_CODE,
        "--conversation-id",
        "conversation-1",
        "--pacing-plan-id",
        "plan-1",
        "--segment-index",
        "0",
        "--operation-id",
        "operation-1",
        "--authorization-id",
        "authorization-1",
        "--draft-id",
        "draft-1",
        "--hub-send-outbox-id",
        "send-outbox-1",
        "--hub-stable-outbox-id",
        "stable-outbox-1",
        "--hub-draft-outbox-id",
        "draft-outbox-1",
        "--binding-id",
        "binding-1",
        "--binding-revision",
        "1",
        "--conversation-revision",
        "3",
        "--body-hash",
        BODY_HASH,
        "--report",
        str(report),
    ]


def _result(*, applied: bool = False, idempotent: bool = False):
    return SimpleNamespace(
        applied=applied,
        idempotent=idempotent,
        evidence_sha256="b" * 64,
        terminal_status="failed",
        artifact_status="rejected",
        legacy_due_operation_id_missing=True,
    )


def _stub_owner(monkeypatch) -> None:
    _Owner.acquired = _Owner.closed = False
    monkeypatch.setattr(MODULE, "QQRuntimeInstanceOwner", _Owner)


def test_target_contract_fields_match_the_host_contract() -> None:
    assert MODULE.REPORT_SCHEMA == "pmai-terminal-send-settlement-result-v2"
    assert set(MODULE.TARGET_CONTRACT_FIELDS) == set(TARGET_VALUES)
    assert set(MODULE.TARGET_CONTRACT_FIELDS) <= HOST_REPORT_FIELDS


def test_validated_report_echoes_only_the_exact_host_contract(
    tmp_path: Path, monkeypatch
) -> None:
    report = tmp_path / "result.json"
    calls = []
    _stub_owner(monkeypatch)
    monkeypatch.setattr(
        MODULE,
        "settle_terminal_send",
        lambda data_dir, target, apply: (
            calls.append((data_dir, target, apply)) or _result()
        ),
    )
    monkeypatch.setattr(sys, "argv", _arguments(report))

    assert MODULE.main() == 0
    raw = report.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert calls[0][2] is False
    assert calls[0][1].hub_draft_outbox_id == "draft-outbox-1"
    assert set(payload) == HOST_REPORT_FIELDS
    assert payload["schema"] == MODULE.REPORT_SCHEMA
    assert payload["status"] == "validated"
    assert {key: payload[key] for key in MODULE.TARGET_CONTRACT_FIELDS} == (
        TARGET_VALUES
    )
    assert payload["artifact_status"] == "rejected"
    assert payload["applied"] is False
    assert payload["idempotent"] is False
    assert payload["evidence_sha256"] == "b" * 64
    assert payload["legacy_due_operation_id_missing"] is True
    assert OPERATOR_ID not in raw and REASON_CODE not in raw
    assert _Owner.acquired is True and _Owner.closed is True


def test_apply_report_echoes_apply_mode_and_succeeded_status(
    tmp_path: Path, monkeypatch
) -> None:
    report = tmp_path / "result.json"
    _stub_owner(monkeypatch)
    monkeypatch.setattr(
        MODULE, "settle_terminal_send", lambda *_a, **_k: _result(applied=True)
    )
    monkeypatch.setattr(sys, "argv", [*_arguments(report), "--apply"])

    assert MODULE.main() == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert set(payload) == HOST_REPORT_FIELDS
    assert payload["mode"] == "apply"
    assert payload["status"] == "succeeded"
    assert payload["applied"] is True
    assert payload["idempotent"] is False
    assert {key: payload[key] for key in MODULE.TARGET_CONTRACT_FIELDS} == {
        **TARGET_VALUES,
        "mode": "apply",
    }


def test_idempotent_apply_report_echoes_the_target(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "result.json"
    _stub_owner(monkeypatch)
    monkeypatch.setattr(
        MODULE,
        "settle_terminal_send",
        lambda *_a, **_k: _result(applied=False, idempotent=True),
    )
    monkeypatch.setattr(sys, "argv", [*_arguments(report), "--apply"])

    assert MODULE.main() == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert set(payload) == HOST_REPORT_FIELDS
    assert payload["status"] == "succeeded"
    assert payload["applied"] is False
    assert payload["idempotent"] is True


def test_rejected_report_stays_minimal_and_leaks_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    report = tmp_path / "result.json"
    _stub_owner(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise MODULE.SettlementRefused("SEND_SETTLEMENT_HUB_OUTBOX_MISSING")

    monkeypatch.setattr(MODULE, "settle_terminal_send", refuse)
    monkeypatch.setattr(sys, "argv", _arguments(report))

    assert MODULE.main() == 2
    raw = report.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload) == REJECTED_FIELDS
    assert payload == {
        "schema": MODULE.REPORT_SCHEMA,
        "status": "rejected",
        "mode": "dry_run",
        "error_code": "SEND_SETTLEMENT_HUB_OUTBOX_MISSING",
    }
    assert set(payload).isdisjoint(set(MODULE.TARGET_CONTRACT_FIELDS) - {"mode"})
    assert set(payload).isdisjoint(
        {
            "artifact_status",
            "applied",
            "idempotent",
            "evidence_sha256",
            "legacy_due_operation_id_missing",
        }
    )
    assert OPERATOR_ID not in raw and REASON_CODE not in raw
    for value in ("settlement-1", "draft-outbox-1", BODY_HASH):
        assert value not in raw
    assert _Owner.acquired is True and _Owner.closed is True


def test_cli_sanitizes_failure_and_retires_owner(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "result.json"
    _stub_owner(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(MODULE, "settle_terminal_send", fail)
    monkeypatch.setattr(sys, "argv", [*_arguments(report), "--apply"])

    assert MODULE.main() == 2
    raw = report.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload == {
        "error_code": "RuntimeError",
        "mode": "apply",
        "schema": MODULE.REPORT_SCHEMA,
        "status": "rejected",
    }
    assert set(payload) == REJECTED_FIELDS
    assert SECRET not in raw
    assert OPERATOR_ID not in raw and REASON_CODE not in raw
    assert _Owner.acquired is True and _Owner.closed is True


def test_cli_requires_hub_draft_outbox_id(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "result.json"
    arguments = _arguments(report)
    index = arguments.index("--hub-draft-outbox-id")
    del arguments[index : index + 2]
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit):
        MODULE.main()
