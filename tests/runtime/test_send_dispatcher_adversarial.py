"""Race tests for the M9/M10 two-phase send boundary.

These tests deliberately change durable runtime control while Hub is in the
reversible prepare phase.  A previously issued authorization must never turn
that item into a commit after a global or contact pause (including a pause
which is resumed before the second check).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from messenger_ai.domain import Authorization, AuthorizationType, AuthorizedSendCommand, Draft, InboundMessage, Platform, ReplyPlan, SendOperation, SendStatus
from messenger_ai.hub import HubService, SQLiteHubStore
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft, DueForRevalidation, ScheduleRequest
from messenger_ai.policy import AuthorizationService, CapabilitySnapshot, DraftSnapshot, LivePolicyState, PolicyEngine, PolicyRequest, SQLiteAuthorizationStore, SupportLevel
from messenger_ai.runtime.state import RuntimeState
from messenger_ai.runtime.send_dispatcher import AuthorizedDueExecution, SendDispatcher


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class CountingDriver:
    def __init__(self, state: RuntimeState, mode: str) -> None:
        self.state, self.mode = state, mode
        self.prepare_calls = self.commit_calls = self.verify_calls = self.abort_calls = 0

    async def prepare_send(self, command: AuthorizedSendCommand, *, operation_id: UUID, segment_ref: str, binding_revision: int, conversation_revision: int) -> SendOperation:
        self.prepare_calls += 1
        revision, paused, _ = self.state.global_control()
        if self.mode == "global_pause":
            assert self.state.set_global_pause(paused=True, expected_revision=revision)
        elif self.mode == "global_pause_resume":
            assert self.state.set_global_pause(paused=True, expected_revision=revision)
            revision, _, _ = self.state.global_control()
            assert self.state.set_global_pause(paused=False, expected_revision=revision)
        elif self.mode == "contact_pause":
            self.state.pause(command.conversation_id)
        return SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key, draft_id=command.draft_id, status=SendStatus.PREPARED)

    async def commit_send(self, operation: SendOperation) -> SendOperation:
        self.commit_calls += 1
        operation.status = SendStatus.COMMITTED
        return operation

    async def verify_send(self, operation: SendOperation) -> SendOperation:
        self.verify_calls += 1
        operation.status = SendStatus.VERIFIED
        return operation

    async def abort_send(self, operation: SendOperation) -> None:
        self.abort_calls += 1


def _request(clock: Clock, draft_id: UUID, plan_id: UUID) -> PolicyRequest:
    capability = CapabilitySnapshot(capability_version="qq-v1", environment_fingerprint="test", send_background=SupportLevel.SUPPORTED, verify_background=SupportLevel.SUPPORTED, healthy=True, client_version="test")
    draft = DraftSnapshot(draft_id=str(draft_id), platform="qq", account_id="acct", conversation_id="conv", contact_id="contact", body="收到啦", expected_last_message_key="in-1", source_message_keys=("in-1",), rulepack_version="rules-v1", pacing_plan_id=str(plan_id), pacing_rule_version="rules-v1", capability_snapshot_hash=capability.snapshot_hash, policy_state_version="state-v1", created_at=clock.now(), expires_at=clock.now()+timedelta(minutes=5))
    return PolicyRequest(draft=draft, inbound_text="刚忙完", state=LivePolicyState(observed_at=clock.now(), last_message_key="in-1", active_rulepack_version="rules-v1", active_pacing_rule_version="rules-v1", capability=capability, policy_state_version="state-v1", contact_whitelisted=True, automation_enabled=True), scheduled_due_at=clock.now(), plan_expires_at=clock.now()+timedelta(minutes=5))


def _fixture(tmp_path, mode: str):
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    hub = HubService(hub_store, clock=clock)
    hub.ingest(InboundMessage(platform=Platform.QQ, account_id="acct", conversation_id="conv", contact_id="contact", platform_message_key="in-1", observed_at=NOW, text="刚忙完"))
    plan_id = UUID("00000000-0000-0000-0000-000000000101")
    draft_id = UUID("00000000-0000-0000-0000-000000000102")
    hub.create_draft(Draft(draft_id=draft_id, conversation_id="conv", contact_id="contact", text="收到啦", source_message_keys=("in-1",), rule_version="rules-v1"))
    state = RuntimeState(tmp_path / "runtime.sqlite")
    state.register(account_id="acct", contact_id="contact", conversation_id="conv", binding_revision=1)
    pacing = PacingScheduler(tmp_path / "pacing.sqlite", clock=clock)
    outcome = pacing.schedule(ScheduleRequest(draft=PacingDraft(draft_id=draft_id, conversation_id="conv", contact_id="contact", text="收到啦", text_hash=sha256("收到啦".encode()).hexdigest(), expected_last_message_key="in-1", rule_version="rules-v1", eligibility_id="eligibility-1"), reply_plan=ReplyPlan(action="draft", reply_text="收到啦", reply_segments=["收到啦"]), source_message_keys=("in-1",), first_inbound_at=NOW, last_inbound_at=NOW, inbound_text="刚忙完"))
    assert outcome.plan is not None
    clock.advance(120)
    due = pacing.due_for_revalidation()[0]
    policy_store = SQLiteAuthorizationStore(tmp_path / "auth.sqlite")
    engine = PolicyEngine(clock.now)
    auth = AuthorizationService(engine, policy_store, b"x" * 32, clock.now)
    request = _request(clock, draft_id, plan_id)
    decision, envelope = auth.authorize_due(engine.evaluate_eligibility(request), request)
    assert envelope is not None and decision.may_authorize
    hub.persist_authorization(Authorization(authorization_id=UUID(envelope.authorization_id), draft_id=draft_id, conversation_id="conv", expected_last_message_key="in-1", text_hash=request.draft.body_hash, idempotency_key="send-1", authorization_type=AuthorizationType.POLICY, policy_version="rules-v1", expires_at=envelope.expires_at))
    command = AuthorizedSendCommand(draft_id=draft_id, conversation_id="conv", expected_last_message_key="in-1", text_hash=request.draft.body_hash, idempotency_key="send-1", authorization_type=AuthorizationType.POLICY, authorization_id=UUID(envelope.authorization_id), policy_version="rules-v1", expires_at=envelope.expires_at)
    binding, conversation, _, global_revision, _ = state.execution_state("conv")
    driver = CountingDriver(state, mode)
    dispatcher = SendDispatcher(state=state, hub=hub, pacing=pacing, authorization=auth, driver=driver)
    item = AuthorizedDueExecution(due=due, command=command, token=envelope.token, binding=envelope.binding, live_policy_request_factory=lambda: request, binding_revision=binding, conversation_revision=conversation, global_revision=global_revision)
    return dispatcher, item, driver, state, hub_store, pacing, policy_store


def _run(tmp_path, mode: str):
    values = _fixture(tmp_path, mode)
    result = asyncio.run(values[0].execute(values[1]))
    return result, values


def test_global_pause_during_prepare_cannot_commit(tmp_path):
    result, values = _run(tmp_path, "global_pause")
    assert result.status is SendStatus.CANCELLED
    assert values[2].commit_calls == 0


def test_global_pause_then_resume_still_invalidates_old_item(tmp_path):
    result, values = _run(tmp_path, "global_pause_resume")
    assert result.status is SendStatus.CANCELLED
    assert values[2].commit_calls == 0


def test_contact_pause_during_prepare_cannot_commit(tmp_path):
    result, values = _run(tmp_path, "contact_pause")
    assert result.status is SendStatus.CANCELLED
    assert values[2].commit_calls == 0
