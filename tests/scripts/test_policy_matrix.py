"""Keep the published acceptance command aligned with the live policy contract."""
from __future__ import annotations

import json

from scripts import policy_matrix


def test_matrix_exercises_content_policy_in_a_certified_direct_conversation(capsys):
    assert policy_matrix.main() == 0
    report = json.loads(capsys.readouterr().out)
    rows = {row["case"]: row for row in report["rows"]}
    assert report["passed"] == report["total"]
    assert rows["ordinary"]["actual"] == "auto_eligible"
    assert rows["ordinary"]["reason_codes"] == ["ELIGIBLE_LOW_RISK"]
    # A missing fixture identity must not accidentally make every sensitive
    # case pass by forcing unrelated manual review.
    for name in ("money", "credentials", "privacy", "meeting", "medical", "conflict"):
        assert rows[name]["actual"] == "review"
        assert "SENSITIVE_TOPIC" in rows[name]["reason_codes"]
        assert "UNSUPPORTED_CONVERSATION" not in rows[name]["reason_codes"]
    assert "PROMPT_INJECTION" in rows["injection"]["reason_codes"]
    assert rows["prohibited_template"]["actual"] == "blocked"
