from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from messenger_ai.domain import Authorization, AuthorizationType, AuthorizedSendCommand, Draft
from messenger_ai.hub.service import HubService
from messenger_ai.pacing import PacingScheduler
from messenger_ai.policy import AuthorizationService, CapabilitySnapshot, PolicyDecision, PolicyRequest
from messenger_ai.rules.service import AtomicRulePackStore

from .projections import policy_version
from .send_dispatcher import AuthorizedDueExecution, SendDispatcher
from .state import RuntimeState


class DueCoordinator:
    """Consumes M10's durable due outbox through the sole M9 authorization path."""

    def __init__(self, *, state: RuntimeState, hub: HubService, pacing: PacingScheduler,
                 rules: AtomicRulePackStore, authorization: AuthorizationService,
                 dispatcher: SendDispatcher, capability_provider) -> None:
        self.state, self.hub, self.pacing, self.rules = state, hub, pacing, rules
        self.authorization, self.dispatcher = authorization, dispatcher
        self.capability_provider = capability_provider

    async def dispatch_one(self) -> bool:
        self.pacing.due_for_revalidation()
        claimed = self.pacing.claim_due_outbox(limit=1)
        if not claimed:
            return False
        outbox_id, due = claimed[0]
        try:
            artifact = self.state.plan_artifact(due.pacing_plan_id)
            snapshot = json.loads(artifact["eligibility_json"])[due.segment_index]
            eligibility = PolicyDecision.model_validate(snapshot["eligibility"])
            original = PolicyRequest.model_validate(snapshot["request"])
            capability: CapabilitySnapshot = self.capability_provider()
            binding, revision, paused, global_revision, global_paused = self.state.execution_state(due.conversation_id)
            rules = self.rules.resolve(artifact["contact_id"], at=self.hub.now())
            def live_request():
                b, r, p, g, gp = self.state.execution_state(due.conversation_id)
                active = self.rules.resolve(artifact["contact_id"], at=self.hub.now())
                version = policy_version(binding_revision=b, conversation_revision=r,
                                         global_revision=g, rule_version=active.rulepack.version)
                state = original.state.model_copy(update={
                    "observed_at": self.hub.now(), "active_rulepack_version": active.rulepack.version,
                    "active_pacing_rule_version": active.rulepack.version,
                    "capability": self.capability_provider(), "policy_state_version": version,
                    "binding_revision": b, "conversation_revision": r,
                    "contact_paused": p, "global_paused": gp,
                })
                return original.model_copy(update={"state": state, "scheduled_due_at": due.due_at})

            request = live_request()
            decision, envelope = self.authorization.authorize_due(eligibility, request)
            if envelope is None:
                self.pacing.record_revalidation_result(due.pacing_plan_id,
                    segment_sent_and_verified=False, segment_index=due.segment_index)
                self.pacing.complete_due_outbox(outbox_id)
                return True
            draft = Draft(draft_id=due.draft_id, conversation_id=due.conversation_id,
                          contact_id=artifact["contact_id"], text=due.body,
                          source_message_keys=tuple(json.loads(artifact["source_keys_json"])),
                          rule_version=artifact["rule_version"])
            self.hub.create_draft(draft)
            auth_type = AuthorizationType.POLICY if envelope.binding.authorization_kind.value == "policy" else AuthorizationType.HUMAN
            idem = f"m10:{due.pacing_plan_id}:{due.segment_index}"
            authorization = Authorization(
                authorization_id=UUID(envelope.authorization_id), draft_id=due.draft_id,
                conversation_id=due.conversation_id,
                expected_last_message_key=due.expected_last_message_key,
                text_hash=sha256(due.body.encode()).hexdigest(), idempotency_key=idem,
                authorization_type=auth_type, policy_version=request.draft.policy_state_version,
                expires_at=envelope.expires_at)
            self.hub.persist_authorization(authorization)
            command = AuthorizedSendCommand(**authorization.model_dump(exclude={"consumed"}))
            await self.dispatcher.execute(AuthorizedDueExecution(
                due=due, command=command, token=envelope.token, binding=envelope.binding,
                live_policy_request_factory=live_request, binding_revision=binding,
                conversation_revision=revision, global_revision=global_revision))
            self.pacing.complete_due_outbox(outbox_id)
            return True
        except Exception:
            # Fail-stop this item for this process; recovery reclaims it on restart.
            raise
