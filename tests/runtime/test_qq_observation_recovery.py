"""One consumed recovery can observe while durable send admission stays paused."""
from __future__ import annotations

import asyncio
from contextvars import Context, copy_context
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget
from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.adapters.qq.vm_driver.hybrid_bridge import QQHybridDriverBridge
from messenger_ai.adapters.qq.vm_driver.message_cursor import MessageCursorStore
from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage
from messenger_ai.runtime.coordinator import RuntimeCoordinator
from messenger_ai.runtime.navigation_state import (
    NavigationTaskStore, ObservationRecoveryReceipt,
)
from messenger_ai.runtime.state import RuntimeState
from messenger_ai.runtime.qq_hybrid_navigation import _CleanupJournal
from messenger_ai.runtime.qq_observation_recovery import QQHybridObservationRecovery
from messenger_ai.runtime import qq_observation_recovery as recovery_module
from messenger_ai.runtime.planning import PlanningCoordinator
from messenger_ai.runtime.send_dispatcher import SendDispatcher


NOW = datetime(2026, 10, 3, tzinfo=UTC)
REASON = "ui_automation_unavailable:identity_profile_capture_failed"


@dataclass
class RecoveryRig:
    state: RuntimeState
    navigation: NavigationTaskStore
    cursor: MessageCursorStore
    target: ContactTarget
    receipt: ObservationRecoveryReceipt | None
    batch: ObservationBatch

    def claim_again(self):
        return self.navigation.claim_observation_recovery(
            self.target,
            conversation_revision=self.state.revisions(self.target.conversation_id)[1],
            global_revision=self.state.global_control()[0],
            pause_reason=REASON,
            now=NOW + timedelta(seconds=21),
        )

    def conversation(self):
        return self.state.connection.execute(
            "SELECT * FROM runtime_conversations WHERE conversation_id=?",
            (self.target.conversation_id,),
        ).fetchone()

    def ordinary_is_paused(self):
        return self.state.execution_state(self.target.conversation_id)[2]

    def observation_is_paused(self):
        return self.state.one_shot_observation_execution_state(
            self.target.conversation_id
        )[2]


def make_recovery(tmp_path, *, claim=True):
    path = tmp_path / "runtime.sqlite3"
    state = RuntimeState(path)
    navigation = NavigationTaskStore(path, owner_id="offline-runtime-owner")
    cursor = MessageCursorStore(tmp_path / "qq.cursor.sqlite3")
    target = ContactTarget(
        account_id="test-account", conversation_id="test-conversation",
        binding_id="test-binding", binding_revision=2,
        display_name="Synthetic contact", search_aliases=(), identity_mode="persistent",
    )
    state.register(
        account_id=target.account_id, contact_id="test-contact",
        conversation_id=target.conversation_id, binding_revision=target.binding_revision,
        conversation_type="direct",
    )
    state.pause(target.conversation_id, reason=REASON)
    br, cr = state.revisions(target.conversation_id)
    task = navigation.ensure_task(
        target, f"observe:{target.conversation_id}:{cr}", now=NOW,
    )
    episode = navigation.begin_episode(task.task_id, now=NOW).episode
    assert episode is not None
    navigation.finish_episode(
        episode.episode_id, status="needs_attention",
        error_code="identity_profile_capture_failed", now=NOW + timedelta(seconds=1),
    )
    receipt = navigation.claim_observation_recovery(
        target, conversation_revision=cr, global_revision=state.global_control()[0],
        pause_reason=REASON, now=NOW + timedelta(seconds=20),
    ) if claim else None
    if claim:
        assert type(receipt) is ObservationRecoveryReceipt
        assert receipt.failure_episode_id == episode.episode_id
    assert cursor.ingest_snapshot(target.conversation_id, []) == ()
    keys = cursor.ingest_snapshot(target.conversation_id, [{
        "message_key": "synthetic-inbound", "direction": "inbound",
        "text": "new synthetic inbound",
    }])
    assert keys == ("1",)
    assert len(cursor.claim(target.conversation_id)) == 1
    batch = ObservationBatch(
        account_id=target.account_id, contact_id="test-contact",
        conversation_id=target.conversation_id, binding_revision=br,
        conversation_revision=cr, complete=True,
        messages=(ObservedMessage(
            local_message_key=keys[0], direction=Direction.INBOUND,
            text="new synthetic inbound", observed_at=NOW + timedelta(seconds=2),
        ),),
    )
    return RecoveryRig(state, navigation, cursor, target, receipt, batch)


