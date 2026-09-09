"""Run the offline prohibited dependency/source audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from messenger_ai.observability.dependencies import DependencyGate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = DependencyGate().audit(args.project_root)
    encoded = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
