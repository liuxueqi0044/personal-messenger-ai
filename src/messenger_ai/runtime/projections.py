from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from messenger_ai.llm.models import ContactProjection, ContextItem, InboundItem, ReplyPlanRequest, RuleProjection
from messenger_ai.memory.models import ContactContext
from messenger_ai.policy import CapabilitySnapshot, DraftSnapshot, LivePolicyState, PlannerAssessment, PolicyRequest
from messenger_ai.rules.models import RuleContext


def policy_version(*, binding_revision: int, conversation_revision: int,
                   global_revision: int, rule_version: str) -> str:
    return f"b{binding_revision}:c{conversation_revision}:g{global_revision}:r{rule_version}"


def build_llm_request(*, account_id: str, contact_id: str, conversation_id: str,
                      conversation_revision: int, memory: ContactContext,
                      rules: RuleContext, inbound: tuple[InboundItem, ...],
                      now: datetime) -> ReplyPlanRequest:
    relation = memory.relationship_states[0].state if memory.relationship_states else "unknown"
    if relation not in {"new", "familiar", "committed", "unknown"}:
        relation = "unknown"
    facts = tuple(ContextItem(key=item.key, value=item.value, source=str(item.fact_id), confidence=item.confidence)
                  for item in memory.confirmed_facts)
    preferences = tuple(ContextItem(key=item.key, value=item.value, source=str(item.preference_id))
                        for item in memory.confirmed_preferences)
    summaries = tuple(ContextItem(key="summary", value=item.text, source=str(item.summary_id)) for item in memory.summaries)
    recent = tuple(InboundItem(message_key=item.platform_message_key, text=item.text,
                               observed_at=item.observed_at, direction=item.direction.value)
                   for item in memory.recent_messages)
    normalized = rules.rulepack.normalized
    tone = tuple(normalized.persona.tone) if hasattr(normalized, "persona") else ()
    if rules.contact_override:
        tone += tuple(rules.contact_override.tone)
    rule_projection = RuleProjection(
        rulepack_id=rules.rulepack.rulepack_id, rule_version=rules.rulepack.version,
        source_hash=rules.rulepack.source_hash,
        system_safety=tuple(item.text for item in rules.effective_prohibited),
        persona_style=tone,
        persona_identity=normalized.persona.identity,
        language=normalized.persona.language,
        preferred_length=normalized.persona.preferred_length,
        behavior=tuple(item.text for item in rules.effective_required),
        prohibited=tuple(item.text for item in rules.effective_prohibited),
        escalation=tuple(item.text for item in normalized.escalation_rules if item.enabled),
        examples_positive=normalized.examples_positive,
        examples_negative=normalized.examples_negative,
    )
    fingerprint = hashlib.sha256(json.dumps({
        "conversation": conversation_id, "revision": conversation_revision,
        "rule": rules.rulepack.version,
        "inbound": [item.model_dump(mode="json") for item in inbound],
        "memory_sources": [str(item) for item in memory.source_message_ids],
    }, sort_keys=True, default=str).encode()).hexdigest()
    return ReplyPlanRequest(
        request_id=str(uuid4()), account_id=account_id,
        contact=ContactProjection(contact_id=contact_id, conversation_id=conversation_id,
                                  relationship_stage=relation, facts=facts, preferences=preferences,
                                  summaries=summaries, recent_messages=recent,
                                  source_ids=tuple(str(item) for item in memory.source_message_ids)),
        rules=rule_projection, inbound=inbound, context_fingerprint=fingerprint, created_at=now,
    )


def build_policy_request(*, draft_id: UUID | str, plan_id: UUID | str, account_id: str,
                         contact_id: str, conversation_id: str, body: str,
                         expected_last_message_key: str, source_message_keys: tuple[str, ...],
                         rule_version: str, capability: CapabilitySnapshot,
                         binding_revision: int, conversation_revision: int, global_revision: int,
                         now: datetime, due_at: datetime, expires_at: datetime,
                         assessment: PlannerAssessment | None = None,
                         paused: bool = False, global_paused: bool = False) -> PolicyRequest:
    version = policy_version(binding_revision=binding_revision, conversation_revision=conversation_revision,
                             global_revision=global_revision, rule_version=rule_version)
    draft = DraftSnapshot(
        draft_id=str(draft_id), platform="qq", account_id=account_id,
        conversation_id=conversation_id, contact_id=contact_id, body=body,
        expected_last_message_key=expected_last_message_key,
        source_message_keys=source_message_keys, rulepack_version=rule_version,
        pacing_plan_id=str(plan_id), pacing_rule_version=rule_version,
        capability_snapshot_hash=capability.snapshot_hash, policy_state_version=version,
        binding_revision=binding_revision, conversation_revision=conversation_revision,
        created_at=now, expires_at=expires_at)
    state = LivePolicyState(
        observed_at=now, last_message_key=expected_last_message_key,
        active_rulepack_version=rule_version, active_pacing_rule_version=rule_version,
        capability=capability, policy_state_version=version,
        binding_revision=binding_revision, conversation_revision=conversation_revision,
        contact_whitelisted=True, automation_enabled=True, contact_paused=paused,
        global_paused=global_paused)
    return PolicyRequest(draft=draft, state=state, assessment=assessment or PlannerAssessment(),
                         scheduled_due_at=due_at, plan_expires_at=expires_at)
