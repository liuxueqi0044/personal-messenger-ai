import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def read_jsonl(name: str):
    path = ROOT / "fixtures" / "llm_evals" / name
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_synthetic_eval_sets_have_required_minimums() -> None:
    assert len(read_jsonl("rule_eval_cases.jsonl")) >= 200
    assert len(read_jsonl("prompt_injection_cases.jsonl")) >= 100


def test_fixture_inputs_are_marked_data_not_claimed_human_results() -> None:
    cases = read_jsonl("prompt_injection_cases.jsonl")
    assert all(item["expected"] == "untrusted" for item in cases)
