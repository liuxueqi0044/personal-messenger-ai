"""Select one certified QQ foreground window and run the frozen read-only probe."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from qq_window_metadata_guest import _collect_once
from activate_default_rulepack_guest import _require_guest_context


PROBE_NAME = "QQ.UiaProbe.exe"
OUTPUT = Path(r"C:\PMAI\data\fixedqq-uia-private-probe.json")
MAX_WAIT_SECONDS = 30


def _safe_error(code: str) -> dict[str, Any]:
    return {
        "probe_version": "qq-uia-readonly-v1",
        "succeeded": False,
        "error_code": code,
        "read_only": True,
    }


def _write(payload: str | dict[str, Any]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp.json")
    if isinstance(payload, str):
        temp.write_text(payload, encoding="utf-8")
    else:
        temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, OUTPUT)


def _candidate_windows(report: dict[str, object]) -> list[dict[str, object]]:
    rows = report.get("windows", [])
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("process_name", "")).casefold() == "qq.exe"
        and row.get("class_name") == "Chrome_WidgetWin_1"
        and row.get("is_visible") is True
        and row.get("is_zoomed") is True
        and row.get("is_foreground") is True
        and int(row.get("pid", 0) or 0) > 0
        and int(row.get("hwnd", 0) or 0) > 0
    ]


def select_target() -> tuple[int, int] | None:
    for _ in range(MAX_WAIT_SECONDS + 1):
        candidates = _candidate_windows(_collect_once())
        if len(candidates) == 1:
            return int(candidates[0]["pid"]), int(candidates[0]["hwnd"])
        if _ < MAX_WAIT_SECONDS:
            time.sleep(1)
    return None


def run(probe_path: Path) -> int:
    _require_guest_context()
    target = select_target()
    if target is None:
        _write(_safe_error("QQ_TARGET_NOT_UNIQUE_OR_FOREGROUND_TIMEOUT"))
        return 2
    pid, hwnd = target
    command = [
        str(probe_path),
        "--target-qq-pid",
        str(pid),
        "--target-qq-hwnd",
        str(hwnd),
        "--max-nodes",
        "2000",
        "--include-topology",
        "--wait-for-foreground-ms",
        "10000",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _write(_safe_error("QQ_UIA_PROBE_TIMEOUT"))
        return 2
    except OSError:
        _write(_safe_error("QQ_UIA_PROBE_EXECUTION_FAILED"))
        return 2
    stdout = result.stdout.strip()
    if not stdout:
        _write(_safe_error("QQ_UIA_PROBE_EMPTY_OUTPUT"))
        return 2
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        _write(_safe_error("QQ_UIA_PROBE_INVALID_JSON"))
        return 2
    if not isinstance(parsed, dict):
        _write(_safe_error("QQ_UIA_PROBE_INVALID_REPORT"))
        return 2
    _write(stdout)
    return int(result.returncode)


def main() -> int:
    try:
        if os.name != "nt":
            raise RuntimeError("windows_guest_required")
        probe = Path(__file__).with_name(PROBE_NAME)
        if not probe.is_file():
            _write(_safe_error("QQ_UIA_PROBE_MISSING"))
            return 2
        return run(probe)
    except RuntimeError as exc:
        _write(_safe_error(str(exc)))
        return 2
    except OSError:
        _write(_safe_error("QQ_PRIVATE_PROBE_FAILED"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
