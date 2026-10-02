import asyncio
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.domain import AuthorizedSendCommand, AuthorizationType, SendOperation, SendStatus
from messenger_ai.runtime.staged_preparation import (
    DraftPreparationRequest, PreparedDraftTicket, DraftCleanupResult, ControlCancellation,
    StagedPreparationController, StagedPrepareAdapter, StagedPreparationError,
    StagedCleanupRequired, source_keys_digest,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def request(duration=45):
    return DraftPreparationRequest(reservation_id=uuid4(), nonce=uuid4(), outbox_id=1, claim_token="claim",
        due_event_id=uuid4(), account_id="account", contact_id="contact", conversation_id="conversation",
        binding_id="binding", binding_revision=1, conversation_revision=2, global_revision=1,
        pacing_plan_id=uuid4(), segment_index=0, draft_id=uuid4(), body="Reply", body_hash=sha256(b"Reply").hexdigest(),
        source_message_keys=("key",), source_keys_digest=source_keys_digest(("key",)), expected_last_message_key="key",
        original_snapshot_digest="b"*64, requested_at=NOW, deadline_at=NOW+timedelta(seconds=duration),
        requested_monotonic_ns=0, deadline_monotonic_ns=int(duration*1e9))


def ticket(req):
    return PreparedDraftTicket(**req.model_dump(exclude={"body"}), run_id="run", session_epoch="session",
        surface_epoch="surface", worker_epoch="worker", process_id=10, window_handle=20,
        process_started_at_100ns=30, issued_at=NOW, expires_at=NOW+timedelta(seconds=min(10,(req.deadline_at-NOW).total_seconds())),
        issued_monotonic_ns=0, expires_monotonic_ns=min(10_000_000_000, req.deadline_monotonic_ns), evidence_digest="a"*64)


class Port:
    def __init__(self):
        self.holds, self.adopts, self.aborts = [], [], []
        self.transform = None
        self.cleanup_status = "cleaned"

    async def prepare_draft(self, req, *, cancel_event=None):
        result = ticket(req)
        return self.transform(result) if self.transform else result

    async def adopt_prepared(self, t, command, *, operation_id, **kwargs):
        self.adopts.append((t, command, operation_id))
        return SendOperation(operation_id=operation_id, draft_id=command.draft_id,
                             idempotency_key=command.idempotency_key, status=SendStatus.PREPARED)

    async def abort_owned_ticket(self, owner, *, deadline_at):
        self.aborts.append(owner)
        return DraftCleanupResult(status=self.cleanup_status, reservation_id=owner.reservation_id, nonce=owner.nonce)

    def retain_cleanup_hold(self, owner, *, reason):
        self.holds.append((owner, reason))


@pytest.mark.parametrize("field,value", [("body", "Other"), ("body_hash", "c"*64), ("source_keys_digest", "c"*64),
                                        ("expected_last_message_key", "other"), ("binding_revision", True)])
def test_preparation_request_rejects_changed_body_or_cursor_or_boolean_revision(field, value):
    data = request().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        DraftPreparationRequest.model_validate(data)


def test_ticket_has_no_authorization_and_rejects_unknown_send_permission():
    data = ticket(request()).model_dump()
    data["authorization_id"] = uuid4()
    with pytest.raises(ValidationError):
        PreparedDraftTicket.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"nonce": uuid4()}, {"claim_token": "new"}, {"binding_id": "other"}, {"global_revision": 2},
    {"due_event_id": uuid4()}, {"original_snapshot_digest": "c"*64}, {"expected_sequence_digest": "bad"},
    {"issued_at": NOW+timedelta(seconds=1)}, {"issued_monotonic_ns": 1_000_000_000},
    {"expires_at": NOW}, {"expires_monotonic_ns": 0},
])
async def test_controller_rejects_reconstructed_or_future_or_expired_ticket_even_model_copy(update):
    port, req = Port(), request()
    port.transform = lambda t: t.model_copy(update=update)
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    with pytest.raises(StagedPreparationError):
        await controller.prepare(req)
    assert controller.cancel.is_set() and port.adopts == []


@pytest.mark.asyncio
async def test_cold_prepare_timeout_is_bounded_discards_late_reply_and_revokes_input():
    port, req = Port(), request(.02)
    release, ended = asyncio.Event(), asyncio.Event()
    observed = []

    async def suppress_cancel(req, *, cancel_event):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        observed.append(cancel_event.is_set())
        ended.set()
        return ticket(req)
    port.prepare_draft = suppress_cancel
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    try:
        with pytest.raises(StagedPreparationError, match="timeout"):
            await asyncio.wait_for(controller.prepare(req), timeout=.2)
        assert controller.cancel.is_set() and port.adopts == []
    finally:
        release.set()
        await asyncio.wait_for(ended.wait(), timeout=.2)
    assert observed == [True]


