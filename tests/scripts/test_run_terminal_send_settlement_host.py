from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).parents[2].parent
    / "qq-vm"
    / "install"
    / "run_terminal_send_settlement_host.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_terminal_send_settlement_host", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SEND_OUTBOX_ID = "00000000-0000-0000-0000-000000000105"
STABLE_OUTBOX_ID = "00000000-0000-0000-0000-000000000106"
DRAFT_OUTBOX_ID = "00000000-0000-0000-0000-000000000107"

SETTLEMENT_ID = "settlement-terminal-1"
CONVERSATION_ID = "conversation-terminal"
PACING_PLAN_ID = "00000000-0000-0000-0000-000000000101"
OPERATION_ID = "00000000-0000-0000-0000-000000000104"
AUTHORIZATION_ID = "00000000-0000-0000-0000-000000000103"
DRAFT_ID = "00000000-0000-0000-0000-000000000102"
BINDING_ID = "contact-terminal"
BINDING_REVISION = 1
CONVERSATION_REVISION = 3
BODY_HASH = "b" * 64


def _expected(**overrides: object) -> dict[str, object]:
    binding: dict[str, object] = {
        "mode": "dry_run",
        "settlement_id": SETTLEMENT_ID,
        "conversation_id": CONVERSATION_ID,
        "pacing_plan_id": PACING_PLAN_ID,
        "segment_index": 0,
        "operation_id": OPERATION_ID,
        "authorization_id": AUTHORIZATION_ID,
        "draft_id": DRAFT_ID,
        "hub_send_outbox_id": SEND_OUTBOX_ID,
        "hub_stable_outbox_id": STABLE_OUTBOX_ID,
        "hub_draft_outbox_id": DRAFT_OUTBOX_ID,
        "binding_id": BINDING_ID,
        "binding_revision": BINDING_REVISION,
        "conversation_revision": CONVERSATION_REVISION,
        "body_hash": BODY_HASH,
        "terminal_status": "failed",
        "pacing_cancel_reason": "revalidation_rejected",
    }
    binding.update(overrides)
    return binding


def _guest_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "validated",
        **_expected(),
        "artifact_status": "rejected",
        "applied": False,
        "idempotent": False,
        "evidence_sha256": "a" * 64,
        "legacy_due_operation_id_missing": True,
    }
    report.update(overrides)
    return report


def _apply_report(*, applied: bool, idempotent: bool) -> dict[str, object]:
    return _guest_report(
        mode="apply",
        status="succeeded",
        applied=applied,
        idempotent=idempotent,
    )


def test_report_field_set_is_the_declared_contract() -> None:
    assert set(_guest_report()) == MODULE.REPORT_FIELDS
    assert MODULE.HOST_SCHEMA == "pmai-terminal-send-settlement-host-v2"
    assert MODULE.GUEST_SCHEMA == "pmai-terminal-send-settlement-result-v2"


def test_accepts_report_that_names_the_exact_target() -> None:
    projected = MODULE._validated_guest_report(_guest_report(), _expected())

    assert projected["hub_draft_outbox_id"] == DRAFT_OUTBOX_ID
    assert projected["hub_send_outbox_id"] == SEND_OUTBOX_ID
    assert projected["hub_stable_outbox_id"] == STABLE_OUTBOX_ID
    assert projected["binding_id"] == BINDING_ID
    assert projected["binding_revision"] == BINDING_REVISION
    assert projected["conversation_revision"] == CONVERSATION_REVISION
    assert projected["body_hash"] == BODY_HASH
    assert projected["authorization_id"] == AUTHORIZATION_ID
    assert projected["draft_id"] == DRAFT_ID
    assert projected["pacing_cancel_reason"] == "revalidation_rejected"


@pytest.mark.parametrize(
    "field",
    ["hub_draft_outbox_id", "authorization_id", "binding_id", "body_hash"],
)
def test_missing_target_field_is_rejected(field: str) -> None:
    report = _guest_report()
    del report[field]

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_FIELDS_INVALID"):
        MODULE._validated_guest_report(report, _expected())


@pytest.mark.parametrize("value", ["", "bad id", "bad/id", 7])
def test_malformed_draft_outbox_identifier_is_rejected(value: object) -> None:
    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_IDENTIFIER_INVALID"):
        MODULE._validated_guest_report(
            _guest_report(hub_draft_outbox_id=value), _expected()
        )


def test_rejected_report_passes_through_without_extra_fields() -> None:
    rejected = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "rejected",
        "mode": "dry_run",
        "error_code": "SettlementRefused",
    }

    assert MODULE._validated_guest_report(rejected, _expected()) == rejected


