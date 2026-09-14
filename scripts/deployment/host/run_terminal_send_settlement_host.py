"""Run one frozen terminal-send settlement command through GuestSession."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(r"C:\Users\xiayu\Documents\Codex\2026-09-09\new-chat")
CONTROL = ROOT / r"outputs\qq-vm\install\control_qq_runtime_host.py"
START_HELPER = ROOT / r"outputs\qq-vm\install\start_qq_runtime_host.py"
DIAG = ROOT / r"outputs\qq-vm\install\diagnostics"
GUEST_PYTHON = r"C:\PMAI\app\.venv\Scripts\python.exe"
GUEST_DATA = r"C:\PMAI\data"
GUEST_RUNTIME_ROOT = GUEST_DATA + r"\runtime\qq-default-account"
HOST_SCHEMA = "pmai-terminal-send-settlement-host-v2"
GUEST_SCHEMA = "pmai-terminal-send-settlement-result-v2"
TERMINAL_STATUS = "failed"
PACING_CANCEL_REASON = "revalidation_rejected"
_RELEASE = re.compile(r"r\d{8}-\d{2}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")

# Every field the guest must report back so the host can prove the report
# describes the exact target and outcome it commanded.
_REPORT_BINDING_FIELDS = (
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
)
_REPORT_IDENTIFIER_FIELDS = (
    "settlement_id",
    "conversation_id",
    "pacing_plan_id",
    "operation_id",
    "authorization_id",
    "draft_id",
    "hub_send_outbox_id",
    "hub_stable_outbox_id",
    "hub_draft_outbox_id",
    "binding_id",
)
_REPORT_COORDINATE_FIELDS = (
    "segment_index",
    "binding_revision",
    "conversation_revision",
)
_REPORT_RESULT_FIELDS = (
    "artifact_status",
    "applied",
    "idempotent",
    "evidence_sha256",
    "legacy_due_operation_id_missing",
)
REPORT_FIELDS = frozenset(
    set(_REPORT_BINDING_FIELDS) | set(_REPORT_RESULT_FIELDS) | {"schema", "status"}
)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(name.upper() + "_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _assert_runtime_stopped(
    control: Any, start_helper: Any, guest: Any, suffix: str
) -> None:
    status = control._runtime_status(guest, "settlement-before-" + suffix)
    if status.get("runtime_process_alive") is not False:
        raise RuntimeError("RUNTIME_NOT_STOPPED")
    pid = status.get("runtime_process_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("RUNTIME_PID_UNAVAILABLE")
    if start_helper._guest_pid_alive(guest, pid):
        raise RuntimeError("RUNTIME_NOT_STOPPED")


def _validated_guest_report(
    raw: object, expected: dict[str, object]
) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("schema") != GUEST_SCHEMA:
        raise RuntimeError("SETTLEMENT_REPORT_SCHEMA_MISMATCH")
    if raw.get("status") == "rejected":
        if set(raw) != {"schema", "status", "mode", "error_code"}:
            raise RuntimeError("SETTLEMENT_REPORT_FIELDS_INVALID")
        if raw.get("mode") not in {"dry_run", "apply"}:
            raise RuntimeError("SETTLEMENT_REPORT_MODE_INVALID")
        if raw.get("mode") != expected.get("mode"):
            raise RuntimeError("SETTLEMENT_REPORT_TARGET_MISMATCH")
        error = raw.get("error_code")
        if not isinstance(error, str) or _SAFE_CODE.fullmatch(error) is None:
            raise RuntimeError("SETTLEMENT_REPORT_ERROR_INVALID")
        return dict(raw)
    if set(raw) != REPORT_FIELDS or raw.get("status") not in {
        "validated",
        "succeeded",
    }:
        raise RuntimeError("SETTLEMENT_REPORT_FIELDS_INVALID")
    if raw.get("mode") not in {"dry_run", "apply"}:
        raise RuntimeError("SETTLEMENT_REPORT_MODE_INVALID")
    for field in _REPORT_IDENTIFIER_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise RuntimeError("SETTLEMENT_REPORT_IDENTIFIER_INVALID")
    for field in _REPORT_COORDINATE_FIELDS:
        value = raw.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("SETTLEMENT_REPORT_COORDINATE_INVALID")
    if raw.get("terminal_status") not in {"failed", "cancelled"}:
        raise RuntimeError("SETTLEMENT_REPORT_STATUS_INVALID")
    if raw.get("artifact_status") not in {"rejected", "cancelled"}:
        raise RuntimeError("SETTLEMENT_REPORT_ARTIFACT_INVALID")
    for field in ("applied", "idempotent", "legacy_due_operation_id_missing"):
        if not isinstance(raw.get(field), bool):
            raise TypeError("SETTLEMENT_REPORT_BOOLEAN_INVALID")
    evidence = raw.get("evidence_sha256")
    if not isinstance(evidence, str) or _SHA256.fullmatch(evidence) is None:
        raise RuntimeError("SETTLEMENT_REPORT_HASH_INVALID")
    for field in _REPORT_BINDING_FIELDS:
        if raw.get(field) != expected.get(field):
            raise RuntimeError("SETTLEMENT_REPORT_TARGET_MISMATCH")
    expected_artifact = {
        "failed": "rejected",
        "cancelled": "cancelled",
    }[str(raw["terminal_status"])]
    if raw.get("artifact_status") != expected_artifact:
        raise RuntimeError("SETTLEMENT_REPORT_ARTIFACT_INVALID")
    applied = raw.get("applied")
    idempotent = raw.get("idempotent")
    if expected.get("mode") == "dry_run":
        outcome_ok = applied is False
    else:
        outcome_ok = (applied is True and idempotent is False) or (
            applied is False and idempotent is True
        )
    if not outcome_ok:
        raise RuntimeError("SETTLEMENT_REPORT_OUTCOME_INVALID")
    expected_status = "succeeded" if applied or idempotent else "validated"
    if raw.get("status") != expected_status:
        raise RuntimeError("SETTLEMENT_REPORT_OUTCOME_INVALID")
    return {key: raw[key] for key in sorted(REPORT_FIELDS)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--settlement-id", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--pacing-plan-id", required=True)
    parser.add_argument("--segment-index", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--hub-send-outbox-id", required=True)
    parser.add_argument("--hub-stable-outbox-id", required=True)
    parser.add_argument("--hub-draft-outbox-id", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--binding-revision", type=int, required=True)
    parser.add_argument("--conversation-revision", type=int, required=True)
    parser.add_argument("--body-hash", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if _RELEASE.fullmatch(args.release_id) is None:
        parser.error("bad release id")
    for name in (
        "settlement_id",
        "operator_id",
        "reason_code",
        "conversation_id",
        "pacing_plan_id",
        "operation_id",
        "authorization_id",
        "draft_id",
        "hub_send_outbox_id",
        "hub_stable_outbox_id",
        "hub_draft_outbox_id",
        "binding_id",
    ):
        if _IDENTIFIER.fullmatch(getattr(args, name)) is None:
            parser.error("bad " + name.replace("_", "-"))
    if _SHA256.fullmatch(args.body_hash) is None:
        parser.error("bad body hash")

    mode = "apply" if args.apply else "dry_run"
    expected: dict[str, object] = {
        "mode": mode,
        "settlement_id": args.settlement_id,
        "conversation_id": args.conversation_id,
        "pacing_plan_id": args.pacing_plan_id,
        "segment_index": args.segment_index,
        "operation_id": args.operation_id,
        "authorization_id": args.authorization_id,
        "draft_id": args.draft_id,
        "hub_send_outbox_id": args.hub_send_outbox_id,
        "hub_stable_outbox_id": args.hub_stable_outbox_id,
        "hub_draft_outbox_id": args.hub_draft_outbox_id,
        "binding_id": args.binding_id,
        "binding_revision": args.binding_revision,
        "conversation_revision": args.conversation_revision,
        "body_hash": args.body_hash,
        "terminal_status": TERMINAL_STATUS,
        "pacing_cancel_reason": PACING_CANCEL_REASON,
    }
    nonce = uuid.uuid4().hex
    report: dict[str, object] = {
        "schema": HOST_SCHEMA,
        "release_id": args.release_id,
        "mode": mode,
        "succeeded": False,
    }
    guest = host = None
    clear = bytearray()
    guest_report = GUEST_DATA + rf"\terminal-send-settlement-{nonce}.json"
    host_guest_report = DIAG / (".terminal-send-settlement-" + nonce + ".tmp.json")
    try:
        control = _load(CONTROL, "pmai_terminal_settlement_control")
        start_helper = _load(START_HELPER, "pmai_terminal_settlement_start")
        _vbox, host, guest, clear, _password = control._open_guest()
        _assert_runtime_stopped(control, start_helper, guest, nonce)
        script = (
            rf"C:\PMAI\app\releases\{args.release_id}"
            r"\reconcile_terminal_send_lane_guest.py"
        )
        command = [
            GUEST_PYTHON,
            script,
            "--data-dir",
            GUEST_RUNTIME_ROOT,
            "--settlement-id",
            args.settlement_id,
            "--operator-id",
            args.operator_id,
            "--reason-code",
            args.reason_code,
            "--conversation-id",
            args.conversation_id,
            "--pacing-plan-id",
            args.pacing_plan_id,
            "--segment-index",
            str(args.segment_index),
            "--operation-id",
            args.operation_id,
            "--authorization-id",
            args.authorization_id,
            "--draft-id",
            args.draft_id,
            "--hub-send-outbox-id",
            args.hub_send_outbox_id,
            "--hub-stable-outbox-id",
            args.hub_stable_outbox_id,
            "--hub-draft-outbox-id",
            args.hub_draft_outbox_id,
            "--binding-id",
            args.binding_id,
            "--binding-revision",
            str(args.binding_revision),
            "--conversation-revision",
            str(args.conversation_revision),
            "--body-hash",
            args.body_hash,
            "--terminal-status",
            TERMINAL_STATUS,
            "--pacing-cancel-reason",
            PACING_CANCEL_REASON,
            "--report",
            guest_report,
        ]
        if args.apply:
            command.append("--apply")
        process = guest.ProcessCreate(
            GUEST_PYTHON, command, GUEST_DATA, [], [], 120_000
        )
        if process.WaitForArray([1], 30_000) != 1:
            raise RuntimeError("SETTLEMENT_DID_NOT_START")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = process.WaitForArray([2, 4], 500)
            if state == 2:
                report["process_status"] = int(process.Status)
                report["exit_code"] = int(process.ExitCode)
                break
            if state == 4:
                raise RuntimeError("SETTLEMENT_WAIT_FAILED")
        else:
            raise RuntimeError("SETTLEMENT_TIMEOUT")
        raw = control._copy_from_guest(guest, guest_report, host_guest_report)
        guest_result = _validated_guest_report(raw, expected)
        report["guest_result"] = guest_result
        if int(report["exit_code"]) != 0 or guest_result.get("status") == "rejected":
            raise RuntimeError("SETTLEMENT_REJECTED")
        report["succeeded"] = True
        return 0
    except Exception as exc:  # noqa: BLE001 - host report must fail closed
        value = str(exc)
        report["error_code"] = (
            value if _SAFE_CODE.fullmatch(value) else type(exc).__name__
        )
        return 2
    finally:
        try:
            host_guest_report.unlink()
        except FileNotFoundError:
            pass
        for index in range(len(clear)):
            clear[index] = 0
        if guest is not None:
            with contextlib.suppress(Exception):
                guest.Close()
        if host is not None:
            with contextlib.suppress(Exception):
                host.UnlockMachine()
        _atomic_json(
            DIAG / ("terminal-send-settlement-" + args.release_id + "-" + nonce + ".json"),
            report,
        )


if __name__ == "__main__":
    raise SystemExit(main())
