"""Read-only metadata for visible QQ process windows; no titles or content."""

from __future__ import annotations

import ctypes
import json
import os
import time
import traceback
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path


OUTPUT = Path(r"C:\PMAI\data\qq-window-metadata.json")
QQ_IMAGES = {"qq.exe", "qqnt.exe"}
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _qq_processes() -> dict[int, str]:
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snap = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise OSError("process_snapshot_failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found: dict[int, str] = {}
        if not kernel.Process32FirstW(snap, ctypes.byref(entry)):
            return found
        while True:
            name = str(entry.szExeFile).casefold()
            if name in QQ_IMAGES:
                found[int(entry.th32ProcessID)] = str(entry.szExeFile)
            if not kernel.Process32NextW(snap, ctypes.byref(entry)):
                break
        return found
    finally:
        kernel.CloseHandle(snap)


def _collect_once() -> dict[str, object]:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    for name in ("IsWindowVisible", "IsIconic", "IsZoomed"):
        fn = getattr(user32, name)
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.BOOL
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    qq = _qq_processes()
    foreground = int(user32.GetForegroundWindow() or 0)
    rows: list[dict[str, object]] = []

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_id = int(pid.value)
        if process_id not in qq:
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        rows.append(
            {
                "pid": process_id,
                "hwnd": int(hwnd),
                "process_name": qq[process_id],
                "class_name": class_name.value,
                "is_visible": bool(user32.IsWindowVisible(hwnd)),
                "is_iconic": bool(user32.IsIconic(hwnd)),
                "is_zoomed": bool(user32.IsZoomed(hwnd)),
                "is_foreground": int(hwnd) == foreground,
                "rect": {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                },
            }
        )
        return True

    if not user32.EnumWindows(callback, 0):
        raise OSError("window_enumeration_failed")
    return {
        "schema": "pmai-qq-window-metadata-v1",
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "qq_process_count": len(qq),
        "windows": rows,
    }


def collect() -> dict[str, object]:
    latest: dict[str, object] = {}
    for attempt in range(31):
        latest = _collect_once()
        windows = latest.get("windows", [])
        if any(
            isinstance(row, dict)
            and row.get("is_visible")
            and row.get("is_zoomed")
            and row.get("is_foreground")
            for row in windows
        ):
            latest["foreground_wait_status"] = "satisfied"
            latest["foreground_wait_seconds"] = attempt
            return latest
        if attempt < 30:
            time.sleep(1)
    latest["foreground_wait_status"] = "timeout"
    latest["foreground_wait_seconds"] = 30
    return latest


def _safe_traceback(exc: BaseException) -> list[dict[str, object]]:
    """Return code-location only: never exception text, source, args, or locals."""
    return [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]
    ]


def _safe_error(code: str, exc: BaseException | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "pmai-qq-window-metadata-v1", "error_code": code, "windows": []
    }
    if exc is not None:
        value["diagnostic"] = {
            "exception_type": type(exc).__name__,
            "frames": _safe_traceback(exc),
        }
    return value


def main() -> int:
    try:
        if os.name != "nt":
            raise RuntimeError("windows_guest_required")
        from activate_default_rulepack_guest import _require_guest_context

        _require_guest_context()
        payload = collect()
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temp = OUTPUT.with_suffix(".tmp.json")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp, OUTPUT)
        return 0
    except RuntimeError as exc:
        try:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(
                json.dumps(_safe_error(str(exc)), sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass
        return 2
    except Exception as exc:
        try:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(
                json.dumps(_safe_error("metadata_collection_failed", exc), sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