def test_rejected_report_for_the_wrong_mode_is_rejected() -> None:
    """A rejected guest report must still name the mode the host commanded."""

    rejected_apply = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "rejected",
        "mode": "apply",
        "error_code": "SEND_SETTLEMENT_HUB_OUTBOX_MISSING",
    }
    rejected_dry_run = {**rejected_apply, "mode": "dry_run"}

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_TARGET_MISMATCH"):
        MODULE._validated_guest_report(rejected_apply, _expected())

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_TARGET_MISMATCH"):
        MODULE._validated_guest_report(rejected_dry_run, _expected(mode="apply"))

    assert (
        MODULE._validated_guest_report(rejected_apply, _expected(mode="apply"))
        == rejected_apply
    )


def test_v1_schema_report_is_rejected() -> None:
    report = _guest_report(schema="pmai-terminal-send-settlement-result-v1")

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_SCHEMA_MISMATCH"):
        MODULE._validated_guest_report(report, _expected())


@pytest.mark.parametrize(
    "overrides",
    [
        {"settlement_id": "settlement-other"},
        {"conversation_id": "conversation-other"},
        {"pacing_plan_id": "00000000-0000-0000-0000-000000000999"},
        {"segment_index": 1},
        {"operation_id": "00000000-0000-0000-0000-000000000998"},
        {"authorization_id": "00000000-0000-0000-0000-000000000997"},
        {"draft_id": "00000000-0000-0000-0000-000000000996"},
        {"hub_send_outbox_id": "00000000-0000-0000-0000-000000000995"},
        {"hub_stable_outbox_id": "00000000-0000-0000-0000-000000000994"},
        {"hub_draft_outbox_id": "00000000-0000-0000-0000-000000000993"},
        {"binding_id": "contact-other"},
        {"binding_revision": 2},
        {"conversation_revision": 4},
        {"body_hash": "c" * 64},
        {"terminal_status": "cancelled"},
        {"pacing_cancel_reason": "other_reason"},
    ],
)
def test_report_for_another_target_is_rejected(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_TARGET_MISMATCH"):
        MODULE._validated_guest_report(_guest_report(**overrides), _expected())


def test_report_for_the_wrong_mode_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_TARGET_MISMATCH"):
        MODULE._validated_guest_report(
            _guest_report(mode="apply", status="succeeded"), _expected()
        )

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_TARGET_MISMATCH"):
        MODULE._validated_guest_report(_guest_report(), _expected(mode="apply"))


def test_dry_run_report_claiming_applied_is_rejected() -> None:
    report = _guest_report(applied=True, idempotent=False)

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_OUTCOME_INVALID"):
        MODULE._validated_guest_report(report, _expected())


def test_dry_run_idempotent_reverification_is_accepted() -> None:
    report = _guest_report(status="succeeded", applied=False, idempotent=True)

    projected = MODULE._validated_guest_report(report, _expected())

    assert projected["applied"] is False
    assert projected["idempotent"] is True


@pytest.mark.parametrize(
    "report,expected",
    [
        (_guest_report(status="succeeded"), _expected()),
        (
            _guest_report(status="validated", applied=False, idempotent=True),
            _expected(),
        ),
        (
            {
                **_apply_report(applied=True, idempotent=False),
                "status": "validated",
            },
            _expected(mode="apply"),
        ),
    ],
)
def test_report_status_must_match_the_outcome(
    report: dict[str, object], expected: dict[str, object]
) -> None:
    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_OUTCOME_INVALID"):
        MODULE._validated_guest_report(report, expected)


@pytest.mark.parametrize("applied,idempotent", [(True, False), (False, True)])
def test_apply_report_with_legal_boolean_pair_is_accepted(
    applied: bool, idempotent: bool
) -> None:
    report = _apply_report(applied=applied, idempotent=idempotent)

    projected = MODULE._validated_guest_report(report, _expected(mode="apply"))

    assert projected["mode"] == "apply"
    assert projected["applied"] is applied
    assert projected["idempotent"] is idempotent


@pytest.mark.parametrize("applied,idempotent", [(True, True), (False, False)])
def test_apply_report_with_illegal_boolean_pair_is_rejected(
    applied: bool, idempotent: bool
) -> None:
    report = _apply_report(applied=applied, idempotent=idempotent)

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_OUTCOME_INVALID"):
        MODULE._validated_guest_report(report, _expected(mode="apply"))


class _FakeProcess:
    def __init__(self) -> None:
        self.Status = 0
        self.ExitCode = 0
        self._waits = 0

    def WaitForArray(self, _events, _timeout):
        self._waits += 1
        return 1 if self._waits == 1 else 2


class _FakeGuest:
    def __init__(self, raw_report: dict[str, object]) -> None:
        self.command: list[str] | None = None
        self._raw_report = raw_report

    def ProcessCreate(self, _python, command, *_rest):
        self.command = list(command)
        return _FakeProcess()

    def Close(self) -> None:
        pass


class _FakeHost:
    def UnlockMachine(self) -> None:
        pass


def _run_main(
    monkeypatch,
    tmp_path: Path,
    extra: tuple[str, ...] = (),
    report: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], _FakeGuest]:
    guest = _FakeGuest(dict(report) if report is not None else _guest_report())
    control = SimpleNamespace(
        _runtime_status=lambda *_a, **_k: {
            "runtime_process_alive": False,
            "runtime_process_id": 4242,
        },
        _open_guest=lambda: (None, _FakeHost(), guest, bytearray(), None),
        _copy_from_guest=lambda *_a, **_k: dict(guest._raw_report),
    )
    start_helper = SimpleNamespace(_guest_pid_alive=lambda *_a, **_k: False)
    loaded = {
        str(MODULE.CONTROL): control,
        str(MODULE.START_HELPER): start_helper,
    }
    monkeypatch.setattr(MODULE, "_load", lambda path, _name: loaded[str(path)])
    monkeypatch.setattr(MODULE, "DIAG", tmp_path)
    arguments = [
        "run_terminal_send_settlement_host.py",
        "--release-id",
        "r20260913-01",
        "--settlement-id",
        SETTLEMENT_ID,
        "--operator-id",
        "codex-test",
        "--reason-code",
        "FAILED_SAFE_NO_COMMIT",
        "--conversation-id",
        CONVERSATION_ID,
        "--pacing-plan-id",
        PACING_PLAN_ID,
        "--segment-index",
        "0",
        "--operation-id",
        OPERATION_ID,
        "--authorization-id",
        AUTHORIZATION_ID,
        "--draft-id",
        DRAFT_ID,
        "--hub-send-outbox-id",
        SEND_OUTBOX_ID,
        "--hub-stable-outbox-id",
        STABLE_OUTBOX_ID,
        "--hub-draft-outbox-id",
        DRAFT_OUTBOX_ID,
        "--binding-id",
        BINDING_ID,
        "--binding-revision",
        str(BINDING_REVISION),
        "--conversation-revision",
        str(CONVERSATION_REVISION),
        "--body-hash",
        BODY_HASH,
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    code = MODULE.main()
    reports = list(tmp_path.glob("terminal-send-settlement-*.json"))
    assert len(reports) == 1
    return code, json.loads(reports[0].read_text(encoding="utf-8")), guest


def test_main_dry_run_binds_the_exact_target(tmp_path: Path, monkeypatch) -> None:
    code, report, guest = _run_main(monkeypatch, tmp_path)

    assert code == 0
    assert report["schema"] == "pmai-terminal-send-settlement-host-v2"
    assert report["succeeded"] is True
    assert guest.command is not None
    assert "--apply" not in guest.command
    index = guest.command.index("--hub-draft-outbox-id")
    assert guest.command[index + 1] == DRAFT_OUTBOX_ID
    assert guest.command[guest.command.index("--hub-stable-outbox-id") + 1] == (
        STABLE_OUTBOX_ID
    )
    assert guest.command[guest.command.index("--binding-id") + 1] == BINDING_ID
    assert guest.command[guest.command.index("--binding-revision") + 1] == "1"
    assert guest.command[guest.command.index("--conversation-revision") + 1] == "3"
    assert guest.command[guest.command.index("--terminal-status") + 1] == "failed"
    assert (
        guest.command[guest.command.index("--pacing-cancel-reason") + 1]
        == "revalidation_rejected"
    )


def test_main_apply_succeeds_on_an_applied_report(tmp_path: Path, monkeypatch) -> None:
    code, report, guest = _run_main(
        monkeypatch,
        tmp_path,
        extra=("--apply",),
        report=_apply_report(applied=True, idempotent=False),
    )

    assert code == 0
    assert report["succeeded"] is True
    assert guest.command is not None and "--apply" in guest.command


def test_main_apply_succeeds_on_an_idempotent_report(
    tmp_path: Path, monkeypatch
) -> None:
    code, report, _ = _run_main(
        monkeypatch,
        tmp_path,
        extra=("--apply",),
        report=_apply_report(applied=False, idempotent=True),
    )

    assert code == 0
    assert report["succeeded"] is True


def test_main_apply_that_receives_a_dry_run_report_fails(
    tmp_path: Path, monkeypatch
) -> None:
    code, report, _ = _run_main(
        monkeypatch, tmp_path, extra=("--apply",), report=_guest_report()
    )

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REPORT_TARGET_MISMATCH"


def test_main_dry_run_with_an_applied_report_fails(tmp_path: Path, monkeypatch) -> None:
    code, report, _ = _run_main(
        monkeypatch,
        tmp_path,
        report=_guest_report(applied=True, idempotent=False),
    )

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REPORT_OUTCOME_INVALID"


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
@pytest.mark.parametrize(
    ("terminal_status", "artifact_status"),
    [("failed", "cancelled"), ("cancelled", "rejected")],
)
def test_report_artifact_must_match_the_terminal_status(
    mode: str, terminal_status: str, artifact_status: str
) -> None:
    """The host only accepts failed->rejected and cancelled->cancelled."""

    report = _guest_report(
        mode=mode,
        status="validated" if mode == "dry_run" else "succeeded",
        applied=False,
        idempotent=mode == "apply",
        terminal_status=terminal_status,
        artifact_status=artifact_status,
    )
    expected = _expected(mode=mode, terminal_status=terminal_status)

    with pytest.raises(RuntimeError, match="SETTLEMENT_REPORT_ARTIFACT_INVALID"):
        MODULE._validated_guest_report(report, expected)


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
def test_main_rejects_an_artifact_status_that_contradicts_terminal(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    """A failed settlement that reports a cancelled artifact must not pass."""

    report = (
        _apply_report(applied=True, idempotent=False)
        if mode == "apply"
        else _guest_report()
    )
    report["artifact_status"] = "cancelled"

    code, host_report, _ = _run_main(
        monkeypatch,
        tmp_path,
        extra=("--apply",) if mode == "apply" else (),
        report=report,
    )

    assert code == 2
    assert host_report["succeeded"] is False
    assert host_report["error_code"] == "SETTLEMENT_REPORT_ARTIFACT_INVALID"


@pytest.mark.parametrize("applied,idempotent", [(True, True), (False, False)])
def test_main_apply_with_an_illegal_boolean_pair_fails(
    tmp_path: Path, monkeypatch, applied: bool, idempotent: bool
) -> None:
    code, report, _ = _run_main(
        monkeypatch,
        tmp_path,
        extra=("--apply",),
        report=_apply_report(applied=applied, idempotent=idempotent),
    )

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REPORT_OUTCOME_INVALID"


def test_main_passes_through_a_rejected_guest_report(
    tmp_path: Path, monkeypatch
) -> None:
    rejected = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "rejected",
        "mode": "dry_run",
        "error_code": "SEND_SETTLEMENT_HUB_OUTBOX_MISSING",
    }

    code, report, _ = _run_main(monkeypatch, tmp_path, report=rejected)

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REJECTED"
    assert report["guest_result"] == rejected


def test_main_apply_that_receives_a_rejected_dry_run_report_fails(
    tmp_path: Path, monkeypatch
) -> None:
    rejected = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "rejected",
        "mode": "dry_run",
        "error_code": "SEND_SETTLEMENT_HUB_OUTBOX_MISSING",
    }

    code, report, _ = _run_main(
        monkeypatch, tmp_path, extra=("--apply",), report=rejected
    )

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REPORT_TARGET_MISMATCH"
    assert "guest_result" not in report