def close_recovery(recovery):
    recovery.cursor.close()
    recovery.navigation.close()
    recovery.state.close()


@pytest.fixture
def recovery(tmp_path):
    recovery = make_recovery(tmp_path)
    try:
        yield recovery
    finally:
        close_recovery(recovery)


def scope(recovery):
    return recovery.state.observation_recovery_scope(
        recovery.receipt, contact_id=recovery.batch.contact_id,
    )


def test_recovery_scope_heals_exact_pause_only_after_apply_and_ack(recovery):
    state, batch = recovery.state, recovery.batch
    before = state.revisions(batch.conversation_id)
    assert recovery.ordinary_is_paused() and recovery.observation_is_paused()
    with scope(recovery) as context:
        assert context.active and context.phase == "observing"
        assert recovery.ordinary_is_paused()
        assert not recovery.observation_is_paused()
        assert state.apply_observation(batch) == ("new_message",)
        assert context.phase == "applied"
        assert recovery.ordinary_is_paused() and recovery.observation_is_paused()
        assert recovery.conversation()["pause_reason"] == REASON
        resultant = state.revisions(batch.conversation_id)
        assert resultant == (before[0], before[1] + 1)

        def acknowledge():
            # The same ordinary state used by planner/send admission remains blocked.
            assert recovery.ordinary_is_paused() and recovery.observation_is_paused()
            return recovery.cursor.acknowledge_keys(batch.conversation_id, ("1",))

        assert acknowledge() == 1
        assert state.finish_observation_recovery(context, batch) is True
        assert state.revisions(batch.conversation_id) == resultant
        assert not recovery.ordinary_is_paused()
        assert state.finish_observation_recovery(context, batch) is False
    assert context.active is False
    assert recovery.claim_again() is None
    assert state.apply_observation(batch) == ()
    assert state.revisions(batch.conversation_id) == resultant
    assert state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 1
    events = state.connection.execute(
        "SELECT event_type FROM runtime_event_outbox WHERE event_type='new_message'"
    ).fetchall()
    assert len(events) == 1
    job = state.connection.execute("SELECT * FROM runtime_planning_jobs").fetchone()
    assert json.loads(job["source_keys_json"]) == ["qq-uia/test-conversation/1"]


def test_recovery_scope_and_copied_context_expire_on_normal_exit(recovery):
    with scope(recovery) as context:
        captured = copy_context()
        assert captured.run(recovery.observation_is_paused) is False
        assert Context().run(recovery.observation_is_paused) is True
        assert Context().run(recovery.ordinary_is_paused) is True
    assert context.active is False
    assert captured.run(recovery.observation_is_paused) is True
    assert captured.run(
        recovery.state.finish_observation_recovery, context, recovery.batch
    ) is False
    assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_exception_or_cancellation_after_apply_never_heals_pause(recovery, failure):
    with pytest.raises(failure):
        with scope(recovery) as context:
            captured = copy_context()
            assert recovery.state.apply_observation(recovery.batch) == ("new_message",)
            raise failure()
    assert context.active is False
    assert recovery.ordinary_is_paused()
    assert captured.run(recovery.observation_is_paused) is True
    assert captured.run(
        recovery.state.finish_observation_recovery, context, recovery.batch
    ) is False
    assert recovery.claim_again() is None


