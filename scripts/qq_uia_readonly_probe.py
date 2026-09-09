"""Run the bundled read-only QQ UI Automation feasibility probe."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def classify(report: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify implementation size without treating read access as send proof."""
    if not report.get("succeeded"):
        return "indeterminate", [str(report.get("error_code", "PROBE_FAILED"))]

    node_count = int(report.get("node_count_examined", 0))
    signals = report.get("semantic_signals", {})
    patterns = report.get("pattern_counts", {})
    checks = {
        "uia_tree": node_count >= 20,
        "message_text": int(signals.get("message_region_named_text_nodes", 0)) >= 3
        or int(signals.get("named_text_nodes", 0)) >= 3
        or int(patterns.get("text", 0)) >= 1,
        "conversation_selection": int(
            signals.get("selectable_conversation_candidates", 0)
        )
        >= 1
        or int(signals.get("left_pane_invoke_candidates", 0)) >= 3,
        "composer": int(signals.get("composer_candidates", 0)) >= 1,
        "send_button": int(signals.get("exact_send_button_candidates", 0)) >= 1
        or int(signals.get("send_keyword_candidates", 0)) >= 1,
    }
    missing = [name for name, passed in checks.items() if not passed]
    report["feasibility_checks"] = checks
    stable_ids = bool(report.get("automation_ids"))
    standard_selection = int(signals.get("selectable_conversation_candidates", 0)) >= 1
    report["selector_quality"] = {
        "has_automation_ids": stable_ids,
        "has_standard_selection_pattern": standard_selection,
    }
    if not missing and (stable_ids or standard_selection):
        return "small_tool_candidate", []
    if not missing:
        return "medium_driver_work", ["stable_selector_metadata"]
    if checks["uia_tree"] and sum(checks.values()) >= 3:
        return "medium_driver_work", missing
    return "large_architecture_likely", missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only QQ UIA feasibility probe; never clicks, types, or sends"
    )
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument(
        "--include-topology",
        action="store_true",
        help="include redacted structural nodes; never includes UIA control names",
    )
    args = parser.parse_args()
    if args.max_nodes < 1 or args.max_nodes > 20000:
        parser.error("--max-nodes must be between 1 and 20000")

    helper = Path(__file__).with_name("qq_uia_probe_helper") / "QQ.UiaProbe.csproj"
    command = [
        "dotnet",
        "run",
        "--project",
        str(helper),
        "--configuration",
        "Release",
        "--verbosity",
        "quiet",
        "--",
        "--max-nodes",
        str(args.max_nodes),
    ]
    if args.include_topology:
        command.append("--include-topology")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            json.dumps(
                {
                    "succeeded": False,
                    "read_only": True,
                    "verdict": "indeterminate",
                    "error_code": type(exc).__name__.upper(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    try:
        report = json.loads(completed.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError:
        report = {
            "succeeded": False,
            "read_only": True,
            "error_code": "INVALID_PROBE_OUTPUT",
            "process_returncode": completed.returncode,
        }
    verdict, missing = classify(report)
    report["verdict"] = verdict
    report["missing_signals"] = missing
    report["real_send_attempted"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("succeeded") else 2


if __name__ == "__main__":
    sys.exit(main())
