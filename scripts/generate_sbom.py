"""Generate the direct-dependency SBOM entirely offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from messenger_ai.observability.sbom import generate_sbom


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parents[1] / "docs" / "sbom.json"
    )
    args = parser.parse_args()
    document = generate_sbom(args.project_root)
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(f"wrote {len(document['components'])} direct dependency records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