def test_finish_from_another_context_has_no_recovery_authority(recovery):
    with scope(recovery) as context:
        recovery.state.apply_observation(recovery.batch)
        assert Context().run(
            recovery.state.finish_observation_recovery, context, recovery.batch
        ) is False
        assert recovery.ordinary_is_paused()
        assert recovery.state.finish_observation_recovery(context, recovery.batch) is True


def test_incomplete_observation_cannot_finish_recovery(recovery):
    batch = recovery.batch.model_copy(update={"complete": False, "gap_reason": "history_gap"})
    with scope(recovery) as context:
        recovery.state.apply_observation(batch)
        assert recovery.ordinary_is_paused()
        assert recovery.state.finish_observation_recovery(context, batch) is False
        assert recovery.state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 0
        assert recovery.state.connection.execute("SELECT count(*) FROM runtime_planning_jobs").fetchone()[0] == 0
    assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


@pytest.mark.parametrize("direction", [Direction.UNKNOWN, Direction.OUTBOUND])
def test_unknown_direction_or_unmatched_outbound_cannot_heal_pause(recovery, direction):
    batch = recovery.batch.model_copy(update={"messages": (
        recovery.batch.messages[0].model_copy(update={
            "direction": direction,
            "operation_id": uuid4() if direction is Direction.OUTBOUND else None,
        }),
    )})
    with scope(recovery) as context:
        recovery.state.apply_observation(batch)
        assert recovery.ordinary_is_paused()
        assert recovery.state.finish_observation_recovery(context, batch) is False
    assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


def test_nested_recovery_cannot_replace_active_scope(recovery):
    with scope(recovery) as context:
        with pytest.raises(RuntimeError, match="^OBSERVATION_RECOVERY_ALREADY_ACTIVE$"):
            with scope(recovery):
                pytest.fail("a nested scope must not be admitted")
        assert context.active and not recovery.observation_is_paused()
        assert recovery.ordinary_is_paused()
    assert context.active is False


@pytest.mark.parametrize("change", ["global_pause", "manual_pause", "cr", "br", "type", "contact"])
def test_scope_rejects_changed_admission_before_any_observation(recovery, change):
    state, batch = recovery.state, recovery.batch
    contact_id = batch.contact_id
    if change == "global_pause":
        assert state.set_global_pause(paused=True, expected_revision=state.global_control()[0])
    elif change == "manual_pause":
        state.pause(batch.conversation_id, reason="manual_pause")
    elif change == "cr":
        state.pause(batch.conversation_id, reason=REASON)
    elif change in {"br", "type"}:
        state.register(
            account_id=batch.account_id, contact_id=batch.contact_id,
            conversation_id=batch.conversation_id,
            binding_revision=batch.binding_revision + (change == "br"),
            conversation_type="group" if change == "type" else "direct",
        )
    else:
        contact_id = "different-contact"
    with pytest.raises(RuntimeError, match="^OBSERVATION_RECOVERY_FENCE_CHANGED$"):
        with state.observation_recovery_scope(recovery.receipt, contact_id=contact_id):
            pytest.fail("stale scope must not admit observation")
    assert recovery.ordinary_is_paused()
    assert state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 0
    assert recovery.claim_again() is None


def test_finish_requires_the_exact_applied_batch(recovery):
    with scope(recovery) as context:
        recovery.state.apply_observation(recovery.batch)
        changed = recovery.batch.model_copy(update={"messages": (
            recovery.batch.messages[0].model_copy(update={"text": "different text"}),
        )})
        assert recovery.state.finish_observation_recovery(context, changed) is False
        assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


def test_finish_without_application_does_not_heal_pause(recovery):
    with scope(recovery) as context:
        assert recovery.state.finish_observation_recovery(context, recovery.batch) is False
    assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


