"""M8 orchestration: provider call plus stale-result protection."""

from __future__ import annotations

from collections.abc import Callable

from .models import ProviderError, ReplyPlanRequest, ReplyPlanResult
from .providers import ModelProvider


class ReplyPlanner:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        is_current: Callable[[ReplyPlanRequest], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.is_current = is_current

    async def plan_reply(self, request: ReplyPlanRequest) -> ReplyPlanResult:
        result = await self.provider.plan_reply(request)
        if (
            result.request_id != request.request_id
            or result.rule_version != request.rules.rule_version
        ):
            return _stale(request, result.model, "rule version or request id changed")
        if result.context_fingerprint != request.context_fingerprint:
            return _stale(request, result.model, "inbound context changed")
        if self.is_current is not None and not self.is_current(request):
            return _stale(request, result.model, "request is no longer current")
        return result

    async def generate(self, request: ReplyPlanRequest) -> ReplyPlanResult:
        return await self.plan_reply(request)

    async def plan(self, request: ReplyPlanRequest) -> ReplyPlanResult:
        return await self.plan_reply(request)


def _stale(request: ReplyPlanRequest, model: str, message: str) -> ReplyPlanResult:
    return ReplyPlanResult(
        request_id=request.request_id,
        rule_version=request.rules.rule_version,
        context_fingerprint=request.context_fingerprint,
        model=model,
        latency_ms=0,
        stale=True,
        error=ProviderError(category="rejected", message=message, retryable=False),
    )
