"""Validate a rule source without activating it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from messenger_ai.rules import RulePackCompiler
from messenger_ai.rules.compiler import RuleCompilationError
from messenger_ai.rules.models import RuleSource


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = RuleSource(name=args.source.name, content=args.source.read_bytes())
    try:
        draft = RulePackCompiler().ingest(source)
    except RuleCompilationError as exc:
        report = exc.report or {"valid": False, "errors": [str(exc)]}
        print(
            json.dumps(
                report.model_dump(mode="json")
                if hasattr(report, "model_dump")
                else report,
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2) from exc
    print(
        json.dumps(
            {
                "draft_id": draft.draft_id,
                "version": draft.version,
                "source_hash": draft.source_hash,
                "report": draft.report.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
