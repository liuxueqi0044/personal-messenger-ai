"""Register and read the currently selected operator-observed QQ direct session."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

_BINDING_ID = re.compile(r"^session-contact-([1-9][0-9]{0,3})$")


def _output_for_binding(binding_id: str) -> Path:
    match = _BINDING_ID.fullmatch(binding_id)
    if match is None:
        raise ValueError("binding-id must be canonical session-contact-N (1 <= N <= 9999)")
    return Path(r"C:\PMAI\data") / f"qq-session-observed-bootstrap-{match.group(1)}.json"


def _write(output: Path, value: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selector-pack", type=Path, required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--operator-observed-direct", action="store_true")
    args = parser.parse_args()
    try:
        output = _output_for_binding(args.binding_id)
    except ValueError as exc:
        parser.error(str(exc))
    report: dict[str, object] = {
        "schema": "pmai-qq-session-observed-bootstrap-v1", "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(), "state": "running", "succeeded": False,
    }
    _write(output, report)
    stage = "initialize"
    try:
        from guest_focus_helper import select_and_focus_target
        from messenger_ai.adapters.qq.models import QQSelectorPack, QQWindow
        from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
        from messenger_ai.adapters.qq.vm_driver.session_identity import observe_selected_identity
        from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility

        stage = "selector_pack"
        pack = QQSelectorPack.model_validate_json(args.selector_pack.read_text(encoding="utf-8"))
        validate_guest_selector_pack(pack)
        stage = "target"
        target, _focus = select_and_focus_target()
        window = QQWindow(process_id=target[0], window_handle=target[1],
                          class_name="Chrome_WidgetWin_1")
        access = WindowsUIAQQAccessibility()
        stage = "reader"
        bubbles = access.list_bubbles(window, pack.selector("bubbles"))
        report["reader"] = {"succeeded": True, "bubble_count": len(bubbles), "bubbles": [{
            "ordinal": index, "direction": bubble.direction.value,
            "text_length": len(bubble.text),
            "text_sha256": hashlib.sha256(bubble.text.encode()).hexdigest(),
        } for index, bubble in enumerate(bubbles)]}
        stage = "candidate_rows"
        rows = access._select(access._window(window), pack.selector("conversations"))
        selected_token = pack.selector("conversations").selected_class_name_token
        selected_count = sum(bool(selected_token and selected_token in
                                  str(getattr(row, "ClassName", "")).split()) for row in rows)
        report["candidate_rows"] = {"count": len(rows), "selected_count": selected_count,
            "classes": [str(getattr(row, "ClassName", "")) for row in rows],
            "patterns": [sorted(access._patterns(row)) for row in rows]}
        stage = "observe_selected"
        evidence = observe_selected_identity(
            accessibility=access, selector_pack=pack, window=window, binding_id=args.binding_id,
            operator_observed_direct=args.operator_observed_direct)
        report.update({"state": "succeeded", "succeeded": True,
            "completed_at": datetime.now(UTC).isoformat(),
            "evidence": evidence.model_dump(mode="json"),
            "participant_signature": evidence.participant_signature,
        })
    except Exception as exc:
        causes = []
        current: BaseException | None = exc
        while current is not None and len(causes) < 5:
            causes.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        report.update({"state": "failed", "failure_stage": stage,
            "completed_at": datetime.now(UTC).isoformat(),
            "error_code": "SESSION_OBSERVED_BOOTSTRAP_FAILED", "exception_type": type(exc).__name__,
            "cause_types": causes,
            "frames": [{"file": Path(frame.filename).name, "function": frame.name,
                        "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-6:]]})
    _write(output, report)
    return 0 if report["succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
