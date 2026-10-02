"""Real authorities + QQ bridge/core; only the native UI and process are synthetic.

Messages enter through the real cursor/RuntimeCoordinator/PlanningCoordinator.
No raw QQ key is substituted for the runtime's namespaced business identity.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.vm_driver.hybrid_bridge import QQHybridDriverBridge
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import HybridQQWorker
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerKind, WorkerStatus
from messenger_ai.domain import SendStatus
from messenger_ai.llm.providers import FakeProvider
from messenger_ai.memory.models import Contact, IdentityBinding
from messenger_ai.policy import CapabilitySnapshot, SupportLevel
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.runtime.assembly import assemble_runtime
from tests.adapters.qq.navigation.test_identity import case as identity_case, BASE, NS
from tests.adapters.qq.navigation.test_profile_verifier import report
from tests.adapters.qq.vm_driver.test_hybrid_worker import rig, Port as CurrentPort
from test_navigation_assembly import RULES


PROFILE_HMAC = hmac.digest(b"synthetic-test-only-key", b"synthetic-test-only-profile-id", "sha256").hex()


@pytest.fixture
def case():
    target, frame, expectation, evidence = identity_case.__wrapped__()
    return (target, frame, expectation.model_copy(update={"expected_profile_hmac": PROFILE_HMAC}),
            evidence.model_copy(update={"profile_id_hmac": PROFILE_HMAC}))


class Clock:
    def __init__(self, port):
        self.port = port

    def now(self):
        return self.port.now


class SupervisedValueCore:
    """The process seam only: the real core still produces every proof/result."""
    def __init__(self, core, port):
        self.core, self.port = core, port
        self.app = None
        self.calls = []
        self.requests = []
        self.after_prepare = None
        self.lost_commit_ack = False

    async def prepare_draft(self, request, *, expected_sequence_digest, cancel_event=None):
        self.calls.append("cold_prepare")
        self.requests.append(request)
        assert self.app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0
        assert self.app.driver._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
        assert self.app.driver._row(request)[0]["status"] == "preparing"
        self.port.advance(20)
        ticket = self.core.prepare_draft(request, expected_sequence_digest=expected_sequence_digest)
        evidence = self.core.prepared_evidence(ticket)
        if self.after_prepare:
            self.after_prepare()
        return SimpleNamespace(ticket=ticket, prepared_evidence=evidence)

    async def adopt_prepared(self, ticket, command, *, cancel_event=None):
        self.calls.append("adopt")
        row = self.app.hub.store.connection.execute("SELECT operation_id,commit_intent FROM send_operations").fetchone()
        assert row["operation_id"] == str(command.operation_id) and row["commit_intent"] == 0
        assert command.operation_id != ticket.reservation_id
        assert self.app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is None
        before = list(self.port.events)
        result = self.core.adopt_prepared(ticket, command)
        assert self.port.events == before  # Adoption must perform zero UI work.
        return result

    async def abort_draft(self, owner, *, deadline_at):
        self.calls.append("abort")
        return self.core.abort_draft(owner, deadline_at=deadline_at)

    async def execute(self, command, *, cancel_event=None):
        self.calls.append(command.kind.value)
        if command.kind is WorkerKind.COMMIT:
            assert not cancel_event.is_set()
            assert self.app.due.authorization._store._keeper.execute("SELECT consumed_at FROM policy_authorizations").fetchone()[0] is not None
            assert self.app.hub.store.connection.execute("SELECT commit_intent FROM send_operations").fetchone()[0] == 1
            assert self.app.driver._db.execute("SELECT commit_intent FROM qq_vm_ops").fetchone()[0] == 1
        result = self.core.execute(command)
        if command.kind is WorkerKind.COMMIT and self.lost_commit_ack:
            assert result.status is WorkerStatus.OK and self.port.sends == 1
            raise ConnectionError("supervisor lost the real core's post-send acknowledgement")
        return result

    def close(self):
        self.core.revoked.set()
        self.core.close()


async def pipeline(tmp_path, rig):
    fixture_core, port, _unused_request = rig
    config = fixture_core.config
    core = HybridQQWorker(config, port, clock=lambda: port.now, monotonic_ns=lambda: port.tick,
                          deadline_at=BASE+timedelta(seconds=120), stop_at=120)
    port.worker = core
    supervised = SupervisedValueCore(core, port)
    fresh = []
    holder = {}

    @asynccontextmanager
    async def fresh_round(command):
        assert command.kind is WorkerKind.VERIFY
        assert port.sends == 1 and port.body == ""
        supervised.close()  # Retire original generation before independent proof.
        epoch = str(uuid4())
        nav = config.navigation.model_copy(update={"expected_worker_epoch": epoch})
        fresh_config = config.model_copy(update={"navigation": nav})
        fresh_port = CurrentPort(fresh_config, port.target,
            port.base_frame.model_copy(update={"worker_epoch": epoch}),
            port.base_witness.model_copy(update={"worker_epoch": epoch}), port.captured)
        fresh_port.now, fresh_port.tick = port.now, port.tick
        fresh_port.body, fresh_port.bubbles = port.body, list(port.bubbles)
        fresh_core = HybridQQWorker(fresh_config, fresh_port, clock=lambda: fresh_port.now,
            monotonic_ns=lambda: fresh_port.tick, deadline_at=port.now+timedelta(seconds=45),
            stop_at=port.tick/NS+45)
        fresh_port.worker = fresh_core
        worker = SupervisedValueCore(fresh_core, fresh_port)
        worker.app = holder["app"]
        fresh.append(worker)
        try:
            yield worker
        finally:
            # A synthetic successor shares the guest clock, but grants no UI
            # input after retirement and never clears the original composer.
            port.now, port.tick = fresh_port.now, fresh_port.tick
            worker.close()

    def scope_guard(request):
        app = holder["app"]
        b, r, p, g, gp = app.state.execution_state(request.conversation_id)
        row = app.pacing.connection.execute("SELECT payload_json,claim_token,status FROM m10_due_outbox WHERE outbox_id=?",
                                            (request.outbox_id,)).fetchone()
        artifact = dict(app.state.plan_artifact(request.pacing_plan_id))
        digest = sha256(json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ((b, r, g) == (request.binding_revision, request.conversation_revision, request.global_revision)
                and not p and not gp and row is not None and row["status"] == "dispatching"
                and row["claim_token"] == request.claim_token
                and json.loads(row["payload_json"])["event_id"] == str(request.due_event_id)
                and digest == request.original_snapshot_digest)

    original_binding = config.bindings[0]
    bridge = QQHybridDriverBridge(worker=supervised, bindings=(original_binding,),
        text_provider=lambda _: pytest.fail("cold UI must use frozen original due body"),
        sqlite_path=tmp_path/"original-qq-journal.sqlite3", scope_guard=scope_guard,
        expected_profile_signatures={original_binding.binding_id: "qq-profile-hmac:"+PROFILE_HMAC},
        verification_round=fresh_round, clock=lambda: port.now, monotonic_ns=lambda: port.tick)
    # Existing empty V1 cursor is preserved. The new inbound is emitted by the
    # actual OBSERVE path, acknowledged by RuntimeCoordinator, then namespaced.
    bridge._cursor.ingest_snapshot(original_binding.hub_conversation_id, [])
    provider = FakeProvider(response={"action":"auto_reply_candidate", "reply_text":"synthetic reply",
        "reply_segments":["synthetic reply"], "confidence":.99, "selection_reason":"fixture response"})
    capability = CapabilitySnapshot(capability_version="synthetic-certified", environment_fingerprint="e"*64,
        send_background=SupportLevel.SUPPORTED, verify_background=SupportLevel.SUPPORTED,
        healthy=True, client_version=config.selector_pack.client_version, binding_revision=7)
    app = assemble_runtime(data_dir=tmp_path/"runtime", planner_provider=provider, driver=bridge,
        capability=capability,
        staged_preparation=bridge, staged_monotonic_ns_clock=lambda: port.tick, clock=Clock(port),
        authorization_signing_key=b"pipeline-test-signing-key-only-not-real", content_policy_checks_enabled=False)
    supervised.app = holder["app"] = app
    app.state.register(account_id=original_binding.account_id, contact_id=original_binding.contact_id,
        conversation_id=original_binding.hub_conversation_id, binding_revision=7, conversation_type="direct")
    app.memory.create_contact(Contact(contact_id=original_binding.contact_id, display_name="fixture", created_at=BASE))
    app.memory.bind_identity(IdentityBinding(contact_id=original_binding.contact_id, platform="qq",
        account_id=original_binding.account_id, conversation_id=original_binding.hub_conversation_id,
        platform_evidence_hash=sha256(original_binding.model_dump_json().encode()).hexdigest(),
        verified_by="existing-synthetic-operator", verified_at=BASE))
    app.state.set_global_pause(paused=True, expected_revision=1, reason="fixture_initial_control")
    app.state.set_global_pause(paused=False, expected_revision=2, reason="fixture_initial_control")
    rule_draft = app.rules.ingest(RuleSource(name="synthetic.yaml", content=RULES))
    app.rules.activate(rule_draft.draft_id, HumanApproval(approver_id="fixture", reason="offline synthetic"))
    await app.coordinator.observe_driver(bridge, original_binding.hub_conversation_id)
    app.coordinator.dispatch_events()
    assert app.state.revisions(original_binding.hub_conversation_id) == (7, 2)
    assert bridge._cursor.connection.execute("SELECT status FROM observation_outbox").fetchone()[0] == "delivered"
    namespaced = app.hub.store.connection.execute("SELECT platform_message_key FROM messages").fetchone()[0]
    assert namespaced == "qq-uia/conversation/1"
    jobs = app.state.claim_planning_jobs(limit=1)
    assert jobs, {"jobs": [dict(x) for x in app.state.connection.execute("SELECT * FROM runtime_planning_jobs")],
                  "events": [dict(x) for x in app.state.connection.execute("SELECT event_type,status FROM runtime_event_outbox")]}
    job = jobs[0]
    assert json.loads(job["source_keys_json"]) == [namespaced]
    assert await app.planning.run_claimed(job) == "scheduled"
    plan_row = app.pacing.connection.execute("SELECT payload_json FROM m10_plans").fetchone()
    plan = json.loads(plan_row[0])
    assert plan["source_message_keys"] == [namespaced]
    port.advance(max(0, (datetime.fromisoformat(plan["earliest_send_at"])-port.now).total_seconds())+.01)
    return app, supervised, fresh, original_binding.model_dump_json(), namespaced


@pytest.mark.asyncio
async def test_real_qq_hybrid_pipeline_namespaces_source_and_settles_all_original_authorities(tmp_path, rig):
    app, worker, fresh, original_binding, key = await pipeline(tmp_path, rig)
    try:
        before_cursor = app.driver._cursor.snapshot_token("conversation")
        assert await app.due.dispatch_one()
        assert worker.calls == ["observe", "cold_prepare", "adopt", "commit"]
        assert worker.requests[0].source_message_keys == (key,)
        assert worker.requests[0].expected_last_message_key == key
        assert json.loads(app.driver._cursor.connection.execute("SELECT payload_json FROM observation_outbox").fetchone()[0])["message_key"] == "m1"
        assert worker.port.writes == worker.port.sends == 1 and worker.port.clears == 0
        assert len(fresh) == 1 and fresh[0].calls == ["verify"]
        assert fresh[0].port.profiles == 1 and fresh[0].port.writes == fresh[0].port.sends == fresh[0].port.clears == 0
        assert fresh[0].core.worker_epoch != worker.core.worker_epoch
        hub = app.hub.store.connection.execute("SELECT operation_id,status,commit_intent FROM send_operations").fetchone()
        qq = app.driver._db.execute("SELECT operation_id,status,commit_intent FROM qq_vm_ops").fetchone()
        assert tuple(qq) == tuple(hub) and hub["status"] == "verified" and hub["commit_intent"] == 1
        owner = app.driver._db.execute("SELECT reservation_id,operation_id,status FROM qq_v2_draft_reservations").fetchone()
        assert owner["operation_id"] == hub["operation_id"] != owner["reservation_id"] and owner["status"] == "verified"
        assert app.driver._db.execute("SELECT operation_id FROM qq_vm_receipts").fetchone()[0] == hub["operation_id"]
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "delivered"
        assert app.pacing.connection.execute("SELECT status FROM m10_plans").fetchone()[0] == "completed"
        assert app.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "verified"
        assert app.driver._by_id["binding"].model_dump_json() == original_binding
        assert app.memory.store.connection.execute("SELECT evidence_hash FROM memory_bindings").fetchone()[0] == sha256(original_binding.encode()).hexdigest()
        assert app.driver._cursor.snapshot_token("conversation") == before_cursor
        auth = app.due.authorization._store._keeper.execute("SELECT issued_at,expires_at,consumed_at FROM policy_authorizations").fetchall()
        assert len(auth) == 1 and auth[0]["consumed_at"] is not None
        assert datetime.fromisoformat(auth[0]["expires_at"])-datetime.fromisoformat(auth[0]["issued_at"]) == timedelta(seconds=10)
        assert datetime.fromisoformat(auth[0]["issued_at"])-worker.requests[0].requested_at >= timedelta(seconds=20)
        # A third independently verified observation must attach the stored
        # receipt to its original operation using the unchanged QQBubble schema.
        prior = fresh[0]
        epoch = str(uuid4())
        config = prior.core.config.model_copy(update={"navigation": prior.core.config.navigation.model_copy(
            update={"expected_worker_epoch": epoch})})
        observed_port = CurrentPort(config, prior.port.target,
            prior.port.base_frame.model_copy(update={"worker_epoch": epoch}),
            prior.port.base_witness.model_copy(update={"worker_epoch": epoch}), prior.port.captured)
        observed_port.now, observed_port.tick = prior.port.now, prior.port.tick
        observed_port.bubbles = list(prior.port.bubbles)
        core = HybridQQWorker(config, observed_port, clock=lambda: observed_port.now,
            monotonic_ns=lambda: observed_port.tick, deadline_at=observed_port.now+timedelta(seconds=45),
            stop_at=observed_port.tick/NS+45)
        observed_port.worker = core
        observer = SupervisedValueCore(core, observed_port)
        observer.app = app
        app.driver._worker = observer
        br, cr = app.state.revisions("conversation")
        batch = await app.driver.observe_conversation("conversation", binding_revision=br, conversation_revision=cr)
        assert batch.complete and len(batch.messages) == 1
        assert str(batch.messages[0].operation_id) == hub["operation_id"]
        assert app.driver._db.execute("SELECT local_key FROM qq_vm_receipts").fetchone()[0] == batch.messages[0].local_message_key
        assert observed_port.profiles == 1 and observed_port.sends == observed_port.writes == 0
    finally:
        await app.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pause", "claim_lost"])
async def test_real_pipeline_cold_result_stale_cleans_only_owned_draft_without_authority_or_replay(tmp_path, rig, failure):
    app, worker, fresh, _original_binding, _key = await pipeline(tmp_path, rig)
    newer = []
    def change():
        if failure == "pause":
            app.begin_global_pause_from_control()
            assert not app.state.global_control()[1]
        else:
            app.pacing.recover_due_outbox()
            newer.extend(app.pacing.claim_due_outbox_with_tokens())
    worker.after_prepare = change
    try:
        if failure == "claim_lost":
            with pytest.raises(RuntimeError, match="lost exact claim"):
                await app.due.dispatch_one()
            row = app.pacing.connection.execute("SELECT status,claim_token FROM m10_due_outbox").fetchone()
            assert row["status"] == "dispatching" and row["claim_token"] == newer[0].claim_token
        else:
            assert await app.due.dispatch_one()
            assert not await app.due.dispatch_one()
        assert worker.calls == ["observe", "cold_prepare", "abort"] and fresh == []
        assert worker.port.writes == worker.port.clears == 1 and worker.port.sends == 0 and worker.port.body == ""
        assert app.driver._db.execute("SELECT status FROM qq_v2_draft_reservations").fetchone()[0] == "cleaned"
        assert app.driver._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
        assert app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
        assert not await app.due.dispatch_one()
        assert worker.port.writes == worker.port.clears == 1 and worker.port.sends == 0
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_real_pipeline_lost_postcommit_ack_preserves_intent_and_never_replays_or_clears(tmp_path, rig):
    app, worker, fresh, _original_binding, _key = await pipeline(tmp_path, rig)
    worker.lost_commit_ack = True
    try:
        assert await app.due.dispatch_one()
        assert worker.port.sends == 1 and worker.port.clears == 0
        assert worker.calls == ["observe", "cold_prepare", "adopt", "commit"] and fresh == []
        assert app.driver.has_cleanup_obligation("account")
        hub = app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()
        qq = app.driver._db.execute("SELECT status,commit_intent FROM qq_vm_ops").fetchone()
        assert tuple(qq) == tuple(hub) == (SendStatus.UNCERTAIN.value, 1)
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "operation_recovery_hold"
        assert not await app.due.dispatch_one()
        assert worker.port.sends == 1 and worker.port.clears == 0
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_real_pipeline_pause_before_reservation_insert_holds_due_without_creating_unknown_owner(tmp_path, rig):
    app, worker, fresh, _original_binding, _key = await pipeline(tmp_path, rig)
    bridge = app.driver
    scope_guard, abort_owned, retain_hold = bridge._scope_guard, bridge.abort_owned_ticket, bridge.retain_cleanup_hold
    owners, aborted, holds = [], [], []

    def pause_before_insert(request):
        owners.append(request)
        assert scope_guard(request)
        assert bridge._db.execute("SELECT COUNT(*) FROM qq_v2_draft_reservations").fetchone()[0] == 0
        app.begin_global_pause_from_control()
        return False

    async def record_abort(owner, *, deadline_at):
        result = await abort_owned(owner, deadline_at=deadline_at)
        aborted.append((owner, result.status))
        return result

    def record_hold(owner, *, reason):
        holds.append(owner)
        retain_hold(owner, reason=reason)  # Real bridge refuses an unknown reservation.

    bridge._scope_guard, bridge.abort_owned_ticket, bridge.retain_cleanup_hold = pause_before_insert, record_abort, record_hold
    try:
        original_plan = app.pacing.connection.execute("SELECT payload_json FROM m10_plans").fetchone()[0]
        assert await app.due.dispatch_one()
        row = app.pacing.connection.execute("SELECT status,defer_reason,operation_id FROM m10_due_outbox").fetchone()
        assert tuple(row) == ("navigation_attention", "staged_cleanup_required", None)
        assert aborted == [(owners[0], "not_owned")] and holds == owners
        assert bridge._db.execute("SELECT COUNT(*) FROM qq_v2_draft_reservations").fetchone()[0] == 0
        assert bridge._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
        assert app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0
        assert app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
        assert app.pacing.connection.execute("SELECT payload_json FROM m10_plans").fetchone()[0] == original_plan
        assert worker.calls == ["observe"] and fresh == []
        assert worker.port.writes == worker.port.sends == worker.port.clears == 0
        worker.port.advance(11)
        assert not await app.due.dispatch_one()
        assert worker.calls == ["observe"]
    finally:
        await app.aclose()
