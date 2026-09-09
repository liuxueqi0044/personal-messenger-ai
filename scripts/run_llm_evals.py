"""Offline M8 evaluation harness.

The cases are synthetic contract probes, not claims about human preference or
real-user performance.  The script only reports counts and deterministic
provider/schema outcomes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from messenger_ai.llm import (
    ContactProjection,
    FakeProvider,
    InboundItem,
    ReplyPlan,
    ReplyPlanRequest,
    RuleProjection,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "llm_evals"


def load(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def synthetic_request(text: str, index: int) -> ReplyPlanRequest:
    return ReplyPlanRequest(
        request_id=f"eval-{index}",
        account_id="synthetic-account",
        contact=ContactProjection(
            contact_id="synthetic-contact", conversation_id="synthetic-conversation"
        ),
        rules=RuleProjection(
            rulepack_id="synthetic", rule_version="fixture-v1", source_hash="fixture"
        ),
        inbound=(InboundItem(message_key=f"message-{index}", text=text),),
        context_fingerprint=f"message-{index}",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


async def evaluate() -> dict[str, int | str]:
    rule_cases = load("rule_eval_cases.jsonl")
    injection_cases = load("prompt_injection_cases.jsonl")
    provider = FakeProvider(
        ReplyPlan(
            action="ignore",
            selection_reason="synthetic fixture",
            variation_seed="fixture",
        )
    )
    for index, case in enumerate(rule_cases + injection_cases):
        await provider.plan_reply(
            synthetic_request(case.get("input_text", case.get("payload", "")), index)
        )
    return {
        "rule_cases": len(rule_cases),
        "injection_cases": len(injection_cases),
        "provider_calls": len(provider.requests),
        "evaluation_status": "synthetic_contract_only",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run offline synthetic M8 contract evaluations"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    args = parser.parse_args()
    import asyncio

    result = asyncio.run(evaluate())
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
