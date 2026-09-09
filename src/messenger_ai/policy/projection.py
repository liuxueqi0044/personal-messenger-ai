"""Safe structural projection from M8-like planner results into M9 input."""

from __future__ import annotations

from typing import Any, Protocol

from .models import PlannerAssessment, ReplyAction, RiskLevel


class StructuredPlannerResult(Protocol):
    plan: Any | None
    error: Any | None
    stale: bool


def project_planner_result(result: StructuredPlannerResult) -> PlannerAssessment:
    """Consume only structured plan fields; provider implementations stay outside M9.

    Stale, failed, or malformed results become a blocked/invalid assessment rather
    than a permissive default. No target, token, timing, or adapter field is read.
    """

    plan = getattr(result, "plan", None)
    if (
        getattr(result, "stale", True)
        or getattr(result, "error", None) is not None
        or plan is None
    ):
        return PlannerAssessment(
            action=ReplyAction.HANDOFF,
            risk_level=RiskLevel.BLOCKED,
            output_validated=False,
            policy_tags=("planner-result-invalid",),
        )
    try:
        return PlannerAssessment(
            action=ReplyAction(str(plan.action)),
            risk_level=RiskLevel(str(plan.risk_level)),
            confidence=float(plan.confidence),
            policy_tags=tuple(plan.policy_tags),
            prohibited_rule_ids=tuple(plan.prohibited_rule_results),
            output_validated=True,
        )
    except (AttributeError, TypeError, ValueError):
        return PlannerAssessment(
            action=ReplyAction.HANDOFF,
            risk_level=RiskLevel.BLOCKED,
            output_validated=False,
            policy_tags=("planner-result-malformed",),
        )
