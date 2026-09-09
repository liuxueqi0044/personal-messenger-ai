from __future__ import annotations

import ast
from pathlib import Path


def test_policy_package_has_no_adapter_outbox_or_send_imports():
    package = Path(__file__).parents[2] / "src" / "messenger_ai" / "policy"
    forbidden = (
        "messenger_ai.adapters",
        "messenger_ai.outbox",
        "prepare_send",
        "commit_send",
    )
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        tree = ast.parse(text)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(item.startswith(forbidden[:2]) for item in imports), source
        assert forbidden[2] not in text and forbidden[3] not in text, source


def test_policy_decisions_never_contain_raw_send_authority(make_request, policy_stack):
    engine, _, _ = policy_stack
    decision = engine.evaluate_eligibility(make_request())
    payload = decision.model_dump(mode="json")
    assert "token" not in payload
    assert "authorization_id" not in payload
    assert "body" not in payload
