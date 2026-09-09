"""DeepSeek transport through the existing strict ReplyPlan provider contract."""

from __future__ import annotations

from typing import Any

from .providers import OpenAIResponsesProvider, ResponsesTransport

DEEPSEEK_RESPONSES_BASE_URL = "https://api.deepseek.com"
SUPPORTED_DEEPSEEK_RESPONSES_MODELS = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"}
)


class DeepSeekResponsesProvider(OpenAIResponsesProvider):
    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str = DEEPSEEK_RESPONSES_BASE_URL,
        transport: ResponsesTransport | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for DeepSeek")
        if model not in SUPPORTED_DEEPSEEK_RESPONSES_MODELS:
            supported = ", ".join(sorted(SUPPORTED_DEEPSEEK_RESPONSES_MODELS))
            raise ValueError(f"unsupported DeepSeek Responses model {model!r}; use one of: {supported}")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be greater than 0 and no more than 300")
        if transport is None:
            try:
                from openai import AsyncOpenAI  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("install the llm extra to use DeepSeek") from exc
            transport = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url.rstrip("/"),
                timeout=timeout_seconds,
            )
        super().__init__(model=model, transport=transport, timeout_seconds=timeout_seconds)


DeepSeekProvider = DeepSeekResponsesProvider
