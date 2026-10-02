from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json
import asyncio

import pytest

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.adapters.qq.vm_driver.hybrid_bridge import QQHybridDriverBridge
from messenger_ai.adapters.qq.vm_driver.contracts import (
    PreparedTargetIdentity, PreparedVerificationEvidence, WorkerResult, WorkerStatus,
)
from messenger_ai.domain import AuthorizedSendCommand, AuthorizationType, SendStatus
from messenger_ai.runtime.staged_preparation import (
    DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket,
    StagedPreparationError, source_keys_digest,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)
TICK = 1_000_000_000
PROFILE = "qq-profile-hmac:" + "a" * 64
BINDING = QQIdentityBinding(hub_conversation_id="old-conversation", account_id="test-account",
    contact_id="test-contact", platform_conversation_id="old-ui-id", binding_id="old-binding",
    participant_signature="qq-session-observed:unchanged", conversation_type="direct")


def _request():
    body = "test response"
    return DraftPreparationRequest(reservation_id=uuid4(), nonce=uuid4(), outbox_id=7,
        claim_token="original-claim", due_event_id=uuid4(), account_id=BINDING.account_id,
        contact_id=BINDING.contact_id, conversation_id=BINDING.hub_conversation_id,
        binding_id=BINDING.binding_id, binding_revision=2, conversation_revision=3, global_revision=4,
        pacing_plan_id=uuid4(), segment_index=0, draft_id=uuid4(), body=body,
        body_hash=hashlib.sha256(body.encode()).hexdigest(), source_message_keys=("qq-uia/old-conversation/1",),
        source_keys_digest=source_keys_digest(("qq-uia/old-conversation/1",)), expected_last_message_key="qq-uia/old-conversation/1",
        original_snapshot_digest="b" * 64, requested_at=NOW, deadline_at=NOW+timedelta(seconds=40),
        requested_monotonic_ns=TICK, deadline_monotonic_ns=TICK+40_000_000_000)


def _command(request):
    return AuthorizedSendCommand(draft_id=request.draft_id, conversation_id=request.conversation_id,
        expected_last_message_key=request.expected_last_message_key, text_hash=request.body_hash,
        idempotency_key=f"m10:{request.pacing_plan_id}:0", authorization_type=AuthorizationType.POLICY,
        authorization_id=uuid4(), policy_version="original", expires_at=NOW+timedelta(seconds=10))


class FakeWorker:
    def __init__(self):
        self.epoch = uuid4()
        self.calls = []
        self.fail_adopt = self.fail_commit = self.fail_cleanup = False
        self.cancel_commit = False
        self.receipt_transform = lambda x: x
        self.prepare_check = lambda: None

    async def prepare_draft(self, request, *, expected_sequence_digest, cancel_event=None):
        self.calls.append("prepare")
        self.prepare_check()
        proof = PreparedVerificationEvidence(owner_binding_id=request.binding_id,
            target_identity=PreparedTargetIdentity(binding_id=request.binding_id,
                participant_signature=PROFILE, conversation_type="direct", process_id=123, window_handle=456),
            before_bubbles=(), text_hash=request.body_hash, segment_ref=f"{request.pacing_plan_id}:0")
        ticket = PreparedDraftTicket(**request.model_dump(exclude={"body"}), run_id="test-run",
            session_epoch="old-session", surface_epoch="test-surface", worker_epoch=str(self.epoch),
            process_id=123, window_handle=456, process_started_at_100ns=789,
            issued_at=NOW, expires_at=NOW+timedelta(seconds=10), issued_monotonic_ns=TICK,
            expires_monotonic_ns=TICK+10_000_000_000,
            evidence_digest=hashlib.sha256(proof.model_dump_json().encode()).hexdigest(),
            expected_sequence_digest=expected_sequence_digest)
        self.prepared = SimpleNamespace(ticket=ticket, prepared_evidence=proof)
        return self.prepared

    def result(self, command, *, evidence=None):
        return WorkerResult(request_id=command.request_id, kind=command.kind,
            binding_id=command.binding_id, operation_id=command.operation_id,
            binding_revision=command.binding_revision, conversation_revision=command.conversation_revision,
            status=WorkerStatus.OK, worker_epoch=self.epoch, evidence=evidence or {})

    async def adopt_prepared(self, ticket, command, *, cancel_event=None):
        self.calls.append("adopt")
        if self.fail_adopt:
            raise ConnectionError("lost acknowledgement")
        return self.result(command, evidence={"prepared_evidence": self.prepared.prepared_evidence.model_dump()})

    async def abort_draft(self, owner, *, deadline_at):
        self.calls.append("abort")
        return DraftCleanupResult(status="cleanup_required" if self.fail_cleanup else "cleaned",
            reservation_id=owner.reservation_id, nonce=owner.nonce)

    async def execute(self, command, *, cancel_event=None):
        self.calls.append(command.kind.value)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        if self.fail_commit:
            raise ConnectionError("commit outcome unknown")
        if self.cancel_commit:
            raise asyncio.CancelledError()
        receipt = self.receipt_transform({"direction": "outbound", "text": "test response",
            "message_key": "qq-2", "conversation_internal_id": "old-ui-id",
            "participant_signature": BINDING.participant_signature})
        return self.result(command, evidence={"receipt": receipt, "operation_id": str(command.operation_id)})

    def close(self):
        pass


