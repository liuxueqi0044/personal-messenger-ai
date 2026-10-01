"""Offline contract checks, not evidence of a live model's compliance."""

import asyncio
from datetime import UTC, datetime

import pytest

from messenger_ai.llm import (
    ContactProjection,
    DeepSeekResponsesProvider,
    InboundItem,
    OpenAIResponsesProvider,
    ReplyPlan,
    ReplyPlanRequest,
    RuleProjection,
    build_projection,
)
from messenger_ai.llm.prompts import SYSTEM_BASE
from messenger_ai.policy import (
    PolicyEngine,
    PolicyOutcome,
    PolicyReason,
    project_planner_result,
)
from scripts.policy_matrix import NOW, request_for


def request(*, content_policy_checks_enabled: bool = True) -> ReplyPlanRequest:
    return ReplyPlanRequest(
        request_id="synthetic-contract-request",
        account_id="synthetic-account",
        contact=ContactProjection(
            contact_id="synthetic-contact", conversation_id="synthetic-conversation"
        ),
        rules=RuleProjection(
            rulepack_id="synthetic-rules",
            rule_version="v1",
            source_hash="synthetic-hash",
            content_policy_checks_enabled=content_policy_checks_enabled,
            prohibited=("不得捏造个人事实",),
        ),
        inbound=(InboundItem(message_key="synthetic-message", text="晚上好呀"),),
        context_fingerprint="synthetic-context",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def assert_violations_only_contract(text: str) -> None:
    assert "当前候选回复实际违反的禁止规则" in text
    assert "不得遗漏实际违规" in text
    assert "没有违规时必须返回空数组 []" in text
    assert "不得填写 PASS、检查通过、已遵守或未违反规则的说明" in text


def test_prohibited_rule_schema_preserves_array_and_empty_default() -> None:
    field = ReplyPlan.model_json_schema()["properties"]["prohibited_rule_results"]
    assert_violations_only_contract(field["description"])
    assert field["type"] == "array"
    assert field["items"] == {"type": "string"}
    assert field["default"] == []
    plan = ReplyPlan(action="ignore")
    assert plan.prohibited_rule_results == ()
    assert plan.model_dump(mode="json")["prohibited_rule_results"] == []


def test_prohibited_rule_prompt_respects_content_policy_switch() -> None:
    assert_violations_only_contract(build_projection(request()).system_safety)
    disabled = build_projection(request(content_policy_checks_enabled=False))
    assert disabled.system_safety == SYSTEM_BASE
    assert "不得捏造个人事实" not in disabled.behavior


class Transport:
    def __init__(self, results: list[str]) -> None:
        self.plan = ReplyPlan(
            action="auto_reply_candidate",
            reply_text="晚上好呀",
            reply_segments=["晚上好呀"],
            prohibited_rule_results=results,
            confidence=0.9,
        )
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return {"output_text": self.plan.model_dump_json()}


@pytest.mark.parametrize("provider_name", ["openai", "deepseek"])
@pytest.mark.parametrize(
    "results",
    [
        [],
        ["no-private-data-disclosure"],
        ["泄露个人隐私"],
        ["未确认恋爱或排他关系"],
        ["未编造个人事实或经历"],
        ["未索取礼物、转账或经济付出"],
        ["PASS", "未编造个人事实或经历", "no-private-data-disclosure"],
    ],
)
def test_provider_contract_and_policy_projection_do_not_filter_results(
    provider_name: str, results: list[str]
) -> None:
    transport = Transport(results)
    provider = (
        DeepSeekResponsesProvider(api_key="synthetic-key", transport=transport)
        if provider_name == "deepseek"
        else OpenAIResponsesProvider(model="synthetic-model", transport=transport)
    )
    result = asyncio.run(provider.plan_reply(request()))

    assert result.error is None
    assert result.plan is not None
    assert result.plan.prohibited_rule_results == tuple(results)
    assert result.plan.model_dump(mode="json")["prohibited_rule_results"] == results
    assessment = project_planner_result(result)
    assert assessment.output_validated
    assert assessment.prohibited_rule_ids == tuple(results)
    assert assessment.confidence == 0.9

    policy_request = request_for("晚上好呀", result.plan.reply_text).model_copy(
        update={"assessment": assessment}
    )
    decision = PolicyEngine(lambda: NOW).evaluate_eligibility(policy_request)
    if results:
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert PolicyReason.PROHIBITED_RULE_HIT in decision.reason_codes
        assert decision.rule_ids == tuple(results)
    else:
        assert decision.outcome is PolicyOutcome.AUTO_ELIGIBLE
        assert PolicyReason.PROHIBITED_RULE_HIT not in decision.reason_codes

    assert transport.kwargs is not None
    output_format = transport.kwargs["text"]["format"]
    assert output_format["strict"] is True
    schema = output_format["schema"]
    assert schema["additionalProperties"] is False
    assert "prohibited_rule_results" in schema["required"]
    assert_violations_only_contract(
        schema["properties"]["prohibited_rule_results"]["description"]
    )
    assert_violations_only_contract(transport.kwargs["input"][0]["content"][0]["text"])
