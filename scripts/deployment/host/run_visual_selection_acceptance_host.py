"""Run one frozen visual-selection acceptance gate through GuestSession."""

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
GUEST_RUNTIME_DATA = GUEST_DATA + r"\runtime\qq-default-account"
REPORT_SCHEMA = "pmai-qq-visual-selection-acceptance-v1"
HOST_SCHEMA = "pmai-qq-visual-selection-acceptance-host-v1"
MAX_REPORT_BYTES = 64 * 1024
_RELEASE = re.compile(r"r\d{8}-\d{2}")
_BINDING = re.compile(r"session-contact-[1-9][0-9]{0,3}")
_LABEL = re.compile(r"([1-9][0-9]{0,3})=(.+)", re.DOTALL)
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


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


def _parse_labels(values: list[str]) -> list[str]:
    labels: dict[int, str] = {}
    for value in values:
        match = _LABEL.fullmatch(value)
        if match is None:
            raise ValueError("VISUAL_LABEL_INVALID")
        index = int(match.group(1))
        label = match.group(2)
        if index in labels or len(label) > 96 or not label.strip():
            raise ValueError("VISUAL_LABEL_INVALID")
        if any(ord(character) < 32 for character in label):
            raise ValueError("VISUAL_LABEL_INVALID")
        labels[index] = label
    if not labels:
        raise ValueError("VISUAL_LABELS_REQUIRED")
    return [f"{index}={labels[index]}" for index in sorted(labels)]


def _terminate_guest_process_tree(guest: Any, process: Any) -> None:
    """Kill an exact timed-out guest process and all of its descendants."""

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
        raise RuntimeError("GUEST_PROCESS_DID_NOT_START")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        event = process.WaitForArray([2, 4], 500)
        if event == 2:
            return int(process.ExitCode)
        if event == 4:
            raise RuntimeError("GUEST_PROCESS_WAIT_FAILED")
    _terminate_guest_process_tree(guest, process)
    raise RuntimeError("GUEST_PROCESS_TIMEOUT")


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _safe_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _safe_identity(value: object, binding_id: str) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("binding_id") != binding_id:
        return {}
    clean: dict[str, object] = {"binding_id": binding_id}
    if value.get("conversation_type") == "direct":
        clean["conversation_type"] = "direct"
    for key in ("process_id", "window_handle"):
        item = _safe_int(value.get(key))
        if item is not None:
            clean[key] = item
    marker = value.get("group_marker_count")
    if isinstance(marker, int) and not isinstance(marker, bool) and 0 <= marker <= 1000:
        clean["group_marker_count"] = marker
    for key in ("selected_row_runtime_id_hash", "header_digest"):
        item = value.get(key)
        if isinstance(item, str) and _SHA256.fullmatch(item):
            clean[key] = item
    return clean


