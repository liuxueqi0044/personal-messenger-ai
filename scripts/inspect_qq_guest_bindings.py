"""Read-only, guest-only export of QQ conversation identity evidence.

This command never selects a conversation, edits the composer, sends, or marks
capabilities supported.  A human must review its output before creating a binding.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from messenger_ai.adapters.qq.models import QQSelectorPack
from messenger_ai.adapters.qq.vm_driver.transport import UIAUnavailable, WindowsUIAQQAccessibility


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guest-only read-only QQ binding evidence export")
    parser.add_argument("--selector-pack", required=True, help="candidate selector-pack JSON")
    parser.add_argument("--output", help="write evidence JSON; stdout when omitted")
    args = parser.parse_args(argv)
    try:
        pack = QQSelectorPack.model_validate_json(Path(args.selector_pack).read_text(encoding="utf-8"))
        port = WindowsUIAQQAccessibility()  # includes real VirtualBox guest identity verification
        windows = port.find_main_windows(pack.selector("main_window"))
        if len(windows) != 1:
            raise UIAUnavailable("QQ main window absent or ambiguous")
        conversations = port.list_conversations(windows[0], pack.selector("conversations"))
        report = {
            "schema": "pmai-v5-qq-binding-evidence-1", "read_only": True,
            "send_attempted": False, "client_version_expected": pack.client_version,
            "environment_fingerprint_expected": pack.environment_fingerprint,
            "binding_certified": False,
            "status": "candidates_require_human_review" if conversations else "binding_not_certifiable",
            "candidates": [{
                "platform_conversation_id": item.internal_id,
                "display_name": item.display_name,
                "participant_signature": item.participant_signature,
                "tree_digest": item.tree_digest,
            } for item in conversations],
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered)
        return 0 if conversations else 2
    except (OSError, ValueError, UIAUnavailable) as exc:
        print(json.dumps({"read_only": True, "send_attempted": False,
                          "status": "binding_not_certifiable", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
