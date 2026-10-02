"""Actual runtime SQLite authority projected into the V2 guard; no UI access."""
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.vm_driver.hybrid_session import HybridSessionStatus
from messenger_ai.pacing.scheduler import PacingScheduler
from messenger_ai.runtime.qq_hybrid_config import parse_hybrid_settings
from messenger_ai.runtime.qq_hybrid_scope import QQHybridRuntimeScope
from messenger_ai.runtime.staged_preparation import DraftPreparationRequest, source_keys_digest
from messenger_ai.runtime.state import RuntimeState
from tests.runtime.test_qq_hybrid_config import configuration


@pytest.fixture
def scope_case(tmp_path):
    raw, pack, bindings = configuration()
    settings = parse_hybrid_settings(raw, selector_pack=pack, bindings=bindings,
        binding_revisions={b.binding_id: 1 for b in bindings}, run_id="scope-test-run")
    state = RuntimeState(tmp_path / "runtime.sqlite3", initially_paused=True)
    pacing = PacingScheduler(tmp_path / "pacing.sqlite3")
    for binding in bindings:
        state.register(account_id=binding.account_id, contact_id=binding.contact_id,
            conversation_id=binding.hub_conversation_id, binding_revision=1, conversation_type="direct")
    db = sqlite3.connect(tmp_path / "bridge.sqlite3", isolation_level=None)
    db.row_factory = sqlite3.Row
    # Only the durable journal facts read by this projection. Business state
    # and pacing use their production schemas/APIs above.
    db.executescript("""
        CREATE TABLE qq_v2_draft_reservations(reservation_id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,request_json TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE qq_vm_ops(operation_id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,
          status TEXT NOT NULL,commit_intent INTEGER NOT NULL);
    """)
    worker = SimpleNamespace(status=HybridSessionStatus(state="idle"))
    worker.status_snapshot = lambda: worker.status
    bridge = SimpleNamespace(_db=db, _by_id={b.binding_id: b for b in bindings}, _worker=worker)
    app = SimpleNamespace(state=state, pacing=pacing, _pause_requested=asyncio.Event())
    lookup = {"app": app, "bridge": bridge}
    scope = QQHybridRuntimeScope(app_lookup=lambda: lookup["app"], bridge_lookup=lambda: lookup["bridge"],
        settings=settings, run_id=settings.run_id)
    case = SimpleNamespace(app=app, state=state, pacing=pacing, db=db, worker=worker, bridge=bridge,
        scope=scope, settings=settings, target=settings.targets[bindings[0].binding_id],
        bindings=bindings, epoch=uuid4(), lookup=lookup, root=tmp_path)
    yield case
    db.close()
    pacing.close()
    state.close()


def snapshot(case, **updates):
    args = dict(target=case.target, purpose="draft", worker_epoch=case.epoch,
        deadline_at=datetime.now(UTC)+timedelta(seconds=40), desktop_lease_id="original-desktop",
        observation_epoch="original-observation")
    args.update(updates)
    return case.scope.snapshot(**args)


def resume(case):
    revision, _, _ = case.state.global_control()
    assert case.state.set_global_pause(paused=False, expected_revision=revision)


def request(case):
    target, binding = case.target, case.bindings[0]
    br, cr, _, gr, _ = case.state.execution_state(target.conversation_id)
    now = datetime.now(UTC)
    keys = (f"qq-uia/{target.conversation_id}/1",)
    return DraftPreparationRequest(reservation_id=uuid4(), nonce=uuid4(), outbox_id=1,
        claim_token="claim-original", due_event_id=uuid4(), account_id=target.account_id,
        contact_id=binding.contact_id, conversation_id=target.conversation_id, binding_id=target.binding_id,
        binding_revision=br, conversation_revision=cr, global_revision=gr, pacing_plan_id=uuid4(),
        segment_index=0, draft_id=uuid4(), body="synthetic draft", body_hash=hashlib.sha256(b"synthetic draft").hexdigest(),
        source_message_keys=keys, source_keys_digest=source_keys_digest(keys), expected_last_message_key=keys[-1],
        original_snapshot_digest="a"*64, requested_at=now, deadline_at=now+timedelta(seconds=40),
        requested_monotonic_ns=1, deadline_monotonic_ns=40_000_000_001)


def reserve(case, req, status="preparing"):
    case.db.execute("INSERT INTO qq_v2_draft_reservations VALUES(?,?,?,?)",
        (str(req.reservation_id), req.account_id, req.model_dump_json(), status))


def claim(case, req, status="dispatching"):
    case.pacing.connection.execute("""INSERT INTO m10_due_outbox
        (outbox_id,pacing_plan_id,segment_index,payload_json,status,created_at,claim_token)
        VALUES(?,?,?,?,?,?,?)""", (req.outbox_id,str(req.pacing_plan_id),req.segment_index,"{}",status,
        req.requested_at.isoformat(),req.claim_token))


