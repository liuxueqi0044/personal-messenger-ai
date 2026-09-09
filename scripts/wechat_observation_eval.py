"""Small synthetic M4 smoke evaluator.

This is intentionally not a 500-sample accuracy claim.  Real evaluation must
consume an independently labelled, redacted corpus and report identity,
direction, layout, OCR and duplicate metrics separately.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def evaluate(path: Path) -> dict[str, object]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    categories = Counter(str(record.get("category", "unknown")) for record in records)
    human_review = [
        record["id"]
        for record in records
        if record.get("expected_status") == "human_review"
    ]
    filtered = [
        record["id"]
        for record in records
        if record.get("expected_action") == "filter_non_text"
    ]
    duplicate = [
        record["id"]
        for record in records
        if record.get("expected_action") == "suppress_duplicate"
    ]
    return {
        "fixture": str(path),
        "samples": len(records),
        "synthetic_only": True,
        "accuracy_claim": False,
        "disclaimer": "合成脱敏 smoke fixture，不可代表 500 张真实评测，也不产生真实准确率结论。",
        "categories": dict(sorted(categories.items())),
        "metrics": {
            "identity": {
                "low_confidence_cases": sum(
                    record.get("identity_confidence", 1) < 0.95 for record in records
                ),
                "status": "fixture_expectations_only",
            },
            "direction": {
                "low_confidence_cases": sum(
                    record.get("direction_confidence", 1) < 0.90 for record in records
                ),
                "status": "fixture_expectations_only",
            },
            "layout": {
                "low_confidence_cases": sum(
                    record.get("layout_confidence", 1) < 0.90 for record in records
                ),
                "status": "fixture_expectations_only",
            },
            "ocr": {
                "low_confidence_cases": sum(
                    record.get("ocr_confidence", 1) < 0.90 for record in records
                ),
                "status": "fixture_expectations_only",
            },
            "duplicate_events": {
                "suppression_cases": duplicate,
                "status": "fixture_expectations_only",
            },
            "human_review_cases": human_review,
            "filtered_non_text_cases": filtered,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.fixture), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
