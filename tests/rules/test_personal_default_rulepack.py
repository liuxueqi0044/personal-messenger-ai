from pathlib import Path

from messenger_ai.rules import RulePackCompiler, RuleSource, SourceFormat

RULEPACK = Path(__file__).parents[2] / "rulepacks" / "personal-default-v1.yaml"


def test_personal_rulepack_has_relationship_goal_and_bounded_variability() -> None:
    draft = RulePackCompiler().ingest(
        RuleSource(
            name=RULEPACK.name,
            content=RULEPACK.read_bytes(),
            format=SourceFormat.YAML,
        )
    )

    assert draft.report.valid
    assert not draft.report.conflicts
    assert not draft.report.ambiguities
    rules = {
        rule.rule_id: rule
        for rule in (
            draft.normalized.required_behaviors
            + draft.normalized.prohibited_behaviors
            + draft.normalized.escalation_rules
        )
    }
    assert rules["relationship-development-primary-objective"].priority == "system"
    assert "POLICY_GUARD" in rules["no-goal-overrides-consent"].enforcement
    assert "PACING_RULE" in rules["conversation-closure-ignore"].enforcement
    assert "OUTPUT_VALIDATOR" in rules["examples-are-not-templates"].enforcement
    assert "CONTEXT_RULE" in rules["relationship-stage-routing"].enforcement
    assert "OUTPUT_VALIDATOR" in rules["no-example-verbatim-default"].enforcement
    assert draft.normalized.pacing.auto_reply_max_segments == 3
    assert draft.normalized.pacing.long_reply_threshold_chars == 60
    assert draft.normalized.pacing.long_reply_min_latency_seconds >= 30