def test_real_paused_state_and_fixed_scope_are_projected_without_mutating_any_record(scope_case):
    c = scope_case
    before = [tuple(db.iterdump()) for db in (c.state.connection, c.pacing.connection, c.db)]
    deadline = datetime.now(UTC)+timedelta(seconds=40)
    guard = snapshot(c, purpose="health", deadline_at=deadline)
    assert guard.paused and guard.control_revision == 1
    assert not guard.has_owned_draft and not guard.has_commit_obligation
    assert guard.session_epoch == "2" and guard.target.binding_revision == 1
    assert guard.run_id == c.settings.run_id and guard.worker_epoch == str(c.epoch)
    assert guard.process_id == c.settings.window.process_id
    assert guard.window_handle == c.settings.window.window_handle
    assert guard.process_started_at_100ns == c.settings.process_started_at_100ns
    assert guard.lease_expires_at == deadline
    assert before == [tuple(db.iterdump()) for db in (c.state.connection, c.pacing.connection, c.db)]


def test_immediate_pause_intent_precedes_durable_global_revision(scope_case):
    c = scope_case
    resume(c)
    revision = c.state.global_control()[0]
    assert not snapshot(c).paused
    c.app._pause_requested.set()
    guard = snapshot(c)
    assert guard.paused and guard.control_revision == revision
    assert c.state.global_control()[1] is False
    assert c.state.set_global_pause(paused=True, expected_revision=revision)
    c.app._pause_requested.clear()
    guard = snapshot(c)
    assert guard.paused and guard.control_revision == revision+1


def test_contact_pause_also_blocks_guard_without_global_pause(scope_case):
    resume(scope_case)
    scope_case.state.pause(scope_case.target.conversation_id)
    assert not scope_case.state.global_control()[1]
    assert snapshot(scope_case).paused


@pytest.mark.parametrize("reason", ["history_gap", "direction_unknown", "driver_temporary:ui_automation_unavailable",
    "driver_quarantine:read_only_observe_timeout", "driver_quarantine:binding_changed_requires_release"])
def test_temporary_read_pause_permits_fresh_observation_but_never_draft_or_verify(scope_case, reason):
    c = scope_case
    resume(c)
    c.state.pause(c.target.conversation_id, reason=reason)
    req = request(c)
    claim(c, req)
    before = tuple(c.state.connection.iterdump())
    for purpose in ("navigation", "observe"):
        assert not snapshot(c, purpose=purpose).paused
    for purpose in ("draft", "verify", "health", "unknown"):
        assert snapshot(c, purpose=purpose).paused
    assert not c.scope.request_is_current(req)
    assert tuple(c.state.connection.iterdump()) == before
    c.app._pause_requested.set()
    assert snapshot(c, purpose="observe").paused
    c.app._pause_requested.clear()
    revision = c.state.global_control()[0]
    assert c.state.set_global_pause(paused=True, expected_revision=revision)
    assert snapshot(c, purpose="navigation").paused


@pytest.mark.parametrize("reason", ["manual_pause", "binding_changed", "identity_mismatch", "unknown_pause"])
def test_permanent_contact_pause_blocks_every_observation_entry(scope_case, reason):
    c = scope_case
    resume(c)
    c.state.pause(c.target.conversation_id, reason=reason)
    assert snapshot(c, purpose="navigation").paused
    assert snapshot(c, purpose="observe").paused


def test_current_cold_reservation_is_excluded_only_for_its_original_actor(scope_case):
    c = scope_case
    resume(c)
    req = request(c)
    reserve(c, req)
    c.worker.status = HybridSessionStatus(state="starting", purpose="draft", worker_epoch=c.epoch)
    assert not snapshot(c).has_owned_draft
    assert snapshot(c, worker_epoch=uuid4()).has_owned_draft
    for purpose in ("observe", "navigation", "verify", "health"):
        assert snapshot(c, purpose=purpose).has_owned_draft
    c.worker.status = HybridSessionStatus(state="active", purpose="observe", worker_epoch=c.epoch)
    assert snapshot(c).has_owned_draft


@pytest.mark.parametrize("actor_state", ["closing", "cleanup_required", "closed"])
def test_stopped_or_cleanup_actor_cannot_hide_its_preparing_obligation(scope_case, actor_state):
    c = scope_case
    reserve(c, request(c))
    c.worker.status = HybridSessionStatus(state=actor_state, purpose="draft", worker_epoch=c.epoch,
        cleanup_required=actor_state == "cleanup_required")
    assert snapshot(c).has_owned_draft


@pytest.mark.parametrize("status", ["prepared", "adopt_intent", "adopted", "cleanup_required", "committed"])
def test_late_owned_or_held_status_never_reuses_starting_exception(scope_case, status):
    c = scope_case
    req = request(c)
    reserve(c, req)
    c.worker.status = HybridSessionStatus(state="active", purpose="draft", worker_epoch=c.epoch)
    assert not snapshot(c).has_owned_draft
    c.db.execute("UPDATE qq_v2_draft_reservations SET status=?", (status,))
    assert snapshot(c).has_owned_draft


