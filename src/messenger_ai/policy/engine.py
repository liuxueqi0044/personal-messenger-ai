"""Deterministic eligibility and due-time revalidation policy engine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from .content import (
    classify_sensitive,
    has_prompt_injection,
    prohibited_output_rule_ids,
)
from .models import (
    AuthorizationKind,
    ConversationType,
    PolicyDecision,
    PolicyOutcome,
    PolicyPhase,
    PolicyReason,
    PolicyRequest,
    ReplyAction,
    RiskLevel,
    SensitiveCategory,
    SupportLevel,
)


class PolicyEngine:
    """Pure policy evaluation; it cannot send or manufacture authorizations."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        decision_ttl_seconds: float = 300,
        minimum_model_confidence: float = 0.85,
    ) -> None:
        if decision_ttl_seconds <= 0:
            raise ValueError("decision_ttl_seconds must be positive")
        self._clock = clock
        self._ttl = timedelta(seconds=decision_ttl_seconds)
        self._minimum_confidence = minimum_model_confidence

    def evaluate_eligibility(self, request: PolicyRequest) -> PolicyDecision:
        return self._evaluate(request, PolicyPhase.ELIGIBILITY)

    def revalidate_due(
        self, eligibility: PolicyDecision, request: PolicyRequest
    ) -> PolicyDecision:
        now = self._clock()
        if (
            not eligibility.may_schedule
            or eligibility.phase is not PolicyPhase.ELIGIBILITY
            or now > eligibility.valid_until
            or eligibility.subject_hash != request.draft.snapshot_hash
        ):
            return self._decision(
                request,
                PolicyPhase.DUE_REVALIDATION,
                PolicyOutcome.BLOCKED,
                (PolicyReason.STALE_ELIGIBILITY,),
            )
        if now < request.scheduled_due_at:
            return self._decision(
                request,
                PolicyPhase.DUE_REVALIDATION,
                PolicyOutcome.BLOCKED,
                (PolicyReason.TOO_EARLY,),
            )
        fresh = self._evaluate(request, PolicyPhase.DUE_REVALIDATION)
        if fresh.may_schedule and fresh.outcome is not eligibility.outcome:
            return self._decision(
                request,
                PolicyPhase.DUE_REVALIDATION,
                PolicyOutcome.BLOCKED,
                (PolicyReason.STALE_ELIGIBILITY,),
            )
        return fresh

    def validate_execution_state(
        self, request: PolicyRequest
    ) -> tuple[PolicyReason, ...]:
        """Final live-state check immediately before atomically consuming a token."""

        return self._hard_state_failures(request, due=True)

    def _evaluate(self, request: PolicyRequest, phase: PolicyPhase) -> PolicyDecision:
        due = phase is PolicyPhase.DUE_REVALIDATION
        hard_state = self._hard_state_failures(request, due=due)
        if hard_state:
            return self._decision(request, phase, PolicyOutcome.BLOCKED, hard_state)

        assessment = request.assessment
        if assessment.action is ReplyAction.IGNORE:
            return self._decision(
                request,
                phase,
                PolicyOutcome.BLOCKED,
                (PolicyReason.NO_REPLY_REQUESTED,),
            )
        if assessment.risk_level is RiskLevel.BLOCKED:
            return self._decision(
                request,
                phase,
                PolicyOutcome.BLOCKED,
                (PolicyReason.MODEL_RISK_BLOCKED,),
            )
        if not assessment.output_validated:
            return self._decision(
                request,
                phase,
                PolicyOutcome.BLOCKED,
                (PolicyReason.OUTPUT_VALIDATION_FAILED,),
            )

        explicit_prohibited = tuple(
            dict.fromkeys(
                (
                    *assessment.prohibited_rule_ids,
                    *prohibited_output_rule_ids(request.draft.body),
                )
            )
        )
        if explicit_prohibited:
            return self._decision(
                request,
                phase,
                PolicyOutcome.BLOCKED,
                (PolicyReason.PROHIBITED_RULE_HIT, PolicyReason.PROHIBITED_OUTPUT),
                rule_ids=explicit_prohibited,
            )

        sensitive = classify_sensitive(request.inbound_text, request.draft.body)
        injection = has_prompt_injection(request.inbound_text, request.draft.body)
        review_reasons: list[PolicyReason] = []
        rule_ids: list[str] = list(assessment.manual_rule_ids)

        if assessment.action is ReplyAction.HANDOFF:
            review_reasons.append(PolicyReason.HANDOFF_REQUESTED)
        if assessment.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}:
            review_reasons.append(PolicyReason.MODEL_RISK_REVIEW)
        if assessment.confidence < self._minimum_confidence:
            review_reasons.append(PolicyReason.LOW_MODEL_CONFIDENCE)
        if assessment.manual_rule_ids:
            review_reasons.append(PolicyReason.MANUAL_RULE_HIT)
        if sensitive:
            review_reasons.append(PolicyReason.SENSITIVE_TOPIC)
        if injection:
            review_reasons.append(PolicyReason.PROMPT_INJECTION)
        if request.state.known_or_suspected_minor:
            review_reasons.append(PolicyReason.MINOR_SAFETY)
        if request.state.conversation_type is not ConversationType.DIRECT:
            review_reasons.append(PolicyReason.UNSUPPORTED_CONVERSATION)
        if not request.state.identity_unique:
            review_reasons.append(PolicyReason.IDENTITY_AMBIGUOUS)
        if not request.state.context_complete:
            review_reasons.append(PolicyReason.CONTEXT_INCOMPLETE)
        if request.state.is_new_contact:
            review_reasons.append(PolicyReason.NEW_CONTACT)
        if not request.state.contact_whitelisted:
            review_reasons.append(PolicyReason.NOT_WHITELISTED)
        if not request.state.automation_enabled:
            review_reasons.append(PolicyReason.AUTO_MODE_DISABLED)

        review_reasons = list(dict.fromkeys(review_reasons))
        if review_reasons:
            approval = request.approval
            if approval is None:
                return self._decision(
                    request,
                    phase,
                    PolicyOutcome.REVIEW_REQUIRED,
                    (*review_reasons, PolicyReason.APPROVAL_MISSING),
                    rule_ids=tuple(rule_ids),
                    sensitive=sensitive,
                )
            if (
                approval.subject_hash != request.draft.snapshot_hash
                or self._clock() > approval.expires_at
                or approval.approved_at > self._clock()
            ):
                return self._decision(
                    request,
                    phase,
                    PolicyOutcome.REVIEW_REQUIRED,
                    (*review_reasons, PolicyReason.APPROVAL_STALE),
                    rule_ids=tuple(rule_ids),
                    sensitive=sensitive,
                )
            return self._decision(
                request,
                phase,
                PolicyOutcome.HUMAN_ELIGIBLE,
                (PolicyReason.ELIGIBLE_HUMAN_APPROVED,),
                kind=AuthorizationKind.HUMAN,
                rule_ids=tuple(rule_ids),
                sensitive=sensitive,
                approval_id=approval.approval_id,
            )

        return self._decision(
            request,
            phase,
            PolicyOutcome.AUTO_ELIGIBLE,
            (PolicyReason.ELIGIBLE_LOW_RISK,),
            kind=AuthorizationKind.POLICY,
        )

    def _hard_state_failures(
        self, request: PolicyRequest, *, due: bool
    ) -> tuple[PolicyReason, ...]:
        now = self._clock()
        state = request.state
        draft = request.draft
        failures: list[PolicyReason] = []

        if now > draft.expires_at:
            failures.append(PolicyReason.DRAFT_EXPIRED)
        if now > request.plan_expires_at:
            failures.append(PolicyReason.PLAN_EXPIRED)
        if state.global_paused or state.platform_paused or state.contact_paused:
            failures.append(PolicyReason.POLICY_PAUSED)
        if state.manual_takeover:
            failures.append(PolicyReason.MANUAL_TAKEOVER)
        if state.contact_requested_stop:
            failures.append(PolicyReason.CONTACT_REQUESTED_STOP)
        if state.consecutive_unanswered_outbound >= 2:
            failures.append(PolicyReason.OUTBOUND_WITHOUT_REPLY_LIMIT)
        if state.last_message_key != draft.expected_last_message_key:
            failures.append(PolicyReason.STALE_LAST_MESSAGE)
        if state.active_rulepack_version != draft.rulepack_version:
            failures.append(PolicyReason.STALE_RULEPACK)
        if state.active_pacing_rule_version != draft.pacing_rule_version:
            failures.append(PolicyReason.STALE_PACING_RULE)
        if state.capability.snapshot_hash != draft.capability_snapshot_hash:
            failures.append(PolicyReason.STALE_CAPABILITY)
        if state.policy_state_version != draft.policy_state_version:
            failures.append(PolicyReason.STALE_POLICY_STATE)
        if (
            state.inbound_content_type != "text"
            or state.outbound_content_type != "text"
        ):
            failures.append(PolicyReason.UNSUPPORTED_CONTENT)
        if not state.counterparty_initiated or not state.source_inbound_unhandled:
            failures.append(PolicyReason.NO_FRESH_INBOUND)
        if not state.capability.healthy:
            failures.append(PolicyReason.ADAPTER_UNHEALTHY)
        if state.capability.send_background is not SupportLevel.SUPPORTED:
            failures.append(PolicyReason.BACKGROUND_SEND_UNSUPPORTED)
        if state.capability.verify_background is not SupportLevel.SUPPORTED:
            failures.append(PolicyReason.BACKGROUND_VERIFY_UNSUPPORTED)
        if not state.rate_limit_available:
            failures.append(PolicyReason.RATE_LIMITED)
        if not state.within_allowed_window:
            failures.append(PolicyReason.OUTSIDE_ALLOWED_WINDOW)
        if not state.quiet_window_complete:
            failures.append(PolicyReason.QUIET_WINDOW_ACTIVE)
        if due and not state.automation_enabled and request.approval is None:
            failures.append(PolicyReason.AUTO_MODE_DISABLED)
        return tuple(dict.fromkeys(failures))

    def _decision(
        self,
        request: PolicyRequest,
        phase: PolicyPhase,
        outcome: PolicyOutcome,
        reasons: tuple[PolicyReason, ...],
        *,
        kind: AuthorizationKind | None = None,
        rule_ids: tuple[str, ...] = (),
        sensitive: tuple[SensitiveCategory, ...] = (),
        approval_id: str | None = None,
    ) -> PolicyDecision:
        now = self._clock()
        valid_until = min(
            request.draft.expires_at, request.plan_expires_at, now + self._ttl
        )
        audit = (
            f"phase={phase.value};outcome={outcome.value};draft={request.draft.draft_id};"
            f"conversation={request.draft.conversation_id};subject={request.draft.snapshot_hash[:12]};"
            f"reasons={','.join(reason.value for reason in reasons)};rulepack={request.draft.rulepack_version}"
        )
        return PolicyDecision(
            phase=phase,
            outcome=outcome,
            subject_hash=request.draft.snapshot_hash,
            authorization_kind=kind,
            reason_codes=reasons,
            rule_ids=tuple(dict.fromkeys(rule_ids)),
            sensitive_categories=sensitive,
            rulepack_version=request.draft.rulepack_version,
            evaluated_at=now,
            valid_until=valid_until,
            audit_summary=audit,
            approval_id=approval_id,
        )
