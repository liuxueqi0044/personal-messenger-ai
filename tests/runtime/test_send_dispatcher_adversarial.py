"""Race tests for the M9/M10 two-phase send boundary.

These tests deliberately change durable runtime control while Hub is in the
reversible prepare phase.  A previously issued authorization must never turn
that item into a commit after a global or contact pause (including a pause
which is resumed before the second check).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

from messenger_ai.domain import (
    Authorization,
    AuthorizationType,
    AuthorizedSendCommand,
    Draft,
    InboundMessage,
    Platform,
    ReplyPlan,
    SendOperation,
    SendStatus,
)
from messenger_ai.hub import HubService, SQLiteHubStore
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft
from messenger_ai.pacing.models import DueForRevalidation, ScheduleRequest
from messenger_ai.policy import (
    AuthorizationService,
    CapabilitySnapshot,
    ConversationType,
    DraftSnapshot,
    LivePolicyState,
    PolicyEngine,
    PolicyRequest,
    SQLiteAuthorizationStore,
    SupportLevel,
)
from messenger_ai.runtime.due_dispatch import DueCoordinator
from messenger_ai.runtime.send_dispatcher import AuthorizedDueExecution, SendDispatcher
from messenger_ai.runtime.state import RuntimeState, VerifiedSendStorePaths

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
        if self.mode == "prepare_failure":
            return SendOperation(
                operation_id=operation_id,
                idempotency_key=command.idempotency_key,
                draft_id=command.draft_id,
                status=SendStatus.FAILED,
            )
        revision, _paused, _ = self.state.global_control()
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
        operation.status = (
            SendStatus.VERIFIED if self.mode == "commit_verified" else SendStatus.COMMITTED
        )
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
    return PolicyRequest(draft=draft, inbound_text="刚忙完", state=LivePolicyState(observed_at=clock.now(), last_message_key="in-1", active_rulepack_version="rules-v1", active_pacing_rule_version="rules-v1", capability=capability, policy_state_version="state-v1", conversation_type=ConversationType.DIRECT, contact_whitelisted=True, automation_enabled=True), scheduled_due_at=clock.now(), plan_expires_at=clock.now()+timedelta(minutes=5))


def _fixture(tmp_path, mode: str):
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    hub = HubService(hub_store, clock=clock)
    hub.ingest(InboundMessage(platform=Platform.QQ, account_id="acct", conversation_id="conv", contact_id="contact", platform_message_key="in-1", observed_at=NOW, text="刚忙完"))
    draft_id = UUID("00000000-0000-0000-0000-000000000102")
    hub.create_draft(Draft(draft_id=draft_id, conversation_id="conv", contact_id="contact", text="收到啦", source_message_keys=("in-1",), rule_version="rules-v1"))
    state = RuntimeState(
        tmp_path / "runtime.sqlite",
        verified_send_stores=VerifiedSendStorePaths(
            hub=tmp_path / "hub.sqlite", pacing=tmp_path / "pacing.sqlite"
        ),
    )
    state.register(account_id="acct", contact_id="contact", conversation_id="conv", binding_revision=1)
    pacing = PacingScheduler(tmp_path / "pacing.sqlite", clock=clock)
    outcome = pacing.schedule(ScheduleRequest(draft=PacingDraft(draft_id=draft_id, conversation_id="conv", contact_id="contact", text="收到啦", text_hash=sha256("收到啦".encode()).hexdigest(), expected_last_message_key="in-1", rule_version="rules-v1", eligibility_id="eligibility-1"), reply_plan=ReplyPlan(action="draft", reply_text="收到啦", reply_segments=["收到啦"]), source_message_keys=("in-1",), first_inbound_at=NOW, last_inbound_at=NOW, inbound_text="刚忙完"))
    assert outcome.plan is not None
    clock.advance(120)
    due = pacing.due_for_revalidation()[0]
    state.save_plan_artifact(
        pacing_plan_id=due.pacing_plan_id,
        conversation_id="conv",
        conversation_revision=1,
        binding_revision=1,
        global_revision=1,
        eligibility_json="[]",
        planner_json=json.dumps({"reply_segments": ["收到啦"]}),
        rule_version="rules-v1",
        account_id="acct",
        contact_id="contact",
        source_keys=("in-1",),
        segment_draft_ids=(str(draft_id),),
    )
    policy_store = SQLiteAuthorizationStore(tmp_path / "auth.sqlite")
    engine = PolicyEngine(clock.now)
    auth = AuthorizationService(engine, policy_store, b"x" * 32, clock.now)
    request = _request(clock, draft_id, due.pacing_plan_id)
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
    assert values[3].connection.execute(
        "SELECT status FROM runtime_segment_executions"
    ).fetchone()[0] == "cancelled"
    assert values[3].connection.execute(
        "SELECT status FROM runtime_plan_artifacts"
    ).fetchone()[0] == "cancelled"


def test_global_pause_then_resume_still_invalidates_old_item(tmp_path):
    result, values = _run(tmp_path, "global_pause_resume")
    assert result.status is SendStatus.CANCELLED
    assert values[2].commit_calls == 0


def test_contact_pause_during_prepare_cannot_commit(tmp_path):
    result, values = _run(tmp_path, "contact_pause")
    assert result.status is SendStatus.CANCELLED
    assert values[2].commit_calls == 0


def test_prepare_failure_closes_runtime_as_rejected(tmp_path):
    result, values = _run(tmp_path, "prepare_failure")
    assert result.status is SendStatus.FAILED
    assert values[2].commit_calls == 0
    assert values[3].connection.execute(
        "SELECT status FROM runtime_segment_executions"
    ).fetchone()[0] == "failed"
    assert values[3].connection.execute(
        "SELECT status FROM runtime_plan_artifacts"
    ).fetchone()[0] == "rejected"
    replay = asyncio.run(values[0].execute(values[1]))
    assert replay.operation_id == result.operation_id
    assert replay.status is SendStatus.FAILED
    assert values[2].prepare_calls == 1


def test_runtime_operation_bind_is_exact_and_terminal_replay_is_idempotent(
    tmp_path,
):
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    plan_id = UUID("00000000-0000-0000-0000-000000000221")
    other_plan_id = UUID("00000000-0000-0000-0000-000000000222")
    operation_id = UUID("00000000-0000-0000-0000-000000000E21")
    assert state.create_segment_execution(
        pacing_plan_id=plan_id,
        segment_index=0,
        conversation_id="conv",
        body_hash="a" * 64,
        binding_revision=1,
        conversation_revision=2,
    )
    assert state.create_segment_execution(
        pacing_plan_id=other_plan_id,
        segment_index=0,
        conversation_id="other",
        body_hash="b" * 64,
        binding_revision=1,
        conversation_revision=1,
    )
    assert state.bind_segment_operation(
        pacing_plan_id=plan_id,
        segment_index=0,
        authorization_id="auth",
        operation_id=operation_id,
    )
    assert state.settle_segment_operation(
        pacing_plan_id=plan_id,
        segment_index=0,
        authorization_id="auth",
        operation_id=operation_id,
        operation_status="failed",
    )
    assert state.bind_segment_operation(
        pacing_plan_id=plan_id,
        segment_index=0,
        authorization_id="auth",
        operation_id=operation_id,
    )
    assert not state.bind_segment_operation(
        pacing_plan_id=plan_id,
        segment_index=0,
        authorization_id="other-auth",
        operation_id=operation_id,
    )
    assert not state.bind_segment_operation(
        pacing_plan_id=other_plan_id,
        segment_index=0,
        authorization_id="auth",
        operation_id=operation_id,
    )


def test_dispatcher_waits_for_durable_pacing_proof_before_runtime_verified(tmp_path):
    result, values = _run(tmp_path, "verified")
    assert result.status is SendStatus.VERIFIED
    assert values[3].connection.execute(
        "SELECT status FROM runtime_segment_executions"
    ).fetchone()[0] == "authorized"
    assert values[3].connection.execute(
        "SELECT status FROM runtime_plan_artifacts"
    ).fetchone()[0] == "waiting"


def test_commit_cannot_skip_verify_into_a_runtime_verified_segment(tmp_path):
    # A driver that claims VERIFIED straight from COMMIT must never let the
    # runtime lane (or artifact) record a verified send; the Hub fails closed
    # and runtime settles the segment as send_uncertain.
    result, values = _run(tmp_path, "commit_verified")
    assert result.status is SendStatus.UNCERTAIN
    assert values[2].verify_calls == 0
    assert values[3].connection.execute(
        "SELECT status FROM runtime_segment_executions"
    ).fetchone()[0] == "send_uncertain"
    assert values[3].connection.execute(
        "SELECT status FROM runtime_plan_artifacts"
    ).fetchone()[0] == "send_uncertain"


def test_due_atomic_completion_is_exact_and_fails_closed() -> None:
    calls: list[dict[str, object]] = []
    operation = SendOperation(
        operation_id=UUID("00000000-0000-0000-0000-000000000333"),
        idempotency_key="send",
        draft_id=UUID("00000000-0000-0000-0000-000000000334"),
        status=SendStatus.VERIFIED,
    )
    due = DueForRevalidation(
        pacing_plan_id=UUID("00000000-0000-0000-0000-000000000335"),
        one_shot_attempt_id=UUID("00000000-0000-0000-0000-000000000336"),
        conversation_id="conv",
        contact_id="contact",
        draft_id=operation.draft_id,
        text_hash="a" * 64,
        expected_last_message_key="in-1",
        rule_version="rules-v1",
        pacing_rule_version="rules-v1",
        eligibility_id="eligibility",
        due_at=NOW,
        segment_index=0,
        segment_count=1,
        body="body",
        body_hash=sha256(b"body").hexdigest(),
    )

    class Pacing:
        result = object()

        def record_revalidation_result_and_complete_due_outbox(
            self, outbox_id, pacing_plan_id, **kwargs
        ):
            calls.append(
                {"outbox_id": outbox_id, "pacing_plan_id": pacing_plan_id, **kwargs}
            )
            return self.result

    pacing = Pacing()
    coordinator = DueCoordinator(
        state=None,
        hub=None,
        pacing=pacing,
        rules=None,
        authorization=None,
        dispatcher=None,
        capability_provider=None,
    )
    coordinator._complete_due_outbox(17, due, operation=operation)
    assert calls == [{
        "outbox_id": 17,
        "pacing_plan_id": due.pacing_plan_id,
        "segment_sent_and_verified": True,
        "segment_index": 0,
        "operation_id": operation.operation_id,
        "one_shot_attempt_id": due.one_shot_attempt_id,
    }]
    pacing.result = None
    try:
        coordinator._complete_due_outbox(17, due, operation=operation)
    except RuntimeError as exc:
        assert "atomic pacing" in str(exc)
    else:
        raise AssertionError("missing pacing CAS must fail closed")


def test_m9_rejection_terminalizes_runtime_and_atomic_outbox(tmp_path) -> None:
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    state.register(
        account_id="acct",
        contact_id="contact",
        conversation_id="conv",
        binding_revision=1,
    )
    plan_id = UUID("00000000-0000-0000-0000-000000000441")
    draft_id = UUID("00000000-0000-0000-0000-000000000442")
    request = _request(Clock(), draft_id, plan_id)
    eligibility = PolicyEngine(lambda: NOW).evaluate_eligibility(request)
    state.save_plan_artifact(
        pacing_plan_id=plan_id,
        conversation_id="conv",
        conversation_revision=1,
        binding_revision=1,
        global_revision=1,
        eligibility_json=json.dumps([{
            "eligibility": eligibility.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        }]),
        planner_json=json.dumps({"reply_segments": ["body"]}),
        rule_version="rules-v1",
        account_id="acct",
        contact_id="contact",
        source_keys=("in-1",),
        segment_draft_ids=(str(draft_id),),
    )
    due = DueForRevalidation(
        pacing_plan_id=plan_id,
        conversation_id="conv",
        contact_id="contact",
        draft_id=draft_id,
        text_hash=sha256(b"body").hexdigest(),
        expected_last_message_key="in-1",
        rule_version="rules-v1",
        pacing_rule_version="rules-v1",
        eligibility_id=eligibility.decision_id,
        due_at=NOW,
        segment_index=0,
        segment_count=1,
        body="body",
        body_hash=sha256(b"body").hexdigest(),
    )
    calls: list[dict[str, object]] = []

    class Pacing:
        def record_revalidation_result_and_complete_due_outbox(
            self, outbox_id, pacing_plan_id, **kwargs
        ):
            calls.append(
                {"outbox_id": outbox_id, "pacing_plan_id": pacing_plan_id, **kwargs}
            )
            return object()

    coordinator = DueCoordinator(
        state=state,
        hub=SimpleNamespace(now=lambda: NOW),
        pacing=Pacing(),
        rules=SimpleNamespace(
            resolve=lambda *_args, **_kwargs: SimpleNamespace(
                rulepack=SimpleNamespace(version="rules-v1")
            )
        ),
        authorization=SimpleNamespace(authorize_due=lambda *_args: (None, None)),
        dispatcher=None,
        capability_provider=lambda: request.state.capability,
    )

    assert asyncio.run(coordinator._dispatch_claimed(23, due)) is None
    assert tuple(state.connection.execute(
        "SELECT status,operation_id FROM runtime_segment_executions"
    ).fetchone()) == ("rejected", None)
    assert state.connection.execute(
        "SELECT status FROM runtime_plan_artifacts"
    ).fetchone()[0] == "rejected"
    assert calls == [{
        "outbox_id": 23,
        "pacing_plan_id": plan_id,
        "segment_sent_and_verified": False,
        "segment_index": 0,
        "operation_id": None,
        "one_shot_attempt_id": None,
    }]
