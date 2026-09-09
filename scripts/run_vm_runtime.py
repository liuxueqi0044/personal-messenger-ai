from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from messenger_ai.llm.deepseek import DeepSeekResponsesProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal Messenger AI V5 guest runtime")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true", help="validate configuration without QQ or API activity")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if config.get("schema") != "pmai-v5-runtime-1":
        raise SystemExit("unsupported or missing runtime config schema")
    if not config.get("contacts"):
        raise SystemExit("at least one explicitly configured contact is required")
    if any(item.get("rulepack_status") != "active" for item in config["contacts"]):
        raise SystemExit("every configured contact must reference an already active RulePack")
    if args.check:
        print("configuration valid; no QQ login, send, or API call performed")
        return 0
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY must be supplied by the guest environment")
    # Concrete binding/selector construction is deliberately delegated to the
    # vm_driver assembly; no string recipient or bare send endpoint exists here.
    raise SystemExit("QQ binding objects must be created by the verified VM setup flow")


if __name__ == "__main__":
    raise SystemExit(main())
