from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from messenger_ai.domain.models import ReplyPlan as DomainReplyPlan
from messenger_ai.hub.service import HubService
from messenger_ai.llm.models import InboundItem, ReplyAction
from messenger_ai.llm.planner import ReplyPlanner
from messenger_ai.memory.service import MemoryService
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import (
    CancellationReason,
    PacingConfiguration,
    ScheduleRequest,
)
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft
from messenger_ai.policy import (
    CapabilitySnapshot,
    ConversationType,
    PolicyEngine,
    PolicyOutcome,
)
from messenger_ai.policy.projection import project_planner_result
from messenger_ai.rules.service import AtomicRulePackStore

from .projections import build_llm_request, build_policy_request
from .state import RuntimeState


class PlanningCoordinator:
    """Turn one durable conversation revision into at most one M10 plan."""

    def __init__(self, *, state: RuntimeState, hub: HubService, memory: MemoryService,
                 rules: AtomicRulePackStore, planner: ReplyPlanner, policy: PolicyEngine,
                 pacing: PacingScheduler, capability: CapabilitySnapshot,
                 context_budget_chars: int = 12000) -> None:
        self.state, self.hub, self.memory, self.rules = state, hub, memory, rules
        self.planner, self.policy, self.pacing = planner, policy, pacing
        self.capability, self.context_budget_chars = capability, context_budget_chars

    def is_current(self, conversation_id: str, contact_id: str, binding_revision: int,
                   conversation_revision: int, global_revision: int, rule_version: str) -> bool:
        try:
            binding, current, paused, current_global, globally_paused = self.state.execution_state(conversation_id)
            active = self.rules.resolve(contact_id).rulepack.version
            return (binding, current, current_global) == (binding_revision, conversation_revision, global_revision) and not paused and not globally_paused and active == rule_version
        except Exception:  # noqa: BLE001 - stale/unavailable state fails closed
            return False

    async def run_claimed(
        self, row, *, max_reply_segments: int | None = None
    ) -> str:
        conversation_id = row["conversation_id"]
        contact_id = row["contact_id"]
        revision = int(row["conversation_revision"])
        binding_revision = int(row["binding_revision"])
        try:
            conversation_type = ConversationType(row["conversation_type"])
        except (IndexError, KeyError, ValueError):
            conversation_type = ConversationType.UNKNOWN
        if conversation_type is not ConversationType.DIRECT:
            error_code = (
                "group_conversation_unsupported"
                if conversation_type is ConversationType.GROUP
                else "conversation_type_unknown"
            )
            self.state.complete_planning_job(
                conversation_id, revision, outcome="failed", error_code=error_code
            )
            return "failed"
        now = self.hub.now()
        start_global_revision, start_global_paused, _ = self.state.global_control()
        if start_global_paused:
            self.state.complete_planning_job(conversation_id, revision, outcome="stale", error_code="global_paused")
            return "stale"
        try:
            rules = self.rules.resolve(contact_id, at=now)
            memory = self.memory.context(contact_id, conversation_id, budget_chars=self.context_budget_chars)
            source_keys = tuple(json.loads(row["source_keys_json"]))
            placeholders = ",".join("?" for _ in source_keys)
            records = self.hub.store.connection.execute(
                f"""SELECT platform_message_key,text,observed_at FROM messages
                    WHERE conversation_id=? AND platform_message_key IN ({placeholders})
                    ORDER BY observed_at,created_at""", (conversation_id, *source_keys)
            ).fetchall() if source_keys else []
        except Exception as exc:  # noqa: BLE001 - provider boundary is audited
            self.state.complete_planning_job(conversation_id, revision, outcome="failed", error_code=type(exc).__name__)
            return "failed"
        inbound = tuple(InboundItem(message_key=item["platform_message_key"], text=item["text"],
                                    observed_at=datetime.fromisoformat(item["observed_at"])) for item in records)
        if not inbound:
            self.state.complete_planning_job(conversation_id, revision, outcome="stale", error_code="no_inbound")
            return "stale"
        request = build_llm_request(
            account_id=row["account_id"], contact_id=contact_id,
            conversation_id=conversation_id, conversation_revision=revision,
            memory=memory, rules=rules, inbound=inbound, now=now,
            content_policy_checks_enabled=self.policy.content_policy_checks_enabled)
        result = await self.planner.plan_reply(request)
        current = self.is_current(conversation_id, contact_id, binding_revision, revision,
                                  start_global_revision, rules.rulepack.version)
        if result.stale or result.error or not result.plan:
            outcome = "stale" if result.stale else "failed"
            self.state.complete_planning_job(conversation_id, revision, outcome=outcome,
                                             error_code=result.error.category if result.error else "stale")
            return outcome

        def complete_evaluation(
            *,
            audit_outcome: str,
            job_outcome: str,
            decision_code: str | None = None,
            policy_decisions=(),
            policy_requests=(),
            non_send_selection_reason: str | None = None,
            plan_artifact: dict[str, object] | None = None,
        ) -> bool:
            decisions = tuple(policy_decisions)
            evaluated_requests = tuple(policy_requests)
            reason_codes = tuple(dict.fromkeys(
                reason.value for decision in decisions for reason in decision.reason_codes
            ))
            rule_ids = tuple(dict.fromkeys(
                rule_id for decision in decisions for rule_id in decision.rule_ids
            ))
            categories = tuple(dict.fromkeys(
                category.value for decision in decisions
                for category in decision.sensitive_categories
            ))
            return self.state.complete_planning_evaluation(
                conversation_id,
                revision,
                binding_revision=binding_revision,
                global_revision=start_global_revision,
                rule_version=rules.rulepack.version,
                request_id=result.request_id,
                provider_request_json=request.model_dump_json(),
                plan_json=result.plan.model_dump_json(),
                action=result.plan.action.value,
                model=result.model,
                latency_ms=result.latency_ms,
                usage_json=result.usage.model_dump_json(),
                audit_outcome=audit_outcome,
                job_outcome=job_outcome,
                decision_code=decision_code,
                policy_requests_json=json.dumps(
                    [item.model_dump(mode="json") for item in evaluated_requests],
                    ensure_ascii=False,
                ),
                policy_decisions_json=json.dumps(
                    [item.model_dump(mode="json") for item in decisions],
                    ensure_ascii=False,
                ),
                policy_reason_codes_json=json.dumps(reason_codes),
                policy_rule_ids_json=json.dumps(rule_ids),
                policy_sensitive_categories_json=json.dumps(categories),
                content_policy_checks_enabled=self.policy.content_policy_checks_enabled,
                non_send_selection_reason=non_send_selection_reason,
                plan_artifact=plan_artifact,
            )

        if not current:
            complete_evaluation(
                audit_outcome="stale",
                job_outcome="stale",
                decision_code="stale",
            )
            return "stale"
        if result.plan.action in {ReplyAction.IGNORE, ReplyAction.HANDOFF}:
            recorded = complete_evaluation(
                audit_outcome=result.plan.action.value,
                job_outcome="ignored",
                non_send_selection_reason=result.plan.selection_reason,
            )
            return "ignored" if recorded else "stale"
        plan_id, parent_draft_id = uuid4(), uuid4()
        segments = tuple(result.plan.reply_segments)
        if max_reply_segments is not None and len(segments) > max_reply_segments:
            recorded = complete_evaluation(
                audit_outcome="one_shot_segment_limit",
                job_outcome="failed",
                decision_code="one_shot_segment_limit",
            )
            return "failed" if recorded else "stale"
        child_draft_ids = tuple(uuid4() for _ in segments)
        body = "".join(segments)
        global_revision, global_paused, _ = self.state.global_control()
        _, _, paused, _, _ = self.state.execution_state(conversation_id)
        config = PacingConfiguration.from_rule_context(rules)
        expires = now + timedelta(seconds=config.profile.plan_ttl_seconds)
        assessment = project_planner_result(result).model_copy(update={
            "marked_long_reply": len(body) >= config.profile.long_reply_threshold_chars,
        })
        eligibilities = []
        policy_requests = []
        for child_id, segment in zip(child_draft_ids, segments, strict=True):
            policy_request = build_policy_request(
                draft_id=child_id, plan_id=plan_id, account_id=row["account_id"],
                contact_id=contact_id, conversation_id=conversation_id, body=segment,
                expected_last_message_key=inbound[-1].message_key,
                source_message_keys=tuple(item.message_key for item in inbound),
                rule_version=rules.rulepack.version, capability=self.capability,
                conversation_type=conversation_type,
                binding_revision=binding_revision, conversation_revision=revision,
                global_revision=global_revision, now=now, due_at=now,
                expires_at=expires, assessment=assessment,
                inbound_text="\n".join(item.text for item in inbound),
                # This coordinator only claims jobs created from a complete
                # inbound batch for an explicitly registered, uniquely bound
                # contact while this automation runtime is active.
                identity_unique=True,
                context_complete=True,
                contact_whitelisted=True,
                automation_enabled=True,
                counterparty_initiated=bool(inbound),
                source_inbound_unhandled=bool(inbound),
                paused=paused, global_paused=global_paused)
            eligibility = self.policy.evaluate_eligibility(policy_request)
            eligibilities.append(eligibility)
            policy_requests.append(policy_request)
            if eligibility.outcome not in {PolicyOutcome.AUTO_ELIGIBLE, PolicyOutcome.HUMAN_ELIGIBLE}:
                audit_outcome = (
                    "review_required"
                    if eligibility.outcome is PolicyOutcome.REVIEW_REQUIRED
                    else "policy_blocked"
                )
                recorded = complete_evaluation(
                    audit_outcome=audit_outcome,
                    job_outcome="failed",
                    decision_code=eligibility.outcome.value,
                    policy_decisions=eligibilities,
                    policy_requests=policy_requests,
                )
                return "failed" if recorded else "stale"
        pacing_draft = PacingDraft(
            draft_id=parent_draft_id, conversation_id=conversation_id, contact_id=contact_id,
            text=body, text_hash=sha256(body.encode()).hexdigest(),
            expected_last_message_key=inbound[-1].message_key,
            rule_version=rules.rulepack.version, eligibility_id=eligibilities[0].decision_id)
        domain_plan = DomainReplyPlan(
            action="draft", reply_text=result.plan.reply_text,
            reply_segments=list(result.plan.reply_segments),
            risk_level=result.plan.risk_level.value, confidence=result.plan.confidence)
        reevaluation_id = row["reevaluation_id"]
        pacing_first_inbound_at = (
            now if reevaluation_id is not None else (inbound[0].observed_at or now)
        )
        pacing_last_inbound_at = (
            now if reevaluation_id is not None else (inbound[-1].observed_at or now)
        )
        scheduled = self.pacing.schedule(ScheduleRequest(
            draft=pacing_draft, reply_plan=domain_plan,
            source_message_keys=tuple(item.message_key for item in inbound),
            first_inbound_at=pacing_first_inbound_at,
            last_inbound_at=pacing_last_inbound_at,
            inbound_text="\n".join(item.text for item in inbound),
            profile=config.profile, limits=config.limits,
            paused=paused or global_paused, capability_healthy=self.capability.healthy,
            reserved_pacing_plan_id=plan_id,
            one_shot_attempt_id=row["one_shot_attempt_id"],
            segment_eligibility_ids=tuple(item.decision_id for item in eligibilities),
            segment_draft_ids=child_draft_ids))
        if scheduled.action != "scheduled":
            recorded = complete_evaluation(
                audit_outcome="schedule_rejected",
                job_outcome="failed",
                decision_code=scheduled.reason_code,
                policy_decisions=eligibilities,
                policy_requests=policy_requests,
            )
            return "failed" if recorded else "stale"
        eligibility_json = json.dumps([
            {"eligibility": decision.model_dump(mode="json"), "request": policy_request.model_dump(mode="json")}
            for decision, policy_request in zip(eligibilities, policy_requests, strict=True)
        ])
        recorded = complete_evaluation(
            audit_outcome="scheduled",
            job_outcome="completed",
            policy_decisions=eligibilities,
            policy_requests=policy_requests,
            plan_artifact={
                "pacing_plan_id": str(plan_id),
                "eligibility_json": eligibility_json,
                "account_id": row["account_id"],
                "contact_id": contact_id,
                "source_keys_json": json.dumps(tuple(item.message_key for item in inbound)),
                "segment_draft_ids_json": json.dumps(tuple(str(item) for item in child_draft_ids)),
            },
        )
        if not recorded:
            # The pacing store is a separate SQLite authority.  A lost runtime
            # CAS must cancel the just-created plan before any due consumer can
            # observe it without a matching runtime artifact.
            self.pacing.cancel(conversation_id, CancellationReason.SUPERSEDED)
        return "scheduled" if recorded else "stale"