@pytest.mark.parametrize("change", ["global_pause", "manual_pause", "cr", "br"])
def test_final_recovery_cas_rejects_control_or_binding_drift(recovery, change):
    state, batch = recovery.state, recovery.batch
    with scope(recovery) as context:
        assert state.apply_observation(batch) == ("new_message",)
        if change == "global_pause":
            assert state.set_global_pause(
                paused=True, expected_revision=state.global_control()[0], reason="manual_global_pause",
            )
        elif change == "manual_pause":
            state.pause(batch.conversation_id, reason="manual_pause")
        elif change == "br":
            state.register(
                account_id=batch.account_id, contact_id=batch.contact_id,
                conversation_id=batch.conversation_id,
                binding_revision=batch.binding_revision + 1, conversation_type="direct",
            )
        else:
            state.pause(batch.conversation_id, reason=REASON)
        changed_revision = state.revisions(batch.conversation_id)
        changed_reason = recovery.conversation()["pause_reason"]
        assert state.finish_observation_recovery(context, batch) is False
        assert state.revisions(batch.conversation_id) == changed_revision
        assert recovery.conversation()["pause_reason"] == changed_reason
        assert recovery.ordinary_is_paused()
    assert recovery.claim_again() is None


@pytest.mark.parametrize("reason", [
    "manual_pause", "driver_temporary:worker_not_alive",
    "ui_automation_unavailable:unknown", "identity_profile_capture_failed",
])
def test_store_does_not_claim_for_manual_or_untyped_pause(recovery, reason):
    target = recovery.target.model_copy(update={
        "binding_id": "independent-binding", "conversation_id": "independent-conversation",
    })
    task = recovery.navigation.ensure_task(target, "observe:independent-conversation:1", now=NOW)
    episode = recovery.navigation.begin_episode(task.task_id, now=NOW + timedelta(seconds=20)).episode
    assert episode is not None
    recovery.navigation.finish_episode(
        episode.episode_id, status="needs_attention", error_code="identity_profile_capture_failed",
        now=NOW + timedelta(seconds=21),
    )
    assert recovery.navigation.claim_observation_recovery(
        target, conversation_revision=1, global_revision=1,
        pause_reason=reason, now=NOW + timedelta(seconds=22),
    ) is None
    assert recovery.navigation.connection.execute(
        "SELECT count(*) FROM runtime_nav_observation_recoveries WHERE conversation_id=?",
        (target.conversation_id,),
    ).fetchone()[0] == 0


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Real state/cursor/journals/coordinator; only native UI execution is fake."""
    rig = make_recovery(tmp_path, claim=False)
    status = SimpleNamespace(state="idle", cleanup_required=False)
    worker = SimpleNamespace(status_snapshot=lambda: status, close=lambda: None)
    binding = QQIdentityBinding(
        hub_conversation_id=rig.target.conversation_id, account_id=rig.target.account_id,
        contact_id=rig.batch.contact_id, platform_conversation_id="synthetic-native-chat",
        binding_id=rig.target.binding_id, participant_signature="synthetic-existing-session",
        conversation_type="direct",
    )
    bridge = QQHybridDriverBridge(
        worker=worker, bindings=(binding,), sqlite_path=tmp_path / "qq.sqlite3",
        text_provider=lambda _: pytest.fail("observation recovery cannot request send text"),
        scope_guard=lambda _: pytest.fail("observation recovery cannot enter preparation"),
        expected_profile_signatures={binding.binding_id: "qq-profile-hmac:" + "a" * 64},
    )
    app = SimpleNamespace(state=rig.state, _pause_requested=asyncio.Event())
    nav = SimpleNamespace(
        store=rig.navigation, settings=SimpleNamespace(targets={binding.binding_id: rig.target}),
        journal=_CleanupJournal(rig.navigation), _active=False, _rounds={}, _closed=False,
    )
    factory = QQHybridObservationRecovery(app=app, bridge=bridge, navigation=nav)
    bridge.observation_recovery_context = factory.context
    coordinator = RuntimeCoordinator(state=rig.state, hub=None, pacing=None)
    native_calls = []

    async def observe(conversation_id, *, binding_revision, conversation_revision):
        native_calls.append("observe")
        assert conversation_id == binding.hub_conversation_id
        assert rig.ordinary_is_paused()
        assert not rig.observation_is_paused()
        return rig.batch.model_copy(update={
            "binding_revision": binding_revision, "conversation_revision": conversation_revision,
        })

    bridge.observe_conversation = observe

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return NOW + timedelta(seconds=20)

    monkeypatch.setattr(recovery_module, "datetime", FixedDateTime)
    result = SimpleNamespace(
        rig=rig, bridge=bridge, app=app, nav=nav, worker_status=status,
        factory=factory, coordinator=coordinator, binding=binding, native_calls=native_calls,
    )
    try:
        yield result
    finally:
        bridge.close()
        close_recovery(rig)


def audit_rows(pipeline):
    return pipeline.rig.navigation.connection.execute(
        "SELECT * FROM runtime_nav_observation_recoveries"
    ).fetchall()


def enter_pipeline_context(pipeline):
    br, cr = pipeline.rig.state.revisions(pipeline.binding.hub_conversation_id)
    return pipeline.factory.context(
        pipeline.binding.hub_conversation_id, binding_revision=br, conversation_revision=cr,
    )


def critical_requests_are_current(pipeline):
    state, binding = pipeline.rig.state, pipeline.binding
    br, cr = state.revisions(binding.hub_conversation_id)
    gr = state.global_control()[0]
    rules = SimpleNamespace(resolve=lambda _: SimpleNamespace(rulepack=SimpleNamespace(version="test-rules")))
    planning = PlanningCoordinator(
        state=state, hub=None, memory=None, rules=rules, planner=None, policy=None,
        pacing=None, capability=None,
    )
    sending = SendDispatcher(state=state, hub=None, pacing=None, authorization=None, driver=pipeline.bridge)
    execution = SimpleNamespace(
        due=SimpleNamespace(conversation_id=binding.hub_conversation_id),
        binding_revision=br, conversation_revision=cr, global_revision=gr, prepared_adapter=None,
    )
    return (
        planning.is_current(binding.hub_conversation_id, binding.contact_id, br, cr, gr, "test-rules"),
        sending._current(execution),
    )


@pytest.mark.asyncio
async def test_coordinator_recovery_acks_while_planner_and_send_are_still_paused(pipeline, monkeypatch):
    rig, bridge = pipeline.rig, pipeline.bridge
    original_ack = bridge.acknowledge_observation
    phases = []

    def acknowledge(conversation_id, keys):
        phases.append("ack")
        assert rig.ordinary_is_paused() and rig.observation_is_paused()
        assert critical_requests_are_current(pipeline) == (False, False)
        assert rig.conversation()["pause_reason"] == REASON
        return original_ack(conversation_id, keys)

    monkeypatch.setattr(bridge, "acknowledge_observation", acknowledge)
    before = rig.state.revisions(bridge.observation_conversation_ids[0])
    assert critical_requests_are_current(pipeline) == (False, False)
    assert await pipeline.coordinator.observe_driver(bridge, pipeline.binding.hub_conversation_id) == ("new_message",)
    assert phases == ["ack"] and pipeline.native_calls == ["observe"]
    assert not rig.ordinary_is_paused()
    assert rig.state.revisions(pipeline.binding.hub_conversation_id) == (before[0], before[1] + 1)
    assert critical_requests_are_current(pipeline) == (True, True)
    assert rig.cursor.connection.execute("SELECT status FROM observation_outbox").fetchone()[0] == "delivered"
    assert len(audit_rows(pipeline)) == 1 and audit_rows(pipeline)[0]["status"] == "succeeded"
    assert rig.state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 1
    assert rig.state.connection.execute("SELECT count(*) FROM runtime_planning_jobs").fetchone()[0] == 1
    assert bridge._db.execute("SELECT count(*) FROM qq_vm_ops").fetchone()[0] == 0
    assert bridge._db.execute("SELECT count(*) FROM qq_v2_draft_reservations").fetchone()[0] == 0
    async with enter_pipeline_context(pipeline) as second:
        assert second is None
    assert len(audit_rows(pipeline)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "false_count", "raise", "cancel"])
async def test_partial_or_failed_ack_keeps_pause_and_consumes_one_claim(pipeline, monkeypatch, failure):
    rig, bridge = pipeline.rig, pipeline.bridge
    original_ack = bridge.acknowledge_observation
    if failure == "partial":
        assert rig.cursor.ingest_snapshot(pipeline.binding.hub_conversation_id, [
            {"message_key": "synthetic-inbound", "direction": "inbound", "text": "new synthetic inbound"},
            {"message_key": "synthetic-next", "direction": "inbound", "text": "second synthetic inbound"},
        ]) == ("2",)
        assert len(rig.cursor.claim(pipeline.binding.hub_conversation_id)) == 1
        rig.batch = rig.batch.model_copy(update={"messages": (*rig.batch.messages,
            rig.batch.messages[0].model_copy(update={"local_message_key": "2", "text": "second synthetic inbound"}),
        )})

    def acknowledge(conversation_id, keys):
        assert rig.ordinary_is_paused() and critical_requests_are_current(pipeline) == (False, False)
        if failure == "partial":
            return original_ack(conversation_id, keys[:1])
        if failure == "false_count":
            return len(keys)  # A count is not durable acknowledgement evidence.
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic ack failure")

    monkeypatch.setattr(bridge, "acknowledge_observation", acknowledge)
    if failure in {"raise", "cancel"}:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await pipeline.coordinator.observe_driver(bridge, pipeline.binding.hub_conversation_id)
    else:
        await pipeline.coordinator.observe_driver(bridge, pipeline.binding.hub_conversation_id)
    assert rig.ordinary_is_paused()
    assert critical_requests_are_current(pipeline) == (False, False)
    assert len(audit_rows(pipeline)) == 1 and audit_rows(pipeline)[0]["status"] == "failed"
    async with enter_pipeline_context(pipeline) as second:
        assert second is None
    assert len(audit_rows(pipeline)) == 1 and pipeline.native_calls == ["observe"]


@pytest.mark.asyncio
async def test_exact_delivered_cursor_rows_allow_ack_count_zero(pipeline):
    rig = pipeline.rig
    async with enter_pipeline_context(pipeline) as handle:
        assert handle is not None
        events = rig.state.apply_observation(rig.batch)
        assert rig.cursor.acknowledge_keys(pipeline.binding.hub_conversation_id, ("1",)) == 1
        assert handle.finish(rig.batch, events, 0) is True
    assert not rig.ordinary_is_paused()
    assert audit_rows(pipeline)[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_incomplete_pipeline_observation_cannot_ack_or_publish_partial_messages(pipeline, monkeypatch):
    rig = pipeline.rig
    rig.batch = rig.batch.model_copy(update={"complete": False, "gap_reason": "history_gap"})
    monkeypatch.setattr(pipeline.bridge, "acknowledge_observation", lambda *_: pytest.fail("incomplete recovery must not ACK"))
    assert await pipeline.coordinator.observe_driver(pipeline.bridge, pipeline.binding.hub_conversation_id) == ()
    assert rig.ordinary_is_paused()
    assert rig.state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 0
    assert rig.state.connection.execute("SELECT count(*) FROM runtime_planning_jobs").fetchone()[0] == 0
    assert rig.cursor.connection.execute("SELECT status FROM observation_outbox").fetchone()[0] == "dispatching"
    assert audit_rows(pipeline)[0]["status"] == "failed"


OBLIGATIONS = (
    "navigation_active", "navigation_round", "navigation_journal", "navigation_running",
    "worker_busy", "worker_cleanup", "draft", "commit_uncertain",
)


def block_recovery(pipeline, condition):
    """Native lifecycle signals are fake; durable obligations use their real stores."""
    rig, bridge, nav, binding = pipeline.rig, pipeline.bridge, pipeline.nav, pipeline.binding
    if condition == "navigation_active":
        nav._active = True
    elif condition == "navigation_round":
        nav._rounds[uuid4()] = object()
    elif condition == "navigation_journal":
        nav.store.connection.execute(
            "INSERT INTO runtime_nav_cleanup_obligations VALUES(?,?,?,?,?)",
            (str(uuid4()), binding.account_id, "synthetic-owned-round", "synthetic-guard", NOW.isoformat()),
        )
    elif condition == "navigation_running":
        target = rig.target.model_copy(update={
            "binding_id": "other-account-binding", "conversation_id": "other-account-conversation",
        })
        task = nav.store.ensure_task(target, "synthetic-pending-observe", now=NOW + timedelta(seconds=20))
        assert nav.store.begin_episode(task.task_id, now=NOW + timedelta(seconds=20)).episode is not None
    elif condition == "worker_busy":
        pipeline.worker_status.state = "observing"
    elif condition == "worker_cleanup":
        pipeline.worker_status.cleanup_required = True
    elif condition == "draft":
        bridge._db.execute("""INSERT INTO qq_v2_draft_reservations(
            reservation_id,nonce,account_id,conversation_id,request_json,
            expected_sequence_digest,status,created_at) VALUES(?,?,?,?,?,?,?,?)""", (
                str(uuid4()), str(uuid4()), binding.account_id, binding.hub_conversation_id,
                "{}", "a" * 64, "cleanup_required", NOW.isoformat(),
            ))
    elif condition == "commit_uncertain":
        bridge._db.execute("""INSERT INTO qq_vm_ops(
            operation_id,idempotency_key,draft_id,conversation_id,binding_id,segment_ref,
            binding_revision,conversation_revision,text_hash,status,commit_intent)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                str(uuid4()), "synthetic-old-operation", str(uuid4()), binding.hub_conversation_id,
                binding.binding_id, "synthetic-old-segment", 2, 2, "a" * 64, "send_uncertain", 1,
            ))
        assert not bridge.has_cleanup_obligation(binding.account_id)
    elif condition == "global_pause":
        assert rig.state.set_global_pause(paused=True, expected_revision=rig.state.global_control()[0])
    elif condition == "pause_intent":
        pipeline.app._pause_requested.set()
    elif condition == "manual_pause":
        rig.state.pause(binding.hub_conversation_id, reason="manual_pause")
    elif condition == "untyped_failure":
        nav.store.connection.execute(
            "UPDATE runtime_nav_episodes SET error_code='identity_profile_capture_revoked'"
        )
    elif condition == "cursor_missing":
        rig.cursor.connection.execute("DELETE FROM cursor_state")
    else:
        raise AssertionError(condition)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", (*OBLIGATIONS, "global_pause", "pause_intent", "manual_pause", "untyped_failure", "cursor_missing"))
