from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime

import pytest

from messenger_ai.llm import (
    ContactProjection,
    DeepSeekResponsesProvider,
    InboundItem,
    ReplyPlanRequest,
    RuleProjection,
)


def request() -> ReplyPlanRequest:
    return ReplyPlanRequest(
        request_id="deepseek-v5-test",
        account_id="account-test",
        contact=ContactProjection(contact_id="contact-a", conversation_id="conversation-a"),
        rules=RuleProjection(rulepack_id="rules", rule_version="v1", source_hash="hash"),
        inbound=(InboundItem(message_key="message-1", text="你好"),),
        context_fingerprint="context-fingerprint",
        created_at=datetime.now(UTC),
    )


def response(text: str = "{\"action\":\"draft\",\"reply_text\":\"好呀\",\"reply_segments\":[\"好呀\"],\"risk_level\":\"low\",\"policy_tags\":[],\"must_rule_results\":[],\"prohibited_rule_results\":[],\"assumptions\":[],\"confidence\":0.8,\"expires_at\":null,\"selection_reason\":\"回应\",\"variation_seed\":\"s\"}") -> dict:
    return {"id": "resp-1", "object": "response", "status": "completed", "model": "deepseek-v4-flash", "output_text": text, "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}}


class Transport:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def test_responses_request_uses_official_model_schema_and_no_secret() -> None:
    transport = Transport(response())
    result = asyncio.run(
        DeepSeekResponsesProvider(api_key="test-key", transport=transport, timeout_seconds=7).plan_reply(request())
    )
    assert result.plan is not None
    assert result.model == "deepseek-v4-flash"
    assert transport.kwargs["model"] == "deepseek-v4-flash"
    assert transport.kwargs["store"] is False
    assert transport.kwargs["tools"] == []
    assert transport.kwargs["text"]["format"]["type"] == "json_schema"
    assert transport.kwargs["text"]["format"]["strict"] is True
    assert "test-key" not in repr(transport.kwargs)


def test_default_model_is_current_deepseek_responses_model() -> None:
    provider = DeepSeekResponsesProvider(api_key="test-key", transport=Transport(response()))
    assert provider.model == "deepseek-v4-flash"


def test_missing_key_is_rejected_even_with_mock_transport() -> None:
    with pytest.raises(ValueError, match="api_key"):
        DeepSeekResponsesProvider(transport=Transport(response()))


def test_model_and_timeout_are_bounded() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        DeepSeekResponsesProvider(api_key="test-key", model="deepseek-chat", transport=Transport(response()))
    with pytest.raises(ValueError, match="timeout_seconds"):
        DeepSeekResponsesProvider(api_key="test-key", transport=Transport(response()), timeout_seconds=0)


def test_sdk_construction_uses_deepseek_base_url_and_finite_timeout(monkeypatch) -> None:
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))
    DeepSeekResponsesProvider(api_key="test-key", base_url="https://api.deepseek.com/", timeout_seconds=11)
    assert captured == {
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com",
        "timeout": 11,
        "max_retries": 0,
    }


def test_invalid_structured_output_is_a_schema_error() -> None:
    transport = Transport(response('{"action":"draft","reply_text":"x"}'))
    result = asyncio.run(
        DeepSeekResponsesProvider(api_key="test-key", transport=transport).plan_reply(request())
    )
    assert result.plan is None
    assert result.error is not None and result.error.category == "schema"


def test_timeout_is_bounded_and_never_raises_to_caller() -> None:
    async def slow():
        await asyncio.sleep(0.05)
        return response()

    class SlowTransport(Transport):
        def create(self, **kwargs):
            self.kwargs = kwargs
            return slow()

    result = asyncio.run(
        DeepSeekResponsesProvider(api_key="test-key", transport=SlowTransport(None), timeout_seconds=0.001).plan_reply(request())
    )
    assert result.plan is None
    assert result.error is not None and result.error.category == "timeout"
