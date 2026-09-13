"""Guest-only QQ foreground request with privacy-safe staged failures."""
from __future__ import annotations

import ctypes
import time
import traceback
from ctypes import wintypes
from pathlib import Path


class GuestFocusStageError(RuntimeError):
    def __init__(self, stage: str, exc: BaseException) -> None:
        super().__init__("GUEST_FOCUS_STAGE_FAILED")
        self.safe_diagnostic = {
            "focus_stage": stage,
            "exception_type": type(exc).__name__,
            "frames": [
                {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(exc.__traceback__)[-6:]
            ],
        }


def _failed(stage: str, exc: BaseException) -> None:
    if isinstance(exc, GuestFocusStageError):
        raise exc
    raise GuestFocusStageError(stage, exc) from exc


def _candidate_rows(report: dict[str, object]) -> list[dict[str, object]]:
    """Return usable main-window candidates without requiring maximization."""
    rows = report.get("windows", [])
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or str(row.get("process_name", "")).casefold() != "qq.exe"
            or row.get("class_name") != "Chrome_WidgetWin_1"
            or row.get("is_visible") is not True
            or row.get("is_iconic") is True
        ):
            continue
        rect = row.get("rect")
        if not isinstance(rect, dict):
            continue
        try:
            width = int(rect["right"]) - int(rect["left"])
            height = int(rect["bottom"]) - int(rect["top"])
            pid, hwnd = int(row["pid"]), int(row["hwnd"])
        except (KeyError, TypeError, ValueError):
            continue
        if width < 480 or height < 420 or pid <= 0 or hwnd <= 0:
            continue
        result.append(row)
    return result


def _choose_window(report: dict[str, object]) -> tuple[int, int] | None:
    """Prefer the unique foreground QQ shell, otherwise require uniqueness."""
    rows = _candidate_rows(report)
    foreground = [row for row in rows if row.get("is_foreground") is True]
    chosen = foreground[0] if len(foreground) == 1 else rows[0] if len(rows) == 1 else None
    if chosen is None:
        return None
    return int(chosen["pid"]), int(chosen["hwnd"])


def select_and_focus_target(max_wait: int = 30) -> tuple[tuple[int, int], dict[str, object]]:
    try:
        from activate_default_rulepack_guest import _require_guest_context
        _require_guest_context()
    except Exception as exc:
        _failed("guest_guard", exc)
    try:
        from qq_window_metadata_guest import _collect_once
        first = _collect_once()
    except Exception as exc:
        _failed("metadata", exc)

    user32 = ctypes.windll.user32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL

    chosen = None
    report = first
    for attempt in range(max_wait + 1):
        try:
            chosen = _choose_window(report)
            if chosen is not None:
                break
            if attempt < max_wait:
                time.sleep(1)
                report = _collect_once()
        except Exception as exc:
            _failed("metadata", exc)
    if chosen is None:
        raise RuntimeError("QQ_TARGET_NOT_UNIQUE_OR_VISIBLE_TIMEOUT")

    pid, hwnd = chosen
    try:
        actual_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(actual_pid))
        if int(actual_pid.value) != pid:
            raise RuntimeError("QQ_TARGET_PID_MISMATCH")
        if not user32.IsWindowVisible(wintypes.HWND(hwnd)) or user32.IsIconic(wintypes.HWND(hwnd)):
            raise RuntimeError("QQ_TARGET_WINDOW_STATE_CHANGED")
        foreground = int(user32.GetForegroundWindow() or 0)
    except Exception as exc:
        _failed("verify", exc)
    if foreground == hwnd:
        return chosen, {"foreground_requested": False, "foreground_request_return": None,
                        "foreground_succeeded": True}
    try:
        requested = bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))
    except Exception as exc:
        _failed("activate", exc)
    try:
        for _ in range(120):
            actual_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(actual_pid))
            foreground = int(user32.GetForegroundWindow() or 0)
            if int(actual_pid.value) == pid and foreground == hwnd:
                return chosen, {"foreground_requested": True,
                                "foreground_request_return": requested,
                                "foreground_succeeded": True}
            time.sleep(1)
    except Exception as exc:
        _failed("poll", exc)
    raise RuntimeError("QQ_FOREGROUND_REQUEST_TIMEOUT")
