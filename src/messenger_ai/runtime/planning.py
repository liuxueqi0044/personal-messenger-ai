from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from messenger_ai.domain.models import ReplyPlan as DomainReplyPlan
from messenger_ai.hub.service import HubService
from messenger_ai.llm.models import InboundItem, ReplyAction
from messenger_ai.llm.planner import ReplyPlanner
from messenger_ai.memory.service import MemoryService
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft
from messenger_ai.pacing.models import PacingConfiguration, ScheduleRequest
from messenger_ai.policy import CapabilitySnapshot, PlannerAssessment, PolicyEngine, PolicyOutcome
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
        except Exception:
            return False

    async def run_claimed(self, row) -> str:
        conversation_id = row["conversation_id"]
        contact_id = row["contact_id"]
        revision = int(row["conversation_revision"])
        binding_revision = int(row["binding_revision"])
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
        except Exception as exc:
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
            memory=memory, rules=rules, inbound=inbound, now=now)
        result = await self.planner.plan_reply(request)
        current = self.is_current(conversation_id, contact_id, binding_revision, revision,
                                  start_global_revision, rules.rulepack.version)
        if result.stale or result.error or not result.plan or not current:
            outcome = "stale" if result.stale or not current else "failed"
            self.state.complete_planning_job(conversation_id, revision, outcome=outcome,
                                             error_code=result.error.category if result.error else "stale")
            return outcome
        if result.plan.action in {ReplyAction.IGNORE, ReplyAction.HANDOFF}:
            self.state.complete_planning_job(conversation_id, revision, outcome="ignored")
            return "ignored"
        plan_id, parent_draft_id = uuid4(), uuid4()
        segments = tuple(result.plan.reply_segments)
        child_draft_ids = tuple(uuid4() for _ in segments)
        body = "".join(segments)
        global_revision, global_paused, _ = self.state.global_control()
        _, _, paused, _, _ = self.state.execution_state(conversation_id)
        config = PacingConfiguration.from_rule_context(rules)
        expires = now + timedelta(seconds=config.profile.plan_ttl_seconds)
        assessment = PlannerAssessment(
            action=result.plan.action.value, risk_level=result.plan.risk_level.value,
            confidence=result.plan.confidence, policy_tags=result.plan.policy_tags,
            output_validated=True,
            marked_long_reply=len(body) >= config.profile.long_reply_threshold_chars)
        eligibilities = []
        policy_requests = []
        for child_id, segment in zip(child_draft_ids, segments, strict=True):
            policy_request = build_policy_request(
                draft_id=child_id, plan_id=plan_id, account_id=row["account_id"],
                contact_id=contact_id, conversation_id=conversation_id, body=segment,
                expected_last_message_key=inbound[-1].message_key,
                source_message_keys=tuple(item.message_key for item in inbound),
                rule_version=rules.rulepack.version, capability=self.capability,
                binding_revision=binding_revision, conversation_revision=revision,
                global_revision=global_revision, now=now, due_at=now,
                expires_at=expires, assessment=assessment,
                paused=paused, global_paused=global_paused)
            eligibility = self.policy.evaluate_eligibility(policy_request)
            if eligibility.outcome not in {PolicyOutcome.AUTO_ELIGIBLE, PolicyOutcome.HUMAN_ELIGIBLE}:
                self.state.complete_planning_job(conversation_id, revision, outcome="failed", error_code=eligibility.outcome.value)
                return "failed"
            eligibilities.append(eligibility)
            policy_requests.append(policy_request)
        pacing_draft = PacingDraft(
            draft_id=parent_draft_id, conversation_id=conversation_id, contact_id=contact_id,
            text=body, text_hash=sha256(body.encode()).hexdigest(),
            expected_last_message_key=inbound[-1].message_key,
            rule_version=rules.rulepack.version, eligibility_id=eligibilities[0].decision_id)
        domain_plan = DomainReplyPlan(
            action="draft", reply_text=result.plan.reply_text,
            reply_segments=list(result.plan.reply_segments),
            risk_level=result.plan.risk_level.value, confidence=result.plan.confidence)
        scheduled = self.pacing.schedule(ScheduleRequest(
            draft=pacing_draft, reply_plan=domain_plan,
            source_message_keys=tuple(item.message_key for item in inbound),
            first_inbound_at=inbound[0].observed_at or now,
            last_inbound_at=inbound[-1].observed_at or now,
            inbound_text="\n".join(item.text for item in inbound),
            profile=config.profile, limits=config.limits,
            paused=paused or global_paused, capability_healthy=self.capability.healthy,
            reserved_pacing_plan_id=plan_id,
            segment_eligibility_ids=tuple(item.decision_id for item in eligibilities),
            segment_draft_ids=child_draft_ids))
        if scheduled.action != "scheduled":
            self.state.complete_planning_job(conversation_id, revision, outcome="failed", error_code=scheduled.reason_code)
            return "failed"
        self.state.save_plan_artifact(
            pacing_plan_id=plan_id, conversation_id=conversation_id,
            conversation_revision=revision, binding_revision=binding_revision,
            global_revision=global_revision,
            eligibility_json=json.dumps([
                {"eligibility": decision.model_dump(mode="json"), "request": request.model_dump(mode="json")}
                for decision, request in zip(eligibilities, policy_requests, strict=True)
            ]),
            planner_json=result.plan.model_dump_json(), rule_version=rules.rulepack.version,
            account_id=row["account_id"], contact_id=contact_id,
            source_keys=tuple(item.message_key for item in inbound),
            segment_draft_ids=tuple(str(item) for item in child_draft_ids))
        self.state.complete_planning_job(conversation_id, revision, outcome="completed")
        return "scheduled"
