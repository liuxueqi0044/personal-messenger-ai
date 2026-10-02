"""Cold UI work precedes the unchanged authorization/Hub send authority."""
import asyncio
import json
from datetime import timedelta
from uuid import UUID

import pytest

from messenger_ai.domain import SendOperation, SendStatus, Draft, Authorization, AuthorizationType, AuthorizedSendCommand
from messenger_ai.runtime.assembly import assemble_runtime
from messenger_ai.runtime.staged_preparation import PreparedDraftTicket, DraftCleanupResult
from messenger_ai.policy import AuthorizationConsumeResult, PolicyReason
from test_navigation_assembly import build, NOW, Planner


class Stage:
    def __init__(self):
        self.app = self.clock = None
        self.prepares, self.adopts, self.aborts, self.holds = [], [], [], []
        self.during_prepare = self.during_adopt = None
        self.cleanup_status = "cleaned"
        self.delay = 20
        self.bad_ticket = None
        self.adopt_error = False

    def tick(self):
        return int((self.clock.now() - NOW).total_seconds() * 1e9)

    def binding_id_for(self, conversation_id):
        assert conversation_id == "conversation"
        return "binding"

    def has_cleanup_obligation(self, account_id):
        return bool(self.holds)

    def retain_cleanup_hold(self, owner, *, reason):
        self.holds.append((owner, reason))

    def auth_count(self):
        return self.app.due.authorization._store._keeper.execute(
            "SELECT COUNT(*) FROM policy_authorizations").fetchone()[0]

    async def prepare_draft(self, request, *, cancel_event=None):
        assert self.auth_count() == 0
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0
        assert self.app.state.connection.execute("SELECT COUNT(*) FROM runtime_segment_executions").fetchone()[0] == 0
        self.prepares.append(request)
        self.clock.advance(self.delay)
        if self.during_prepare:
            await self.during_prepare(request, cancel_event)
        expiry = min(request.deadline_at, self.clock.now() + timedelta(seconds=15))
        ticket = PreparedDraftTicket(**request.model_dump(exclude={"body"}), run_id="run", session_epoch="session",
            surface_epoch="surface", worker_epoch="worker", process_id=100, window_handle=200,
            process_started_at_100ns=300, issued_at=self.clock.now(), expires_at=expiry,
            issued_monotonic_ns=self.tick(), expires_monotonic_ns=self.tick() + int((expiry-self.clock.now()).total_seconds()*1e9),
            evidence_digest="a"*64)
        if self.bad_ticket:
            ticket = self.bad_ticket(ticket)
        return ticket

    async def adopt_prepared(self, ticket, command, *, operation_id, segment_ref,
                            binding_revision, conversation_revision, cancel_event=None):
        assert self.auth_count() == 1
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 1
        assert self.app.due.authorization._store._keeper.execute(
            "SELECT consumed_at FROM policy_authorizations").fetchone()[0] is None
        self.adopts.append((ticket, command, operation_id))
        if self.during_adopt:
            await self.during_adopt(ticket, cancel_event)
        if self.adopt_error:
            raise RuntimeError("lost adoption reply")
        return SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key,
                             draft_id=command.draft_id, status=SendStatus.PREPARED)

    async def abort_owned_ticket(self, owner, *, deadline_at):
        self.aborts.append(owner)
        return DraftCleanupResult(status=self.cleanup_status, reservation_id=owner.reservation_id, nonce=owner.nonce)


def staged_app(tmp_path, stage):
    # Clock is bound before the injected function is first called.
    app, driver, clock, plan, request, eligibility = build(tmp_path,
        staged_preparation=stage, staged_monotonic_ns_clock=stage.tick)
    stage.app, stage.clock = app, clock
    return app, driver, clock, plan, request, eligibility


