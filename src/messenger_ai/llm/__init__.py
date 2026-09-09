"""Side-effect-free LLM Reply Planner (M8)."""

from .models import (
    ContactProjection,
    ContextItem,
    InboundItem,
    PromptProjection,
    ProviderError,
    ReplyAction,
    ReplyPlan,
    ReplyPlanRequest,
    ReplyPlanResult,
    RiskLevel,
    RuleProjection,
    UsageRecord,
)
from .planner import ReplyPlanner
from .prompts import build_projection, build_responses_input
from .providers import (
    FakeProvider,
    ModelProvider,
    ModelProviderPort,
    OpenAIProvider,
    OpenAIResponsesProvider,
    classify_provider_error,
)

__all__ = [
    "ContactProjection",
    "ContextItem",
    "FakeProvider",
    "InboundItem",
    "ModelProvider",
    "ModelProviderPort",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "PromptProjection",
    "ProviderError",
    "ReplyAction",
    "ReplyPlan",
    "ReplyPlanRequest",
    "ReplyPlanResult",
    "ReplyPlanner",
    "RiskLevel",
    "RuleProjection",
    "UsageRecord",
    "build_projection",
    "build_responses_input",
    "classify_provider_error",
]
from .deepseek import DeepSeekProvider, DeepSeekResponsesProvider

__all__ = ["DeepSeekProvider", "DeepSeekResponsesProvider"]
