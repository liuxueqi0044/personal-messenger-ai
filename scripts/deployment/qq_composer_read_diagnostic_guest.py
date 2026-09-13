"""Safe staged diagnostic for the QQ composer UIA read path; never emits text."""
from __future__ import annotations
import hashlib, importlib.metadata, importlib.util, json, os, platform, time, uuid
from datetime import UTC, datetime
from pathlib import Path

OUTPUT = Path(r"C:\PMAI\data\qq-composer-read-diagnostic.json")
MAX_NODES = 2000

def _exc(exc: BaseException) -> str:
    return {"ModuleNotFoundError": "UIA_MODULE_MISSING", "ImportError": "UIA_IMPORT_FAILED", "AttributeError": "UIA_API_SHAPE_FAILED", "TimeoutError": "UIA_TIMEOUT"}.get(type(exc).__name__, "UIA_STAGE_FAILED")

def main() -> int:
    started = datetime.now(UTC).isoformat(); run_id = str(uuid.uuid4()); stages = []
    report = {"schema": "pmai-qq-composer-read-diagnostic-v2", "run_started_at": started, "run_id": run_id, "succeeded": False, "stages": stages}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True); tmp = OUTPUT.with_suffix(".tmp.json"); tmp.write_text(json.dumps(report | {"state": "running"}, sort_keys=True), encoding="utf-8"); os.replace(tmp, OUTPUT)
    try:
        try:
            spec = importlib.util.find_spec("uiautomation")
            version = importlib.metadata.version("uiautomation")
            stages.append({"stage": "import_uia", "ok": spec is not None, "module_available": spec is not None, "package_version": version, "python_version": platform.python_version()})
            if spec is None: raise ModuleNotFoundError("uiautomation")
            import uiautomation as auto
        except Exception as exc:
            stages.append({"stage": "import_uia", "ok": False, "error_code": _exc(exc), "exception_type": type(exc).__name__, "python_version": platform.python_version()})
            raise RuntimeError("STOP")
        try:
            from activate_default_rulepack_guest import _require_guest_context
            _require_guest_context()
            from guest_focus_helper import select_and_focus_target
            target, focus = select_and_focus_target()
            stages.append({"stage": "target", "ok": True, "process_id": target[0], "window_handle": target[1], **focus})
        except Exception as exc:
            failure = {"stage": "target", "ok": False, "error_code": _exc(exc),
                       "exception_type": type(exc).__name__}
            safe = getattr(exc, "safe_diagnostic", None)
            if isinstance(safe, dict):
                failure["safe_diagnostic"] = safe
            stages.append(failure)
            raise RuntimeError("STOP")
        try:
            root = auto.ControlFromHandle(target[1])
            if root is None or int(getattr(root, "ProcessId", 0) or 0) != target[0]: raise RuntimeError("ROOT_PID_MISMATCH")
            stages.append({"stage": "root", "ok": True, "process_id_matches": True})
        except Exception as exc:
            stages.append({"stage": "root", "ok": False, "error_code": "ROOT_PID_MISMATCH" if str(exc) == "ROOT_PID_MISMATCH" else _exc(exc), "exception_type": type(exc).__name__}); raise RuntimeError("STOP")
        try:
            queue = list(root.GetChildren()); matches = []; count = 0
            while queue:
                if count >= MAX_NODES: raise OverflowError()
                item = queue.pop(0); count += 1
                classes = set(str(getattr(item, "ClassName", "")).split()); kind = str(getattr(item, "ControlTypeName", "")).casefold().replace("controltype.", "")
                if kind.endswith("control"): kind = kind[:-7]
                if kind == "group" and {"ProseMirror", "ExEditor-qq-msg-editor"} <= classes: matches.append(item)
                queue.extend(item.GetChildren())
            if len(matches) != 1: raise LookupError()
            control = matches[0]
            if int(getattr(control, "ProcessId", 0) or 0) != target[0]: raise RuntimeError("COMPOSER_PID_MISMATCH")
            stages.append({"stage": "enumerate", "ok": True, "node_count": count, "candidate_count": 1})
        except Exception as exc:
            code = "UIA_NODE_LIMIT_EXCEEDED" if isinstance(exc, OverflowError) else "COMPOSER_PID_MISMATCH" if str(exc) == "COMPOSER_PID_MISMATCH" else "COMPOSER_NOT_UNIQUE" if isinstance(exc, LookupError) else _exc(exc)
            stages.append({"stage": "enumerate", "ok": False, "error_code": code, "exception_type": type(exc).__name__}); raise RuntimeError("STOP")
        try:
            text_pattern = control.GetPattern(auto.PatternId.TextPattern)
            value_pattern = control.GetPattern(auto.PatternId.ValuePattern)
            if text_pattern is None: raise RuntimeError("COMPOSER_TEXT_PATTERN_MISSING")
            stages.append({"stage": "pattern", "ok": True, "supports_text_pattern": True, "supports_value_pattern": value_pattern is not None, "is_keyboard_focusable": bool(getattr(control, "IsKeyboardFocusable", False)), "has_keyboard_focus": bool(getattr(control, "HasKeyboardFocus", False))})
        except Exception as exc:
            stages.append({"stage": "pattern", "ok": False, "error_code": "COMPOSER_TEXT_PATTERN_MISSING" if str(exc) == "COMPOSER_TEXT_PATTERN_MISSING" else _exc(exc), "exception_type": type(exc).__name__}); raise RuntimeError("STOP")
        try:
            document_range = text_pattern.DocumentRange; getter = getattr(document_range, "GetText", None)
            stages.append({"stage": "read", "ok": False, "document_range_type": type(document_range).__name__, "get_text_callable": callable(getter)})
            if not callable(getter): raise RuntimeError("DOCUMENT_RANGE_GETTEXT_MISSING")
            text = str(getter(-1))
            stages[-1].update({"ok": True, "text_length": len(text), "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            if len(text) <= 4 and all(ch in "\r\n\t " for ch in text):
                stages[-1]["whitespace_codepoints"] = [ord(ch) for ch in text]
            report["succeeded"] = True
        except Exception as exc:
            stages.append({"stage": "read", "ok": False, "error_code": "DOCUMENT_RANGE_GETTEXT_MISSING" if str(exc) == "DOCUMENT_RANGE_GETTEXT_MISSING" else _exc(exc), "exception_type": type(exc).__name__})
    except RuntimeError:
        pass
    except Exception:
        stages.append({"stage": "unknown", "ok": False, "error_code": "DIAGNOSTIC_FAILED", "exception_type": "Exception"})
    report["state"] = "succeeded" if report["succeeded"] else "failed"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True); tmp = OUTPUT.with_suffix(".tmp.json"); tmp.write_text(json.dumps(report, sort_keys=True), encoding="utf-8"); os.replace(tmp, OUTPUT)
    return 0 if report["succeeded"] else 2

if __name__ == "__main__": raise SystemExit(main())
