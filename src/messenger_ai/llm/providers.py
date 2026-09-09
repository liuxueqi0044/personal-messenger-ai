"""Replaceable model providers with no platform side effects."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from .models import (
    ProviderError,
    ReplyPlan,
    ReplyPlanRequest,
    ReplyPlanResult,
    UsageRecord,
)
from .prompts import _output_schema, build_responses_input


class ModelProvider(Protocol):
    async def plan_reply(self, request: ReplyPlanRequest) -> ReplyPlanResult: ...


ModelProviderPort = ModelProvider


class ResponsesTransport(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class FakeProvider:
    """Deterministic provider used by contract tests and offline evaluation."""

    def __init__(
        self,
        response: ReplyPlan | dict[str, Any] | None = None,
        *,
        handler: Callable[[ReplyPlanRequest], ReplyPlan | dict[str, Any]] | None = None,
        error: ProviderError | None = None,
        model: str = "fake-model",
        latency_ms: int = 0,
    ) -> None:
        self.response = response
        self.handler = handler
        self.error = error
        self.model = model
        self.latency_ms = latency_ms
        self.requests: list[ReplyPlanRequest] = []

    async def plan_reply(self, request: ReplyPlanRequest) -> ReplyPlanResult:
        self.requests.append(request)
        if self.error is not None:
            return ReplyPlanResult(
                request_id=request.request_id,
                rule_version=request.rules.rule_version,
                context_fingerprint=request.context_fingerprint,
                model=self.model,
                latency_ms=self.latency_ms,
                error=self.error,
            )
        if self.handler is not None:
            value = self.handler(request)
        elif self.response is not None:
            value = self.response
        else:
            value = ReplyPlan(
                action="ignore",
                selection_reason="没有足够上下文",
                variation_seed="fake-0",
            )
        try:
            plan = (
                value
                if isinstance(value, ReplyPlan)
                else ReplyPlan.model_validate(value)
            )
        except Exception as exc:  # noqa: BLE001 - fake provider mirrors schema failure
            return ReplyPlanResult(
                request_id=request.request_id,
                rule_version=request.rules.rule_version,
                context_fingerprint=request.context_fingerprint,
                model=self.model,
                latency_ms=self.latency_ms,
                error=ProviderError(
                    category="schema", message=str(exc)[:500], retryable=False
                ),
            )
        return ReplyPlanResult(
            request_id=request.request_id,
            rule_version=request.rules.rule_version,
            context_fingerprint=request.context_fingerprint,
            plan=plan.normalized(),
            model=self.model,
            latency_ms=self.latency_ms,
            usage=UsageRecord(),
        )


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter; SDK import is delayed until construction."""

    def __init__(
        self,
        *,
        model: str,
        transport: ResponsesTransport | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        if transport is None:
            # Keep the optional dependency out of import-time dependency graphs.
            try:
                from openai import AsyncOpenAI  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "install the llm extra to use OpenAI provider"
                ) from exc
            self.transport = AsyncOpenAI()
        else:
            self.transport = transport

    async def plan_reply(self, request: ReplyPlanRequest) -> ReplyPlanResult:
        started = time.perf_counter()
        kwargs = {
            "model": self.model,
            "input": build_responses_input(request),
            "store": False,
            "tools": [],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "reply_plan",
                    "strict": True,
                    "schema": _output_schema(),
                }
            },
        }
        try:
            target = getattr(self.transport, "responses", self.transport)
            call = target.create(**kwargs)
            response = (
                await asyncio.wait_for(call, timeout=self.timeout_seconds)
                if inspect.isawaitable(call)
                else call
            )
        except Exception as exc:  # noqa: BLE001 - provider SDK errors are heterogeneous
            error = classify_provider_error(exc)
            return ReplyPlanResult(
                request_id=request.request_id,
                rule_version=request.rules.rule_version,
                context_fingerprint=request.context_fingerprint,
                model=self.model,
                latency_ms=_elapsed_ms(started),
                error=error,
            )
        try:
            raw = _extract_output(response)
            plan = ReplyPlan.model_validate(raw).normalized()
        except Exception as exc:  # noqa: BLE001 - schema/parser errors need one safe path
            return ReplyPlanResult(
                request_id=request.request_id,
                rule_version=request.rules.rule_version,
                context_fingerprint=request.context_fingerprint,
                model=self.model,
                latency_ms=_elapsed_ms(started),
                error=ProviderError(
                    category="schema", message=str(exc)[:500], retryable=False
                ),
            )
        usage = _extract_usage(response)
        return ReplyPlanResult(
            request_id=request.request_id,
            rule_version=request.rules.rule_version,
            context_fingerprint=request.context_fingerprint,
            plan=plan,
            model=self.model,
            latency_ms=_elapsed_ms(started),
            usage=usage,
        )


OpenAIProvider = OpenAIResponsesProvider


def _extract_output(response: Any) -> Any:
    if isinstance(response, dict):
        if "output_parsed" in response:
            return response["output_parsed"]
        if "output_text" in response:
            return json.loads(response["output_text"])
        if "output" in response:
            output = response["output"]
        else:
            return response
    else:
        if getattr(response, "output_parsed", None) is not None:
            return response.output_parsed
        output_text = getattr(response, "output_text", None)
        if output_text:
            return json.loads(output_text)
        output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = (
                item.get("content", [])
                if isinstance(item, dict)
                else getattr(item, "content", [])
            )
            for part in content:
                parsed = (
                    part.get("parsed")
                    if isinstance(part, dict)
                    else getattr(part, "parsed", None)
                )
                if parsed is not None:
                    return parsed
                text = (
                    part.get("text")
                    if isinstance(part, dict)
                    else getattr(part, "text", None)
                )
                if text:
                    return json.loads(text)
    raise ValueError("Responses API returned no structured output")


def _extract_usage(response: Any) -> UsageRecord:
    usage = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return UsageRecord()
    get = (
        usage.get
        if isinstance(usage, dict)
        else lambda key, default=0: getattr(usage, key, default)
    )
    prompt = int(get("input_tokens", get("prompt_tokens", 0)) or 0)
    completion = int(get("output_tokens", get("completion_tokens", 0)) or 0)
    total = int(get("total_tokens", prompt + completion) or 0)
    cost = get("cost_usd", None)
    return UsageRecord(
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=total,
        cost_usd=float(cost) if cost is not None else None,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def classify_provider_error(exc: BaseException) -> ProviderError:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    text = str(exc)[:500] or exc.__class__.__name__
    if isinstance(status, int) and status == 429:
        return ProviderError(
            category="rate_limited", message=text, retryable=True, status_code=status
        )
    if isinstance(status, int) and 500 <= status <= 599:
        return ProviderError(
            category="server_error", message=text, retryable=True, status_code=status
        )
    if (
        isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        or "timeout" in text.lower()
    ):
        return ProviderError(
            category="timeout", message=text, retryable=True, status_code=status
        )
    if isinstance(exc, (ConnectionError, OSError)) or "network" in text.lower():
        return ProviderError(
            category="network", message=text, retryable=True, status_code=status
        )
    return ProviderError(
        category="unknown", message=text, retryable=False, status_code=status
    )
