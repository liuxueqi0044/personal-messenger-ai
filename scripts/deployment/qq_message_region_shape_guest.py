"""Capture the production Python UIA shape below QQ's current message region."""
from __future__ import annotations

import hashlib
import json
import os
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path


OUTPUT = Path(r"C:\PMAI\data\qq-message-region-shape.json")
MAX_DEPTH = 6
MAX_NODES = 400


def _hash_runtime(control) -> str:
    runtime = control.GetRuntimeId()
    encoded = ",".join(str(int(value)) for value in runtime).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write(report: dict[str, object]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    os.replace(temporary, OUTPUT)


def main() -> int:
    report: dict[str, object] = {
        "schema": "pmai-qq-message-region-shape-v1",
        "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "state": "running",
        "succeeded": False,
    }
    _write(report)
    stage = "initialize"
    try:
        from guest_focus_helper import select_and_focus_target
        from messenger_ai.adapters.qq.models import QQSelector, QQWindow
        from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility

        stage = "target"
        target, _focus = select_and_focus_target()
        window = QQWindow(process_id=target[0], window_handle=target[1],
                          class_name="Chrome_WidgetWin_1")
        access = WindowsUIAQQAccessibility()
        if not access._guest_scope(window):
            raise RuntimeError("TARGET_SCOPE_MISMATCH")

        stage = "message_region"
        selector = QQSelector(name="messages", control_type="Group",
                              class_name_tokens=("ml-root",))
        regions = access._select(access._window(window), selector)
        if len(regions) != 1:
            raise RuntimeError("MESSAGE_REGION_NOT_UNIQUE")
        region = regions[0]
        region_hash = _hash_runtime(region)

        stage = "enumerate"
        nodes: list[dict[str, object]] = []
        queue = [(child, region_hash, 1) for child in region.GetChildren()]
        while queue:
            if len(nodes) >= MAX_NODES:
                raise RuntimeError("MESSAGE_REGION_NODE_LIMIT")
            control, parent_hash, depth = queue.pop(0)
            runtime_hash = _hash_runtime(control)
            children = list(control.GetChildren())
            nodes.append({
                "runtime_id_hash": runtime_hash,
                "parent_runtime_id_hash": parent_hash,
                "depth": depth,
                "control_type": str(getattr(control, "ControlTypeName", "")),
                "class_name": str(getattr(control, "ClassName", "")),
                "patterns": sorted(access._patterns(control)),
                "child_count": len(children),
                "process_id_matches": int(getattr(control, "ProcessId", 0) or 0) == target[0],
                "is_enabled": bool(getattr(control, "IsEnabled", False)),
                "is_offscreen": bool(getattr(control, "IsOffscreen", True)),
            })
            if depth < MAX_DEPTH:
                queue.extend((child, runtime_hash, depth + 1) for child in children)

        report.update({
            "state": "succeeded",
            "succeeded": True,
            "process_id": target[0],
            "window_handle": target[1],
            "message_region": {
                "runtime_id_hash": region_hash,
                "control_type": str(getattr(region, "ControlTypeName", "")),
                "class_name": str(getattr(region, "ClassName", "")),
                "patterns": sorted(access._patterns(region)),
                "direct_child_count": len(region.GetChildren()),
            },
            "node_count": len(nodes),
            "max_depth": MAX_DEPTH,
            "nodes": nodes,
        })
    except Exception as exc:
        code = str(exc) if str(exc) in {
            "TARGET_SCOPE_MISMATCH", "MESSAGE_REGION_NOT_UNIQUE",
            "MESSAGE_REGION_NODE_LIMIT",
        } else "MESSAGE_REGION_SHAPE_FAILED"
        report.update({
            "state": "failed",
            "failure_stage": stage,
            "error_code": code,
            "exception_type": type(exc).__name__,
            "frames": [{"file": Path(frame.filename).name,
                        "function": frame.name, "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-6:]],
        })
    _write(report)
    return 0 if report["succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