async def test_recovery_rejects_before_claim_without_consuming_budget_or_reading_ui(pipeline, condition):
    block_recovery(pipeline, condition)
    async with enter_pipeline_context(pipeline) as handle:
        assert handle is None
        assert pipeline.rig.ordinary_is_paused()
    assert audit_rows(pipeline) == []
    assert pipeline.native_calls == []
    assert pipeline.rig.state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", (*OBLIGATIONS, "pause_intent"))
async def test_obligation_after_durable_claim_revokes_read_and_never_refunds(pipeline, monkeypatch, condition):
    original_claim = pipeline.nav.store.claim_observation_recovery

    def claim_then_block(*args, **kwargs):
        receipt = original_claim(*args, **kwargs)
        assert type(receipt) is ObservationRecoveryReceipt
        block_recovery(pipeline, condition)
        return receipt

    monkeypatch.setattr(pipeline.nav.store, "claim_observation_recovery", claim_then_block)
    async with enter_pipeline_context(pipeline) as handle:
        assert handle is None
    assert pipeline.rig.ordinary_is_paused()
    assert pipeline.native_calls == []
    assert len(audit_rows(pipeline)) == 1 and audit_rows(pipeline)[0]["status"] == "failed"
    assert original_claim(
        pipeline.rig.target, conversation_revision=pipeline.rig.batch.conversation_revision,
        global_revision=pipeline.rig.state.global_control()[0], pause_reason=REASON,
        now=NOW + timedelta(seconds=21),
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", (*OBLIGATIONS, "global_pause", "pause_intent", "manual_pause"))
async def test_finish_rechecks_current_obligations_after_exact_cursor_ack(pipeline, condition):
    rig = pipeline.rig
    async with enter_pipeline_context(pipeline) as handle:
        assert handle is not None
        events = rig.state.apply_observation(rig.batch)
        assert rig.cursor.acknowledge_keys(pipeline.binding.hub_conversation_id, ("1",)) == 1
        block_recovery(pipeline, condition)
        before = rig.state.revisions(pipeline.binding.hub_conversation_id)
        reason = rig.conversation()["pause_reason"]
        assert handle.finish(rig.batch, events, 1) is False
        assert rig.ordinary_is_paused()
        assert rig.state.revisions(pipeline.binding.hub_conversation_id) == before
        assert rig.conversation()["pause_reason"] == reason
        assert critical_requests_are_current(pipeline) == (False, False)
    assert len(audit_rows(pipeline)) == 1 and audit_rows(pipeline)[0]["status"] == "failed"
    assert rig.claim_again() is None
    assert pipeline.native_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["global_pause", "manual_pause"])
