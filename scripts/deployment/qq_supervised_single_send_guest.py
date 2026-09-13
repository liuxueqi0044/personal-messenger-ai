"""One supervised QQ send against an already verified current conversation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
import traceback
from datetime import UTC, datetime
from pathlib import Path


TEXT = "你好呀"
OUTPUT = Path(r"C:\PMAI\data\qq-supervised-single-send.json")
INTENT = Path(r"C:\PMAI\data\qq-supervised-single-send.intent.json")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(report: dict[str, object]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp.json")
    temp.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    os.replace(temp, OUTPUT)


def _record_once_intent(run_id: str) -> None:
    INTENT.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(INTENT, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.write(descriptor, json.dumps({"run_id": run_id, "text_sha256": _sha(TEXT)},
                                        sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _evidence(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "pmai-qq-supervised-send-evidence-v1":
        raise ValueError("EVIDENCE_SCHEMA_INVALID")
    for key in ("expected_header_digest", "expected_composer_runtime_id_hash"):
        if not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", str(value[key])) is None:
            raise ValueError("EVIDENCE_DIGEST_INVALID")
    if int(value.get("process_id", 0)) <= 0 or int(value.get("window_handle", 0)) <= 0:
        raise ValueError("EVIDENCE_WINDOW_INVALID")
    return value


def _semantic(bubbles) -> list[tuple[str, str, str]]:
    return [(item.direction.value, item.text, item.message_key) for item in bubbles]


def _safe_failure(stage: str, exc: BaseException) -> dict[str, object]:
    allowed = {
        "TARGET_SCOPE_MISMATCH", "SESSION_IDENTITY_MISMATCH", "COMPOSER_NOT_EMPTY",
        "COMPOSER_READBACK_MISMATCH", "SESSION_IDENTITY_DRIFT",
        "SEND_CONTROL_NOT_READY", "OUTBOUND_RECEIPT_NOT_UNIQUE",
    }
    code = str(exc) if str(exc) in allowed else "SUPERVISED_SEND_STAGE_FAILED"
    return {"stage": stage, "ok": False, "error_code": code,
            "exception_type": type(exc).__name__,
            "frames": [{"file": Path(frame.filename).name, "function": frame.name,
                        "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-6:]]}


def run(evidence_path: Path) -> int:
    report: dict[str, object] = {
        "schema": "pmai-qq-supervised-single-send-v1", "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(), "succeeded": False,
        "send_attempted": False, "stages": [],
    }
    _write(report | {"state": "running"})
    stages = report["stages"]
    write_attempted = False
    owned = False
    awaiting_visual_check = False
    current_stage = "initialize"
    access = window = composer = None
    try:
        from guest_focus_helper import select_and_focus_target
        from messenger_ai.adapters.qq.models import QQSelector, QQWindow
        from messenger_ai.adapters.qq.vm_driver.message_decoder import MessageDecodeError
        from messenger_ai.adapters.qq.vm_driver.sequence_alignment import unique_suffix_start
        from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility
        from qq_supervised_session import capture_current_session

        current_stage = "evidence"
        evidence = _evidence(evidence_path)
        current_stage = "target"
        target, _focus = select_and_focus_target()
        if target != (int(evidence["process_id"]), int(evidence["window_handle"])):
            raise RuntimeError("TARGET_SCOPE_MISMATCH")
        window = QQWindow(process_id=target[0], window_handle=target[1], class_name="Chrome_WidgetWin_1")
        access = WindowsUIAQQAccessibility()
        composer = QQSelector(name="composer", control_type="Group",
            class_name_tokens=("ProseMirror", "ExEditor-qq-msg-editor"), required_patterns=("TextPattern",))
        send = QQSelector(name="send", control_type="Button", class_name_tokens=("send-msg",), required_patterns=("InvokePattern",))
        bubbles = QQSelector(name="bubbles", control_type="Group", class_name_tokens=("ml-root",))
        current_stage = "session_precheck"
        current_session = capture_current_session(access, window, composer)
        if current_session != evidence:
            raise RuntimeError("SESSION_IDENTITY_MISMATCH")
        if access.read_composer(window, composer) != "":
            raise RuntimeError("COMPOSER_NOT_EMPTY")
        try:
            before = access.list_bubbles(window, bubbles)
            baseline_available = True
        except MessageDecodeError:
            before = []
            baseline_available = False
            stages.append({"stage": "baseline", "ok": False,
                           "error_code": "MESSAGE_BASELINE_UNAVAILABLE"})
        current_stage = "write"
        write_attempted = True
        access.write_composer(window, TEXT, composer)
        owned = True
        if access.read_composer(window, composer) != TEXT:
            raise RuntimeError("COMPOSER_READBACK_MISMATCH")
        current_stage = "preinvoke"
        if not access._guest_scope(window) or capture_current_session(access, window, composer) != evidence:
            raise RuntimeError("SESSION_IDENTITY_DRIFT")
        send_controls = access._select(access._window(window), send)
        if (len(send_controls) != 1 or not bool(getattr(send_controls[0], "IsEnabled", False))
                or bool(getattr(send_controls[0], "IsOffscreen", True))):
            raise RuntimeError("SEND_CONTROL_NOT_READY")
        # Persist intent before the single irreversible Invoke. This script is
        # never safe to retry after this point, even when receipt verification fails.
        _record_once_intent(str(report["run_id"]))
        report["send_attempted"] = True
        stages.append({"stage": "commit_intent", "ok": True, "text_length": len(TEXT), "text_sha256": _sha(TEXT)})
        _write(report | {"state": "commit_intent"})
        current_stage = "invoke"
        access.invoke_send(window, send)
        owned = False
        stages.append({"stage": "invoke", "ok": True})
        if not baseline_available:
            awaiting_visual_check = True
            stages.append({"stage": "receipt", "ok": False,
                           "error_code": "VISUAL_RECEIPT_CHECK_REQUIRED"})
        after = None
        for _ in range(0 if awaiting_visual_check else 20):
            time.sleep(.25)
            current_stage = "receipt_scope"
            if not access._guest_scope(window) or capture_current_session(access, window, composer) != evidence:
                raise RuntimeError("SESSION_IDENTITY_DRIFT")
            current_stage = "receipt"
            candidate = access.list_bubbles(window, bubbles)
            try:
                start = unique_suffix_start(_semantic(before), _semantic(candidate))
            except ValueError:
                continue
            suffix = candidate[start:]
            receipts = [item for item in suffix if item.direction.value == "outbound" and item.text == TEXT]
            if len(receipts) == 1:
                after = receipts[0]
                break
        if after is None and not awaiting_visual_check:
            raise RuntimeError("OUTBOUND_RECEIPT_NOT_UNIQUE")
        if after is not None:
            stages.append({"stage": "receipt", "ok": True, "text_length": len(after.text), "text_sha256": after.text_hash})
            report["succeeded"] = True
    except Exception as exc:
        stages.append(_safe_failure(current_stage, exc))
        cleanup_unknown = write_attempted
        if write_attempted and access is not None and window is not None and composer is not None and report["send_attempted"] is False:
            try:
                if access.read_composer(window, composer) == TEXT:
                    owned = True
                    access.clear_composer(window, TEXT, composer)
                    owned = False
                    cleanup_unknown = False
                    stages.append({"stage": "owned_clear", "ok": True})
                elif access.read_composer(window, composer) == "":
                    cleanup_unknown = False
            except Exception as cleanup_exc:
                stages.append({"stage": "owned_clear", "ok": False, "exception_type": type(cleanup_exc).__name__})
        report["cleanup_required"] = owned or cleanup_unknown
    report["state"] = ("succeeded" if report["succeeded"] else
                       "invoked_awaiting_visual_check" if awaiting_visual_check else
                       "uncertain" if report["send_attempted"] else "failed")
    _write(report)
    return 0 if report["succeeded"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    return run(parser.parse_args().evidence)


if __name__ == "__main__":
    raise SystemExit(main())