@pytest.mark.asyncio
async def test_prepare_caller_cancellation_is_preserved_and_input_cancelled():
    port, req = Port(), request()
    entered, observed = asyncio.Event(), []

    async def wait(req, *, cancel_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            observed.append(cancel_event.is_set())
    port.prepare_draft = wait
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    task = asyncio.create_task(controller.prepare(req))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert observed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_owned", "cleanup_required"])
async def test_cleanup_without_exact_proof_retains_durable_owned_hold(status):
    port, req = Port(), request()
    port.cleanup_status = status
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    with pytest.raises(StagedCleanupRequired):
        await controller.abort(req)
    assert port.aborts == [req] and port.holds[0][0] is req


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_error", [RuntimeError, asyncio.CancelledError])
async def test_cleanup_hold_failure_cannot_replace_unproven_cleanup_contract(hold_error):
    port, req = Port(), request()
    port.cleanup_status = "not_owned"

    def fail_hold(owner, *, reason):
        assert owner is req
        raise hold_error("hold unavailable")

    port.retain_cleanup_hold = fail_hold
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    with pytest.raises(StagedCleanupRequired, match="staged_cleanup_unproven"):
        await controller.abort(req)
    assert port.aborts == [req] and port.adopts == []


@pytest.mark.asyncio
async def test_cleanup_caller_cancellation_survives_failed_hold_write():
    port, req = Port(), request()
    entered, ended = asyncio.Event(), asyncio.Event()

    async def wait(owner, *, deadline_at):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    def fail_hold(owner, *, reason):
        raise RuntimeError("hold unavailable")

    port.abort_owned_ticket, port.retain_cleanup_hold = wait, fail_hold
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    task = asyncio.create_task(controller.abort(req))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(ended.wait(), timeout=.2)


@pytest.mark.asyncio
async def test_cleanup_wrong_nonce_is_not_accepted():
    port, req = Port(), request()

    async def wrong(owner, **kwargs):
        return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=uuid4())
    port.abort_owned_ticket = wrong
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    with pytest.raises(StagedCleanupRequired):
        await controller.abort(req)
    assert port.holds[0][0] is req


@pytest.mark.asyncio
async def test_blocking_cleanup_late_ack_cannot_clear_hold_after_envelope_timeout():
    port, req = Port(), request()
    async def late(owner, **kwargs):
        time.sleep(.03)
        return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=owner.nonce)
    port.abort_owned_ticket = late
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0, cleanup_timeout_seconds=.01)
    with pytest.raises(StagedCleanupRequired):
        await controller.abort(req)
    assert port.holds[0][0] is req


@pytest.mark.asyncio
async def test_cleanup_contradictory_cleaned_error_is_not_proof():
    port, req = Port(), request()
    async def contradictory(owner, **kwargs):
        return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id,
                                  nonce=owner.nonce, error_code="cleanup_failed")
    port.abort_owned_ticket = contradictory
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0)
    with pytest.raises(StagedCleanupRequired):
        await controller.abort(req)
    assert port.holds[0][0] is req


@pytest.mark.asyncio
async def test_cleanup_timeout_does_not_wait_for_cancellation_suppressing_backend():
    port, req = Port(), request()
    release, ended = asyncio.Event(), asyncio.Event()

    async def late(owner, **kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        ended.set()
        return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=owner.nonce)
    port.abort_owned_ticket = late
    controller = StagedPreparationController(port, clock=lambda: NOW, monotonic_ns=lambda: 0, cleanup_timeout_seconds=.02)
    try:
        with pytest.raises(StagedCleanupRequired):
            await asyncio.wait_for(controller.abort(req), timeout=.2)
        assert port.holds[0][0] is req
    finally:
        release.set()
        await asyncio.wait_for(ended.wait(), timeout=.2)


def command(req):
    return AuthorizedSendCommand(authorization_id=uuid4(), draft_id=req.draft_id, conversation_id=req.conversation_id,
        expected_last_message_key=req.expected_last_message_key, text_hash=req.body_hash,
        idempotency_key=f"m10:{req.pacing_plan_id}:{req.segment_index}", authorization_type=AuthorizationType.POLICY,
        policy_version="policy", expires_at=NOW+timedelta(seconds=10))


@pytest.mark.asyncio
async def test_adoption_binds_to_original_hub_operation_and_cannot_be_repeated():
    port, req, operation_id = Port(), request(), uuid4()
    adapter = StagedPrepareAdapter(port, ticket(req), clock=lambda: NOW, monotonic_ns=lambda: 0)
    params = dict(operation_id=operation_id, segment_ref=f"{req.pacing_plan_id}:0", binding_revision=1, conversation_revision=2)
    result = await adapter.prepare_send(command(req), **params)
    assert result.operation_id == operation_id and adapter.confirmed
    with pytest.raises(StagedPreparationError):
        await adapter.prepare_send(command(req), **params)
    assert len(port.adopts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["claim", "pause", "wall", "monotonic"])
async def test_adopt_checks_claim_and_dual_clock_and_immediate_control_before_ipc(failure):
    port, req = Port(), request()
    adapter = StagedPrepareAdapter(port, ticket(req),
        clock=lambda: NOW+timedelta(seconds=10 if failure == "wall" else 0),
        monotonic_ns=lambda: 10_000_000_000 if failure == "monotonic" else 0,
        cancel=ControlCancellation(lambda: failure == "pause"), claim_current=lambda: failure != "claim")
    with pytest.raises(StagedPreparationError):
        await adapter.prepare_send(command(req), operation_id=uuid4(), segment_ref=f"{req.pacing_plan_id}:0",
                                   binding_revision=1, conversation_revision=2)
    assert port.adopts == []
