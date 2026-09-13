"""Read current QQ bubbles through the production decoder without exporting text."""
from __future__ import annotations

import hashlib
import json
import os
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

OUTPUT = Path(r"C:\PMAI\data\qq-message-reader-diagnostic.json")


def _write(value: dict[str, object]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, OUTPUT)


def main() -> int:
    report: dict[str, object] = {"schema": "pmai-qq-message-reader-v1",
        "run_id": str(uuid.uuid4()), "started_at": datetime.now(UTC).isoformat(),
        "state": "running", "succeeded": False}
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
        stage = "read"
        bubbles = access.list_bubbles(window, QQSelector(
            name="bubbles", control_type="Group", class_name_tokens=("ml-root",)))
        report.update({"state": "succeeded", "succeeded": True,
            "process_id": target[0], "window_handle": target[1],
            "bubble_count": len(bubbles), "bubbles": [{
                "ordinal": index, "direction": bubble.direction.value,
                "text_length": len(bubble.text),
                "text_sha256": hashlib.sha256(bubble.text.encode()).hexdigest(),
            } for index, bubble in enumerate(bubbles)]})
    except Exception as exc:
        report.update({"state": "failed", "failure_stage": stage,
            "error_code": "MESSAGE_READER_FAILED", "exception_type": type(exc).__name__,
            "frames": [{"file": Path(frame.filename).name, "function": frame.name,
                        "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-6:]]})
    _write(report)
    return 0 if report["succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