def test_main_dry_run_that_receives_a_rejected_apply_report_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A rejected apply report must not be accepted while the host dry runs."""

    rejected = {
        "schema": MODULE.GUEST_SCHEMA,
        "status": "rejected",
        "mode": "apply",
        "error_code": "SEND_SETTLEMENT_HUB_OUTBOX_MISSING",
    }

    code, report, _ = _run_main(monkeypatch, tmp_path, report=rejected)

    assert code == 2
    assert report["succeeded"] is False
    assert report["error_code"] == "SETTLEMENT_REPORT_TARGET_MISMATCH"
    assert "guest_result" not in report


def test_main_requires_the_draft_outbox_flag(tmp_path: Path, monkeypatch) -> None:
    arguments = [
        "run_terminal_send_settlement_host.py",
        "--release-id",
        "r20260913-01",
        "--settlement-id",
        SETTLEMENT_ID,
        "--operator-id",
        "codex-test",
        "--reason-code",
        "FAILED_SAFE_NO_COMMIT",
        "--conversation-id",
        CONVERSATION_ID,
        "--pacing-plan-id",
        PACING_PLAN_ID,
        "--segment-index",
        "0",
        "--operation-id",
        OPERATION_ID,
        "--authorization-id",
        AUTHORIZATION_ID,
        "--draft-id",
        DRAFT_ID,
        "--hub-send-outbox-id",
        SEND_OUTBOX_ID,
        "--hub-stable-outbox-id",
        STABLE_OUTBOX_ID,
        "--binding-id",
        BINDING_ID,
        "--binding-revision",
        str(BINDING_REVISION),
        "--conversation-revision",
        str(CONVERSATION_REVISION),
        "--body-hash",
        BODY_HASH,
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit):
        MODULE.main()
