from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

from messenger_ai.domain import (
    Authorization,
    AuthorizationType,
    AuthorizedSendCommand,
    Draft,
    SendOperation,
    SendStatus,
)
from messenger_ai.hub.service import HubService
from messenger_ai.pacing import DueForRevalidation, PacingScheduler
from messenger_ai.policy import (
    AuthorizationService,
    PolicyDecision,
    PolicyRequest,
)
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
        await self._dispatch_claimed(outbox_id, due)
        return True

    async def dispatch_exact(
        self,
        *,
        pacing_plan_id: UUID,
        conversation_id: str,
        segment_index: int,
        one_shot_attempt_id: UUID | None = None,
    ) -> tuple[bool, SendOperation | None]:
        """Dispatch only one named plan segment, leaving every other item untouched."""

        artifact = self.state.plan_artifact(pacing_plan_id)
        if str(artifact["conversation_id"]) != conversation_id:
            raise ValueError("exact dispatch conversation does not own pacing plan")
        artifact_attempt_id = artifact["one_shot_attempt_id"]
        requested_attempt_id = (
            str(one_shot_attempt_id) if one_shot_attempt_id is not None else None
        )
        if artifact_attempt_id != requested_attempt_id:
            raise RuntimeError("exact dispatch one-shot attempt does not own pacing plan")
        self.pacing.due_for_revalidation(
            pacing_plan_id=pacing_plan_id,
            one_shot_attempt_id=one_shot_attempt_id,
        )
        claimed = self.pacing.claim_due_outbox(
            limit=1,
            pacing_plan_id=pacing_plan_id,
            segment_index=segment_index,
            one_shot_attempt_id=one_shot_attempt_id,
            recoverable=False,
        )
        if not claimed:
            return False, None
        outbox_id, due = claimed[0]
        if due.conversation_id != conversation_id:
            raise RuntimeError("exact dispatch claimed a mismatched conversation")
        return True, await self._dispatch_claimed(outbox_id, due, artifact=artifact)

    async def _dispatch_claimed(
        self,
        outbox_id: int,
        due: DueForRevalidation,
        *,
        artifact=None,
    ) -> SendOperation | None:
        try:
            if artifact is None:
                artifact = self.state.plan_artifact(due.pacing_plan_id)
            artifact_attempt_id = artifact["one_shot_attempt_id"]
            due_attempt_id = (
                str(due.one_shot_attempt_id)
                if due.one_shot_attempt_id is not None
                else None
            )
            if artifact_attempt_id != due_attempt_id:
                raise RuntimeError("due event one-shot provenance does not match artifact")
            snapshot = json.loads(artifact["eligibility_json"])[due.segment_index]
            eligibility = PolicyDecision.model_validate(snapshot["eligibility"])
            original = PolicyRequest.model_validate(snapshot["request"])
            binding, revision, _paused, global_revision, _global_paused = (
                self.state.execution_state(due.conversation_id)
            )
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
            _decision, envelope = self.authorization.authorize_due(eligibility, request)
            if envelope is None:
                if not self.state.reject_due_segment(
                    pacing_plan_id=due.pacing_plan_id,
                    segment_index=due.segment_index,
                    conversation_id=due.conversation_id,
                    body_hash=due.body_hash,
                    binding_revision=binding,
                    conversation_revision=revision,
                ):
                    raise RuntimeError("runtime M9 rejection CAS mismatch")
                self._complete_due_outbox(outbox_id, due, operation=None)
                return None
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
            operation = await self.dispatcher.execute(AuthorizedDueExecution(
                due=due, command=command, token=envelope.token, binding=envelope.binding,
                live_policy_request_factory=live_request, binding_revision=binding,
                conversation_revision=revision, global_revision=global_revision))
            self._complete_due_outbox(outbox_id, due, operation=operation)
            if operation.status is SendStatus.VERIFIED and not self.state.settle_verified_segment(
                pacing_plan_id=due.pacing_plan_id,
                segment_index=due.segment_index,
                authorization_id=authorization.authorization_id,
                operation_id=operation.operation_id,
            ):
                raise RuntimeError("durable verified send proof rejected")
            return operation
        except Exception:  # noqa: TRY203 - claim stays recoverable after failure
            # Fail-stop this item for this process; recovery reclaims it on restart.
            raise

    def _complete_due_outbox(
        self,
        outbox_id: int,
        due: DueForRevalidation,
        *,
        operation: SendOperation | None,
    ) -> None:
        result = self.pacing.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            due.pacing_plan_id,
            segment_sent_and_verified=(
                operation is not None and operation.status.value == "verified"
            ),
            segment_index=due.segment_index,
            operation_id=operation.operation_id if operation is not None else None,
            one_shot_attempt_id=due.one_shot_attempt_id,
        )
        if result is None:
            raise RuntimeError("atomic pacing due settlement rejected exact result")
