"""One-shot, no-message QQ visual-selection acceptance gate.

The first worker may authorize one contact-row click.  It is always retired
before a second worker verifies the selected direct-chat identity.  The second
worker receives neither a visual provider nor a selection-capable command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.adapters.qq.vm_driver.contracts import (
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
)
from messenger_ai.adapters.qq.vm_driver.worker import QQVMWorkerProcess
from messenger_ai.observability import WindowsDPAPISecretStore

try:
    from run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _capability,
        _validated_visual_selection,
        load_config,
        validate_config,
    )
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _capability,
        _validated_visual_selection,
        load_config,
        validate_config,
    )


REPORT_SCHEMA = "pmai-qq-visual-selection-acceptance-v1"
INTENT_SCHEMA = "pmai-qq-visual-selection-attempt-v1"
_BINDING_ID = re.compile(r"session-contact-[1-9][0-9]{0,3}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _config_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_intent(
    path: Path, *, attempt_id: str, binding_id: str, config_sha256: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("VISUAL_SELECTION_ATTEMPT_REPLAY") from exc
    try:
        payload = json.dumps(
            {
                "schema": INTENT_SCHEMA,
                "attempt_id": attempt_id,
                "binding_id": binding_id,
                "config_sha256": config_sha256,
                "created_at": _now(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else fallback


def _safe_identity(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, object] = {}
    binding_id = value.get("binding_id")
    if isinstance(binding_id, str) and _BINDING_ID.fullmatch(binding_id):
        clean["binding_id"] = binding_id
    conversation_type = value.get("conversation_type")
    if conversation_type == "direct":
        clean["conversation_type"] = "direct"
    for key in ("process_id", "window_handle"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            clean[key] = item
    marker_count = value.get("group_marker_count")
    if (
        isinstance(marker_count, int)
        and not isinstance(marker_count, bool)
        and 0 <= marker_count <= 1000
    ):
        clean["group_marker_count"] = marker_count
    for key in ("selected_row_runtime_id_hash", "header_digest"):
        item = value.get(key)
        if isinstance(item, str) and _SHA256.fullmatch(item):
            clean[key] = item
    return clean


def _project_worker_result(result: WorkerResult) -> dict[str, object]:
    projected: dict[str, object] = {
        "kind": result.kind.value,
        "status": result.status.value,
        "worker_epoch": str(result.worker_epoch),
    }
    if result.error_code is not None:
        projected["error_code"] = _safe_code(
            result.error_code, "WORKER_RESULT_ERROR_CODE_INVALID"
        )
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    for key in ("process_id", "window_handle"):
        item = evidence.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            projected[key] = item
    frame_sha256 = evidence.get("frame_sha256")
    if isinstance(frame_sha256, str) and _SHA256.fullmatch(frame_sha256):
        projected["frame_sha256"] = frame_sha256
    model = evidence.get("model")
    if isinstance(model, str) and _MODEL_ID.fullmatch(model):
        projected["model"] = model
    latency = evidence.get("latency_ms")
    if (
        isinstance(latency, int)
        and not isinstance(latency, bool)
        and 0 <= latency <= 300_000
    ):
        projected["latency_ms"] = latency
    for key, allowed in {
        "visual_decision": {"match", "not_match", "ambiguous"},
        "visual_reason": {
            "exact_label",
            "different_label",
            "label_missing",
            "multiple_primary_labels",
            "unreadable",
        },
        "provider_error_category": {
            "rate_limited",
            "server_error",
            "timeout",
            "network",
            "schema",
            "rejected",
            "unknown",
        },
    }.items():
        item = evidence.get(key)
        if item in allowed:
            projected[key] = item
    confidence = evidence.get("visual_confidence")
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
    ):
        projected["visual_confidence"] = float(confidence)
    label_match = evidence.get("normalized_label_match")
    if isinstance(label_match, bool):
        projected["normalized_label_match"] = label_match
    if evidence.get("selection_confirmed") is True:
        projected["selection_confirmed"] = True
    identity = _safe_identity(evidence.get("target_identity"))
    if identity:
        projected["target_identity"] = identity
    return projected


def _start_without_inherited_api_key(worker: QQVMWorkerProcess) -> None:
    inherited = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        worker.start()
    finally:
        if inherited is not None:
            os.environ["DEEPSEEK_API_KEY"] = inherited


def _worker_pid(worker: QQVMWorkerProcess) -> int | None:
    value = worker.status_snapshot().get("worker_process_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _retire_worker(worker: QQVMWorkerProcess) -> bool:
    """Stop a worker and positively confirm that its child process is gone."""

    try:
        worker.stop()
        snapshot = worker.status_snapshot()
    except Exception:  # noqa: BLE001 - retirement failure is projected as uncertain
        return False
    return snapshot.get("worker_alive") is False


def _require_guest() -> None:
    try:
        from activate_default_rulepack_guest import _require_guest_context
    except ModuleNotFoundError:  # repository-root test/import path
        from scripts.deployment.activate_default_rulepack_guest import (
            _require_guest_context,
        )

    _require_guest_context()


def execute(
    *, config_path: Path, binding_id: str, attempt_id: str, report_path: Path
) -> dict[str, object]:
    canonical_attempt = str(UUID(attempt_id))
    if _BINDING_ID.fullmatch(binding_id) is None:
        raise RuntimeError("VISUAL_SELECTION_BINDING_ID_INVALID")
    owner = QQRuntimeInstanceOwner()
    owner.acquire()
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "attempt_id": canonical_attempt,
        "binding_id": binding_id,
        "status": "initializing",
        "succeeded": False,
        "action_attempted": False,
        "fresh_process_verified": False,
        "first_worker_retired": False,
        "second_worker_retired": False,
        "started_at": _now(),
        "stages": [],
    }
    try:
        _atomic_json(report_path, report)
    except BaseException:
        owner.close()
        raise
    first: QQVMWorkerProcess | None = None
    second: QQVMWorkerProcess | None = None
    continue_to_verify = False
    try:
        _require_guest()
        os.environ["PERSONAL_MESSENGER_VM_GUEST"] = "1"
        config = load_config(config_path)
        pack, bindings, evidence = validate_config(
            config, api_key="offline-visual-selection-acceptance"
        )
        _capability(config, pack)
        visual = _validated_visual_selection(config, bindings)
        if visual is None:
            raise RuntimeError("VISUAL_SELECTION_CONFIG_REQUIRED")
        binding = next((item for item in bindings if item.binding_id == binding_id), None)
        if binding is None:
            raise RuntimeError("VISUAL_SELECTION_BINDING_NOT_FOUND")
        if binding.conversation_type != "direct":
            raise RuntimeError("VISUAL_SELECTION_BINDING_NOT_DIRECT")
        if not any(item.binding_id == binding_id for item in evidence):
            raise RuntimeError("VISUAL_SELECTION_SESSION_EVIDENCE_MISSING")
        digest = _config_digest(config)
        report["config_sha256"] = digest
        intent_path = (
            Path(str(config["data_dir"]))
            / f"qq-visual-selection-{canonical_attempt}.intent.json"
        )
        _record_intent(
            intent_path,
            attempt_id=canonical_attempt,
            binding_id=binding_id,
            config_sha256=digest,
        )
        report["intent_recorded"] = True
        report["status"] = "intent_recorded"
        _atomic_json(report_path, report)

        key = WindowsDPAPISecretStore(Path(str(config["secret_vault"]))).get_secret(
            "deepseek.api_key"
        ).decode("utf-8")
        timeout_seconds = float(config.get("worker_timeout_seconds", 15))
        first = QQVMWorkerProcess(
            pack,
            bindings,
            session_evidence=evidence,
            run_id=canonical_attempt,
            visual_selection=visual,
            visual_api_key=key,
        )
        _start_without_inherited_api_key(first)
        report["first_worker_process_id"] = _worker_pid(first)
        health = first.request(WorkerCommand(kind=WorkerKind.HEALTH), timeout_seconds)
        report["stages"].append({"stage": "first_health", **_project_worker_result(health)})
        if health.status is not WorkerStatus.OK:
            raise RuntimeError("VISUAL_SELECTION_FIRST_HEALTH_FAILED")
        report["action_attempted"] = None
        report["status"] = "selection_request_pending"
        _atomic_json(report_path, report)
        try:
            selected = first.request(
                WorkerCommand(kind=WorkerKind.SELECT_ONLY, binding_id=binding_id),
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - IPC failures are heterogeneous
            report["status"] = "uncertain"
            report["error_code"] = _safe_code(
                str(exc), "VISUAL_SELECTION_FIRST_REQUEST_UNCERTAIN"
            )
            return report
        report["stages"].append({"stage": "select_once", **_project_worker_result(selected)})
        if selected.status is WorkerStatus.UNCERTAIN:
            report["action_attempted"] = None
            report["status"] = "uncertain"
            report["error_code"] = _safe_code(
                selected.error_code, "VISUAL_SELECTION_FIRST_RESULT_UNCERTAIN"
            )
            return report
        if not (
            selected.status is WorkerStatus.FAILED_SAFE
            and selected.error_code == "selection_process_refresh_required"
        ):
            report["action_attempted"] = False
            report["status"] = "rejected"
            report["error_code"] = _safe_code(
                selected.error_code, "VISUAL_SELECTION_ACTION_NOT_ATTEMPTED"
            )
            return report
        report["action_attempted"] = True
        report["status"] = "action_attempted"
        _atomic_json(report_path, report)
        continue_to_verify = True
    finally:
        if first is not None:
            report["first_worker_retired"] = _retire_worker(first)
            if report["first_worker_retired"] is not True:
                report["status"] = "uncertain"
                report["error_code"] = "VISUAL_SELECTION_FIRST_WORKER_NOT_RETIRED"
                continue_to_verify = False
        if not continue_to_verify:
            owner.close()

    if not continue_to_verify:
        return report

    try:
        # This process has no visual provider and receives a command that cannot
        # call either the visual actuator or the legacy selection implementation.
        second = QQVMWorkerProcess(
            pack,
            bindings,
            session_evidence=evidence,
            run_id=canonical_attempt,
        )
        _start_without_inherited_api_key(second)
        report["second_worker_process_id"] = _worker_pid(second)
        health = second.request(WorkerCommand(kind=WorkerKind.HEALTH), timeout_seconds)
        report["stages"].append({"stage": "second_health", **_project_worker_result(health)})
        if health.status is not WorkerStatus.OK:
            report["status"] = "uncertain"
            report["error_code"] = _safe_code(
                health.error_code, "VISUAL_SELECTION_SECOND_HEALTH_UNCERTAIN"
            )
            return report
        try:
            verified = second.request(
                WorkerCommand(
                    kind=WorkerKind.VERIFY_SELECTION_ONLY,
                    binding_id=binding_id,
                ),
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - action already happened
            report["status"] = "uncertain"
            report["error_code"] = _safe_code(
                str(exc), "VISUAL_SELECTION_FRESH_VERIFY_UNCERTAIN"
            )
            return report
        report["stages"].append(
            {"stage": "fresh_process_verify", **_project_worker_result(verified)}
        )
        if verified.status in {WorkerStatus.UNCERTAIN, WorkerStatus.UNAVAILABLE}:
            report["status"] = "uncertain"
            report["error_code"] = _safe_code(
                verified.error_code, "VISUAL_SELECTION_FRESH_VERIFY_UNCERTAIN"
            )
            return report
        identity = _safe_identity(verified.evidence.get("target_identity"))
        if not (
            verified.status is WorkerStatus.OK
            and verified.evidence.get("selection_confirmed") is True
            and identity.get("binding_id") == binding_id
            and identity.get("conversation_type") == "direct"
            and identity.get("group_marker_count") == 0
        ):
            report["status"] = "rejected"
            report["error_code"] = _safe_code(
                verified.error_code, "VISUAL_SELECTION_FRESH_VERIFY_FAILED"
            )
            return report
        report["fresh_process_verified"] = True
        report["succeeded"] = True
        report["status"] = "succeeded"
        return report
    except Exception as exc:  # noqa: BLE001 - action already happened
        report["status"] = "uncertain"
        report["error_code"] = _safe_code(
            str(exc), "VISUAL_SELECTION_FRESH_VERIFY_UNCERTAIN"
        )
        return report
    finally:
        if second is not None:
            report["second_worker_retired"] = _retire_worker(second)
            if report["second_worker_retired"] is not True:
                report["succeeded"] = False
                report["fresh_process_verified"] = False
                report["status"] = "uncertain"
                report["error_code"] = "VISUAL_SELECTION_SECOND_WORKER_NOT_RETIRED"
        owner.close()


def _error_code(exc: BaseException) -> str:
    return _safe_code(str(exc), "VISUAL_SELECTION_ACCEPTANCE_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = execute(
            config_path=args.config,
            binding_id=args.binding_id,
            attempt_id=args.attempt_id,
            report_path=args.report,
        )
        code = 0 if report.get("succeeded") is True else 3 if report.get("status") == "uncertain" else 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary must fail closed
        report = {
            "schema": REPORT_SCHEMA,
            "attempt_id": args.attempt_id,
            "binding_id": args.binding_id,
            "status": "rejected",
            "succeeded": False,
            "action_attempted": None,
            "fresh_process_verified": False,
            "first_worker_retired": False,
            "second_worker_retired": False,
            "error_code": _error_code(exc),
            "completed_at": _now(),
        }
        code = 2
    report["completed_at"] = _now()
    _atomic_json(args.report, report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