def _validate_guest_report(value: object, *, binding_id: str, attempt_id: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    allowed = {
        "schema", "attempt_id", "binding_id", "status", "succeeded",
        "action_attempted", "fresh_process_verified", "started_at",
        "completed_at", "config_sha256", "intent_recorded",
        "first_worker_process_id", "second_worker_process_id", "stages",
        "first_worker_retired", "second_worker_retired",
        "error_code",
    }
    if (
        set(value) - allowed
        or value.get("schema") != REPORT_SCHEMA
        or value.get("binding_id") != binding_id
        or value.get("attempt_id") != attempt_id
        or not isinstance(value.get("succeeded"), bool)
        or value.get("action_attempted") not in (True, False, None)
        or not isinstance(value.get("fresh_process_verified"), bool)
        or not isinstance(value.get("first_worker_retired"), bool)
        or not isinstance(value.get("second_worker_retired"), bool)
    ):
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    clean: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "attempt_id": attempt_id,
        "binding_id": binding_id,
        "status": value.get("status") if value.get("status") in {
            "initializing", "intent_recorded", "selection_request_pending",
            "action_attempted", "succeeded", "rejected", "uncertain",
        } else "invalid",
        "succeeded": value["succeeded"],
        "action_attempted": value["action_attempted"],
        "fresh_process_verified": value["fresh_process_verified"],
        "first_worker_retired": value["first_worker_retired"],
        "second_worker_retired": value["second_worker_retired"],
    }
    if clean["status"] == "invalid":
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    for key in ("started_at", "completed_at"):
        item = _safe_time(value.get(key))
        if item is not None:
            clean[key] = item
    digest = value.get("config_sha256")
    if isinstance(digest, str) and _SHA256.fullmatch(digest):
        clean["config_sha256"] = digest
    if value.get("intent_recorded") is True:
        clean["intent_recorded"] = True
    for key in ("first_worker_process_id", "second_worker_process_id"):
        item = _safe_int(value.get(key))
        if item is not None:
            clean[key] = item
    error = value.get("error_code")
    if isinstance(error, str) and _SAFE_CODE.fullmatch(error):
        clean["error_code"] = error
    raw_stages = value.get("stages")
    if not isinstance(raw_stages, list) or len(raw_stages) > 8:
        raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    stages: list[dict[str, object]] = []
    for stage in raw_stages:
        if not isinstance(stage, dict):
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
        row: dict[str, object] = {}
        stage_name = stage.get("stage")
        kind = stage.get("kind")
        worker_status = stage.get("status")
        if stage_name not in {
            "first_health", "select_once", "second_health", "fresh_process_verify"
        } or kind not in {
            "health", "select_only", "verify_selection_only"
        } or worker_status not in {
            "ok", "failed_safe", "uncertain", "unavailable"
        }:
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
        row.update({"stage": stage_name, "kind": kind, "status": worker_status})
        worker_epoch = stage.get("worker_epoch")
        try:
            if not isinstance(worker_epoch, str) or str(UUID(worker_epoch)) != worker_epoch:
                raise ValueError
        except ValueError as exc:
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID") from exc
        row["worker_epoch"] = worker_epoch
        error_code = stage.get("error_code")
        if error_code is not None:
            if not isinstance(error_code, str) or _SAFE_CODE.fullmatch(error_code) is None:
                raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
            row["error_code"] = error_code
        model = stage.get("model")
        if model is not None:
            if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
                raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
            row["model"] = model
        for key in ("process_id", "window_handle", "latency_ms"):
            item = stage.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                row[key] = item
        for key, allowed_values in {
            "visual_decision": {"match", "not_match", "ambiguous"},
            "visual_reason": {
                "exact_label", "different_label", "label_missing",
                "multiple_primary_labels", "unreadable",
            },
            "provider_error_category": {
                "rate_limited", "server_error", "timeout", "network",
                "schema", "rejected", "unknown",
            },
        }.items():
            item = stage.get(key)
            if item in allowed_values:
                row[key] = item
        confidence = stage.get("visual_confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
        ):
            row["visual_confidence"] = float(confidence)
        label_match = stage.get("normalized_label_match")
        if isinstance(label_match, bool):
            row["normalized_label_match"] = label_match
        frame = stage.get("frame_sha256")
        if isinstance(frame, str) and _SHA256.fullmatch(frame):
            row["frame_sha256"] = frame
        if stage.get("selection_confirmed") is True:
            row["selection_confirmed"] = True
        identity = _safe_identity(stage.get("target_identity"), binding_id)
        if identity:
            row["target_identity"] = identity
        stages.append(row)
    clean["stages"] = stages
    if clean["succeeded"] is True:
        expected_stages = [
            ("first_health", "health", "ok"),
            ("select_once", "select_only", "failed_safe"),
            ("second_health", "health", "ok"),
            ("fresh_process_verify", "verify_selection_only", "ok"),
        ]
        actual_stages = [
            (item.get("stage"), item.get("kind"), item.get("status"))
            for item in stages
        ]
        selection = stages[1] if len(stages) == 4 else {}
        verification = stages[3] if len(stages) == 4 else {}
        identity = verification.get("target_identity", {})
        first_pid = clean.get("first_worker_process_id")
        second_pid = clean.get("second_worker_process_id")
        first_epoch = stages[0].get("worker_epoch") if len(stages) == 4 else None
        second_epoch = stages[2].get("worker_epoch") if len(stages) == 4 else None
        if not (
            clean["status"] == "succeeded"
            and clean["action_attempted"] is True
            and clean["fresh_process_verified"] is True
            and clean["first_worker_retired"] is True
            and clean["second_worker_retired"] is True
            and clean.get("intent_recorded") is True
            and actual_stages == expected_stages
            and isinstance(first_pid, int) and first_pid > 0
            and isinstance(second_pid, int) and second_pid > 0
            and first_pid != second_pid
            and first_epoch == stages[1].get("worker_epoch")
            and second_epoch == stages[3].get("worker_epoch")
            and first_epoch != second_epoch
            and selection.get("error_code") == "selection_process_refresh_required"
            and verification.get("selection_confirmed") is True
            and isinstance(identity, dict)
            and identity.get("binding_id") == binding_id
            and identity.get("conversation_type") == "direct"
            and identity.get("group_marker_count") == 0
        ):
            raise RuntimeError("GUEST_REPORT_SCHEMA_INVALID")
    return clean


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--visual-label", action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if _RELEASE.fullmatch(args.release_id) is None:
        parser.error("invalid release id")
    if _BINDING.fullmatch(args.binding_id) is None:
        parser.error("invalid binding id")
    attempt_id = str(UUID(args.attempt_id))
    labels = _parse_labels(args.visual_label)
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    report_path = args.report or (
        DIAGNOSTICS / f"qq-visual-selection-{args.release_id}-{attempt_id}.json"
    )
    report: dict[str, object] = {
        "schema": HOST_SCHEMA,
        "release_id": args.release_id,
        "attempt_id": attempt_id,
        "binding_id": args.binding_id,
        "succeeded": False,
        "acceptance_started": False,
    }
    guest = host = None
    clear = bytearray()
    guest_report = GUEST_DATA + f"\\qq-visual-selection-{attempt_id}.json"
    guest_config = (
        GUEST_RUNTIME_DATA + f"\\qq-visual-selection-{attempt_id}.config.json"
    )
    local_temporary = DIAGNOSTICS / f".qq-visual-selection-{attempt_id}.json"
    try:
        control = _load(CONTROL, "pmai_visual_selection_control")
        start = _load(START_HELPER, "pmai_visual_selection_start")
        _vbox, host, guest, clear, _password = control._open_guest()
        runtime = control._runtime_status(guest, "visual-selection-" + attempt_id)
        if runtime.get("runtime_process_alive") is True and start._guest_pid_alive(
            guest, runtime.get("runtime_process_id")
        ):
            raise RuntimeError("RUNTIME_NOT_STOPPED")
        release = rf"C:\PMAI\app\releases\{args.release_id}"
        builder = release + r"\build_visual_selection_acceptance_config_guest.py"
        builder_args = [
            GUEST_PYTHON,
            builder,
            "--source-config",
            GUEST_CONFIG,
            "--output-config",
            guest_config,
        ]
        for label in labels:
            builder_args.extend(("--visual-label", label))
        process = guest.ProcessCreate(
            GUEST_PYTHON, builder_args, GUEST_DATA, [], [], 180_000
        )
        builder_exit = _poll_process(process, timeout_seconds=180, guest=guest)
        report["builder_exit_code"] = builder_exit
        if builder_exit != 0:
            raise RuntimeError("VISUAL_CONFIG_BUILD_FAILED")
        acceptance = release + r"\qq_visual_selection_acceptance_guest.py"
        report["acceptance_started"] = True
        process = guest.ProcessCreate(
            GUEST_PYTHON,
            [
                GUEST_PYTHON, acceptance,
                "--config", guest_config,
                "--binding-id", args.binding_id,
                "--attempt-id", attempt_id,
                "--report", guest_report,
            ],
            GUEST_DATA,
            [],
            [],
            180_000,
        )
        acceptance_exit = _poll_process(process, timeout_seconds=180, guest=guest)
        report["acceptance_exit_code"] = acceptance_exit
        local_temporary.unlink(missing_ok=True)
        control._wait_progress(
            guest.FileCopyFromGuest(guest_report, str(local_temporary), []), 30_000
        )
        if not 2 <= local_temporary.stat().st_size <= MAX_REPORT_BYTES:
            raise RuntimeError("GUEST_REPORT_SIZE_INVALID")
        guest_value = json.loads(local_temporary.read_text(encoding="utf-8-sig"))
        projected = _validate_guest_report(
            guest_value, binding_id=args.binding_id, attempt_id=attempt_id
        )
        report["guest_result"] = projected
        if acceptance_exit not in (0, 2, 3):
            raise RuntimeError("ACCEPTANCE_PROCESS_EXIT_INVALID")
        expected_exit = 0 if projected["succeeded"] else 3 if projected["status"] == "uncertain" else 2
        if acceptance_exit != expected_exit:
            raise RuntimeError("ACCEPTANCE_RESULT_EXIT_MISMATCH")
        report["succeeded"] = projected["succeeded"] is True
        return 0 if report["succeeded"] else acceptance_exit
    except Exception as exc:
        code = str(exc)
        report["error_code"] = code if _SAFE_CODE.fullmatch(code) else "HOST_VISUAL_SELECTION_FAILED"
        if report["acceptance_started"] and "guest_result" not in report:
            report["action_attempted"] = None
        return 3 if report.get("acceptance_started") and report.get("action_attempted") is None else 2
    finally:
        local_temporary.unlink(missing_ok=True)
        if guest is not None:
            for guest_path in (guest_report, guest_config):
                try:
                    guest.FileDelete(guest_path)
                except Exception:
                    pass
        for index in range(len(clear)):
            clear[index] = 0
        if guest is not None:
            try:
                guest.Close()
            except Exception:
                pass
        if host is not None:
            try:
                host.UnlockMachine()
            except Exception:
                pass
        _atomic_json(report_path, report)


if __name__ == "__main__":
    raise SystemExit(main())