def _bridge(tmp_path, worker, *, guard=lambda _: True, verification_round=None, guard_refresh=None):
    bridge = QQHybridDriverBridge(worker=worker, bindings=(BINDING,), text_provider=lambda _: "test response",
        sqlite_path=tmp_path / "original.sqlite3", scope_guard=guard,
        expected_profile_signatures={BINDING.binding_id: PROFILE}, clock=lambda: NOW,
        monotonic_ns=lambda: TICK, verification_round=verification_round, guard_refresh=guard_refresh)
    if not bridge._cursor.has_snapshot(BINDING.hub_conversation_id):
        bridge._cursor.ingest_snapshot(BINDING.hub_conversation_id, [])
        _new_inbound(bridge, "qq-1", "test incoming")
        bridge._cursor.claim(BINDING.hub_conversation_id)
        bridge.acknowledge_observation(BINDING.hub_conversation_id, ("1",))
    return bridge


def _new_inbound(bridge, key, text):
    old = [json.loads(x["payload_json"]) for x in bridge._cursor.connection.execute(
        "SELECT payload_json FROM observation_outbox ORDER BY outbox_id")]
    bridge._cursor.ingest_snapshot(BINDING.hub_conversation_id, old + [{"direction": "inbound", "text": text,
        "message_key": key, "conversation_internal_id": BINDING.platform_conversation_id,
        "observed_at": NOW.isoformat()}])


async def _adopt(bridge, ticket, request, operation_id=None):
    return await bridge.adopt_prepared(ticket, _command(request), operation_id=operation_id or uuid4(),
        segment_ref=f"{request.pacing_plan_id}:0", binding_revision=2, conversation_revision=3)


