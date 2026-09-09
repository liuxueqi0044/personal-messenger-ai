"""Static guardrail: M6 must not use display-name or unscoped context retrieval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit M6 isolation query patterns")
    parser.add_argument(
        "path", nargs="?", type=Path, default=Path("src/messenger_ai/memory")
    )
    args = parser.parse_args()
    findings: list[dict[str, str | int]] = []
    for file in args.path.rglob("*.py"):
        for line_number, line in enumerate(
            file.read_text(encoding="utf-8").splitlines(), 1
        ):
            lowered = line.lower()
            if (
                "select *" in lowered
                or "like '%" in lowered
                or "display_name" in lowered
                and "where" in lowered
            ):
                findings.append(
                    {
                        "path": str(file),
                        "line": line_number,
                        "rule": "unscoped identity lookup",
                    }
                )
    print(
        json.dumps(
            {"status": "failed" if findings else "passed", "findings": findings},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