async def test_exact_control_drift_after_claim_cannot_admit_read(pipeline, monkeypatch, condition):
    original_claim = pipeline.nav.store.claim_observation_recovery

    def claim_then_change_control(*args, **kwargs):
        receipt = original_claim(*args, **kwargs)
        assert type(receipt) is ObservationRecoveryReceipt
        block_recovery(pipeline, condition)
        return receipt

    monkeypatch.setattr(pipeline.nav.store, "claim_observation_recovery", claim_then_change_control)
    with pytest.raises(RuntimeError, match="^OBSERVATION_RECOVERY_FENCE_CHANGED$"):
        async with enter_pipeline_context(pipeline):
            pytest.fail("changed exact pause must not admit observation")
    assert pipeline.rig.ordinary_is_paused()
    assert pipeline.native_calls == []
    assert audit_rows(pipeline)[0]["status"] == "failed"
    monkeypatch.setattr(pipeline.nav.store, "claim_observation_recovery", original_claim)
    assert pipeline.rig.claim_again() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_observe_failure_revokes_copied_context_without_apply_or_ack(pipeline, monkeypatch, failure):
    copied = []

    async def failed_observe(*args, **kwargs):
        pipeline.native_calls.append("observe")
        assert pipeline.rig.ordinary_is_paused()
        assert not pipeline.rig.observation_is_paused()
        copied.append(copy_context())
        raise failure()

    monkeypatch.setattr(pipeline.bridge, "observe_conversation", failed_observe)
    monkeypatch.setattr(pipeline.bridge, "acknowledge_observation", lambda *_: pytest.fail("failed observe cannot ACK"))
    with pytest.raises(failure):
        await pipeline.coordinator.observe_driver(pipeline.bridge, pipeline.binding.hub_conversation_id)
    assert copied[0].run(pipeline.rig.observation_is_paused) is True
    assert pipeline.rig.ordinary_is_paused()
    assert pipeline.rig.state.connection.execute("SELECT count(*) FROM runtime_observations").fetchone()[0] == 0
    assert pipeline.rig.state.connection.execute("SELECT count(*) FROM runtime_planning_jobs").fetchone()[0] == 0
    assert pipeline.rig.cursor.connection.execute("SELECT status FROM observation_outbox").fetchone()[0] == "dispatching"
    assert audit_rows(pipeline)[0]["status"] == "failed"
    async with enter_pipeline_context(pipeline) as second:
        assert second is None
    assert pipeline.native_calls == ["observe"]