def test_late_foreign_reservation_prevents_original_starting_exception(scope_case):
    c = scope_case
    reserve(c, request(c))
    c.worker.status = HybridSessionStatus(state="starting", purpose="draft", worker_epoch=c.epoch)
    assert not snapshot(c).has_owned_draft
    other = c.bindings[1]
    foreign = request(c).model_copy(update={"binding_id":other.binding_id,
        "conversation_id":other.hub_conversation_id,"contact_id":other.contact_id})
    reserve(c, foreign, "cleanup_required")
    assert snapshot(c).has_owned_draft
    assert c.db.execute("SELECT COUNT(*) FROM qq_v2_draft_reservations").fetchone()[0] == 2


@pytest.mark.parametrize("field,value", [("binding_id","binding-2"),("conversation_id","conversation-2"),
    ("global_revision",999)])
def test_mismatched_starting_request_is_an_obligation(scope_case, field, value):
    c = scope_case
    reserve(c, request(c).model_copy(update={field:value}))
    c.worker.status = HybridSessionStatus(state="starting", purpose="draft", worker_epoch=c.epoch)
    assert snapshot(c).has_owned_draft


def test_historical_failed_rows_are_preserved_and_commit_obligations_remain_visible(scope_case):
    c = scope_case
    for _ in range(3):
        c.db.execute("INSERT INTO qq_vm_ops VALUES(?,?,?,?)", (str(uuid4()),c.target.binding_id,"failed",0))
    before = tuple(c.db.iterdump())
    assert not snapshot(c).has_owned_draft and not snapshot(c).has_commit_obligation
    assert tuple(c.db.iterdump()) == before
    operation = str(uuid4())
    c.db.execute("INSERT INTO qq_vm_ops VALUES(?,?,?,?)", (operation,c.target.binding_id,"send_uncertain",1))
    assert snapshot(c).has_commit_obligation
    c.db.execute("UPDATE qq_vm_ops SET status='verified' WHERE operation_id=?", (operation,))
    assert not snapshot(c).has_commit_obligation


@pytest.mark.parametrize("claim_status", ["dispatching", "dispatching_nonrecoverable"])
def test_actual_current_claim_is_accepted_without_changing_it(scope_case, claim_status):
    c = scope_case
    resume(c)
    req = request(c)
    claim(c, req, claim_status)
    before = tuple(c.pacing.connection.iterdump())
    assert c.scope.request_is_current(req)
    assert tuple(c.pacing.connection.iterdump()) == before


@pytest.mark.parametrize("field,value", [("claim_token","successor-claim"),("outbox_id",99),
    ("pacing_plan_id",uuid4()),("segment_index",1),("binding_revision",99),
    ("conversation_revision",99),("global_revision",99),("binding_id","unknown-binding")])
def test_stale_claim_or_any_revision_is_rejected(scope_case, field, value):
    c = scope_case
    resume(c)
    req = request(c)
    claim(c, req)
    assert not c.scope.request_is_current(req.model_copy(update={field:value}))


def test_late_claim_replacement_and_runtime_revision_are_read_again_from_sqlite(scope_case):
    c = scope_case
    resume(c)
    req = request(c)
    claim(c, req)
    assert c.scope.request_is_current(req)
    c.pacing.connection.execute("UPDATE m10_due_outbox SET claim_token='new-owner' WHERE outbox_id=?", (req.outbox_id,))
    assert not c.scope.request_is_current(req)
    successor = req.model_copy(update={"claim_token":"new-owner"})
    assert c.scope.request_is_current(successor)
    c.state.connection.execute("UPDATE runtime_conversations SET conversation_revision=conversation_revision+1 "
        "WHERE conversation_id=?", (req.conversation_id,))
    assert not c.scope.request_is_current(successor)


@pytest.mark.parametrize("claim_status", ["pending", "delivered", "navigation_attention", "operation_recovery_hold"])
def test_nonexecuting_claim_states_never_authorize_preparation(scope_case, claim_status):
    c = scope_case
    resume(c)
    req = request(c)
    claim(c, req, claim_status)
    assert not c.scope.request_is_current(req)


@pytest.mark.parametrize("pause", ["intent", "global", "contact"])
def test_pause_during_current_claim_immediately_revokes_request(scope_case, pause):
    c = scope_case
    resume(c)
    req = request(c)
    claim(c, req)
    assert c.scope.request_is_current(req)
    if pause == "intent":
        c.app._pause_requested.set()
    elif pause == "global":
        assert c.state.set_global_pause(paused=True, expected_revision=req.global_revision)
    else:
        c.state.pause(req.conversation_id)
    assert not c.scope.request_is_current(req)


@pytest.mark.parametrize("column,value", [("account_id","other-account"),("contact_id","other-contact"),
    ("conversation_type","group"),("binding_revision",2)])
def test_live_business_scope_changes_cannot_be_hidden_by_original_settings(scope_case, column, value):
    c = scope_case
    c.state.connection.execute(f"UPDATE runtime_conversations SET {column}=? WHERE conversation_id=?",
        (value,c.target.conversation_id))
    with pytest.raises(RuntimeError, match="binding_changed"):
        snapshot(c)
