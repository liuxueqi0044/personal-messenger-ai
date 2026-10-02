from __future__ import annotations

import json
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from messenger_ai.pacing.scheduler import ClaimedDueOutbox
from messenger_ai.policy import (
    AuthorizationService,
    PolicyDecision,
    PolicyRequest,
)
from messenger_ai.rules.service import AtomicRulePackStore

from .projections import policy_version
from .send_dispatcher import AuthorizedDueExecution, SendDispatcher
from .state import RuntimeState
from .staged_preparation import (
    DraftPreparationRequest, StagedPreparationPort, StagedPreparationController,
    StagedPrepareAdapter, StagedCleanupRequired, source_keys_digest,
)


class DueNavigationPreflightResult(BaseModel):
    """An optional navigation gate result, never a send authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ready", "retry_wait", "needs_attention"]
    retry_at: datetime | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$")

    @field_validator("retry_at")
    @classmethod
    def _aware_retry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("navigation retry time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _result_shape(self) -> DueNavigationPreflightResult:
        if self.status == "ready":
            if self.retry_at is not None or self.error_code is not None:
                raise ValueError("ready preflight has no retry/error")
        elif self.error_code is None:
            raise ValueError("navigation failure requires a local reason code")
        return self


class DueNavigationPreflight(Protocol):
    async def check(self, due: DueForRevalidation, *, binding_revision: int,
                    conversation_revision: int, global_revision: int) -> DueNavigationPreflightResult: ...


@dataclass(frozen=True, slots=True)
class _ExistingDueWork:
    operation: SendOperation | None
    authorization_id: str | None
    error_code: str


class DueCoordinator:
    """Consumes M10's durable due outbox through the sole M9 authorization path."""

    def __init__(self, *, state: RuntimeState, hub: HubService, pacing: PacingScheduler,
                 rules: AtomicRulePackStore, authorization: AuthorizationService,
                 dispatcher: SendDispatcher, capability_provider,
                 navigation_preflight: DueNavigationPreflight | None = None,
                 due_operation_recovery: Callable[[DueForRevalidation, SendOperation], Awaitable[SendOperation | None]] | None = None,
                 staged_preparation: StagedPreparationPort | None = None,
                 control_cancelled: Callable[[], bool] | None = None,
                 monotonic_ns_clock: Callable[[], int] = time.monotonic_ns) -> None:
        self.state, self.hub, self.pacing, self.rules = state, hub, pacing, rules
        self.authorization, self.dispatcher = authorization, dispatcher
        self.capability_provider = capability_provider
        self.navigation_preflight = navigation_preflight
        self.due_operation_recovery = due_operation_recovery
        self.staged_preparation = staged_preparation
        self.control_cancelled = control_cancelled
        self.monotonic_ns_clock = monotonic_ns_clock

    @property
    def _gated(self):
        return self.navigation_preflight is not None or self.staged_preparation is not None

    async def dispatch_one(self) -> bool:
        self.pacing.due_for_revalidation()
        claimed = (self.pacing.claim_due_outbox(limit=1) if not self._gated
                   else self.pacing.claim_due_outbox_with_tokens(limit=1))
        if not claimed:
            return False
        if not self._gated:
            outbox_id, due = claimed[0]
            await self._dispatch_claimed(outbox_id, due)
        else:
            claim = claimed[0]
            await self._dispatch_claimed(claim.outbox_id, claim.due, claim=claim)
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
        claim_method = (self.pacing.claim_due_outbox if not self._gated
                        else self.pacing.claim_due_outbox_with_tokens)
        claimed = claim_method(
            limit=1,
            pacing_plan_id=pacing_plan_id,
            segment_index=segment_index,
            one_shot_attempt_id=one_shot_attempt_id,
            recoverable=False,
        )
        if not claimed:
            return False, None
        claim = None if not self._gated else claimed[0]
        outbox_id, due = claimed[0] if claim is None else (claim.outbox_id, claim.due)
        if due.conversation_id != conversation_id:
            raise RuntimeError("exact dispatch claimed a mismatched conversation")
        if claim is None:
            return True, await self._dispatch_claimed(outbox_id, due, artifact=artifact)
        return True, await self._dispatch_claimed(outbox_id, due, artifact=artifact, claim=claim)

    async def _dispatch_claimed(
        self,
        outbox_id: int,
        due: DueForRevalidation,
        *,
        artifact=None,
        claim: ClaimedDueOutbox | None = None,
    ) -> SendOperation | None:
        prepared_adapter = None
        preparation_transferred = False
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
            if self._gated:
                if claim is None or claim.outbox_id != outbox_id or claim.due != due:
                    raise RuntimeError("navigation preflight requires the exact claimed due nonce")
                self._validate_navigation_artifact(due, artifact, eligibility, original)
                self._require_current_claim(claim)
                frozen_artifact = dict(artifact)
                frozen_eligibility, frozen_original = eligibility, original
                existing = self._existing_due_work(due)
                if existing is not None:
                    return await self._recover_existing_due(claim, existing)
                claim_state = self.pacing.due_claim_state(claim)
                if (min(original.plan_expires_at, original.draft.expires_at) <= self.hub.now()
                        or claim_state.error_code == "due_plan_expired"):
                    binding, revision = self.state.revisions(due.conversation_id)
                    self._reject_due(outbox_id, due, binding=binding, revision=revision, claim=claim)
                    return None
                if not claim_state.eligible:
                    self._defer_navigation(claim, DueNavigationPreflightResult(
                        status="needs_attention", error_code=claim_state.error_code or "due_plan_not_eligible"))
                    return None
                b, r, p, g, gp = self.state.execution_state(due.conversation_id)
                if self.staged_preparation is not None and self.staged_preparation.has_cleanup_obligation(artifact["account_id"]):
                    self._defer_navigation(claim, DueNavigationPreflightResult(
                        status="needs_attention", error_code="staged_cleanup_required"))
                    return None
                if self.navigation_preflight is not None and not p and not gp:
                    result = await self.navigation_preflight.check(
                        due, binding_revision=b, conversation_revision=r, global_revision=g,
                    )
                    self._require_current_claim(claim)
                    if not isinstance(result, DueNavigationPreflightResult):
                        raise RuntimeError("navigation preflight returned an unvalidated result")
                    if result.status != "ready":
                        self._defer_navigation(claim, result)
                        return None
                    # Navigation awaits may span a pause, cancellation, new
                    # message, or binding/rule change. Re-read all authorities;
                    # keep the original draft/plan expiry and source keys.
                    artifact = self.state.plan_artifact(due.pacing_plan_id)
                    snapshot = json.loads(artifact["eligibility_json"])[due.segment_index]
                    eligibility = PolicyDecision.model_validate(snapshot["eligibility"])
                    original = PolicyRequest.model_validate(snapshot["request"])
                    self._validate_navigation_artifact(due, artifact, eligibility, original)
                    if (dict(artifact) != frozen_artifact or original != frozen_original
                            or eligibility != frozen_eligibility):
                        self._defer_navigation(claim, DueNavigationPreflightResult(
                            status="needs_attention", error_code="due_artifact_changed"))
                        return None
                    existing = self._existing_due_work(due)
                    if existing is not None:
                        return await self._recover_existing_due(claim, existing)
                    claim_state = self.pacing.due_claim_state(claim)
                    if claim_state.error_code == "due_plan_expired" or min(
                        original.plan_expires_at, original.draft.expires_at
                    ) <= self.hub.now():
                        binding, revision = self.state.revisions(due.conversation_id)
                        self._reject_due(outbox_id, due, binding=binding, revision=revision, claim=claim)
                        return None
                    if not claim_state.eligible:
                        self._defer_navigation(claim, DueNavigationPreflightResult(
                            status="needs_attention", error_code=claim_state.error_code or "due_plan_not_eligible"))
                        return None
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
            if self.staged_preparation is not None:
                prepared_adapter = await self._stage_due(claim, artifact, eligibility, original, live_request,
                    revisions=(binding, revision, global_revision))
                if prepared_adapter is None:
                    return None
                # These are the original revisions; staging may not upgrade a
                # draft to a new binding, rule pack, or message cursor.
                request = live_request()
                if not prepared_adapter.current():
                    owned_adapter = prepared_adapter
                    prepared_adapter = None
                    try:
                        await owned_adapter.abort()
                    except StagedCleanupRequired:
                        pass  # The port retains the durable owned hold.
                    self._require_current_claim(claim)
                    self._defer_navigation(claim, DueNavigationPreflightResult(
                        status="needs_attention", error_code="staged_pregrant_stale"))
                    return None
            _decision, envelope = self.authorization.authorize_due(eligibility, request)
            if envelope is None:
                if prepared_adapter is not None:
                    owned_adapter = prepared_adapter
                    prepared_adapter = None
                    try:
                        await owned_adapter.abort()
                    except StagedCleanupRequired:
                        self._require_current_claim(claim)
                        self._defer_navigation(claim, DueNavigationPreflightResult(
                            status="needs_attention", error_code="staged_cleanup_required"))
                        return None
                self._reject_due(outbox_id, due, binding=binding, revision=revision, claim=claim)
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
            preparation_transferred = True
            operation = await self.dispatcher.execute(AuthorizedDueExecution(
                due=due, command=command, token=envelope.token, binding=envelope.binding,
                live_policy_request_factory=live_request, binding_revision=binding,
                conversation_revision=revision, global_revision=global_revision,
                prepared_adapter=prepared_adapter))
            if (prepared_adapter is not None
                    and self.staged_preparation.has_cleanup_obligation(artifact["account_id"])):
                self._require_current_claim(claim)
                if not self.pacing.hold_due_outbox_for_recovery(
                    claim, operation_id=operation.operation_id, reason="staged_cleanup_required",
                ):
                    raise RuntimeError("staged cleanup recovery hold lost claim")
                return operation
            self._complete_due_outbox(outbox_id, due, operation=operation, claim=claim)
            if operation.status is SendStatus.VERIFIED and not self.state.settle_verified_segment(
                pacing_plan_id=due.pacing_plan_id,
                segment_index=due.segment_index,
                authorization_id=authorization.authorization_id,
                operation_id=operation.operation_id,
            ):
                raise RuntimeError("durable verified send proof rejected")
            return operation
        except BaseException:  # claim stays recoverable after failure/cancellation
            if prepared_adapter is not None and not preparation_transferred:
                try:
                    await prepared_adapter.abort()
                except StagedCleanupRequired:
                    if claim is not None and self.pacing.is_due_claim_current(claim):
                        self._defer_navigation(claim, DueNavigationPreflightResult(
                            status="needs_attention", error_code="staged_cleanup_required"))
            # Fail-stop this item for this process; recovery reclaims it on restart.
            raise

    async def _stage_due(self, claim, artifact, eligibility, original, live_request, *, revisions):
        """Cold preparation precedes the sole authorization issuance."""
        self._require_current_claim(claim)
        due, port = claim.due, self.staged_preparation
        request = live_request()
        if ((self.control_cancelled is not None and self.control_cancelled())
                or not self.authorization.revalidate_due_passthrough(eligibility, request).may_authorize):
            self._reject_due(claim.outbox_id, due, binding=revisions[0], revision=revisions[1], claim=claim)
            return None
        now, tick = self.hub.now(), self.monotonic_ns_clock()
        deadline = min(now + timedelta(seconds=45), eligibility.valid_until,
                       original.plan_expires_at, original.draft.expires_at,
                       self.pacing.get_plan(due.pacing_plan_id).expires_at)
        if deadline <= now:
            self._reject_due(claim.outbox_id, due, binding=revisions[0], revision=revisions[1], claim=claim)
            return None
        keys = tuple(json.loads(artifact["source_keys_json"]))
        preparation = DraftPreparationRequest(
            reservation_id=uuid4(), nonce=uuid4(), outbox_id=claim.outbox_id, claim_token=claim.claim_token,
            due_event_id=due.event_id, one_shot_attempt_id=due.one_shot_attempt_id,
            account_id=artifact["account_id"], contact_id=due.contact_id, conversation_id=due.conversation_id,
            binding_id=port.binding_id_for(due.conversation_id), binding_revision=revisions[0],
            conversation_revision=revisions[1], global_revision=revisions[2], pacing_plan_id=due.pacing_plan_id,
            segment_index=due.segment_index, draft_id=due.draft_id, body=due.body, body_hash=due.body_hash,
            source_message_keys=keys, source_keys_digest=source_keys_digest(keys),
            expected_last_message_key=due.expected_last_message_key,
            original_snapshot_digest=sha256(json.dumps(dict(artifact), ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            requested_at=now, deadline_at=deadline, requested_monotonic_ns=tick,
            deadline_monotonic_ns=tick + int((deadline - now).total_seconds() * 1e9))
        controller = StagedPreparationController(port, clock=self.hub.now,
            monotonic_ns=self.monotonic_ns_clock, control_cancelled=self.control_cancelled)
        owner = preparation
        try:
            owner = await controller.prepare(preparation)
            self._require_current_claim(claim)
            fresh_artifact = self.state.plan_artifact(due.pacing_plan_id)
            fresh_snapshot = json.loads(fresh_artifact["eligibility_json"])[due.segment_index]
            fresh_eligibility = PolicyDecision.model_validate(fresh_snapshot["eligibility"])
            fresh_original = PolicyRequest.model_validate(fresh_snapshot["request"])
            self._validate_navigation_artifact(due, fresh_artifact, fresh_eligibility, fresh_original)
            if (dict(fresh_artifact) != dict(artifact) or fresh_eligibility != eligibility or fresh_original != original):
                raise RuntimeError("staged_artifact_changed")
            if self._existing_due_work(due) is not None:
                raise RuntimeError("staged_operation_appeared")
            live = live_request()
            b, r, p, g, gp = self.state.execution_state(due.conversation_id)
            if ((b, r, g) != revisions or p or gp or controller.cancel.is_set()
                    or not self.pacing.due_claim_state(claim).eligible
                    or min(eligibility.valid_until, original.plan_expires_at, original.draft.expires_at) <= self.hub.now()
                    or not self.authorization.revalidate_due_passthrough(eligibility, live).may_authorize):
                raise RuntimeError("staged_revalidation_failed")
            return StagedPrepareAdapter(port, owner, clock=self.hub.now, monotonic_ns=self.monotonic_ns_clock,
                cancel=controller.cancel, claim_current=lambda: self.pacing.is_due_claim_current(claim))
        except BaseException as exc:
            controller.cancel.set()
            cleanup_failed = False
            try:
                await controller.abort(owner)
            except StagedCleanupRequired:
                cleanup_failed = True
            if isinstance(exc, asyncio.CancelledError):
                raise
            # A late result must never acknowledge, reject, or defer a newer
            # claimant. Its exact reservation is still ours to clean up.
            if not self.pacing.is_due_claim_current(claim):
                raise RuntimeError("staged result lost exact claim") from exc
            if self._existing_due_work(due) is not None:
                existing = self._existing_due_work(due)
                await self._recover_existing_due(claim, existing)
                return None
            self._defer_navigation(claim, DueNavigationPreflightResult(status="needs_attention",
                error_code="staged_cleanup_required" if cleanup_failed else "staged_preparation_rejected"))
            return None

    @staticmethod
    def _validate_navigation_artifact(due, artifact, eligibility, original) -> None:
        source_keys = tuple(json.loads(artifact["source_keys_json"]))
        draft_ids = json.loads(artifact["segment_draft_ids_json"])
        if (artifact["conversation_id"] != due.conversation_id or artifact["contact_id"] != due.contact_id
                or original.draft.account_id != artifact["account_id"]
                or original.draft.conversation_id != due.conversation_id
                or original.draft.contact_id != due.contact_id
                or original.draft.draft_id != str(due.draft_id)
                or original.draft.pacing_plan_id != str(due.pacing_plan_id)
                or original.draft.source_message_keys != source_keys
                or original.draft.expected_last_message_key != due.expected_last_message_key
                or original.draft.body_hash != due.body_hash
                or sha256(due.body.encode()).hexdigest() != due.body_hash
                or eligibility.decision_id != due.eligibility_id
                or due.segment_index >= len(draft_ids) or draft_ids[due.segment_index] != str(due.draft_id)):
            raise RuntimeError("navigation due artifact membership mismatch")

    def _defer_navigation(self, claim, result: DueNavigationPreflightResult) -> None:
        if not self.pacing.defer_due_outbox(
            claim, not_before=result.retry_at, reason=result.error_code,
            needs_attention=result.status == "needs_attention",
        ):
            raise RuntimeError("navigation due defer lost exact unsent claim")

    def _require_current_claim(self, claim: ClaimedDueOutbox) -> None:
        if not self.pacing.is_due_claim_current(claim):
            raise RuntimeError("navigation due result lost exact claim")

    def _reject_due(self, outbox_id, due, *, binding, revision, claim=None) -> None:
        if claim is not None:
            self._require_current_claim(claim)
            # Fence acknowledgement before touching the other database. There
            # is no await between these steps; failed pacing CAS changes no
            # runtime segment belonging to a newer claimant.
            self._complete_due_outbox(outbox_id, due, operation=None, claim=claim)
        if not self.state.reject_due_segment(
            pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
            conversation_id=due.conversation_id, body_hash=due.body_hash,
            binding_revision=binding, conversation_revision=revision,
        ):
            raise RuntimeError("runtime M9 rejection CAS mismatch")
        if claim is None:
            self._complete_due_outbox(outbox_id, due, operation=None)

    def _existing_due_work(self, due: DueForRevalidation) -> _ExistingDueWork | None:
        segment = self.state.connection.execute(
            "SELECT * FROM runtime_segment_executions WHERE pacing_plan_id=? AND segment_index=?",
            (str(due.pacing_plan_id), due.segment_index),
        ).fetchone()
        operation_id = segment["operation_id"] if segment is not None else None
        rows = self.hub.store.connection.execute(
            "SELECT s.operation_id,s.idempotency_key,s.draft_id,s.authorization_id,d.conversation_id,d.contact_id,d.text_hash "
            "FROM send_operations s JOIN drafts d ON d.draft_id=s.draft_id "
            "WHERE s.idempotency_key=? OR s.operation_id=?",
            (f"m10:{due.pacing_plan_id}:{due.segment_index}", operation_id),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
            if (row["draft_id"] != str(due.draft_id) or row["conversation_id"] != due.conversation_id
                    or row["contact_id"] != due.contact_id or row["text_hash"] != due.body_hash
                    or (operation_id is not None and row["operation_id"] != operation_id)
                    or (segment is not None and segment["authorization_id"] not in {None, row["authorization_id"]})):
                return _ExistingDueWork(None, None, "navigation_existing_operation_scope_conflict")
            return _ExistingDueWork(self.hub._operation(row["operation_id"]), row["authorization_id"],
                                    "navigation_existing_operation")
        if rows or (segment is not None and (segment["operation_id"] is not None
                                             or segment["authorization_id"] is not None or segment["status"] != "due")):
            return _ExistingDueWork(None, None, "navigation_existing_execution_requires_recovery")
        return None

    async def _recover_existing_due(self, claim: ClaimedDueOutbox, existing: _ExistingDueWork) -> SendOperation | None:
        self._require_current_claim(claim)
        operation = existing.operation
        operation_id = operation.operation_id if operation else None
        expected_identity = self.hub._operation_identity(operation) if operation else None

        def hold(reason=existing.error_code):
            if not self.pacing.hold_due_outbox_for_recovery(
                claim, operation_id=operation_id, reason=reason,
            ):
                raise RuntimeError("existing due recovery hold lost exact claim")

        if operation is None or self.due_operation_recovery is None:
            hold()
            return operation
        reported = await self.due_operation_recovery(claim.due, operation)
        self._require_current_claim(claim)
        if reported is None:
            hold("navigation_operation_recovery_incomplete")
            return self.hub._operation(operation_id)
        authoritative = self.hub._operation(operation_id)
        if (self.hub._operation_identity(reported) != expected_identity
                or self.hub._operation_identity(authoritative) != expected_identity
                or reported.status != authoritative.status):
            hold("navigation_operation_recovery_result_mismatch")
            return authoritative
        intent = self.hub.store.connection.execute(
            "SELECT commit_intent FROM send_operations WHERE operation_id=?", (str(operation_id),)
        ).fetchone()[0]
        if authoritative.status == SendStatus.VERIFIED:
            if not intent or not self._runtime_recovery_binding_valid(
                claim, existing, allowed_statuses={"authorized", "verified"},
            ):
                hold("navigation_operation_recovery_proof_incomplete")
                return authoritative
            self._complete_due_outbox(claim.outbox_id, claim.due, operation=authoritative, claim=claim)
            if not self.state.settle_verified_segment(
                pacing_plan_id=claim.due.pacing_plan_id, segment_index=claim.due.segment_index,
                authorization_id=existing.authorization_id, operation_id=authoritative.operation_id,
            ):
                raise RuntimeError("recovered due durable verified proof rejected")
            return authoritative
        if not intent and authoritative.status in {SendStatus.FAILED, SendStatus.CANCELLED}:
            if not self._runtime_recovery_binding_valid(
                claim, existing, allowed_statuses={"authorized", authoritative.status.value},
            ):
                hold("navigation_operation_recovery_runtime_unbound")
                return authoritative
            self._complete_due_outbox(claim.outbox_id, claim.due, operation=authoritative, claim=claim)
            if not self.state.settle_segment_operation(
                pacing_plan_id=claim.due.pacing_plan_id, segment_index=claim.due.segment_index,
                authorization_id=existing.authorization_id, operation_id=authoritative.operation_id,
                operation_status=authoritative.status.value,
            ):
                raise RuntimeError("recovered terminal due runtime binding rejected")
            return authoritative
        hold("navigation_operation_recovery_incomplete")
        return authoritative

    def _runtime_recovery_binding_valid(self, claim, existing, *, allowed_statuses) -> bool:
        row = self.state.connection.execute(
            "SELECT conversation_id,body_hash,authorization_id,operation_id,status "
            "FROM runtime_segment_executions WHERE pacing_plan_id=? AND segment_index=?",
            (str(claim.due.pacing_plan_id), claim.due.segment_index),
        ).fetchone()
        return (row is not None and row["conversation_id"] == claim.due.conversation_id
                and row["body_hash"] == claim.due.body_hash
                and row["authorization_id"] == existing.authorization_id
                and row["operation_id"] == str(existing.operation.operation_id)
                and row["status"] in allowed_statuses)

    def _complete_due_outbox(
        self,
        outbox_id: int,
        due: DueForRevalidation,
        *,
        operation: SendOperation | None,
        claim: ClaimedDueOutbox | None = None,
    ) -> None:
        claim_arguments = {"expected_claim_token": claim.claim_token} if claim is not None else {}
        result = self.pacing.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            due.pacing_plan_id,
            segment_sent_and_verified=(
                operation is not None and operation.status.value == "verified"
            ),
            segment_index=due.segment_index,
            operation_id=operation.operation_id if operation is not None else None,
            one_shot_attempt_id=due.one_shot_attempt_id,
            **claim_arguments,
        )
        if result is None:
            raise RuntimeError("atomic pacing due settlement rejected exact result")