@pytest.mark.asyncio
async def test_late_prepare_cannot_clear_timeout_hold(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    worker.prepare_check = lambda: bridge.retain_cleanup_hold(request, reason="test_timeout")
    with pytest.raises(StagedPreparationError, match="hybrid_reservation_held"):
        await bridge.prepare_draft(request)
    row, _ = bridge._row(request)
    assert row["status"] == "cleanup_required" and row["ticket_json"]
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
    assert (await bridge.abort_owned_ticket(request, deadline_at=NOW+timedelta(seconds=20))).status == "cleaned"
    bridge.close()


@pytest.mark.asyncio
async def test_late_adoption_rolls_back_operation_after_timeout_hold(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    original = worker.adopt_prepared
    async def late(*args, **kwargs):
        result = await original(*args, **kwargs)
        bridge.retain_cleanup_hold(ticket, reason="test_timeout")
        return result
    worker.adopt_prepared = late
    with pytest.raises(StagedPreparationError, match="hybrid_reservation_held"):
        await _adopt(bridge, ticket, request)
    assert bridge._row(ticket)[0]["status"] == "cleanup_required"
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_prepared_evidence").fetchone()[0] == 0
    assert (await bridge.abort_owned_ticket(ticket, deadline_at=NOW+timedelta(seconds=20))).status == "cleaned"
    bridge.close()


@pytest.mark.asyncio
async def test_commit_flushes_actual_own_intent_before_ipc_and_keeps_authority_deadline(tmp_path):
    worker, request = FakeWorker(), _request()
    published, bridge = [], None
    async def refresh(epoch):
        assert str(epoch) == str(worker.epoch)
        assert bridge._db.execute("SELECT commit_intent FROM qq_vm_ops").fetchone()[0] == 1
        published.append(True)
    bridge = _bridge(tmp_path, worker, guard_refresh=refresh)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    original = worker.execute
    async def commit(command, **kwargs):
        assert published == [True]
        assert command.deadline == _command(request).expires_at
        return await original(command, **kwargs)
    worker.execute = commit
    result = await bridge.commit_send(operation)
    assert result.status is SendStatus.COMMITTED
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["publication", "pause"])
async def test_failed_commit_handoff_never_submits_ipc_or_retries(tmp_path, failure):
    worker, request = FakeWorker(), _request()
    current = [True]
    async def refresh(_):
        if failure == "publication":
            raise RuntimeError("publication_failed")
        current[0] = False
    bridge = _bridge(tmp_path, worker, guard=lambda _: current[0], guard_refresh=refresh)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    result = await bridge.commit_send(operation)
    assert result.status is SendStatus.UNCERTAIN
    assert worker.calls == ["prepare", "adopt"]
    assert (await bridge.commit_send(result)).status is SendStatus.UNCERTAIN
    assert (await bridge.abort_owned_ticket(ticket, deadline_at=NOW+timedelta(seconds=20))).status == "cleanup_required"
    assert worker.calls == ["prepare", "adopt"]
    bridge.close()


@pytest.mark.asyncio
async def test_cold_ownership_is_durable_before_write_without_hub_operation(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    worker.prepare_check = lambda: (bridge._row(request)[0]["status"] == "preparing") or pytest.fail("missing ownership")
    before = bridge._cursor.snapshot_token(request.conversation_id)
    ticket = await bridge.prepare_draft(request)
    assert ticket.expected_sequence_digest == before
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
    assert bridge._cursor.snapshot_token(request.conversation_id) == before
    assert bridge._by_id[request.binding_id].participant_signature == BINDING.participant_signature
    with pytest.raises(StagedPreparationError, match="hybrid_staged_ticket_required"):
        await bridge.prepare_send(_command(request))
    assert worker.calls == ["prepare"]
    bridge.close()


@pytest.mark.asyncio
async def test_adoption_is_once_and_creates_only_hub_allocated_operation(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    operation_id = uuid4()
    operation = await _adopt(bridge, ticket, request, operation_id)
    assert operation.operation_id == operation_id and operation.status is SendStatus.PREPARED
    with pytest.raises(StagedPreparationError):
        await _adopt(bridge, ticket, request, operation_id)
    assert worker.calls == ["prepare", "adopt"]
    assert bridge._db.execute("SELECT operation_id FROM qq_vm_ops").fetchone()[0] == str(operation_id)
    bridge.close()


@pytest.mark.asyncio
async def test_lost_adoption_acknowledgement_holds_restart_without_replay(tmp_path):
    worker, request = FakeWorker(), _request()
    worker.fail_adopt = True
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    with pytest.raises(ConnectionError):
        await _adopt(bridge, ticket, request)
    bridge.close()
    successor = FakeWorker()
    restored = _bridge(tmp_path, successor)
    assert restored.has_cleanup_obligation(request.account_id)
    with pytest.raises(StagedPreparationError, match="hybrid_cleanup_obligation"):
        await restored.prepare_draft(_request())
    assert successor.calls == []
    assert restored._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
    restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["pause", "new_inbound"])
async def test_drift_between_prepare_and_adopt_only_allows_exact_owned_abort(tmp_path, drift):
    current = [True]
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker, guard=lambda _: current[0])
    ticket = await bridge.prepare_draft(request)
    if drift == "pause":
        current[0] = False
    else:
        _new_inbound(bridge, "qq-new", "new incoming")
    with pytest.raises(StagedPreparationError):
        await _adopt(bridge, ticket, request)
    result = await bridge.abort_owned_ticket(ticket, deadline_at=NOW+timedelta(seconds=20))
    assert result.status == "cleaned" and worker.calls == ["prepare", "abort"]
    assert not bridge.has_cleanup_obligation(request.account_id)
    bridge.close()


@pytest.mark.asyncio
async def test_failed_cleanup_blocks_navigation_and_next_draft(tmp_path):
    worker, request = FakeWorker(), _request()
    worker.fail_cleanup = True
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    with pytest.raises(StagedPreparationError, match="cleanup_unproven"):
        await bridge.abort_owned_ticket(ticket, deadline_at=NOW+timedelta(seconds=20))
    assert bridge.has_cleanup_obligation(request.account_id)
    with pytest.raises(StagedPreparationError):
        await bridge.prepare_draft(_request())
    assert worker.calls == ["prepare", "abort"]
    bridge.close()


@pytest.mark.asyncio
async def test_uncertain_commit_is_never_repeated_or_cleared(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    worker.fail_commit = True
    assert (await bridge.commit_send(operation)).status is SendStatus.UNCERTAIN
    assert (await bridge.commit_send(operation)).status is SendStatus.UNCERTAIN
    result = await bridge.abort_owned_ticket(ticket, deadline_at=NOW+timedelta(seconds=20))
    assert result.status == "cleanup_required"
    assert worker.calls == ["prepare", "adopt", "commit"]
    bridge.close()


@pytest.mark.asyncio
async def test_commit_cancellation_remains_cancelled_after_durable_uncertainty(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    worker.cancel_commit = True
    with pytest.raises(asyncio.CancelledError):
        await bridge.commit_send(operation)
    row = bridge._db.execute("SELECT status,commit_intent FROM qq_vm_ops").fetchone()
    assert row["status"] == SendStatus.UNCERTAIN.value and row["commit_intent"] == 1
    assert bridge.has_cleanup_obligation(request.account_id)
    assert (await bridge.commit_send(operation)).status is SendStatus.UNCERTAIN
    assert worker.calls == ["prepare", "adopt", "commit"]
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [{"direction": "inbound"}, {"text": "other response"},
    {"conversation_internal_id": "another-contact"}, {"message_key": ""},
    {"participant_signature": "other-contact"}, {"participant_signature": None}, None])
async def test_wrong_receipt_cannot_settle_the_original_operation(tmp_path, update):
    worker, request = FakeWorker(), _request()
    successor = FakeWorker()
    successor.receipt_transform = (lambda receipt: {key:value for key,value in receipt.items()
        if key != "participant_signature"}) if update is None else lambda receipt: {**receipt, **update}
    @asynccontextmanager
    async def verification_round(command):
        yield successor
    bridge = _bridge(tmp_path, worker, verification_round=verification_round)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    await bridge.commit_send(operation)
    assert (await bridge.verify_send(operation)).status is SendStatus.UNCERTAIN
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_receipts").fetchone()[0] == 0
    assert bridge.has_cleanup_obligation(request.account_id)
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [True, False])
async def test_verification_requires_new_epoch_and_persists_original_receipt(tmp_path, fresh):
    worker, request = FakeWorker(), _request()
    successor = FakeWorker() if fresh else worker
    retired = []
    @asynccontextmanager
    async def verification_round(command):
        retired.append(worker)
        yield successor
    bridge = _bridge(tmp_path, worker, verification_round=verification_round)
    ticket = await bridge.prepare_draft(request)
    operation = await _adopt(bridge, ticket, request)
    assert (await bridge.commit_send(operation)).status is SendStatus.COMMITTED
    verified = await bridge.verify_send(operation)
    assert verified.status is (SendStatus.VERIFIED if fresh else SendStatus.UNCERTAIN)
    assert len(retired) == 1
    assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_receipts").fetchone()[0] == int(fresh)
    assert bridge.has_cleanup_obligation(request.account_id) is (not fresh)
    bridge.close()


@pytest.mark.asyncio
async def test_retained_cancel_fence_blocks_commit_and_hold_blocks_observation(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    ticket = await bridge.prepare_draft(request)
    cancelled = asyncio.Event()
    operation = await bridge.adopt_prepared(ticket, _command(request), operation_id=uuid4(),
        segment_ref=f"{request.pacing_plan_id}:0", binding_revision=2, conversation_revision=3,
        cancel_event=cancelled)
    cancelled.set()
    with pytest.raises(StagedPreparationError, match="hybrid_control_cancelled"):
        await bridge.commit_send(operation)
    batch = await bridge.observe_conversation(request.conversation_id, binding_revision=2, conversation_revision=3)
    assert not batch.complete and worker.calls == ["prepare", "adopt"]
    bridge.close()


@pytest.mark.asyncio
async def test_v2_construction_preserves_historical_failed_operations_and_cursor(tmp_path):
    worker, request = FakeWorker(), _request()
    bridge = _bridge(tmp_path, worker)
    old_operation = uuid4()
    bridge._db.execute("INSERT INTO qq_vm_ops VALUES(?,?,?,?,?,?,?,?,?,?,0,?)", (str(old_operation),
        "old-key", str(uuid4()), request.conversation_id, request.binding_id, "old-segment", 2, 3,
        "c" * 64, SendStatus.FAILED.value, "composer_focus_drift"))
    before = dict(bridge._db.execute("SELECT * FROM qq_vm_ops").fetchone())
    cursor_before = [dict(x) for x in bridge._cursor.connection.execute("SELECT * FROM cursor_state")]
    bridge.close()
    restored = _bridge(tmp_path, FakeWorker())
    assert dict(restored._db.execute("SELECT * FROM qq_vm_ops").fetchone()) == before
    assert [dict(x) for x in restored._cursor.connection.execute("SELECT * FROM cursor_state")] == cursor_before
    assert not restored.has_cleanup_obligation(request.account_id)
    restored.close()
