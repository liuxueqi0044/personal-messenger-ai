"""Run one bounded, non-retrying DeepSeek QQ reply in a stopped VM runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(r"C:\Users\xiayu\Documents\Codex\2026-09-09\new-chat")
CONTROL = ROOT / r"outputs\qq-vm\install\control_qq_runtime_host.py"
START_HELPER = ROOT / r"outputs\qq-vm\install\start_qq_runtime_host.py"
DIAGNOSTICS = ROOT / r"outputs\qq-vm\install\diagnostics"
GUEST_PYTHON = r"C:\PMAI\app\.venv\Scripts\python.exe"
GUEST_DATA = r"C:\PMAI\data"
GUEST_CONFIG = GUEST_DATA + r"\runtime-session-1.json"
REPORT_SCHEMA = "pmai-qq-one-shot-reply-v1"
HOST_SCHEMA = "pmai-qq-one-shot-reply-host-v1"
MAX_REPORT_BYTES = 64 * 1024
_RELEASE = re.compile(r"r\d{8}-\d{2}")
_BINDING = re.compile(r"session-contact-[1-9][0-9]{0,3}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("HOST_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _terminate_guest_process_tree(guest: Any, process: Any) -> None:
    pid = getattr(process, "PID", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("GUEST_PROCESS_PID_INVALID")
    taskkill = r"C:\Windows\System32\taskkill.exe"
    killer = guest.ProcessCreate(
        taskkill,
        [taskkill, "/PID", str(pid), "/T", "/F"],
        GUEST_DATA,
        [],
        [],
        30_000,
    )
    if killer.WaitForArray([1], 30_000) != 1:
        raise RuntimeError("GUEST_PROCESS_TREE_KILL_DID_NOT_START")
    if killer.WaitForArray([2, 4], 30_000) != 2 or int(killer.ExitCode) != 0:
        raise RuntimeError("GUEST_PROCESS_TREE_KILL_FAILED")
    if process.WaitForArray([2, 4], 10_000) != 2:
        raise RuntimeError("GUEST_PROCESS_TREE_NOT_RETIRED")


def _poll_process(process: Any, *, timeout_seconds: float, guest: Any) -> int:
    if process.WaitForArray([1], 30_000) != 1:
        _terminate_guest_process_tree(guest, process)
        raise RuntimeError("GUEST_PROCESS_DID_NOT_START")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        event = process.WaitForArray([2, 4], 500)
        if event == 2:
            return int(process.ExitCode)
        if event == 4:
            _terminate_guest_process_tree(guest, process)
            raise RuntimeError("GUEST_PROCESS_WAIT_FAILED")
    _terminate_guest_process_tree(guest, process)
    raise RuntimeError("GUEST_PROCESS_TIMEOUT")


def _ensure_process_tree_retired(guest: Any, process: Any) -> None:
    """Confirm normal exit, killing the exact tree on any ambiguous state."""

    if process.WaitForArray([2, 4], 1_000) == 2:
        return
    _terminate_guest_process_tree(guest, process)


def _safe_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _safe_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value)) if str(UUID(value)) == value else None
    except ValueError:
        return None


def _safe_positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _project_handoff(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "operation_id",
        "commit_worker_process_id",
        "verify_worker_process_id",
        "commit_worker_epoch",
        "verify_health_worker_epoch",
        "verify_worker_epoch",
        "first_worker_retired",
        "verify_status",
    }
    if set(value) - allowed:
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    clean: dict[str, object] = {}
    for key in (
        "operation_id",
        "commit_worker_epoch",
        "verify_health_worker_epoch",
        "verify_worker_epoch",
    ):
        item = _safe_uuid(value.get(key))
        if item is not None:
            clean[key] = item
    for key in ("commit_worker_process_id", "verify_worker_process_id"):
        item = _safe_positive_int(value.get(key))
        if item is not None:
            clean[key] = item
    if isinstance(value.get("first_worker_retired"), bool):
        clean["first_worker_retired"] = value["first_worker_retired"]
    if value.get("verify_status") in {"ok", "failed_safe", "uncertain", "unavailable"}:
        clean["verify_status"] = value["verify_status"]
    return clean or None


def _validate_guest_report(
    value: object, *, binding_id: str, attempt_id: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")  # noqa: TRY004
    allowed = {
        "schema",
        "attempt_id",
        "binding_id",
        "status",
        "succeeded",
        "provider_called",
        "action_attempted",
        "send_action_attempted",
        "selection_action_attempted",
        "intent_recorded",
        "active_worker_retired",
        "active_worker_exit_code",
        "started_at",
        "completed_at",
        "config_sha256",
        "execution_config_sha256",
        "source_key_hashes",
        "pacing_plan_id",
        "operation_id",
        "send_status",
        "error_code",
        "handoff",
    }
    statuses = {
        "no_new_inbound",
        "duplicate_source",
        "ignored",
        "failed",
        "deferred_cancelled",
        "verified",
        "uncertain",
    }
    if (
        set(value) - allowed
        or value.get("schema") != REPORT_SCHEMA
        or value.get("attempt_id") != attempt_id
        or value.get("binding_id") != binding_id
        or value.get("status") not in statuses
        or not isinstance(value.get("succeeded"), bool)
        or value.get("provider_called") not in (True, False)
        or value.get("action_attempted") not in (True, False, None)
        or "send_action_attempted" not in value
        or "selection_action_attempted" not in value
        or value.get("send_action_attempted") not in (True, False, None)
        or value.get("selection_action_attempted") not in (True, False, None)
        or not isinstance(value.get("intent_recorded"), bool)
        or not isinstance(value.get("active_worker_retired"), bool)
    ):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    clean: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "attempt_id": attempt_id,
        "binding_id": binding_id,
        "status": value["status"],
        "succeeded": value["succeeded"],
        "provider_called": value["provider_called"],
        "action_attempted": value["action_attempted"],
        "send_action_attempted": value["send_action_attempted"],
        "selection_action_attempted": value["selection_action_attempted"],
        "intent_recorded": value["intent_recorded"],
        "active_worker_retired": value["active_worker_retired"],
    }
    for key in ("started_at", "completed_at"):
        item = _safe_time(value.get(key))
        if item is not None:
            clean[key] = item
    digest = value.get("config_sha256")
    if isinstance(digest, str) and _SHA256.fullmatch(digest):
        clean["config_sha256"] = digest
    execution_digest = value.get("execution_config_sha256")
    if isinstance(execution_digest, str) and _SHA256.fullmatch(execution_digest):
        clean["execution_config_sha256"] = execution_digest
    exit_code = value.get("active_worker_exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        clean["active_worker_exit_code"] = exit_code
    hashes = value.get("source_key_hashes", [])
    if (
        not isinstance(hashes, list)
        or len(hashes) > 32
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in hashes)
        or len(set(hashes)) != len(hashes)
    ):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    clean["source_key_hashes"] = hashes
    for key in ("pacing_plan_id", "operation_id"):
        item = _safe_uuid(value.get(key))
        if item is not None:
            clean[key] = item
    if value.get("send_status") in {
        "pending",
        "prepared",
        "committed",
        "verified",
        "cancelled",
        "send_uncertain",
        "failed",
    }:
        clean["send_status"] = value["send_status"]
    error = value.get("error_code")
    if error is not None:
        if not isinstance(error, str) or _SAFE_CODE.fullmatch(error) is None:
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
        clean["error_code"] = error
    handoff = _project_handoff(value.get("handoff"))
    if handoff is not None:
        clean["handoff"] = handoff

    if clean["succeeded"] is True:
        first_pid = handoff.get("commit_worker_process_id") if handoff else None
        second_pid = handoff.get("verify_worker_process_id") if handoff else None
        commit_epoch = handoff.get("commit_worker_epoch") if handoff else None
        health_epoch = handoff.get("verify_health_worker_epoch") if handoff else None
        verify_epoch = handoff.get("verify_worker_epoch") if handoff else None
        if not (
            clean["status"] == "verified"
            and clean["provider_called"] is True
            and clean["action_attempted"] is True
            and clean["send_action_attempted"] is True
            and clean["intent_recorded"] is True
            and clean["active_worker_retired"] is True
            and isinstance(clean.get("active_worker_exit_code"), int)
            and "config_sha256" in clean
            and "execution_config_sha256" in clean
            and len(hashes) >= 1
            and "pacing_plan_id" in clean
            and clean.get("operation_id") == (handoff or {}).get("operation_id")
            and clean.get("send_status") == "verified"
            and isinstance(first_pid, int)
            and isinstance(second_pid, int)
            and first_pid != second_pid
            and commit_epoch != verify_epoch
            and health_epoch == verify_epoch
            and (handoff or {}).get("first_worker_retired") is True
            and (handoff or {}).get("verify_status") == "ok"
        ):
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    if clean["status"] == "no_new_inbound" and not (
        clean["succeeded"] is False
        and clean["provider_called"] is False
        and clean["action_attempted"] is False
        and clean["send_action_attempted"] is False
        and clean["selection_action_attempted"] is None
        and clean["intent_recorded"] is True
        and clean["active_worker_retired"] is True
        and isinstance(clean.get("active_worker_exit_code"), int)
        and "config_sha256" in clean
        and "execution_config_sha256" in clean
        and hashes == []
        and "operation_id" not in clean
        and handoff is None
    ):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    return clean


def _runtime_is_stopped(control: Any, start: Any, guest: Any, marker: str) -> bool:
    runtime = control._runtime_status(guest, marker)
    return not (
        runtime.get("runtime_process_alive") is True
        and start._guest_pid_alive(guest, runtime.get("runtime_process_id"))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--max-wait-seconds", type=float, default=90)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if _RELEASE.fullmatch(args.release_id) is None:
        parser.error("invalid release id")
    if _BINDING.fullmatch(args.binding_id) is None:
        parser.error("invalid binding id")
    if not 1 <= args.max_wait_seconds <= 300:
        parser.error("invalid max wait")
    attempt_id = str(UUID(args.attempt_id))
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    report_path = args.report or (
        DIAGNOSTICS / f"qq-one-shot-{args.release_id}-{attempt_id}.json"
    )
    report: dict[str, object] = {
        "schema": HOST_SCHEMA,
        "release_id": args.release_id,
        "attempt_id": attempt_id,
        "binding_id": args.binding_id,
        "succeeded": False,
        "one_shot_started": False,
        "runtime_stopped_before": False,
        "runtime_stopped_after": False,
    }
    guest = host = control = start = process = None
    clear = bytearray()
    guest_report = GUEST_DATA + f"\\qq-one-shot-{attempt_id}.json"
    local_temporary = DIAGNOSTICS / f".qq-one-shot-{attempt_id}.json"
    exit_code = 2
    try:
        control = _load(CONTROL, "pmai_one_shot_control")
        start = _load(START_HELPER, "pmai_one_shot_start")
        _vbox, host, guest, clear, _password = control._open_guest()
        if not _runtime_is_stopped(control, start, guest, "one-shot-before-" + attempt_id):
            raise RuntimeError("RUNTIME_NOT_STOPPED")
        report["runtime_stopped_before"] = True
        release = rf"C:\PMAI\app\releases\{args.release_id}"
        one_shot = release + r"\qq_one_shot_deepseek_reply_guest.py"
        report["one_shot_started"] = True
        process = guest.ProcessCreate(
            GUEST_PYTHON,
            [
                GUEST_PYTHON,
                one_shot,
                "--config",
                GUEST_CONFIG,
                "--binding-id",
                args.binding_id,
                "--attempt-id",
                attempt_id,
                "--report",
                guest_report,
                "--max-wait-seconds",
                str(args.max_wait_seconds),
            ],
            GUEST_DATA,
            [],
            [],
            int((args.max_wait_seconds + 90) * 1000),
        )
        guest_exit = _poll_process(
            process,
            timeout_seconds=args.max_wait_seconds + 90,
            guest=guest,
        )
        report["guest_exit_code"] = guest_exit
        local_temporary.unlink(missing_ok=True)
        control._wait_progress(
            guest.FileCopyFromGuest(guest_report, str(local_temporary), []), 30_000
        )
        if not 2 <= local_temporary.stat().st_size <= MAX_REPORT_BYTES:
            raise RuntimeError("GUEST_REPORT_SIZE_INVALID")
        projected = _validate_guest_report(
            json.loads(local_temporary.read_text(encoding="utf-8-sig")),
            binding_id=args.binding_id,
            attempt_id=attempt_id,
        )
        report["guest_result"] = projected
        expected_exit = (
            0
            if projected["succeeded"]
            else 4
            if projected["status"] == "no_new_inbound"
            else 3
            if projected["status"] == "uncertain"
            else 2
        )
        if guest_exit != expected_exit:
            raise RuntimeError("ONE_SHOT_RESULT_EXIT_MISMATCH")
        if not _runtime_is_stopped(control, start, guest, "one-shot-after-" + attempt_id):
            raise RuntimeError("RUNTIME_NOT_STOPPED_AFTER")
        report["runtime_stopped_after"] = True
        report["succeeded"] = projected["succeeded"] is True
        exit_code = expected_exit
    except Exception as exc:  # noqa: BLE001 - host report is fail-closed
        code = str(exc)
        report["error_code"] = (
            code if _SAFE_CODE.fullmatch(code) else "HOST_ONE_SHOT_FAILED"
        )
        if report["one_shot_started"] and "guest_result" not in report:
            report["action_attempted"] = None
        exit_code = 3 if report.get("action_attempted") is None else 2
    finally:
        local_temporary.unlink(missing_ok=True)
        if guest is not None and process is not None:
            try:
                _ensure_process_tree_retired(guest, process)
            except Exception:  # noqa: BLE001 - ambiguous action must stay uncertain
                report["action_attempted"] = None
                report["succeeded"] = False
                report["error_code"] = "GUEST_PROCESS_TREE_NOT_RETIRED"
                exit_code = 3
        if (
            guest is not None
            and control is not None
            and start is not None
            and report["one_shot_started"] is True
            and report["runtime_stopped_after"] is not True
        ):
            try:
                report["runtime_stopped_after"] = _runtime_is_stopped(
                    control, start, guest, "one-shot-final-" + attempt_id
                )
            except Exception:  # noqa: BLE001 - retain fail-closed false witness
                report["runtime_stopped_after"] = False
        if guest is not None:
            try:
                guest.FileDelete(guest_report)
            except Exception:  # noqa: BLE001,S110 - best-effort secretless cleanup
                pass
        for index in range(len(clear)):
            clear[index] = 0
        if guest is not None:
            try:
                guest.Close()
            except Exception:  # noqa: BLE001,S110 - COM session cleanup
                pass
        if host is not None:
            try:
                host.UnlockMachine()
            except Exception:  # noqa: BLE001,S110 - COM session cleanup
                pass
        _atomic_json(report_path, report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
