"""Promote an accepted visual-selection block into the stopped guest runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(r"C:\Users\xiayu\Documents\Codex\2026-09-09\new-chat")
CONTROL = ROOT / r"outputs\qq-vm\install\control_qq_runtime_host.py"
START = ROOT / r"outputs\qq-vm\install\start_qq_runtime_host.py"
VISUAL_HOST = ROOT / r"outputs\qq-vm\install\run_visual_selection_acceptance_host.py"
DIAGNOSTICS = ROOT / r"outputs\qq-vm\install\diagnostics"
PYTHON = r"C:\PMAI\app\.venv\Scripts\python.exe"
DATA = r"C:\PMAI\data"
CONFIG = DATA + r"\runtime-session-1.json"
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_REPORT_BYTES = 64 * 1024
PROMOTION_SCHEMA = "pmai-visual-selection-promotion-v2"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("HOST_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate(
    value: object, *, accepted_config_sha256: str | None = None
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "state", "succeeded", "committed", "rolled_back",
        "accepted_config_sha256", "before_sha256", "after_sha256",
        "backup_sha256", "binding_count", "visual_model", "min_confidence",
        "semantic_change",
    }:
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    if (
        value.get("schema") != PROMOTION_SCHEMA
        or value.get("state") != "succeeded"
        or value.get("succeeded") is not True
        or value.get("committed") is not True
        or value.get("rolled_back") is not False
        or value.get("binding_count") != 3
        or value.get("visual_model") != "deepseek-v4-flash-vision-exp"
        or value.get("min_confidence") != 0.98
        or value.get("semantic_change") != "visual_selection_only"
        or not all(
            isinstance(value.get(key), str) and SHA256.fullmatch(value[key])
            for key in (
                "accepted_config_sha256", "before_sha256", "after_sha256",
                "backup_sha256",
            )
        )
        or value.get("before_sha256") != value.get("backup_sha256")
        or value.get("before_sha256") == value.get("after_sha256")
        or value.get("after_sha256") != value.get("accepted_config_sha256")
        or (
            accepted_config_sha256 is not None
            and value.get("accepted_config_sha256") != accepted_config_sha256
        )
    ):
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    return dict(value)


def _validate_transaction(
    value: object, *, accepted_config_sha256: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    allowed = {
        "schema", "state", "succeeded", "committed", "rolled_back",
        "accepted_config_sha256", "error_code", "before_sha256",
        "after_sha256", "backup_sha256", "binding_count", "visual_model",
        "min_confidence", "semantic_change",
    }
    if set(value) - allowed or value.get("schema") != PROMOTION_SCHEMA:
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    if value.get("succeeded") is True:
        return _validate(value, accepted_config_sha256=accepted_config_sha256)
    state = value.get("state")
    committed = value.get("committed")
    rolled_back = value.get("rolled_back")
    if (
        state not in {
            "preparing", "backup_verified", "committed", "precommit_failed",
            "rolled_back", "rollback_failed",
        }
        or value.get("succeeded") is not False
        or not isinstance(committed, bool)
        or not isinstance(rolled_back, bool)
        or value.get("accepted_config_sha256") != accepted_config_sha256
        or SHA256.fullmatch(accepted_config_sha256) is None
        or (state == "rolled_back" and (committed is not True or rolled_back is not True))
        or (state == "committed" and committed is not True)
        or (state == "rollback_failed" and committed is not True)
    ):
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    error = value.get("error_code")
    if not isinstance(error, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", error) is None:
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    for key in ("before_sha256", "after_sha256", "backup_sha256"):
        digest = value.get(key)
        if digest is not None and (
            not isinstance(digest, str) or SHA256.fullmatch(digest) is None
        ):
            raise RuntimeError("PROMOTION_REPORT_INVALID")
    if value.get("after_sha256") is not None and value.get(
        "after_sha256"
    ) != accepted_config_sha256:
        raise RuntimeError("PROMOTION_REPORT_INVALID")
    return dict(value)


def _load_acceptance_evidence(
    paths: list[Path], *, labels: list[str], visual_host: Any
) -> dict[str, object]:
    expected_bindings = {
        f"session-contact-{int(value.split('=', 1)[0])}" for value in labels
    }
    if len(paths) != 3 or len(expected_bindings) != 3:
        raise RuntimeError("ACCEPTANCE_EVIDENCE_INCOMPLETE")
    accepted_digest: str | None = None
    acceptance_release: str | None = None
    attempts: list[str] = []
    seen: set[str] = set()
    diagnostics_root = DIAGNOSTICS.resolve()
    for path in paths:
        resolved = path.resolve()
        if (
            resolved.parent != diagnostics_root
            or not resolved.is_file()
            or resolved.stat().st_size > MAX_REPORT_BYTES
        ):
            raise RuntimeError("ACCEPTANCE_EVIDENCE_INVALID")
        raw = json.loads(resolved.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise RuntimeError("ACCEPTANCE_EVIDENCE_INVALID")
        binding_id = raw.get("binding_id")
        attempt_id = raw.get("attempt_id")
        release_id = raw.get("release_id")
        if (
            raw.get("schema") != "pmai-qq-visual-selection-acceptance-host-v1"
            or raw.get("succeeded") is not True
            or raw.get("acceptance_started") is not True
            or raw.get("builder_exit_code") != 0
            or raw.get("acceptance_exit_code") != 0
            or not isinstance(binding_id, str)
            or binding_id not in expected_bindings
            or binding_id in seen
            or not isinstance(attempt_id, str)
            or not isinstance(release_id, str)
            or re.fullmatch(r"r\d{8}-\d{2}", release_id) is None
        ):
            raise RuntimeError("ACCEPTANCE_EVIDENCE_INVALID")
        clean = visual_host._validate_guest_report(
            raw.get("guest_result"),
            binding_id=binding_id,
            attempt_id=attempt_id,
        )
        digest = clean.get("config_sha256")
        if clean.get("succeeded") is not True or not isinstance(digest, str):
            raise RuntimeError("ACCEPTANCE_EVIDENCE_INVALID")
        if accepted_digest is not None and digest != accepted_digest:
            raise RuntimeError("ACCEPTANCE_CONFIG_DIGEST_MISMATCH")
        if acceptance_release is not None and release_id != acceptance_release:
            raise RuntimeError("ACCEPTANCE_RELEASE_MISMATCH")
        accepted_digest = digest
        acceptance_release = release_id
        attempts.append(attempt_id)
        seen.add(binding_id)
    if seen != expected_bindings or accepted_digest is None or acceptance_release is None:
        raise RuntimeError("ACCEPTANCE_EVIDENCE_INCOMPLETE")
    return {
        "accepted_config_sha256": accepted_digest,
        "acceptance_release_id": acceptance_release,
        "acceptance_attempt_ids": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--visual-label", action="append", default=[])
    parser.add_argument("--acceptance-report", type=Path, action="append", default=[])
    args = parser.parse_args()
    if re.fullmatch(r"r\d{8}-\d{2}", args.release_id) is None:
        parser.error("invalid release id")
    visual_host = _load(VISUAL_HOST, "promotion_visual_host")
    labels = visual_host._parse_labels(args.visual_label)
    acceptance = _load_acceptance_evidence(
        args.acceptance_report, labels=labels, visual_host=visual_host
    )
    accepted_config_sha256 = str(acceptance["accepted_config_sha256"])
    nonce = uuid.uuid4().hex
    local_report = DIAGNOSTICS / f"visual-selection-promotion-{args.release_id}-{nonce}.json"
    guest_report = DATA + f"\visual-selection-promotion-{args.release_id}-{nonce}.json"
    backup = DATA + f"\runtime-session-1.pre-visual-{args.release_id}.json"
    candidate = (
        DATA + r"\runtime\qq-default-account"
        + f"\runtime-session-1.visual-candidate-{args.release_id}.json"
    )
    report: dict[str, object] = {
        "schema": "pmai-visual-selection-promotion-host-v1",
        "release_id": args.release_id,
        **acceptance,
        "succeeded": False,
    }
    host = guest = None
    clear = bytearray()
    try:
        control = _load(CONTROL, "promotion_control")
        start = _load(START, "promotion_start")
        _vbox, host, guest, clear, _password = control._open_guest()
        before = control._runtime_status(guest, "visual-promotion-before-" + nonce)
        if before.get("runtime_process_alive") is True and start._guest_pid_alive(
            guest, before.get("runtime_process_id")
        ):
            raise RuntimeError("RUNTIME_NOT_STOPPED")
        script = rf"C:\PMAI\app\releases\{args.release_id}\promote_visual_selection_config_guest.py"
        command = [
            PYTHON, script, "--config", CONFIG, "--backup", backup,
            "--candidate", candidate, "--report", guest_report,
            "--accepted-config-sha256", accepted_config_sha256,
        ]
        for label in labels:
            command.extend(("--visual-label", label))
        process = guest.ProcessCreate(PYTHON, command, DATA, [], [], 180_000)
        exit_code = visual_host._poll_process(
            process, timeout_seconds=180, guest=guest
        )
        report["promotion_exit_code"] = exit_code
        temporary = DIAGNOSTICS / ("." + nonce + ".promotion.json")
        try:
            control._wait_progress(
                guest.FileCopyFromGuest(guest_report, str(temporary), []), 30_000
            )
            transaction = _validate_transaction(
                json.loads(temporary.read_text(encoding="utf-8-sig")),
                accepted_config_sha256=accepted_config_sha256,
            )
        finally:
            temporary.unlink(missing_ok=True)
        report["promotion"] = transaction
        if exit_code != 0:
            raise RuntimeError("PROMOTION_PROCESS_FAILED_" + str(transaction["state"]).upper())
        promoted = _validate(
            transaction, accepted_config_sha256=accepted_config_sha256
        )
        after = control._runtime_status(guest, "visual-promotion-after-" + nonce)
        if after.get("runtime_process_alive") is not False:
            raise RuntimeError("RUNTIME_STATE_CHANGED_DURING_PROMOTION")
        report.update({"promotion": promoted, "runtime_stopped": True, "succeeded": True})
        return 0
    except Exception as exc:
        report["error_code"] = str(exc)[:128]
        return 2
    finally:
        DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
        local_report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
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


if __name__ == "__main__":
    raise SystemExit(main())
