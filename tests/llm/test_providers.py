import asyncio
from datetime import UTC, datetime

import pytest

from messenger_ai.llm import (
    ContactProjection,
    InboundItem,
    OpenAIResponsesProvider,
    ReplyPlanRequest,
    RuleProjection,
    classify_provider_error,
)


def request() -> ReplyPlanRequest:
    return ReplyPlanRequest(
        request_id="r",
        account_id="a",
        contact=ContactProjection(contact_id="c", conversation_id="v"),
        rules=RuleProjection(rulepack_id="p", rule_version="1", source_hash="h"),
        inbound=(InboundItem(message_key="m", text="hi"),),
        context_fingerprint="f",
        created_at=datetime.now(UTC),
    )


class StatusError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class Transport:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def valid_response():
    return {
        "output_text": '{"action":"draft","reply_text":"好呀","reply_segments":["好呀"],"risk_level":"low","policy_tags":[],"must_rule_results":[],"prohibited_rule_results":[],"assumptions":[],"confidence":0.8,"expires_at":null,"selection_reason":"回应","variation_seed":"s"}',
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    }


def test_openai_transport_is_structured_no_tools_no_store() -> None:
    transport = Transport(valid_response())
    result = asyncio.run(
        OpenAIResponsesProvider(model="gpt-test", transport=transport).plan_reply(
            request()
        )
    )
    assert result.plan is not None
    assert transport.kwargs["store"] is False
    assert transport.kwargs["tools"] == []
    assert transport.kwargs["text"]["format"]["type"] == "json_schema"
    assert transport.kwargs["text"]["format"]["strict"] is True


def test_invalid_schema_is_failure_not_repaired() -> None:
    transport = Transport({"output_text": '{"action":"draft","reply_text":"x"}'})
    result = asyncio.run(
        OpenAIResponsesProvider(model="gpt-test", transport=transport).plan_reply(
            request()
        )
    )
    assert result.plan is None
    assert result.error is not None and result.error.category == "schema"


@pytest.mark.parametrize(
    ("exc", "category", "retryable"),
    [
        (StatusError(429), "rate_limited", True),
        (StatusError(503), "server_error", True),
        (TimeoutError(), "timeout", True),
    ],
)
def test_failure_classification(exc, category, retryable) -> None:
    result = classify_provider_error(exc)
    assert result.category == category
    assert result.retryable is retryable
