from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.execution_guard import scan_paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed static audit for APIs forbidden by the D0 execution contract."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[PROJECT_ROOT / "src" / "messenger_ai"],
        help="Python source files or directories to audit",
    )
    args = parser.parse_args()
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        print(json.dumps({"status": "error", "missing": missing}, indent=2))
        return 2
    findings = scan_paths(args.paths)
    report = {
        "status": "failed" if findings else "passed",
        "scanned": [str(path.resolve()) for path in args.paths],
        "finding_count": len(findings),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