def no_authority(app):
    assert app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
    assert app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
    assert app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_twenty_second_cold_prepare_has_no_authority_then_unique_ten_second_m9_and_fast_adopt(tmp_path):
    stage = Stage()
    app, driver, clock, plan, request, _eligibility = staged_app(tmp_path, stage)
    try:
        assert await app.due.dispatch_one()
        assert len(stage.prepares) == len(stage.adopts) == driver.commit_calls == 1
        assert driver.prepares == stage.aborts == stage.holds == []
        row = app.due.authorization._store._keeper.execute("SELECT issued_at,expires_at,consumed_at FROM policy_authorizations").fetchone()
        from datetime import datetime
        assert datetime.fromisoformat(row["issued_at"]) == clock.now()
        assert datetime.fromisoformat(row["expires_at"]) - clock.now() == timedelta(seconds=10)
        assert row["consumed_at"] is not None
        assert stage.prepares[0].draft_id == stage.adopts[0][1].draft_id
        assert app.pacing.connection.execute("SELECT status FROM m10_plans").fetchone()[0] == "completed"
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "delivered"
        assert app.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "verified"
    finally:
        await app.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["cleaned", "cleanup_required"])
async def test_unique_authorizer_denial_after_prepare_aborts_once_and_preserves_hold_when_unknown(tmp_path, cleanup):
    stage = Stage()
    stage.cleanup_status = cleanup
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    calls = []
    def deny(eligibility, request):
        calls.append(request)
        return None, None
    app.due.authorization.authorize_due = deny
    try:
        assert await app.due.dispatch_one()
        assert len(calls) == len(stage.prepares) == len(stage.aborts) == 1
        assert stage.adopts == [] and driver.commit_calls == 0
        no_authority(app)
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == (
            "navigation_attention" if cleanup == "cleanup_required" else "delivered")
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_m9_consume_denial_after_fast_adoption_aborts_without_commit(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    def deny(*args, **kwargs):
        return AuthorizationConsumeResult(accepted=False, reason_codes=(PolicyReason.AUTHORIZATION_EXPIRED,))
    app.due.authorization.consume = deny
    try:
        assert await app.due.dispatch_one()
        assert len(stage.prepares) == len(stage.adopts) == len(stage.aborts) == 1
        assert driver.commit_calls == 0
        assert app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()[0] == "cancelled"
        assert app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is None
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_commit_unknown_retains_original_operation_and_never_clears_a_possibly_sent_composer(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    async def lost_ack(operation):
        driver.commit_calls += 1
        raise RuntimeError("worker acknowledgement lost after send may have happened")
    driver.commit_send = lost_ack
    try:
        assert await app.due.dispatch_one()
        assert driver.commit_calls == 1 and stage.aborts == []
        assert stage.holds
        row = app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()
        assert row["status"] == SendStatus.UNCERTAIN.value and row["commit_intent"] == 1
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "operation_recovery_hold"
        assert app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is not None
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_cancelled_cold_prepare_aborts_exact_request_and_rethrows_cancellation(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    entered = asyncio.Event()
    async def wait(request, cancel_event):
        entered.set()
        await asyncio.Event().wait()
    stage.during_prepare = wait
    task = asyncio.create_task(app.due.dispatch_one())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(stage.aborts) == 1 and stage.aborts[0] == stage.prepares[0]
        no_authority(app)
        assert driver.prepares == stage.adopts == []
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_existing_operation_expired_authorization_is_recovered_before_any_cold_preparation(tmp_path):
    stage = Stage()
    app, driver, clock, _plan, request, eligibility = staged_app(tmp_path, stage)
    recovered = []
    try:
        app.pacing.due_for_revalidation()
        claim = app.pacing.claim_due_outbox_with_tokens()[0]
        due = claim.due
        app.pacing.recover_due_outbox()
        _decision, envelope = app.due.authorization.authorize_due(
            eligibility, request.model_copy(update={"scheduled_due_at": due.due_at}))
        app.hub.create_draft(Draft(draft_id=due.draft_id, conversation_id=due.conversation_id,
            contact_id=due.contact_id, text=due.body, source_message_keys=(due.expected_last_message_key,), rule_version=due.rule_version))
        authorization = Authorization(authorization_id=UUID(envelope.authorization_id), draft_id=due.draft_id,
            conversation_id=due.conversation_id, expected_last_message_key=due.expected_last_message_key, text_hash=due.body_hash,
            idempotency_key=f"m10:{due.pacing_plan_id}:0", authorization_type=AuthorizationType.POLICY,
            policy_version=request.draft.policy_state_version, expires_at=envelope.expires_at)
        app.hub.persist_authorization(authorization)
        operation = app.hub.create_send_operation(AuthorizedSendCommand(**authorization.model_dump(exclude={"consumed"})))
        b, r = app.state.revisions(due.conversation_id)
        app.state.create_segment_execution(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            conversation_id=due.conversation_id, body_hash=due.body_hash, binding_revision=b, conversation_revision=r)
        app.state.bind_segment_operation(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            authorization_id=authorization.authorization_id, operation_id=operation.operation_id)
        async def recover(due, op):
            recovered.append(op.operation_id)
            return None
        app.due.due_operation_recovery = recover
        clock.advance(700)
        assert await app.due.dispatch_one()
        assert recovered == [operation.operation_id]
        assert stage.prepares == stage.adopts == stage.aborts == driver.prepares == []
        assert stage.auth_count() == 1 and driver.commit_calls == 0
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "operation_recovery_hold"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_owned_cleanup_attention_and_original_due_are_not_reset_by_startup_reconcile(tmp_path):
    stage = Stage()
    stage.cleanup_status = "cleanup_required"
    stage.bad_ticket = lambda ticket: ticket.model_copy(update={"binding_id": "other"})
    app, driver, clock, _plan, request, _eligibility = staged_app(tmp_path, stage)
    await app.due.dispatch_one()
    before = tuple(app.pacing.connection.execute(
        "SELECT payload_json,status,claim_token FROM m10_due_outbox").fetchone())
    await app.aclose()
    restarted = assemble_runtime(data_dir=tmp_path, planner_provider=Planner(), driver=driver,
        capability=request.state.capability, authorization_signing_key=b"assembly-test-signing-key-not-real",
        clock=clock, content_policy_checks_enabled=False, staged_preparation=stage,
        staged_monotonic_ns_clock=stage.tick)
    stage.app = restarted
    try:
        assert tuple(restarted.pacing.connection.execute(
            "SELECT payload_json,status,claim_token FROM m10_due_outbox").fetchone()) == before
        assert not await restarted.due.dispatch_one()
        assert len(stage.prepares) == 1 and stage.auth_count() == 0
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_cancel_during_commit_keeps_intent_and_uncertainty_without_abort(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    entered = asyncio.Event()
    async def wait(op):
        driver.commit_calls += 1
        entered.set()
        await asyncio.Event().wait()
    driver.commit_send = wait
    task = asyncio.create_task(app.due.dispatch_one())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert driver.commit_calls == 1 and stage.aborts == []
        assert stage.holds
        row = app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()
        assert row["status"] == SendStatus.UNCERTAIN.value and row["commit_intent"] == 1
        assert app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is not None
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_pure_due_policy_rejects_before_cold_ui_or_issuing_authority(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    try:
        revision, _paused, _reason = app.state.global_control()
        app.state.set_global_pause(paused=True, expected_revision=revision, reason="test")
        assert await app.due.dispatch_one()
        assert stage.prepares == driver.prepares == []
        no_authority(app)
    finally:
        await app.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause_intent", "binding", "artifact", "expiry", "eligibility"])
async def test_cold_prepare_return_rechecks_frozen_task_control_and_expiry_before_authorize(tmp_path, change):
    stage = Stage()
    app, driver, clock, plan, _request, _eligibility = staged_app(tmp_path, stage)

    async def mutate(request, cancel_event):
        if change == "pause_intent":
            app.begin_global_pause_from_control()
            assert cancel_event.is_set()
            assert not app.state.global_control()[1]
        elif change == "binding":
            app.state.connection.execute("UPDATE runtime_conversations SET binding_revision=binding_revision+1")
            app.state.connection.commit()
        elif change == "artifact":
            app.state.connection.execute("UPDATE runtime_plan_artifacts SET planner_json=?", (json.dumps({"reply_segments":["Other"]}),))
            app.state.connection.commit()
        elif change == "expiry":
            clock.advance(700)
            raise RuntimeError("prepare expired before finishing")
        else:
            app.pacing.connection.execute("UPDATE m10_plans SET status='cancelled'")
            app.pacing.connection.commit()
    stage.during_prepare = mutate
    try:
        assert await app.due.dispatch_one()
        no_authority(app)
        assert stage.adopts == driver.prepares == [] and driver.commit_calls == 0
        assert len(stage.aborts) == 1
        assert stage.aborts[0].reservation_id == stage.prepares[0].reservation_id
        assert app.pacing.get_plan(plan.pacing_plan_id).segment_index == 0
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "navigation_attention"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_stale_claim_cleans_its_reservation_but_cannot_change_new_owner(tmp_path):
    stage = Stage()
    app, driver, _clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)
    newer = []

    async def reclaim(request, cancel_event):
        app.pacing.recover_due_outbox()
        newer.extend(app.pacing.claim_due_outbox_with_tokens())
    stage.during_prepare = reclaim
    try:
        with pytest.raises(RuntimeError, match="lost exact claim"):
            await app.due.dispatch_one()
        no_authority(app)
        row = app.pacing.connection.execute("SELECT status,claim_token FROM m10_due_outbox").fetchone()
        assert row["status"] == "dispatching" and row["claim_token"] == newer[0].claim_token
        assert len(stage.aborts) == 1 and stage.adopts == []
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_cleanup_unknown_holds_original_due_and_blocks_future_preparation(tmp_path):
    stage = Stage()
    stage.cleanup_status = "cleanup_required"
    stage.bad_ticket = lambda ticket: ticket.model_copy(update={"binding_id": "other"})
    app, driver, _clock, plan, _request, _eligibility = staged_app(tmp_path, stage)
    try:
        assert await app.due.dispatch_one()
        no_authority(app)
        assert len(stage.holds) == len(stage.aborts) == 1
        assert app.pacing.get_plan(plan.pacing_plan_id).segment_index == 0
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "navigation_attention"
        assert not await app.due.dispatch_one()
        assert len(stage.prepares) == 1
    finally:
        await app.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "expired", "failed", "claim_lost", "cleanup_unknown"])
async def test_fast_adoption_failure_never_consumes_m9_or_commits_and_uses_exact_owned_cleanup(tmp_path, change):
    stage = Stage()
    app, driver, clock, _plan, _request, _eligibility = staged_app(tmp_path, stage)

    async def mutate(ticket, cancel_event):
        if change == "pause":
            app.begin_global_pause_from_control()
        elif change == "expired":
            clock.advance(16)
        elif change == "claim_lost":
            app.pacing.recover_due_outbox()
            app.pacing.claim_due_outbox_with_tokens()
        elif change == "cleanup_unknown":
            stage.cleanup_status = "cleanup_required"
            raise RuntimeError("lost adoption reply")
        else:
            raise RuntimeError("adopt failed")
    stage.during_adopt = mutate
    try:
        if change == "claim_lost":
            with pytest.raises(RuntimeError, match="settlement rejected"):
                await app.due.dispatch_one()
        else:
            assert await app.due.dispatch_one()
        assert stage.auth_count() == 1 and len(stage.adopts) == len(stage.aborts) == 1
        assert app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is None
        assert driver.commit_calls == 0 and driver.prepares == []
        row = app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()
        assert row["commit_intent"] == 0
        assert row["status"] == (SendStatus.UNCERTAIN.value if change == "cleanup_unknown" else "cancelled")
        if change == "cleanup_unknown":
            assert stage.holds
            assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "operation_recovery_hold"
    finally:
        await app.aclose()
