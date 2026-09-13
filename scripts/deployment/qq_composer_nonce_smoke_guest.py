"""Guest-only composer write/read/owned-clear smoke test; never sends."""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
import uuid
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

OUTPUT = Path(r"C:\PMAI\data\qq-composer-nonce-smoke.json")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_report(report: dict[str, object]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    os.replace(temporary, OUTPUT)


_COMPOSER_CODES = {
    "composer_scope_rejected", "composer_not_empty", "composer_focus_drift",
    "composer_focus_or_scope_drift", "send_input_incomplete",
    "composer_readback_mismatch", "composer_clear_precondition_failed",
    "composer_clear_not_verified", "composer_has_no_readable_pattern",
}


def _failure(stage: str, exc: BaseException) -> dict[str, object]:
    code = str(exc) if type(exc).__name__ == "GuestComposerError" and str(exc) in _COMPOSER_CODES else "NONCE_STAGE_FAILED"
    return {"stage": stage, "ok": False, "error_code": code,
            "exception_type": type(exc).__name__,
            "frames": [{"file": Path(frame.filename).name, "function": frame.name,
                        "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-8:]]}


def _focus_diagnostic(transport, window, composer_hash: str) -> dict[str, object]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    focused = transport._auto.GetFocusedControl()
    runtime = tuple(focused.GetRuntimeId() or ()) if focused is not None else ()
    focused_hash = _hash(repr(runtime)) if runtime else None
    return {"foreground_matches": int(user32.GetForegroundWindow() or 0) == window.window_handle,
            "focused_present": focused is not None,
            "focused_process_matches": focused is not None and int(getattr(focused, "ProcessId", 0) or 0) == window.process_id,
            "focused_control_type": str(getattr(focused, "ControlTypeName", "")) if focused is not None else None,
            "focused_class_name": str(getattr(focused, "ClassName", "")) if focused is not None else None,
            "focused_runtime_id_hash": focused_hash,
            "focused_runtime_matches_composer": focused_hash == composer_hash}


def run() -> int:
    run_id = str(uuid.uuid4())
    nonce = "pmai-smoke-" + uuid.uuid4().hex
    stages: list[dict[str, object]] = []
    report: dict[str, object] = {
        "schema": "pmai-qq-composer-nonce-smoke-v1",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "succeeded": False,
        "stages": stages,
    }
    _write_report(report | {"state": "running"})
    write_attempted = False
    wrote_owned_nonce = False
    transport = None
    window = None
    selector = None
    session = None
    current_stage = "initialize"
    try:
        current_stage = "imports"
        from guest_focus_helper import select_and_focus_target
        from messenger_ai.adapters.qq.models import QQSelector, QQWindow
        from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility
        from qq_supervised_session import capture_current_session

        selector = QQSelector(
            name="composer",
            control_type="Group",
            class_name_tokens=("ProseMirror", "ExEditor-qq-msg-editor"),
            required_patterns=("TextPattern",),
        )
        current_stage = "target"
        target, focus = select_and_focus_target()
        pid, hwnd = target
        stages.append({"stage": "target", "ok": True, "process_id": pid,
                       "window_handle": hwnd, **focus})
        current_stage = "transport"
        transport = WindowsUIAQQAccessibility()
        window = QQWindow(process_id=pid, window_handle=hwnd,
                          class_name="Chrome_WidgetWin_1")
        current_stage = "session_identity"
        session = capture_current_session(transport, window, selector)
        stages.append({"stage": "session_identity", "ok": True,
                       "header_digest": session["expected_header_digest"],
                       "composer_runtime_id_hash": session["expected_composer_runtime_id_hash"]})
        current_stage = "initial_read"
        initial = transport.read_composer(window, selector)
        stages.append({"stage": "initial_read", "ok": initial == "",
                       "text_length": len(initial), "text_sha256": _hash(initial)})
        if initial != "":
            raise RuntimeError("COMPOSER_NOT_EMPTY")
        current_stage = "write"
        write_attempted = True
        transport.write_composer(window, nonce, selector)
        wrote_owned_nonce = True
        stages.append({"stage": "write", "ok": True,
                       "text_length": len(nonce), "text_sha256": _hash(nonce)})
        current_stage = "readback"
        readback = transport.read_composer(window, selector)
        readback_ok = readback == nonce
        stages.append({"stage": "readback", "ok": readback_ok,
                       "text_length": len(readback), "text_sha256": _hash(readback)})
        if not readback_ok:
            raise RuntimeError("COMPOSER_READBACK_MISMATCH")
        current_stage = "owned_clear"
        transport.clear_composer(window, nonce, selector)
        wrote_owned_nonce = False
        current_stage = "final_read"
        final = transport.read_composer(window, selector)
        stages.append({"stage": "owned_clear", "ok": final == "",
                       "text_length": len(final), "text_sha256": _hash(final)})
        if final != "":
            raise RuntimeError("COMPOSER_CLEAR_NOT_VERIFIED")
        report["succeeded"] = True
        report["cleanup_required"] = False
        report["supervised_send_evidence"] = session
    except Exception as exc:
        diagnostic = getattr(exc, "safe_diagnostic", None)
        failure = _failure(current_stage, exc)
        if isinstance(diagnostic, dict):
            failure["diagnostic"] = diagnostic
        stages.append(failure)
        if (write_attempted and transport is not None and window is not None and
                isinstance(session, dict)):
            try:
                stages.append({"stage": "write_failure_focus", "ok": True,
                    **_focus_diagnostic(transport, window, str(session["expected_composer_runtime_id_hash"]))})
            except Exception as focus_exc:
                stages.append(_failure("write_failure_focus", focus_exc))
        # Cleanup is permitted only after exact owned-text readback. Transport
        # rechecks guest scope, focus, and exact expected text before deleting.
        cleanup_unknown = write_attempted
        if write_attempted and transport is not None and window is not None:
            try:
                current = transport.read_composer(window, selector)
                for _ in range(5):
                    if current != "":
                        break
                    time.sleep(0.1)
                    current = transport.read_composer(window, selector)
                if current == nonce:
                    wrote_owned_nonce = True
                    transport.clear_composer(window, nonce, selector)
                    wrote_owned_nonce = False
                    cleanup_unknown = False
                    stages.append({"stage": "failure_owned_clear", "ok": True})
                elif current == "":
                    cleanup_unknown = False
                else:
                    stages.append({"stage": "failure_observed_content", "ok": False,
                                   "text_length": len(current), "text_sha256": _hash(current),
                                   "trailing_codepoint": ord(current[-1]) if current else None,
                                   "expected_nonce_sha256": _hash(nonce),
                                   "observed_equals_nonce_plus_lf": current == nonce + "\n",
                                   "observed_equals_nonce_plus_crlf": current == nonce + "\r\n"})
            except Exception as cleanup_exc:
                stages.append(_failure("failure_owned_clear", cleanup_exc))
                try:
                    final_observed = transport.read_composer(window, selector)
                    controls = transport._select(transport._window(window), selector)
                    class_tokens = (set(str(getattr(controls[0], "ClassName", "")).split())
                                    if len(controls) == 1 else set())
                    stages.append({"stage": "failure_final_read", "ok": True,
                                   "text_length": len(final_observed),
                                   "text_sha256": _hash(final_observed),
                                   "trailing_codepoint": ord(final_observed[-1]) if final_observed else None,
                                   "composer_is_empty_class": "is-empty" in class_tokens})
                except Exception as final_exc:
                    stages.append(_failure("failure_final_read", final_exc))
        report["cleanup_required"] = wrote_owned_nonce or cleanup_unknown
    report["state"] = "succeeded" if report["succeeded"] else "failed"
    _write_report(report)
    return 0 if report["succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(run())
