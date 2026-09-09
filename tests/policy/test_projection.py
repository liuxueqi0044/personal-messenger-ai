from datetime import UTC, datetime

from messenger_ai.llm import ProviderError, ReplyPlanResult
from messenger_ai.llm import ReplyAction as M8Action
from messenger_ai.llm import ReplyPlan as M8Plan
from messenger_ai.llm import RiskLevel as M8Risk
from messenger_ai.policy import (
    ReplyAction,
    RiskLevel,
    project_planner_result,
)


def test_m8_structured_result_projects_without_provider_dependency():
    result = ReplyPlanResult(
        request_id="request-1",
        rule_version="rule-v1",
        context_fingerprint="context-1",
        plan=M8Plan(
            action=M8Action.AUTO_REPLY_CANDIDATE,
            reply_text="还好呀 你呢",
            reply_segments=["还好呀 你呢"],
            risk_level=M8Risk.LOW,
            policy_tags=("ordinary",),
            prohibited_rule_results=(),
            confidence=0.96,
            expires_at=datetime(2026, 9, 8, 13, 0, tzinfo=UTC),
        ),
        model="fake",
        latency_ms=1,
    )
    assessment = project_planner_result(result)
    assert assessment.action is ReplyAction.AUTO_REPLY_CANDIDATE
    assert assessment.risk_level is RiskLevel.LOW
    assert assessment.output_validated


def test_stale_or_failed_m8_result_fails_closed():
    failed = ReplyPlanResult(
        request_id="request-1",
        rule_version="rule-v1",
        context_fingerprint="context-1",
        model="fake",
        latency_ms=1,
        error=ProviderError(category="schema", message="bad schema"),
    )
    assessment = project_planner_result(failed)
    assert assessment.risk_level is RiskLevel.BLOCKED
    assert not assessment.output_validated
