from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.models import BubbleDirection, QQBubble, QQIdentityBinding, QQSelector, QQSelectorPack
from messenger_ai.adapters.qq.navigation.windows_backend import WindowsNavigationConfig, NavigationGuardState
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerCommand, WorkerKind, WorkerStatus, PreparedVerificationEvidence
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import (
    HybridQQWorker, HybridSnapshot, HybridWorkerConfig, HybridWorkerError,
    WindowsHybridCurrentChatPort, semantic_sequence_digest,
)
from messenger_ai.runtime.staged_preparation import DraftPreparationRequest, source_keys_digest
from tests.adapters.qq.navigation.test_identity import BASE, NS, PROCESS_START, case
from tests.adapters.qq.navigation.test_profile_verifier import report, parse


class Port:
    def __init__(self, config, target, frame, witness, captured):
        self.config, self.target = config, target
        self.base_frame, self.base_witness, self.captured = frame, witness, captured
        self.now, self.tick = BASE, 0
        self.body = ""
        self.bubbles = [QQBubble(conversation_internal_id="volatile-ui-id", message_key="m1",
            direction="inbound", text="synthetic inbound", observed_at=BASE, tree_digest="tree")]
        self.events = []
        self.mode = "idle"
        self.paused = False
        self.snapshot_hook = self.capture_hook = self.write_hook = self.send_hook = None
        self.writes = self.sends = self.clears = self.profiles = 0
        self.worker = None

    def advance(self, seconds=0.05):
        self.now += timedelta(seconds=seconds)
        self.tick += int(seconds * NS)

    def begin(self, mode, *, deadline_at):
        self.mode = mode
        self.events.append("begin:" + mode)

    def guard(self):
        if self.paused and self.mode not in ("abort", "health"):
            raise HybridWorkerError("hybrid_paused")

    def health(self):
        self.guard()
        self.events.append("health")
        return {"process_id": 123, "window_handle": 456, "process_started_at_100ns": PROCESS_START}

    def allow_paused_cleanup_revision(self, target, original, current):
        return self.mode == "abort" and self.paused and current > original

    def frame(self, target):
        self.guard()
        self.events.append("frame")
        return self.base_frame.model_copy(update={"frame_id": str(uuid4()), "captured_at": self.now})

    def snapshot(self, target):
        self.guard()
        self.advance()
        self.events.append("snapshot")
        if self.snapshot_hook:
            self.snapshot_hook()
        witness = self.base_witness.model_copy(update={"captured_at": self.now,
            "captured_monotonic_ns": self.tick, "composer_empty": not self.body})
        self.last_snapshot = HybridSnapshot(witness, tuple(self.bubbles), self.body)
        return self.last_snapshot

    def capture(self, target, frame, expectation, *, deadline_at):
        self.guard()
        assert self.mode in ("idle", "verify")
        self.events.append("profile")
        self.profiles += 1
        self.advance()
        before = self.captured.acquisition.before.model_copy(update={
            "captured_at": self.now, "captured_monotonic_ns": self.tick,
            "selected_row_runtime_id_hash": self.base_witness.selected_row_runtime_id_hash})
        self.advance()
        at, tick = self.now, self.tick
        self.advance()
        after = before.model_copy(update={"captured_at": self.now, "captured_monotonic_ns": self.tick})
        captured = self.captured.model_copy(update={"acquisition": self.captured.acquisition.model_copy(update={
            "before": before, "after": after, "profile_captured_at": at, "profile_captured_monotonic_ns": tick})})
        if self.capture_hook:
            self.capture_hook()
        return captured

    def write(self, target, text, *, before_action):
        assert self.worker._prepared is not None  # Ownership precedes first mutation.
        self.events.append("write-entry")
        if self.write_hook:
            self.write_hook()
        before_action()
        if self.body:
            raise HybridWorkerError("composer_not_empty")
        self.writes += 1
        self.body = text
        self.events.append("write")

    def send(self, target, *, before_action):
        self.events.append("send-entry")
        if self.send_hook:
            self.send_hook()
        self.guard()
        if self.body != self.last_snapshot.composer_text:
            raise HybridWorkerError("composer_drift")
        if semantic_sequence_digest(tuple(self.bubbles)) != semantic_sequence_digest(self.last_snapshot.bubbles):
            raise HybridWorkerError("stale_context")
        before_action()
        self.sends += 1
        self.bubbles.append(self.bubbles[0].model_copy(update={"message_key": "out1", "direction": BubbleDirection.OUTBOUND, "text": self.body}))
        self.body = ""
        self.events.append("send")

    def clear(self, target, text, *, before_action):
        before_action()
        assert self.body == text
        self.clears += 1
        self.body = ""
        before_action()  # Native clear also checks scope while waiting for empty.


@pytest.fixture
def rig(case, report, tmp_path):
    target, frame, expectation, evidence = case
    epoch = str(uuid4())
    frame = frame.model_copy(update={"worker_epoch": epoch})
    witness = evidence.before.model_copy(update={"worker_epoch": epoch})
    selectors = {name: QQSelector(name=name, control_type="Text") for name in
                 ("list", "row", "name", "search", "header", "composer", "bubbles", "send")}
    nav = WindowsNavigationConfig(guard_state_path=str(tmp_path / "guard.json"),
        window=dict(process_id=123, window_handle=456, class_name="QQ"),
        expected_process_started_at_100ns=PROCESS_START, expected_run_id="run", expected_worker_epoch=epoch,
        **{f"{name}_selector": selectors["bubbles" if name == "message" else name] for name in
           ("list", "row", "name", "search", "header", "composer", "message")})
    binding = QQIdentityBinding(hub_conversation_id=target.conversation_id, contact_id="contact", account_id="account",
        platform_conversation_id="stable-platform", participant_signature="qq-session-observed:existing-unchanged",
        binding_id=target.binding_id, conversation_type="direct")
    pack = QQSelectorPack(client_version=expectation.client_version, environment_fingerprint=expectation.environment_fingerprint,
        selectors=tuple(selectors.values()), last_verified_at=BASE, fixture_suite_version=expectation.selector_pack_version)
    config = HybridWorkerConfig(navigation=nav, selector_pack=pack, bindings=(binding,), targets=(target,),
        expectations=(expectation,), helper_path=str(tmp_path / "fixed.exe"), vault_path=str(tmp_path / "fixed-vault"))
    port = Port(config, target, frame, witness, parse(report))
    worker = HybridQQWorker(config, port, clock=lambda: port.now, monotonic_ns=lambda: port.tick,
                            deadline_at=BASE + timedelta(seconds=45), stop_at=45)
    port.worker = worker
    request = DraftPreparationRequest(reservation_id=uuid4(), nonce=uuid4(), outbox_id=1, claim_token="claim",
        due_event_id=uuid4(), account_id="account", contact_id="contact", conversation_id="conversation",
        binding_id="binding", binding_revision=7, conversation_revision=2, global_revision=3,
        pacing_plan_id=uuid4(), segment_index=0, draft_id=uuid4(), body="synthetic reply",
        body_hash=hashlib.sha256(b"synthetic reply").hexdigest(), source_message_keys=("durable1",),
        source_keys_digest=source_keys_digest(("durable1",)), expected_last_message_key="durable1",
        original_snapshot_digest="e" * 64, requested_at=BASE, deadline_at=BASE + timedelta(seconds=30),
        requested_monotonic_ns=0, deadline_monotonic_ns=30 * NS)
    return worker, port, request


def prepare(rig):
    worker, port, request = rig
    return worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(port.bubbles)))


def command(rig, kind, **overrides):
    _, _, req = rig
    values = dict(kind=kind, binding_id=req.binding_id, binding_revision=req.binding_revision,
        conversation_revision=req.conversation_revision, operation_id=uuid4(), text=req.body,
        segment_ref=f"{req.pacing_plan_id}:{req.segment_index}", deadline=BASE + timedelta(seconds=30))
    values.update(overrides)
    return WorkerCommand(**values)


def adopted(rig):
    ticket = prepare(rig)
    cmd = command(rig, WorkerKind.PREPARE)
    result = rig[0].adopt_prepared(ticket, cmd)
    assert result.status is WorkerStatus.OK
    return ticket, cmd, PreparedVerificationEvidence.model_validate(result.evidence["prepared_evidence"])


def test_prepare_owns_before_write_uses_real_hmac_and_never_changes_binding(rig):
    worker, port, _ = rig
    original = worker.config.bindings[0].model_dump_json()
    ticket = prepare(rig)
    assert port.body == "synthetic reply" and port.profiles == 1 and port.sends == 0
    assert ticket.worker_epoch == str(worker.worker_epoch)
    assert worker._prepared.portable.target_identity.participant_signature == "qq-profile-hmac:" + "a" * 64
    assert worker.config.bindings[0].model_dump_json() == original
    assert port.events.index("profile") < port.events.index("write")
    assert ticket.expected_sequence_digest == semantic_sequence_digest(tuple(port.bubbles))


def test_observe_preserves_business_association_and_raw_message_key(rig):
    worker, port, _ = rig
    result = worker.execute(command(rig, WorkerKind.OBSERVE))
    assert result.status is WorkerStatus.OK
    assert result.evidence["target"]["participant_signature"] == worker.config.bindings[0].participant_signature
    assert result.evidence["bubbles"][0]["message_key"] == "m1"
    assert result.evidence["bubbles"][0]["conversation_internal_id"] == "stable-platform"
    assert port.profiles == 1 and port.writes == port.sends == 0


def test_portable_evidence_is_exact_value_only_and_cannot_use_foreign_ticket(rig):
    worker, port, _ = rig
    ticket = prepare(rig)
    events = list(port.events)
    proof = worker.prepared_evidence(ticket)
    assert hashlib.sha256(proof.model_dump_json().encode()).hexdigest() == ticket.evidence_digest
    assert port.events == events
    with pytest.raises(HybridWorkerError, match="prepared_ticket_mismatch"):
        worker.prepared_evidence(ticket.model_copy(update={"nonce": uuid4()}))


def test_adopt_is_one_use_exact_locked_memory_transition_with_no_ui(rig):
    worker, port, _ = rig
    ticket = prepare(rig)
    before = list(port.events)
    cmd = command(rig, WorkerKind.PREPARE)
    assert worker.adopt_prepared(ticket, cmd).status is WorkerStatus.OK
    assert worker.adopt_prepared(ticket, cmd).error_code == "prepared_ticket_already_adopted"
    assert port.events == before and worker._prepared.operation_id == cmd.operation_id


@pytest.mark.parametrize("field,value", [("worker_epoch", "other"), ("nonce", uuid4()),
    ("body_hash", "9" * 64), ("evidence_digest", "9" * 64), ("expected_sequence_digest", "9" * 64)])
def test_adopt_rejects_tampered_ticket_without_ui(rig, field, value):
    worker, port, _ = rig
    ticket = prepare(rig)
    events = list(port.events)
    result = worker.adopt_prepared(ticket.model_copy(update={field: value}), command(rig, WorkerKind.PREPARE))
    assert result.error_code == "prepared_ticket_mismatch" and port.events == events
    assert worker._prepared.operation_id is None


@pytest.mark.parametrize("field,value", [("binding_revision", 8), ("conversation_revision", 3),
    ("text", "other body"), ("segment_ref", "other:1"), ("binding_id", "other")])
def test_adopt_rejects_payload_or_business_scope_change(rig, field, value):
    ticket = prepare(rig)
    result = rig[0].adopt_prepared(ticket, command(rig, WorkerKind.PREPARE, **{field: value}))
    assert result.status is WorkerStatus.FAILED_SAFE and rig[0]._prepared.operation_id is None


def test_stale_source_digest_is_not_replaced_by_newly_read_snapshot(rig):
    worker, port, req = rig
    with pytest.raises(HybridWorkerError, match="stale_context"):
        worker.prepare_draft(req, expected_sequence_digest="9" * 64)
    assert port.profiles == 1 and port.writes == 0 and worker._prepared is None


@pytest.mark.parametrize("field,value", [("header_digest", "9" * 64), ("selected_row_runtime_id_hash", "9" * 64),
    ("active_chat_structure_digest", "9" * 64), ("process_id", 999), ("window_handle", 999),
    ("process_started_at_100ns", 999), ("session_epoch", "other"), ("surface_epoch", "other"),
    ("worker_epoch", str(uuid4())), ("desktop_lease_id", "other"), ("control_revision", 4),
    ("observation_epoch", "other"), ("latest_tail", False), ("selected_row_candidate_count", 2),
    ("group_marker_count", 1), ("group_marker_probe_complete", False)])
def test_commit_refuses_current_fence_drift_without_profile_or_send(rig, field, value):
    worker, port, _ = rig
    _, cmd, _ = adopted(rig)
    port.base_witness = port.base_witness.model_copy(update={field: value})
    result = worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT}))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert port.sends == 0 and port.profiles == 1 and worker._prepared is not None


@pytest.mark.parametrize("change", ["message", "draft", "pause", "cancel"])
def test_last_send_boundary_rechecks_new_message_draft_pause_and_cancel(rig, change):
    worker, port, _ = rig
    _, cmd, _ = adopted(rig)
    def mutate():
        if change == "message":
            port.bubbles.append(port.bubbles[0].model_copy(update={"message_key": "m2", "text": "new message"}))
        elif change == "draft":
            port.body = "human draft"
        elif change == "pause":
            port.paused = True
        else:
            worker.revoked.set()
    port.send_hook = mutate
    result = worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT}))
    assert result.status is WorkerStatus.FAILED_SAFE and port.sends == 0 and port.profiles == 1


def test_commit_once_then_verify_never_reopens_profile_or_resends(rig):
    worker, port, _ = rig
    _, cmd, portable = adopted(rig)
    commit = cmd.model_copy(update={"kind": WorkerKind.COMMIT})
    assert worker.execute(commit).status is WorkerStatus.OK
    assert worker.execute(commit).status is WorkerStatus.UNCERTAIN
    assert port.sends == 1 and port.profiles == 1
    result = worker.execute(cmd.model_copy(update={"kind": WorkerKind.VERIFY, "prepared_evidence": portable}))
    assert result.status is WorkerStatus.OK and result.evidence["receipt"]["text"] == "synthetic reply"
    assert result.evidence["receipt"]["participant_signature"] == worker._bindings[cmd.binding_id].participant_signature
    assert port.sends == 1 and port.profiles == 1 and worker._prepared is None


@pytest.mark.parametrize("mutation", [None, "draft", "hmac", "fingerprint", "no_new_receipt", "duplicate_receipt"])
def test_fresh_worker_verify_requires_own_profile_empty_composer_exact_portable_unique_receipt(rig, mutation):
    worker, port, _ = rig
    _, cmd, portable = adopted(rig)
    assert worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT})).status is WorkerStatus.OK
    if mutation == "draft":
        port.body = "human draft"
    elif mutation == "hmac":
        port.captured = port.captured.model_copy(update={"profile": port.captured.profile.model_copy(update={"profile_id_hmac": "9" * 64})})
    elif mutation == "fingerprint":
        portable = portable.model_copy(update={"target_identity": portable.target_identity.model_copy(update={"window_handle": 999})})
    elif mutation == "no_new_receipt":
        port.bubbles.pop()
    elif mutation == "duplicate_receipt":
        port.bubbles.append(port.bubbles[-1].model_copy(update={"message_key": "out2"}))
    epoch = str(uuid4())
    config = worker.config.model_copy(update={"navigation": worker.config.navigation.model_copy(update={"expected_worker_epoch": epoch})})
    port.base_frame = port.base_frame.model_copy(update={"worker_epoch": epoch})
    port.base_witness = port.base_witness.model_copy(update={"worker_epoch": epoch})
    fresh = HybridQQWorker(config, port, clock=lambda: port.now, monotonic_ns=lambda: port.tick)
    result = fresh.execute(cmd.model_copy(update={"kind": WorkerKind.VERIFY, "prepared_evidence": portable}))
    assert result.status is (WorkerStatus.OK if mutation is None else WorkerStatus.UNCERTAIN)
    if mutation is None:
        assert result.evidence["receipt"]["participant_signature"] == fresh._bindings[cmd.binding_id].participant_signature
    assert port.sends == 1 and port.writes == 1
    assert port.profiles == (1 if mutation == "draft" else 2)
    assert fresh.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT})).status is WorkerStatus.FAILED_SAFE
    assert port.sends == 1


def test_send_provider_exception_after_attempt_never_allows_abort_or_retry(rig):
    worker, port, req = rig
    _, cmd, _ = adopted(rig)
    def fail(target, *, before_action):
        before_action()
        raise RuntimeError("private provider detail")
    port.send = fail
    result = worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT}))
    assert result.status is WorkerStatus.UNCERTAIN and "private" not in result.model_dump_json()
    assert worker.abort_draft(req, deadline_at=req.deadline_at).status == "cleanup_required"
    assert worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT})).error_code == "commit_already_attempted"
    assert port.clears == port.sends == 0


def test_foreign_draft_blocks_profile_and_is_never_overwritten(rig):
    worker, port, _ = rig
    port.body = "human draft"
    with pytest.raises(HybridWorkerError, match="identity_composer_not_empty"):
        prepare(rig)
    assert port.profiles == port.writes == 0 and port.body == "human draft"


def test_new_draft_at_write_entry_keeps_reservation_for_proven_cleanup(rig):
    worker, port, req = rig
    port.write_hook = lambda: setattr(port, "body", "human draft")
    with pytest.raises(HybridWorkerError) as exc:
        prepare(rig)
    assert exc.value.cleanup_required and port.writes == 0
    result = worker.abort_draft(req, deadline_at=req.deadline_at)
    assert result.status == "cleanup_required" and port.body == "human draft" and port.clears == 0


def test_pause_allows_only_exact_owned_abort_with_original_request(rig):
    worker, port, req = rig
    prepare(rig)
    port.paused = True
    port.base_witness = port.base_witness.model_copy(update={"control_revision": 4})
    result = worker.abort_draft(req, deadline_at=req.deadline_at)
    assert result.status == "cleaned" and port.body == "" and port.clears == 1
    assert worker._prepared is None and port.profiles == 1 and port.sends == 0


@pytest.mark.parametrize("paused,revision", [(True, 2), (False, 4)])
def test_abort_revision_exception_cannot_roll_back_or_apply_without_pause(rig, paused, revision):
    worker, port, req = rig
    prepare(rig)
    port.paused = paused
    port.base_witness = port.base_witness.model_copy(update={"control_revision": revision})
    assert worker.abort_draft(req, deadline_at=req.deadline_at).status == "cleanup_required"
    assert port.clears == 0


def test_abort_foreign_owner_or_original_contact_drift_cannot_clear(rig):
    worker, port, req = rig
    prepare(rig)
    wrong = req.model_copy(update={"nonce": uuid4()})
    assert worker.abort_draft(wrong, deadline_at=req.deadline_at).status == "not_owned"
    port.base_witness = port.base_witness.model_copy(update={"selected_row_runtime_id_hash": "9" * 64})
    assert worker.abort_draft(req, deadline_at=req.deadline_at).status == "cleanup_required"
    assert port.clears == 0 and port.profiles == 1


def test_failed_postwrite_fence_retains_original_owner_and_no_ticket(rig):
    worker, port, req = rig
    original = port.write
    def drift(*args, **kwargs):
        original(*args, **kwargs)
        port.base_witness = port.base_witness.model_copy(update={"header_digest": "9" * 64})
    port.write = drift
    with pytest.raises(HybridWorkerError) as exc:
        prepare(rig)
    assert exc.value.cleanup_required and worker._prepared.ticket is None
    assert port.body == req.body and port.profiles == 1


def test_wrong_hmac_or_messages_during_profile_never_write(rig):
    worker, port, _ = rig
    port.captured = port.captured.model_copy(update={"profile": port.captured.profile.model_copy(update={"profile_id_hmac": "9" * 64})})
    with pytest.raises(HybridWorkerError, match="identity_profile_mismatch"):
        prepare(rig)
    assert port.writes == 0 and worker._prepared is None


def test_message_arriving_during_profile_invalidates_source_before_write(rig):
    worker, port, _ = rig
    port.capture_hook = lambda: port.bubbles.append(port.bubbles[0].model_copy(update={"message_key": "new", "text": "new"}))
    with pytest.raises(HybridWorkerError, match="stale_context"):
        prepare(rig)
    assert port.writes == 0 and worker._prepared is None


@pytest.mark.parametrize("clock", ["utc", "monotonic"])
def test_prepare_write_reserve_and_original_deadline_are_not_renewed(rig, clock):
    worker, port, req = rig
    def delay():
        if clock == "utc":
            port.now = BASE + timedelta(seconds=26)
        else:
            port.tick = 26 * NS
    port.capture_hook = delay
    with pytest.raises(HybridWorkerError):
        prepare(rig)
    assert port.writes == 0 and worker._prepared is None


def test_fresh_profile_still_cannot_cross_write_with_less_than_twenty_second_reserve(rig):
    worker, port, request = rig
    request = request.model_copy(update={"deadline_at": BASE + timedelta(seconds=20.2),
                                        "deadline_monotonic_ns": int(20.2 * NS)})
    with pytest.raises(HybridWorkerError, match="prepare_write_budget_exhausted") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(port.bubbles)))
    assert not error.value.cleanup_required and port.profiles == 1 and port.writes == 0


def test_raw_sequence_token_ignores_locator_metadata_but_preserves_semantic_keys(rig):
    _, port, _ = rig
    original = semantic_sequence_digest(tuple(port.bubbles))
    moved = tuple(b.model_copy(update={"tree_digest": "new-tree", "conversation_internal_id": "new-locator",
                                      "observed_at": BASE + timedelta(seconds=1)}) for b in port.bubbles)
    assert semantic_sequence_digest(moved) == original
    changed = tuple(b.model_copy(update={"message_key": "other"}) for b in moved)
    assert semantic_sequence_digest(changed) != original


def test_expired_ticket_cannot_be_adopted_but_owner_can_clear(rig):
    worker, port, req = rig
    ticket = prepare(rig)
    port.advance(16)
    assert worker.adopt_prepared(ticket, command(rig, WorkerKind.PREPARE)).error_code == "prepared_ticket_expired"
    assert worker.abort_draft(req, deadline_at=req.deadline_at).status == "cleaned"


def test_global_worker_deadline_does_not_reset_across_commands(rig):
    worker, port, req = rig
    prepare(rig)
    port.advance(46)
    cleanup = worker.abort_draft(req, deadline_at=BASE + timedelta(seconds=90))
    assert cleanup.status == "cleanup_required" and port.clears == 0


def test_cold_worker_cannot_commit_a_ticket_from_another_worker(rig):
    worker, port, _ = rig
    ticket = prepare(rig)
    other = HybridQQWorker(worker.config, port, clock=lambda: port.now, monotonic_ns=lambda: port.tick)
    assert other.adopt_prepared(ticket, command(rig, WorkerKind.PREPARE)).error_code == "prepared_ticket_mismatch"
    assert other.execute(command(rig, WorkerKind.COMMIT)).error_code == "not_prepared"
    assert port.sends == 0


@pytest.mark.parametrize("kind", [WorkerKind.SELECT_ONLY, WorkerKind.VERIFY_SELECTION_ONLY, WorkerKind.PREPARE])
def test_old_navigation_and_authorized_prepare_paths_are_closed(rig, kind):
    result = rig[0].execute(command(rig, kind))
    assert result.error_code == "hybrid_command_unsupported" and rig[1].events == []


class NativeTransport:
    """Synthetic UIA controls: tests normal methods without any Windows/QQ I/O."""
    def __init__(self, bubbles):
        self.bubbles, self.body, self.active = bubbles, "", 0
        self.inputs = []
        self.phases = self.region_reads = 0
        self.phase = None
        self.root = SimpleNamespace(ClassName="root", ControlTypeName="PaneControl", ProcessId=123,
            GetRuntimeId=lambda: [1, 0], GetChildren=lambda: list(self.controls.values()))
        class Value:
            IsReadOnly = False
            @property
            def Value(pattern):
                return self.body
            def SetValue(pattern, text):
                self.inputs.append(("value", text))
                self.body = text
        self.value = Value()
        def control(name, rid, **kwargs):
            return SimpleNamespace(ClassName=name, ControlTypeName="GroupControl", IsOffscreen=False, GetRuntimeId=lambda: [1, rid],
                GetParentControl=lambda: self.root, GetChildren=lambda: [], ProcessId=123, **kwargs)
        self.scroll = SimpleNamespace(VerticallyScrollable=False, VerticalScrollPercent=-1, VerticalViewSize=100)
        self.controls = {
            "row": control("row selected", 1),
            "header": control("header", 2, Name="Synthetic label"),
            "bubbles": control("ml-root", 3, GetScrollPattern=lambda: self.scroll),
            "composer": control("editor", 4, GetValuePattern=lambda: self.value),
            "send": control("send", 5, GetInvokePattern=lambda: SimpleNamespace(Invoke=lambda: self.inputs.append(("send",)))),
        }
        self.read_hook = None
    @contextmanager
    def read_phase(self, window):
        from messenger_ai.adapters.qq.vm_driver.phase_index import UIAPhaseIndex
        from messenger_ai.adapters.qq.vm_driver.guest_composer import get_uia_pattern
        self.phases += 1
        self.active += 1
        with UIAPhaseIndex(window=window, root=self.root, pattern_loader=get_uia_pattern) as phase:
            self.phase = phase
            try:
                yield phase
            finally:
                self.phase = None
                self.active -= 1
    def _select(self, root, selector):
        tuple(self.phase.controls())
        return [self.controls[selector.name]] if selector.name in self.controls else []
    def _window(self, window):
        return self.root
    def _descendants(self, root):
        return tuple(self.phase.controls())
    def message_tail_is_latest(self, window, selector):
        return True
    def read_composer(self, window, selector):
        return self.body
    def list_bubbles(self, window, selector):
        assert self.active > 0
        if self.read_hook:
            self.read_hook()
        return self.bubbles
    def _guest_scope(self, window):
        return True
    def _composer_focused(self, control, window):
        return True


@pytest.fixture
def native(rig, tmp_path):
    worker, fake, _ = rig
    nav = worker.config.navigation.model_copy(update={"row_selector": worker.config.navigation.row_selector.model_copy(update={"selected_class_name_token": "selected"})})
    config = worker.config.model_copy(update={"navigation": nav})
    now = datetime.now(UTC)
    guard = NavigationGuardState(target=fake.target, run_id="run", session_epoch="session", surface_epoch="surface",
        worker_epoch=str(worker.worker_epoch), observation_epoch="observation", desktop_lease_id="desktop",
        lease_expires_at=now + timedelta(seconds=45), control_revision=3, process_id=123, window_handle=456,
        process_started_at_100ns=PROCESS_START, paused=False, has_owned_draft=False, has_commit_obligation=False,
        published_at=now)
    from pathlib import Path
    path = Path(nav.guard_state_path)
    path.write_text(guard.model_dump_json(), encoding="utf-8")
    transport = NativeTransport(fake.bubbles)
    surface = SimpleNamespace(snapshot=lambda window: ((0, 0, 100, 100), (0, 0, 100, 100), 1.0, PROCESS_START),
        capture=lambda window, bounds: b"\x10\x20\x30\xff" * 100 * 100)
    revoked = threading.Event()
    port = WindowsHybridCurrentChatPort(config, revoked, deadline_at=now + timedelta(seconds=45),
        stop_at=time.monotonic() + 45, transport=transport, surface=surface)
    def region_bubbles(region, digest, *, reads):
        transport.region_reads += 1
        if transport.read_hook:
            transport.read_hook()
        return tuple(transport.bubbles)
    port._region_bubbles = region_bubbles
    return port, transport, fake.target, guard, path, revoked


def test_actual_current_port_fresh_marker_and_metadata_bracket_content_without_navigation(native):
    port, transport, target, *_ = native
    snap = port.snapshot(target)
    assert snap.witness.selected_row_runtime_id_hash == hashlib.sha256(b"1.1").hexdigest()
    assert snap.witness.header_digest == hashlib.sha256(b"Synthetic label").hexdigest()
    assert snap.witness.latest_tail and snap.witness.composer_empty
    assert transport.active == 0 and transport.inputs == []
    transport.read_hook = lambda: setattr(transport.controls["row"], "GetRuntimeId", lambda: [1, 99])
    with pytest.raises(HybridWorkerError, match="target_drift"):
        port.snapshot(target)


def production_structure(transport):
    transport.controls["header"].ClassName = "chat-header__contact-name"
    transport.controls["bubbles"].ClassName = "q-scroll-view scroll-view--hide-scrollbar ml-container ml-root container"
    transport.controls["composer"].ClassName = "ProseMirror is-empty ExEditor-qq-msg-editor ProseMirror-focused"


@pytest.fixture
def cold_native(native, rig, monkeypatch):
    """Real core + Windows port; only the external UI/provider is synthetic."""
    from messenger_ai.adapters.qq.vm_driver import guest_composer
    from messenger_ai.adapters.qq.navigation.windows_backend import selected_runtime_token
    port, transport, target, guard, _, revoked = native
    _, fake, original_request = rig
    production_structure(transport)
    now, tick = datetime.now(UTC), time.monotonic_ns()
    request = original_request.model_copy(update={"requested_at": now, "requested_monotonic_ns": tick,
        "deadline_at": now + timedelta(seconds=30), "deadline_monotonic_ns": tick + 30 * NS})
    worker = HybridQQWorker(port.config, port, revoked=revoked, deadline_at=guard.lease_expires_at,
                            stop_at=time.monotonic() + 40)
    events = []

    def capture(current_target, frame, expectation, *, deadline_at):
        assert current_target == target and worker._prepared is None and transport.active == 0
        assert port.mode == "idle" and deadline_at <= request.deadline_at
        port.discard_fence()
        events.append("profile")
        inner = fake.captured.acquisition.before.model_copy(update={
            "captured_at": datetime.now(UTC), "captured_monotonic_ns": time.monotonic_ns(),
            "selected_row_runtime_id_hash": selected_runtime_token(transport.controls["row"].GetRuntimeId()),
        })
        captured_at, captured_ns = datetime.now(UTC), time.monotonic_ns()
        # Normal profile closure removes the focus class without replacing the editor.
        transport.controls["composer"].ClassName = "ProseMirror is-empty ExEditor-qq-msg-editor"
        after = inner.model_copy(update={"captured_at": datetime.now(UTC), "captured_monotonic_ns": time.monotonic_ns()})
        return fake.captured.model_copy(update={"acquisition": fake.captured.acquisition.model_copy(update={
            "before": inner, "after": after, "profile_captured_at": captured_at,
            "profile_captured_monotonic_ns": captured_ns})})
    port.capture = capture

    set_value = transport.value.SetValue
    def write_value(text):
        assert worker._prepared is not None and worker._prepared.request == request
        assert worker._prepared.ticket is None and not worker._prepared.commit_attempted
        events.append("owned-write")
        set_value(text)
        transport.controls["composer"].ClassName = "ProseMirror ExEditor-qq-msg-editor ProseMirror-focused"
    transport.value.SetValue = write_value

    def clear_value():
        assert worker._prepared is not None and worker._prepared.request == request
        assert transport.body == request.body and not worker._prepared.commit_attempted
        events.append("owned-clear")
        transport.body = ""
        transport.controls["composer"].ClassName = "ProseMirror is-empty ExEditor-qq-msg-editor ProseMirror-focused"
    monkeypatch.setattr(guest_composer, "_select_all_delete", clear_value)
    return worker, port, transport, request, events


@pytest.mark.parametrize("owner_kind", ["request", "ticket"])
def test_native_cold_prepare_write_post_and_abort_survive_empty_class_round_trip(cold_native, owner_kind):
    worker, port, transport, request, events = cold_native
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert worker._prepared.ticket == ticket and worker._prepared.operation_id is None
    assert not worker._prepared.commit_attempted
    proof = worker.prepared_evidence(ticket)
    assert proof.text_hash == request.body_hash and proof.target_identity.participant_signature.startswith("qq-profile-hmac:")
    assert transport.body == request.body and events == ["profile", "owned-write"]
    assert "is-empty" not in transport.controls["composer"].ClassName.split()
    result = worker.abort_draft(request if owner_kind == "request" else ticket, deadline_at=request.deadline_at)
    assert result.status == "cleaned" and worker._prepared is None
    assert transport.body == "" and events == ["profile", "owned-write", "owned-clear"]
    assert "is-empty" in transport.controls["composer"].ClassName.split()
    assert transport.inputs == [("value", request.body)] and port._pending_fence is None


def test_native_cold_prepare_nonempty_entry_cannot_be_hidden_by_empty_css(cold_native):
    worker, _, transport, request, events = cold_native
    transport.body = "foreign draft"
    with pytest.raises(HybridWorkerError, match="identity_composer_not_empty"):
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert worker._prepared is None and transport.body == "foreign draft"
    assert events == transport.inputs == []


def test_native_cold_prepare_wrong_written_body_retains_owner_and_cannot_clear(cold_native):
    worker, _, transport, request, events = cold_native
    set_value = transport.value.SetValue
    def wrong_body(text):
        set_value(text)
        transport.body += " foreign suffix"
    transport.value.SetValue = wrong_body
    with pytest.raises(HybridWorkerError, match="composer_drift") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    result = worker.abort_draft(request, deadline_at=request.deadline_at)
    assert result.status == "cleanup_required" and result.error_code == "needs_manual_cleanup"
    assert transport.body.endswith(" foreign suffix") and events == ["profile", "owned-write"]
    assert transport.inputs == [("value", request.body)]


@pytest.mark.parametrize("action", ["write", "clear"])
def test_native_cold_owned_flow_still_requires_actual_focus_at_each_input(cold_native, action):
    worker, _, transport, request, events = cold_native
    if action == "clear":
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    checks = []
    def focus(*_):
        checks.append(True)
        return len(checks) == 1
    transport._composer_focused = focus
    if action == "write":
        with pytest.raises(HybridWorkerError, match="composer_focus_or_scope_drift") as error:
            worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
        assert error.value.cleanup_required and worker._prepared.ticket is None
        assert events == ["profile"] and transport.inputs == [] and transport.body == ""
    else:
        result = worker.abort_draft(request, deadline_at=request.deadline_at)
        assert result.status == "cleanup_required" and result.error_code == "composer_focus_or_scope_drift"
        assert events == ["profile", "owned-write"] and transport.body == request.body
    assert len(checks) == 2


@pytest.mark.parametrize("change", ["unknown_class", "header_runtime", "messages_runtime", "composer_runtime"])
def test_native_cold_write_instance_drift_still_blocks_ticket_and_owned_cleanup(cold_native, change):
    worker, _, transport, request, events = cold_native
    set_value = transport.value.SetValue
    def drift(text):
        set_value(text)
        if change == "unknown_class":
            transport.controls["composer"].ClassName += " unknown-state"
        else:
            key = change.removesuffix("_runtime")
            key = "bubbles" if key == "messages" else key
            transport.controls[key].GetRuntimeId = lambda: [1, 999]
    transport.value.SetValue = drift
    with pytest.raises(HybridWorkerError, match="target_drift") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    result = worker.abort_draft(request, deadline_at=request.deadline_at)
    assert result.status == "cleanup_required" and result.error_code == "target_drift"
    assert events == ["profile", "owned-write"] and transport.body == request.body
    assert transport.inputs == [("value", request.body)]


def verify_produced_pair(case, before, after):
    from tests.adapters.qq.navigation.test_identity import check
    target, frame, expectation, evidence = case
    frame = frame.model_copy(update={"worker_epoch": before.worker_epoch})
    evidence = evidence.model_copy(update={
        "before": before.model_copy(update={"captured_at": BASE + timedelta(seconds=1), "captured_monotonic_ns": NS}),
        "after": after.model_copy(update={"captured_at": BASE + timedelta(seconds=3), "captured_monotonic_ns": 3 * NS}),
    })
    return check((target, frame, expectation, evidence))


def test_production_navigation_and_worker_share_only_composer_blur_projection(native, case):
    port, transport, target, *_ = native
    production_structure(transport)
    nav_before = port.handler._local_witness(target)
    worker_before = port.snapshot(target).witness
    composer = transport.controls["composer"]
    composer.ClassName = composer.ClassName.removesuffix(" ProseMirror-focused")
    nav_after = port.handler._local_witness(target)
    worker_after = port.snapshot(target).witness
    assert len({w.active_chat_structure_digest for w in (nav_before, nav_after, worker_before, worker_after)}) == 1
    assert verify_produced_pair(case, nav_before, nav_after).verified
    assert verify_produced_pair(case, worker_before, worker_after).verified
    assert transport.inputs == [] and transport.active == 0


@pytest.mark.parametrize("change", ["header_focus", "messages_focus", "unknown_class", "header_runtime",
    "messages_runtime", "composer_runtime", "selected", "header", "draft"])
def test_production_projection_keeps_full_identity_refusals(native, case, change):
    port, transport, target, *_ = native
    production_structure(transport)
    before = port.handler._local_witness(target)
    port.snapshot(target)
    if change.endswith("_focus"):
        key = "bubbles" if change == "messages_focus" else "header"
        transport.controls[key].ClassName += " ProseMirror-focused"
    elif change.endswith("_runtime"):
        key = change.removesuffix("_runtime")
        key = "bubbles" if key == "messages" else key
        transport.controls[key].GetRuntimeId = lambda: [1, 999]
    elif change == "unknown_class":
        transport.controls["composer"].ClassName += " unknown-state"
    else:
        mutate_native(native, change)
    after = port.handler._local_witness(target)
    result = verify_produced_pair(case, before, after)
    assert not result.verified and result.lease is None
    assert result.error_code == ("identity_composer_not_empty" if change == "draft" else "identity_chat_drift")
    with pytest.raises(HybridWorkerError, match="composer_drift" if change == "draft" else "target_drift"):
        port._check_fence(target, port._pending_fence, composer_check=lambda value: value == "")
    assert transport.inputs == [] and transport.active == 0


def test_composer_focus_class_change_during_same_phase_keeps_fresh_fence(native):
    port, transport, target, *_ = native
    production_structure(transport)
    def blur():
        if transport.region_reads == 2:
            control = transport.controls["composer"]
            control.ClassName = control.ClassName.removesuffix(" ProseMirror-focused")
    transport.read_hook = blur
    assert port.snapshot(target).witness.composer_empty
    assert transport.phases == 1 and transport.inputs == []


def test_projection_does_not_replace_real_focus_guard_with_css_marker(native):
    from messenger_ai.adapters.qq.vm_driver.guest_composer import GuestComposerError
    port, transport, target, guard, *_ = native
    production_structure(transport)
    port.begin("owned", deadline_at=guard.lease_expires_at)
    checks = iter((True, False))  # Initially focused; drift at the actual SetValue boundary.
    transport._composer_focused = lambda *_: next(checks)
    with pytest.raises(GuestComposerError, match="composer_focus_or_scope_drift"):
        port.write(target, "owned", before_action=lambda: port.snapshot(target))
    assert transport.inputs == [] and transport.body == ""


def test_projection_does_not_hide_wrong_owned_text_with_stable_css(native):
    port, transport, target, guard, *_ = native
    production_structure(transport)
    port.begin("owned", deadline_at=guard.lease_expires_at)
    transport.body = "owned"
    port.snapshot(target)
    with pytest.raises(HybridWorkerError, match="composer_drift"):
        port.send(target, before_action=lambda: setattr(transport, "body", "foreign"))
    assert transport.inputs == []


def test_current_worker_frame_uses_only_exact_window_no_list_search_or_row_geometry(native):
    port, transport, target, *_ = native
    def forbidden(*_):
        raise AssertionError("current scope frame must not traverse candidate rows")
    transport._select = forbidden
    frame = port.frame(target)
    assert frame.allowed_regions == () and frame.process_id == 123 and frame.window_handle == 456
    assert frame.privacy_mask_applied and frame.png_bytes.startswith(b"\x89PNG")
    assert transport.inputs == []


@pytest.mark.parametrize("field,value", [("paused", True), ("has_owned_draft", True),
    ("has_commit_obligation", True), ("worker_epoch", str(uuid4())), ("process_id", 999)])
def test_actual_idle_port_refuses_unowned_obligation_or_scope_before_read(native, field, value):
    port, transport, target, guard, path, _ = native
    path.write_text(guard.model_copy(update={field: value}).model_dump_json(), encoding="utf-8")
    with pytest.raises(HybridWorkerError):
        port.snapshot(target)
    assert transport.active == 0 and transport.inputs == []


def test_actual_owned_port_never_opens_profile_but_can_read_exact_draft(native):
    port, transport, target, guard, path, _ = native
    path.write_text(guard.model_copy(update={"has_owned_draft": True}).model_dump_json(), encoding="utf-8")
    transport.body = "owned"
    port.begin("owned", deadline_at=guard.lease_expires_at)
    snap = port.snapshot(target)
    assert snap.composer_text == "owned" and snap.witness.composer_empty is False
    with pytest.raises(HybridWorkerError, match="ui_reserved"):
        port.frame(target)
    with pytest.raises(HybridWorkerError, match="ui_reserved"):
        port.capture(target, None, None, deadline_at=guard.lease_expires_at)


def test_actual_value_pattern_input_and_send_require_live_last_boundary(native):
    port, transport, target, guard, _, _ = native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    calls = []
    def write_boundary():
        calls.append("guard")
        port.snapshot(target)
    port.write(target, "owned", before_action=write_boundary)
    assert calls and transport.inputs == [("value", "owned")]
    port.snapshot(target)
    port.send(target, before_action=lambda: calls.append("send-guard"))
    assert transport.inputs[-1] == ("send",) and calls[-1] == "send-guard"


@pytest.mark.parametrize("action", ["write", "send"])
@pytest.mark.parametrize("kind", ["pause", "revoke"])
def test_actual_input_rechecks_parent_pause_or_revoke_after_callback(native, action, kind):
    port, transport, target, guard, path, revoked = native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    def stop():
        if action == "write":
            port.snapshot(target)
        if kind == "pause":
            path.write_text(guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
        else:
            revoked.set()
    with pytest.raises(HybridWorkerError):
        if action == "write":
            port.write(target, "owned", before_action=stop)
        else:
            port.snapshot(target)
            port.send(target, before_action=stop)
    assert transport.inputs == []


def test_actual_abort_revision_exception_requires_pause_and_no_commit_obligation(native):
    port, _, target, guard, path, _ = native
    port.begin("abort", deadline_at=guard.lease_expires_at)
    paused = guard.model_copy(update={"paused": True, "control_revision": 4, "has_owned_draft": True})
    path.write_text(paused.model_dump_json(), encoding="utf-8")
    assert port.allow_paused_cleanup_revision(target, 3, 4)
    assert not port.allow_paused_cleanup_revision(target, 5, 4)
    path.write_text(paused.model_copy(update={"has_commit_obligation": True}).model_dump_json(), encoding="utf-8")
    with pytest.raises(HybridWorkerError, match="committed_cannot_abort"):
        port.allow_paused_cleanup_revision(target, 3, 4)


def mutate_native(native, kind):
    port, transport, _, guard, path, revoked = native
    if kind == "selected":
        transport.controls["row"].GetRuntimeId = lambda: [1, 99]
    elif kind == "header":
        transport.controls["header"].Name = "other label"
    elif kind == "structure":
        transport.controls["composer"].ClassName = "other editor"
    elif kind == "replaced_region":
        old = transport.controls["bubbles"]
        transport.controls["bubbles"] = SimpleNamespace(**{**old.__dict__, "GetRuntimeId": lambda: [1, 88]})
    elif kind == "new_selected_row":
        old = transport.controls["row"]
        transport.controls["newrow"] = SimpleNamespace(**{**old.__dict__, "GetRuntimeId": lambda: [1, 98]})
    elif kind == "tail":
        transport.scroll.VerticallyScrollable, transport.scroll.VerticalScrollPercent = True, 99.9
    elif kind == "new_message":
        transport.bubbles.append(transport.bubbles[0].model_copy(update={"message_key": "m2", "text": "new"}))
    elif kind == "edit_message":
        transport.bubbles[0] = transport.bubbles[0].model_copy(update={"text": "edited, same key and count"})
    elif kind == "direction":
        transport.bubbles[0] = transport.bubbles[0].model_copy(update={"direction": BubbleDirection.OUTBOUND})
    elif kind == "draft":
        transport.body = "human draft"
    elif kind == "control":
        path.write_text(guard.model_copy(update={"control_revision": 4}).model_dump_json(), encoding="utf-8")
    elif kind == "foreground":
        port.handler.surface.snapshot = lambda w: ((1, 0, 100, 100), (0, 0, 100, 100), 1.0, PROCESS_START)
    elif kind == "process":
        port.handler.surface.snapshot = lambda w: ((0, 0, 100, 100), (0, 0, 100, 100), 1.0, PROCESS_START + 1)
    else:
        revoked.set()


_INTERLEAVES = ("selected", "header", "structure", "replaced_region", "new_selected_row", "tail",
                "new_message", "edit_message", "direction", "draft", "control", "foreground", "process", "revoke")


@pytest.mark.parametrize("kind", _INTERLEAVES)
def test_one_phase_snapshot_uncached_closing_fence_rejects_interleaving(native, kind):
    port, transport, target, *_ = native
    def change_at_closing_read():
        if transport.region_reads == 2:
            mutate_native(native, kind)
    transport.read_hook = change_at_closing_read
    with pytest.raises(HybridWorkerError):
        port.snapshot(target)
    assert transport.phases == 1 and transport.active == 0 and transport.inputs == []
    assert port._pending_fence is None


@pytest.mark.parametrize("kind", _INTERLEAVES)
def test_send_final_uncached_fence_detects_changes_without_second_full_phase(native, kind):
    port, transport, target, guard, *_ = native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    transport.body = "owned"
    port.snapshot(target)
    with pytest.raises(HybridWorkerError):
        port.send(target, before_action=lambda: mutate_native(native, kind))
    assert transport.phases == 1 and transport.inputs == [] and port._pending_fence is None


def test_value_pattern_scope_checks_do_not_repeat_full_tree_phases(native):
    port, transport, target, guard, *_ = native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    calls = []
    def before():
        calls.append("full")
        port.snapshot(target)
    port.write(target, "owned", before_action=before)
    assert calls == ["full"] and transport.phases == 1
    assert transport.region_reads > 2  # Every input scope check re-reads current messages.
    assert transport.inputs == [("value", "owned")] and port._pending_fence is None


def test_message_arrival_after_value_write_refuses_readback_without_clearing(native):
    port, transport, target, guard, *_ = native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    original = transport.value.SetValue
    def write_then_message(text):
        original(text)
        mutate_native(native, "new_message")
    transport.value.SetValue = write_then_message
    with pytest.raises(HybridWorkerError, match="stale_context"):
        port.write(target, "owned", before_action=lambda: port.snapshot(target))
    assert transport.inputs == [("value", "owned")] and transport.body == "owned"
    assert port._pending_fence is None


def test_clear_uses_one_original_fence_and_discards_it_after_input(native, monkeypatch):
    from messenger_ai.adapters.qq.vm_driver import guest_composer
    port, transport, target, guard, *_ = native
    port.begin("abort", deadline_at=guard.lease_expires_at)
    transport.body = "owned"
    port.snapshot(target)
    monkeypatch.setattr(guest_composer, "_select_all_delete", lambda: setattr(transport, "body", ""))
    port.clear(target, "owned", before_action=lambda: None)
    assert transport.body == "" and transport.phases == 1 and port._pending_fence is None


def test_input_fence_never_survives_command_or_profile_boundary(native, monkeypatch):
    from messenger_ai.adapters.qq.navigation import profile_verifier
    port, _, target, guard, *_ = native
    frame = port.frame(target)
    port.snapshot(target)
    assert port._pending_fence is not None
    def no_capture(*_, **__):
        assert port._pending_fence is None
        raise HybridWorkerError("test_capture_stopped")
    monkeypatch.setattr(profile_verifier, "capture_profile_acquisition", no_capture)
    with pytest.raises(HybridWorkerError, match="test_capture_stopped"):
        port.capture(target, frame, port.config.expectations[0], deadline_at=guard.lease_expires_at)
    port.snapshot(target)
    port.begin("owned", deadline_at=guard.lease_expires_at)
    assert port._pending_fence is None
    with pytest.raises(HybridWorkerError, match="hybrid_input_fence_missing"):
        port.send(target, before_action=lambda: None)


class CountedUIANode:
    """Counts provider reads, including repeated accesses inside the real decoder."""
    def __init__(self, rid, class_name, *, name="", control_type="GroupControl", children=()):
        self.rid, self.class_name, self.name, self.control_type = rid, class_name, name, control_type
        self.children, self.parent, self.reads = list(children), None, Counter()
        self.hook = None
        for child in self.children:
            child.parent = self

    def read(self, name, value):
        self.reads[name] += 1
        if self.hook:
            self.hook(name, self.reads[name])
        return value

    ClassName = property(lambda self: self.read("ClassName", self.class_name))
    Name = property(lambda self: self.read("Name", self.name))
    ControlTypeName = property(lambda self: self.read("ControlTypeName", self.control_type))
    AutomationId = property(lambda self: self.read("AutomationId", ""))
    ProcessId = property(lambda self: self.read("ProcessId", 123))
    IsOffscreen = property(lambda self: self.read("IsOffscreen", False))

    def GetRuntimeId(self):
        return self.read("GetRuntimeId", [1, self.rid])

    def GetChildren(self):
        return self.read("GetChildren", list(self.children))

    def GetParentControl(self):
        return self.read("GetParentControl", self.parent)


class IndexedNativeTransport(NativeTransport):
    """The production phase index plus actual decoder, over a counted UIA tree."""
    def __init__(self):
        super().__init__([])
        self.leaves, self.message_rows, self.content_nodes = [], [], []
        for index in range(16):
            leaf = CountedUIANode(100 + index * 4, "text", name=f"synthetic-{index}", control_type="TextControl")
            content = CountedUIANode(101 + index * 4, "msg-content-container container--others", children=(leaf,))
            message = CountedUIANode(102 + index * 4, "message", children=(content,))
            self.leaves.append(leaf)
            self.content_nodes.append(content)
            self.message_rows.append(message)
        self.rows = [CountedUIANode(10 + i, "row selected" if i == 0 else "row") for i in range(20)]
        self.controls = {
            "row": self.rows[0],
            "header": CountedUIANode(2, "header", name="Synthetic label"),
            "bubbles": CountedUIANode(3, "ml-root", children=self.message_rows),
            "composer": CountedUIANode(4, "editor"),
            "send": CountedUIANode(5, "send"),
        }
        self.controls["bubbles"].GetScrollPattern = lambda: self.scroll
        self.controls["composer"].GetValuePattern = lambda: self.value
        self.controls["send"].GetInvokePattern = lambda: SimpleNamespace(Invoke=lambda: self.inputs.append(("send",)))
        self.sidebar = CountedUIANode(6, "sidebar", children=self.rows)
        self.chat = CountedUIANode(7, "chat", children=tuple(self.controls[x] for x in ("header", "bubbles", "composer", "send")))
        self.root = CountedUIANode(0, "root", children=(self.sidebar, self.chat))
        self.indices, self.phase = [], None
        self.all_nodes = [self.root, self.sidebar, self.chat, *self.rows,
            *(self.controls[x] for x in ("header", "bubbles", "composer", "send")),
            *self.message_rows, *self.content_nodes, *self.leaves]

    @contextmanager
    def read_phase(self, window):
        from messenger_ai.adapters.qq.vm_driver.phase_index import UIAPhaseIndex
        from messenger_ai.adapters.qq.vm_driver.guest_composer import get_uia_pattern
        self.phases += 1
        self.active += 1
        with UIAPhaseIndex(window=window, root=self.root, pattern_loader=get_uia_pattern) as phase:
            self.phase = phase
            self.indices.append(phase)
            try:
                yield phase
            finally:
                self.phase = None
                self.active -= 1

    def _select(self, root, selector):
        # Selection still materializes the same complete index as production.
        tuple(self.phase.controls())
        return self.rows if selector.name == "row" else [self.controls[selector.name]]

    def _descendants(self, root):
        return tuple(self.phase.controls())


@pytest.fixture
def counted_native(native):
    port, _, target, guard, path, revoked = native
    transport = IndexedNativeTransport()
    port.handler.transport = transport
    del port._region_bubbles  # Use the production decoder, not NativeTransport's value stub.
    return port, transport, target, guard, path, revoked


def test_complete_index_and_decoder_deduplicate_actual_provider_reads(counted_native):
    port, transport, target, *_ = counted_native
    snapshot = port.snapshot(target)
    assert [bubble.text for bubble in snapshot.bubbles] == [f"synthetic-{i}" for i in range(16)]
    assert all(bubble.direction is BubbleDirection.INBOUND for bubble in snapshot.bubbles)
    # Shared parent paths use the completed phase graph; final membership is
    # independently read once, rather than visiting each row's shared ancestors.
    assert sum(node.reads["GetParentControl"] for node in transport.all_nodes) == 0
    for node in (transport.root, transport.sidebar, transport.chat):
        assert node.reads["GetChildren"] == 3  # Opening, adjacency, final independent critical fence.
        assert node.reads["GetRuntimeId"] == 3
    for node in (transport.controls["header"], transport.controls["composer"], transport.rows[0]):
        assert node.reads["ClassName"] == 2  # Complete opening + independent closing.
        assert node.reads["GetRuntimeId"] == 3
    # The real decoder revisits message descendants and each text leaf. Each
    # native value is fetched once in the baseline, once in the new decode,
    # and ClassName once more by the final independent group/identity fence.
    for node in (*transport.message_rows, *transport.content_nodes, *transport.leaves):
        assert node.reads["ClassName"] == 3
        assert node.reads["GetChildren"] == 3  # Opening, fresh decode, then complete outside adjacency.
    for node in (*transport.message_rows, *transport.leaves):
        assert node.reads["ControlTypeName"] == 2
    assert all(node.reads["ControlTypeName"] == 1 for node in transport.content_nodes)
    assert all(node.reads["Name"] == 2 for node in transport.leaves)
    assert all(not phase.active and phase.root is None for phase in transport.indices)
    assert transport.phases == 1 and transport.inputs == []


def test_complete_index_values_are_not_reused_in_next_snapshot(counted_native):
    port, transport, target, *_ = counted_native
    first = port.snapshot(target)
    transport.leaves[-1].name = "fresh next command"
    for node in transport.all_nodes:
        node.reads.clear()
    second = port.snapshot(target)
    assert first.bubbles[-1].text == "synthetic-15" and second.bubbles[-1].text == "fresh next command"
    assert transport.leaves[-1].reads["Name"] == 2
    assert transport.root.reads["GetChildren"] == 3 and transport.phases == 2
    assert all(not phase.active for phase in transport.indices)


@pytest.mark.parametrize("change,at_read", [(change, 1) for change in (
    "body", "direction", "insert", "selected", "header", "group", "membership")]
    + [(change, 2) for change in ("selected", "header", "group", "membership")])
def test_real_decoder_closing_boundary_reads_changes_after_opening_index(counted_native, change, at_read):
    port, transport, target, *_ = counted_native
    def interleave(name, count):
        if name != "Name" or count != at_read:
            return
        if change == "body":
            transport.leaves[-1].name = "same count, edited body"
        elif change == "direction":
            transport.content_nodes[-1].class_name = "msg-content-container container--self"
        elif change == "insert":
            transport.controls["bubbles"].children.append(transport.message_rows[0])
        elif change == "selected":
            transport.rows[0].rid = 999
        elif change == "header":
            transport.controls["header"].name = "different current label"
        elif change == "group":
            transport.chat.class_name = "group-member-list"
        else:
            transport.sidebar.children.append(CountedUIANode(999, "row selected"))
    # Change during either decode: the first must not reuse the opening graph
    # on closing, and the second must not supply cached classes/membership to
    # the final identity check. Old wrappers remain readable in both cases.
    transport.leaves[-1].hook = interleave
    with pytest.raises(HybridWorkerError):
        port.snapshot(target)
    assert transport.inputs == [] and port._pending_fence is None and transport.active == 0


def test_input_message_decode_is_fresh_after_opening_properties(counted_native):
    port, transport, target, guard, *_ = counted_native
    port.begin("owned", deadline_at=guard.lease_expires_at)
    transport.body = "owned"
    port.snapshot(target)
    # Selected RuntimeId is fetched after the full group-marker class scan.
    # Mutating a decoded class here must not reuse those just-read classes.
    def after_classes(name, count):
        if name == "GetRuntimeId":
            transport.content_nodes[-1].class_name = "msg-content-container container--self"
    transport.rows[0].hook = after_classes
    with pytest.raises(HybridWorkerError, match="stale_context"):
        port.send(target, before_action=lambda: None)
    assert transport.inputs == []


def test_fallback_parent_walk_reads_shared_ancestry_once_and_requires_root(native):
    from messenger_ai.adapters.qq.vm_driver.hybrid_worker import _BoundaryReads
    port, *_ = native
    leaves = [CountedUIANode(20 + i, "row") for i in range(20)]
    shared = CountedUIANode(2, "list", children=leaves)
    outer = CountedUIANode(1, "outer", children=(shared,))
    root = CountedUIANode(0, "root", children=(outer,))
    with _BoundaryReads() as reads:
        parents = port._parents(leaves, root, reads)
    assert parents == (shared, outer, root)
    assert shared.reads["GetParentControl"] == outer.reads["GetParentControl"] == 1
    assert all(node.reads["GetParentControl"] == 1 for node in leaves)
    shared.parent = shared
    with _BoundaryReads() as reads, pytest.raises(HybridWorkerError, match="hybrid_parent_scope_unproven"):
        port._parents(leaves, root, reads)


def test_completed_phase_graph_cannot_certify_selected_control_outside_root(counted_native):
    port, transport, target, *_ = counted_native
    transport.rows = [CountedUIANode(999, "row selected")]
    with pytest.raises(HybridWorkerError, match="hybrid_parent_scope_unproven"):
        port.snapshot(target)
    assert all(not phase.active for phase in transport.indices)
    assert transport.inputs == [] and port._pending_fence is None


@pytest.mark.parametrize("opaque", ["missing_body", "media", "nested_message", "bad_direction"])
def test_cached_decoder_keeps_unsupported_content_closed(counted_native, opaque):
    from messenger_ai.adapters.qq.vm_driver.message_decoder import MessageDecodeError
    port, transport, target, *_ = counted_native
    if opaque == "missing_body":
        transport.content_nodes[0].children.clear()
    elif opaque == "media":
        transport.leaves[0].control_type = "ImageControl"
    elif opaque == "nested_message":
        transport.leaves[0].class_name = "message"
    else:
        transport.content_nodes[0].class_name += " container--self"
    with pytest.raises(MessageDecodeError):
        port.snapshot(target)
    assert all(not phase.active for phase in transport.indices)
    assert transport.inputs == [] and port._pending_fence is None


def test_core_preparation_and_commit_snapshot_budget_is_four_then_one(rig):
    worker, port, _ = rig
    ticket, cmd, _ = adopted(rig)
    assert port.events.count("snapshot") == 4
    assert port.profiles == 1
    assert worker.execute(cmd.model_copy(update={"kind": WorkerKind.COMMIT})).status is WorkerStatus.OK
    assert port.events.count("snapshot") == 5 and port.profiles == 1 and port.sends == 1


def test_health_has_no_profile_navigation_or_full_snapshot_and_rejects_owned_state(rig):
    worker, port, _ = rig
    cmd = WorkerCommand(kind=WorkerKind.HEALTH, deadline=BASE + timedelta(seconds=30))
    assert worker.execute(cmd).status is WorkerStatus.OK
    assert port.events == ["begin:health", "health"] and port.profiles == 0
    prepare(rig)
    assert worker.execute(cmd).error_code == "ui_reserved"


def test_actual_health_only_proves_fixed_native_window_without_uia_tree(native):
    port, transport, *_ = native
    def forbidden(*_):
        raise AssertionError("health must not walk tree")
    transport._select = forbidden
    port.health()
    assert transport.phases == 0 and transport.inputs == []


class RetiredUIAElement(RuntimeError):
    hresult = -2147220991


class ComposerDOMNode(CountedUIANode):
    """Provider-shaped element that becomes unreadable when Chromium drops it."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.available, self.process_id, self.runtime_value = True, 123, [1, self.rid]

    def read(self, name, value):
        if not self.available:
            self.reads["retired_access"] += 1
            raise RetiredUIAElement()
        return super().read(name, value)

    ProcessId = property(lambda self: self.read("ProcessId", self.process_id))

    def GetRuntimeId(self):
        return self.read("GetRuntimeId", self.runtime_value)


@pytest.fixture
def cold_text_dom(cold_native, native, monkeypatch):
    """Actual cold core/port/TextPattern/Unicode batching over a changing DOM."""
    from messenger_ai.adapters.qq.vm_driver import guest_composer
    worker, port, transport, old_request, events = cold_native
    composer = transport.controls["composer"]
    body = "PMAI-V2-DRAFT-" + str(uuid4())
    assert len(body) == 50
    request = DraftPreparationRequest.model_validate(old_request.model_copy(update={
        "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest()}).model_dump())
    placeholder = ComposerDOMNode(700, "placeholder", control_type="TextControl")
    placeholder.parent = composer
    outside = ComposerDOMNode(799, "outside")
    outside.parent = transport.root
    transport.controls["outside"] = outside
    dom = SimpleNamespace(children=[placeholder], nodes=[placeholder], after_chunk=None,
        placeholder=placeholder, outside=outside, clears=0, text_reads=0, adjacency_calls=[])
    adjacency = port._outside_adjacency
    def counted_adjacency(fence, reads, *, opening=False):
        dom.adjacency_calls.append(opening)
        return adjacency(fence, reads, opening=opening)
    port._outside_adjacency = counted_adjacency
    composer.GetChildren = lambda: list(dom.children)
    composer.GetValuePattern = lambda: None

    def get_text(length):
        assert length == -1
        dom.text_reads += 1
        return transport.body if transport.body else guest_composer.EMPTY_PLACEHOLDER + "\n"
    composer.GetTextPattern = lambda: SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text))

    def replace_children(*, empty=False):
        for node in dom.nodes:
            node.available = False
        if empty:
            child = ComposerDOMNode(710, "placeholder", control_type="TextControl")
            child.parent = composer
            dom.children = [child]
            dom.nodes = [child]
        else:
            leaf = ComposerDOMNode(702, "", control_type="TextControl")
            group = ComposerDOMNode(701, "paragraph", children=(leaf,))
            group.parent = composer
            dom.children = [group]
            dom.nodes = [group, leaf]
    dom.replace_children = replace_children

    def send_inputs(inputs):
        assert worker._prepared is not None and worker._prepared.request == request
        assert worker._prepared.ticket is None and not worker._prepared.commit_attempted
        assert all(event.type == 1 and event.u.ki.dwFlags & 4 for event in inputs)
        chunk = "".join(chr(event.u.ki.wScan) for event in inputs if not event.u.ki.dwFlags & 2)
        transport.inputs.append(("unicode", chunk))
        transport.body += chunk
        composer.ClassName = "ProseMirror ExEditor-qq-msg-editor ProseMirror-focused"
        if len(transport.inputs) == 1:
            assert len(transport.body) == 32
            replace_children()
        if dom.after_chunk:
            dom.after_chunk(len(transport.inputs))
    monkeypatch.setattr(guest_composer, "_send_inputs", send_inputs)

    def clear():
        assert worker._prepared is not None and worker._prepared.request == request
        assert not worker._prepared.commit_attempted and transport.body == request.body
        dom.clears += 1
        transport.body = ""
        replace_children(empty=True)
        composer.ClassName = "ProseMirror is-empty ExEditor-qq-msg-editor ProseMirror-focused"
        events.append("owned-clear")
    monkeypatch.setattr(guest_composer, "_select_all_delete", clear)
    return worker, port, transport, request, events, dom, native


@pytest.mark.parametrize("owner_kind", ["request", "ticket"])
def test_text_batches_rebuild_only_strict_composer_dom_and_abort_exact_owner(cold_text_dom, owner_kind, monkeypatch):
    from messenger_ai.adapters.qq.vm_driver.phase_index import UIAPhaseIndex
    original_nodes = UIAPhaseIndex.nodes
    def scoped_nodes(index):
        assert index.root is not None, "fresh boundary must not materialize a whole phase tree"
        return original_nodes(index)
    monkeypatch.setattr(UIAPhaseIndex, "nodes", scoped_nodes)
    worker, port, transport, request, events, dom, _ = cold_text_dom
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert transport.inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
    assert transport.body == request.body and worker._prepared.ticket == ticket
    assert not dom.placeholder.available and dom.placeholder.reads["retired_access"] == 0
    assert transport.phases == 4  # The core's original cold snapshots; input scopes add none.
    assert dom.adjacency_calls.count(True) == 4 and dom.adjacency_calls.count(False) == 9
    assert dom.text_reads > 2 and events == ["profile"]
    assert worker._prepared.operation_id is None and not worker._prepared.commit_attempted
    current_nodes = tuple(dom.nodes)
    result = worker.abort_draft(request if owner_kind == "request" else ticket, deadline_at=request.deadline_at)
    assert result.status == "cleaned" and worker._prepared is None and transport.body == ""
    assert dom.clears == 1 and all(not node.available and node.reads["retired_access"] == 0 for node in current_nodes)
    assert events == ["profile", "owned-clear"] and port._pending_fence is None
    assert transport.phases == 6  # Original pre/post-abort snapshots only.
    assert dom.adjacency_calls.count(True) == 6 and dom.adjacency_calls.count(False) == 14
    # No descendant Name/Text/Value pattern was read by a group/scope probe.
    assert all(node.reads["Name"] == 0 for node in (*current_nodes, dom.placeholder, *dom.nodes))
    assert transport.inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]


def test_frozen_outside_element_failure_is_not_treated_as_editable_dom(cold_text_dom):
    worker, _, transport, request, _, dom, _ = cold_text_dom
    dom.after_chunk = lambda _: setattr(dom.outside, "available", False)
    with pytest.raises(HybridWorkerError, match="hybrid_ui_action_failed") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    assert transport.body == request.body[:32] and dom.clears == 0
    diagnostic = error.value.prepare_failure
    assert diagnostic == worker.last_prepare_failure
    assert diagnostic.stage == "write" and diagnostic.input_attempted is True and diagnostic.ticket_known is False
    assert diagnostic.code == "hybrid_ui_action_failed" and diagnostic.deadline_at == request.deadline_at
    assert diagnostic.deadline_monotonic_ns == request.deadline_monotonic_ns
    assert diagnostic.remaining_monotonic_ns == request.deadline_monotonic_ns - diagnostic.observed_monotonic_ns
    assert request.body not in diagnostic.model_dump_json() and request.body_hash not in diagnostic.model_dump_json()
    assert dom.outside.reads["retired_access"] == 1 and dom.placeholder.reads["retired_access"] == 0
    assert transport.inputs == [("unicode", request.body[:32])]


def test_prepare_failure_before_native_input_is_known_false_and_keeps_original_owner(cold_text_dom, monkeypatch):
    worker, port, transport, request, _, _, _ = cold_text_dom
    original = port._check_fence
    def reject(target, fence, **kwargs):
        if port.mode == "owned":
            raise HybridWorkerError("target_drift")
        return original(target, fence, **kwargs)
    monkeypatch.setattr(port, "_check_fence", reject)
    with pytest.raises(HybridWorkerError, match="target_drift") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    diagnostic = error.value.prepare_failure
    assert diagnostic.input_attempted is False and diagnostic.stage == "before_write" and not diagnostic.ticket_known
    assert transport.inputs == [] and transport.body == "" and worker._prepared is not None
    assert getattr(port, "_prepare_input_observer") is None


def test_uninstrumented_port_failure_reports_unknown_input_instead_of_guessing(rig):
    worker, port, request = rig
    def fail():
        raise HybridWorkerError("target_drift")
    port.write_hook = fail
    with pytest.raises(HybridWorkerError, match="target_drift") as error:
        prepare(rig)
    assert error.value.prepare_failure.input_attempted is None and port.writes == 0
    assert error.value.prepare_failure.stage == "before_write"


def test_real_profile_window_failure_preserves_first_code_without_input(rig, report, monkeypatch):
    worker, port, request = rig
    report["acquisition"]["profile_window_handle"] = port.base_frame.window_handle

    def capture(*args, **kwargs):
        assert kwargs["deadline_at"] <= request.deadline_at
        return parse(report)  # The actual strict acquisition parser rejects this HWND.

    monkeypatch.setattr(port, "capture", capture)
    with pytest.raises(HybridWorkerError, match="identity_profile_window_mismatch") as error:
        prepare(rig)

    diagnostic = error.value.prepare_failure
    assert diagnostic.code == "identity_profile_window_mismatch" and diagnostic.stage == "identity"
    assert diagnostic.input_attempted is False and diagnostic.ticket_known is False
    assert diagnostic.requested_at == request.requested_at and diagnostic.deadline_at == request.deadline_at
    assert diagnostic.requested_monotonic_ns == request.requested_monotonic_ns
    assert diagnostic.deadline_monotonic_ns == request.deadline_monotonic_ns
    assert port.writes == port.sends == port.clears == 0 and worker._prepared is None
    assert request.body not in diagnostic.model_dump_json() and request.body_hash not in diagnostic.model_dump_json()


@pytest.mark.parametrize("helper_stage,helper_code", [("header", "ACTIVE_HEADER_AMBIGUOUS"),
                                                      ("profile", "PROFILE_WINDOW_AMBIGUOUS")])
def test_real_capture_helper_error_keeps_finite_metadata_and_original_public_error(rig, monkeypatch, helper_stage, helper_code):
    import json
    import subprocess
    from messenger_ai.adapters.qq.navigation.profile_verifier import capture_profile_acquisition
    from tests.adapters.qq.vm_driver.test_profile_capture import reports, Store
    worker, port, request = rig
    header = reports()[0]
    header.update(process_id=123, window_handle=456, active_header_digest="f"*64,
                  right_region_structure_digest="e"*64)
    calls = []
    def runner(command, **kwargs):
        stage = "header" if "--inspect-guest-current-header" in command else "profile"
        calls.append(stage)
        port.advance(.25)
        assert kwargs["timeout"] <= (request.deadline_at - request.requested_at).total_seconds()
        failed = stage == helper_stage
        payload = {"succeeded": False, "status": helper_code, "private_body": request.body} if failed else header
        return subprocess.CompletedProcess(command, 1 if failed else 0, json.dumps(payload).encode(), b"private stderr")
    def capture(*args, **kwargs):
        return capture_profile_acquisition(worker.config.helper_path, pid=123, hwnd=456,
            vault=worker.config.vault_path, environment_fingerprint="e"*64, selector_pack_version="selectors",
            deadline=kwargs["deadline_at"], runner=runner, secret_store=Store(),
            clock=lambda: port.now, monotonic=lambda: port.tick / NS)
    monkeypatch.setattr(port, "capture", capture)
    with pytest.raises(HybridWorkerError, match="^hybrid_ui_action_failed$") as error:
        prepare(rig)
    value = error.value.prepare_failure
    assert value.code == "hybrid_ui_action_failed" and value.stage == "identity"
    assert value.helper_code == helper_code and value.helper_stage == helper_stage
    assert value.input_attempted is False and value.ticket_known is False
    assert value.deadline_at == request.deadline_at and value.deadline_monotonic_ns == request.deadline_monotonic_ns
    assert calls == (["header"] if helper_stage == "header" else ["header", "profile"])
    assert port.writes == port.sends == port.clears == 0 and worker._prepared is None
    assert request.body not in value.model_dump_json() and "private stderr" not in value.model_dump_json()
    assert not set(type(value).model_fields) & {"diagnostic", "stdout", "stderr", "profile_id_hmac"}


@pytest.mark.parametrize("known_code,plain_stage", [(False, True), (True, False)])
def test_capture_error_unknown_code_or_untrusted_diagnostic_is_sanitized(rig, monkeypatch, known_code, plain_stage):
    from messenger_ai.adapters.qq.vm_driver.profile_identity import ProfileCaptureError
    worker, port, _ = rig
    class UntrustedDict(dict):
        def get(self, *args):
            pytest.fail("must not invoke an arbitrary diagnostic mapping")
    exc = ProfileCaptureError("HELPER_TIMEOUT" if known_code else "private_body",
        {"stage": "profile"} if plain_stage else UntrustedDict(stage="profile", stdout="private_body"))
    def capture(*args, **kwargs):
        raise exc
    monkeypatch.setattr(port, "capture", capture)
    with pytest.raises(HybridWorkerError, match="^hybrid_ui_action_failed$") as error:
        prepare(rig)
    value = error.value.prepare_failure
    assert value.helper_code == ("HELPER_TIMEOUT" if known_code else None) and value.helper_stage is None
    assert "private_body" not in value.model_dump_json() and port.writes == port.sends == 0


@pytest.mark.parametrize("fault", ["group", "class", "type", "pid", "pid_bool", "rid_empty", "rid_bool",
    "rid_long", "duplicate_rid", "outside_alias", "critical_alias", "parent", "parent_pid", "cycle", "count", "depth"])
def test_rebuilt_composer_tree_metadata_and_membership_are_closed(cold_text_dom, fault):
    worker, _, transport, request, _, dom, _ = cold_text_dom
    def change(_):
        group, leaf = dom.nodes
        if fault == "group":
            leaf.class_name = "group-member-list"
        elif fault == "class":
            leaf.class_name = None
        elif fault == "type":
            leaf.control_type = "UnknownControl"
        elif fault in {"pid", "pid_bool"}:
            leaf.process_id = 987 if fault == "pid" else True
        elif fault.startswith("rid_"):
            leaf.runtime_value = {"rid_empty": [], "rid_bool": [True, 702], "rid_long": list(range(65))}[fault]
        elif fault == "duplicate_rid":
            leaf.runtime_value = group.runtime_value
        elif fault == "outside_alias":
            leaf.runtime_value = dom.outside.runtime_value
        elif fault == "critical_alias":
            leaf.runtime_value = transport.controls["header"].GetRuntimeId()
        elif fault == "parent":
            leaf.parent = transport.root
        elif fault == "parent_pid":
            leaf.parent = ComposerDOMNode(701, "paragraph")
            leaf.parent.process_id = 987
        elif fault == "cycle":
            leaf.children = [group]
        elif fault == "count":
            group.children = [ComposerDOMNode(800 + i, "", control_type="TextControl") for i in range(257)]
            for child in group.children:
                child.parent = group
        else:
            tail = leaf
            for i in range(33):
                child = ComposerDOMNode(800 + i, "", control_type="TextControl")
                child.parent = tail
                tail.children = [child]
                tail = child
    dom.after_chunk = change
    with pytest.raises(HybridWorkerError) as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.code == ("identity_conversation_not_direct" if fault == "group" else
        "identity_chat_correlation_unproven" if fault in {"rid_empty", "rid_bool"} else "hybrid_composer_scope_unproven")
    assert error.value.cleanup_required and worker._prepared.ticket is None
    assert transport.body == request.body[:32] and dom.clears == 0
    assert transport.inputs == [("unicode", request.body[:32])]
    assert dom.placeholder.reads["retired_access"] == 0


@pytest.mark.parametrize("critical", ["row", "header", "bubbles", "send"])
def test_initial_graph_cannot_exempt_a_critical_control_as_composer_descendant(native, critical):
    port, transport, target, *_ = native
    composer, node = transport.controls["composer"], transport.controls[critical]
    transport.root.GetChildren = lambda: [value for key, value in transport.controls.items() if key != critical]
    composer.GetChildren = lambda: [node]
    node.GetParentControl = lambda: composer
    with pytest.raises(HybridWorkerError, match="hybrid_composer_scope_unproven"):
        port.snapshot(target)
    assert transport.inputs == [] and port._pending_fence is None


def test_initial_full_tree_group_probe_still_includes_composer_placeholder(cold_text_dom):
    worker, _, transport, request, events, dom, _ = cold_text_dom
    dom.placeholder.class_name = "group-member-list"
    with pytest.raises(HybridWorkerError, match="identity_conversation_not_direct") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert not error.value.cleanup_required and worker._prepared is None
    assert transport.inputs == [] and events == [] and transport.body == ""


@pytest.mark.parametrize("kind", _INTERLEAVES)
def test_dom_replacement_does_not_hide_original_identity_body_or_control_change(cold_text_dom, kind):
    worker, _, transport, request, _, dom, native = cold_text_dom
    dom.after_chunk = lambda _: mutate_native(native, kind)
    with pytest.raises(HybridWorkerError) as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    assert transport.inputs == [("unicode", request.body[:32])] and dom.clears == 0
    assert transport.body == ("human draft" if kind == "draft" else request.body[:32])
    assert dom.placeholder.reads["retired_access"] == 0


@pytest.mark.parametrize("change", ["utc", "monotonic", "revoked"])
def test_composer_probe_checks_remaining_time_and_revoke_at_each_native_read(cold_text_dom, change):
    worker, port, transport, request, _, dom, native = cold_text_dom
    def after_input(_):
        def during_property(name, count):
            if name != "ClassName":
                return
            if change == "utc":
                port.deadline = datetime.now(UTC) - timedelta(microseconds=1)
            elif change == "monotonic":
                port.stop_at = time.monotonic() - 1
            else:
                native[-1].set()
        dom.nodes[-1].hook = during_property
    dom.after_chunk = after_input
    with pytest.raises(HybridWorkerError, match="hybrid_revoked") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    assert transport.inputs == [("unicode", request.body[:32])] and transport.body == request.body[:32]
    assert dom.clears == 0 and dom.placeholder.reads["retired_access"] == 0


@pytest.mark.parametrize("site", ["container", "empty_leaf"])
@pytest.mark.parametrize("change", ["insert", "insert_plain", "delete", "order", "replace", "class_group"])
def test_complete_outside_adjacency_detects_changes_without_walking_new_tree(native, site, change):
    port, transport, target, *_ = native
    first, second = ComposerDOMNode(810, "first"), ComposerDOMNode(811, "second")
    outside = ComposerDOMNode(809, "outside", children=(first, second))
    outside.parent = transport.root
    transport.controls["outside"] = outside
    inserted = ComposerDOMNode(812, "group-member-list" if change == "insert" else "plain")
    parent = outside if site == "container" else first
    # For deletion/order/replacement the previously empty leaf becomes a
    # baseline container; insertion/class mutations retain its empty shape.
    if site == "empty_leaf" and change in {"delete", "order", "replace"}:
        parent.children = [ComposerDOMNode(813, "a"), ComposerDOMNode(814, "b")]
        for child in parent.children:
            child.parent = parent
    def mutate():
        if transport.region_reads != 2:
            return
        if change.startswith("insert"):
            inserted.parent = parent
            parent.children.append(inserted)
        elif change == "delete":
            parent.children.pop()
        elif change == "order":
            parent.children.reverse()
        elif change == "replace":
            inserted.parent = parent
            parent.children[0] = inserted
        else:
            parent.class_name = "group-member-list"
    transport.read_hook = mutate
    with pytest.raises(HybridWorkerError, match="identity_conversation_not_direct" if change == "class_group" else "target_drift"):
        port.snapshot(target)
    assert transport.inputs == [] and port._pending_fence is None and transport.phases == 1
    assert inserted.reads["GetChildren"] == inserted.reads["ClassName"] == inserted.reads["ControlTypeName"] == 0
    assert inserted.reads["GetRuntimeId"] <= 1


def test_outside_adjacency_reads_each_frozen_parent_and_runtime_once_per_boundary(counted_native, monkeypatch):
    from messenger_ai.adapters.qq.vm_driver.hybrid_worker import _BoundaryReads
    from messenger_ai.adapters.qq.vm_driver.phase_index import UIAPhaseIndex
    port, transport, target, *_ = counted_native
    port.snapshot(target)
    fence = port._pending_fence
    expected_parents = tuple(node for node in transport.all_nodes if node is not transport.controls["composer"])
    assert {id(node) for node in fence["outside_parents"]} == {id(node) for node in expected_parents}
    for node in transport.all_nodes:
        node.reads.clear()
    def no_phase(_):
        raise AssertionError("adjacency must not materialize a new phase")
    monkeypatch.setattr(UIAPhaseIndex, "nodes", no_phase)
    with _BoundaryReads() as reads:
        assert port._outside_adjacency(fence, reads) == fence["outside_edges"]
    assert all(node.reads["GetChildren"] == 1 for node in expected_parents)
    assert transport.controls["composer"].reads["GetChildren"] == 0
    assert all(node.reads["GetRuntimeId"] == 1 for node in transport.all_nodes)
    assert all(node.reads["ClassName"] == node.reads["Name"] == node.reads["ControlTypeName"] == 0 for node in transport.all_nodes)
    assert transport.phases == 1


@pytest.mark.parametrize("method", ["GetChildren", "GetRuntimeId"])
def test_outside_adjacency_com_failure_retains_partial_owned_body_without_replay(cold_text_dom, method):
    worker, _, transport, request, _, dom, _ = cold_text_dom
    def after_input(_):
        def failed_read():
            dom.outside.reads["native_failure"] += 1
            raise RetiredUIAElement()
        setattr(dom.outside, method, failed_read)
    dom.after_chunk = after_input
    with pytest.raises(HybridWorkerError, match="hybrid_ui_action_failed") as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None
    assert dom.outside.reads["native_failure"] == 1 and dom.placeholder.reads["retired_access"] == 0
    assert transport.inputs == [("unicode", request.body[:32])] and transport.body == request.body[:32]
    assert dom.clears == 0


@pytest.mark.parametrize("change", ["utc", "monotonic", "revoked", "control", "pause"])
def test_outside_scan_native_calls_and_following_critical_fence_preserve_original_limits(native, change):
    port, transport, target, guard, path, revoked = native
    outside = ComposerDOMNode(830, "outside")
    outside.parent = transport.root
    transport.controls["outside"] = outside
    def during_read(name, count):
        if name != "GetChildren" or count != 2:  # First fresh scan; opening only used its indexed graph.
            return
        if change == "utc":
            port.deadline = datetime.now(UTC) - timedelta(microseconds=1)
        elif change == "monotonic":
            port.stop_at = time.monotonic() - 1
        elif change == "revoked":
            revoked.set()
        elif change == "control":
            path.write_text(guard.model_copy(update={"control_revision": 4}).model_dump_json(), encoding="utf-8")
        else:
            path.write_text(guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
    outside.hook = during_read
    with pytest.raises(HybridWorkerError, match={"control": "target_drift", "pause": "hybrid_paused"}.get(change, "hybrid_revoked")):
        port.snapshot(target)
    assert outside.reads["GetChildren"] == 2 and port._pending_fence is None
    assert transport.inputs == [] and transport.phases == 1


@pytest.mark.parametrize("which", ["parent", "edge"])
def test_trusted_outside_witness_caps_reject_before_followup_native_reads(native, which):
    from messenger_ai.adapters.qq.vm_driver.hybrid_worker import _BoundaryReads
    port, transport, target, *_ = native
    # Root + four ordinary controls are checked parents; composer is a child
    # of root but its own children remain outside this immutable witness.
    extra = [ComposerDOMNode(10000 + i, "extra") for i in range(1020 if which == "parent" else 1019)]
    for i, node in enumerate(extra):
        node.parent = transport.root
        transport.controls[f"extra-{i}"] = node
    if which == "parent":
        with pytest.raises(HybridWorkerError, match="hybrid_group_adjacency_unproven"):
            port.snapshot(target)
        # The unchanged critical opening edges already read these IDs once;
        # cap failure must not add a new child walk or native ID call.
        assert all(node.reads["GetChildren"] == 1 and node.reads["GetRuntimeId"] == 1 for node in extra)
    else:
        port.snapshot(target)
        fence = port._pending_fence
        assert len(fence["outside_parents"]) == 1024 and sum(len(children) for _, children in fence["outside_edges"]) == 1024
        inserted = ComposerDOMNode(20000, "plain")
        inserted.parent = transport.root
        transport.controls["inserted"] = inserted
        with _BoundaryReads() as reads, pytest.raises(HybridWorkerError, match="hybrid_group_adjacency_unproven"):
            port._outside_adjacency(fence, reads)
        assert inserted.reads["GetRuntimeId"] == inserted.reads["GetChildren"] == 0
    assert transport.inputs == [] and transport.phases == 1


def test_abort_outside_insert_after_snapshot_keeps_owned_ticket_and_never_reanchors(cold_text_dom):
    worker, port, transport, request, _, dom, _ = cold_text_dom
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    clear = port.clear
    inserted = ComposerDOMNode(850, "plain")
    inserted.parent = dom.outside
    def insert_then_clear(*args, **kwargs):
        dom.outside.children.append(inserted)
        return clear(*args, **kwargs)
    port.clear = insert_then_clear
    result = worker.abort_draft(ticket, deadline_at=request.deadline_at)
    assert result.status == "cleanup_required" and result.error_code == "target_drift"
    assert worker._prepared.ticket == ticket and transport.body == request.body and dom.clears == 0
    assert transport.inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
    assert inserted.reads["GetChildren"] == inserted.reads["ClassName"] == 0


def _install_decoded_inbound_region(port, transport):
    """Use the production decoder over provider-shaped inbound message nodes."""
    leaf = ComposerDOMNode(860, "text", name="synthetic original inbound", control_type="TextControl")
    content = ComposerDOMNode(861, "msg-content-container container--others", children=(leaf,))
    message = ComposerDOMNode(862, "message", children=(content,))
    message.parent = transport.controls["bubbles"]
    rows = [message]
    transport.controls["bubbles"].GetChildren = lambda: list(rows)
    del port._region_bubbles  # Remove the fixture's value stub; retain the real port and decoder.
    return rows


@pytest.mark.parametrize("change", ["reorder", "badge"])
def test_other_contact_change_stable_before_abort_snapshot_allows_exact_owned_cleanup(cold_text_dom, change):
    worker, port, transport, request, events, dom, native = cold_text_dom
    _install_decoded_inbound_region(port, transport)
    selected = transport.controls["row"]
    other = ComposerDOMNode(870, "row")
    second_other = ComposerDOMNode(871, "row")
    sidebar = ComposerDOMNode(872, "recent-list", children=(selected, other, second_other))
    sidebar.parent = transport.root
    selected.GetParentControl = lambda: sidebar
    root_siblings = tuple(node for name, node in transport.controls.items() if name != "row")
    transport.root.GetChildren = lambda: [sidebar, *root_siblings]
    select = transport._select

    def selected_rows(root, selector):
        if selector.name == "row":
            tuple(transport.phase.controls())
            return list(sidebar.children)
        return select(root, selector)
    transport._select = selected_rows
    target = native[2]
    initial = port.snapshot(target)
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(initial.bubbles))
    record = worker._prepared
    request_json, ticket_json = record.request.model_dump_json(), ticket.model_dump_json()
    original_binding = worker.config.bindings[0].model_dump_json()
    original_inputs = list(transport.inputs)
    phases = transport.phases
    if change == "reorder":
        sidebar.children[:] = [other, selected, second_other]
    else:
        badge = ComposerDOMNode(873, "unread-badge", name="1", control_type="TextControl")
        badge.parent = other
        other.children.append(badge)
    # The new shape is already stable before ABORT takes its first snapshot;
    # the original selected control and all current-chat roles stay unchanged.
    snapshots = []
    snapshot = port.snapshot
    def capture_snapshot(current_target):
        value = snapshot(current_target)
        snapshots.append(value)
        return value
    port.snapshot = capture_snapshot
    result = worker.abort_draft(ticket, deadline_at=request.deadline_at)
    assert result.status == "cleaned" and result.error_code is None
    assert result.reservation_id == request.reservation_id and result.nonce == request.nonce
    assert worker._prepared is None and transport.body == "" and dom.clears == 1
    assert transport.inputs == original_inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
    assert events == ["profile", "owned-clear"] and transport.phases == phases + 2
    assert port._pending_fence is None and transport.active == 0
    assert record.request.model_dump_json() == request_json and ticket.model_dump_json() == ticket_json
    assert worker.config.bindings[0].model_dump_json() == original_binding
    assert len(snapshots) == 2
    for value in snapshots:
        assert value.witness.latest_tail and value.witness.group_marker_count == 0
        assert all(getattr(value.witness, field) == getattr(record.snapshot.witness, field) for field in (
            "selected_row_runtime_id_hash", "header_digest", "active_chat_structure_digest"))
    assert other.reads["Name"] == second_other.reads["Name"] == 0
    if change == "badge":
        assert badge.reads["ClassName"] > 0 and badge.reads["Name"] == 0


def test_current_contact_inbound_stable_before_abort_snapshot_allows_exact_owned_cleanup(cold_text_dom):
    worker, port, transport, request, events, dom, native = cold_text_dom
    rows = _install_decoded_inbound_region(port, transport)
    initial = port.snapshot(native[2])
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(initial.bubbles))
    record = worker._prepared
    request_json, ticket_json = record.request.model_dump_json(), ticket.model_dump_json()
    original_binding = worker.config.bindings[0].model_dump_json()
    original_inputs, phases = list(transport.inputs), transport.phases
    leaf = ComposerDOMNode(880, "text", name="synthetic later inbound", control_type="TextControl")
    content = ComposerDOMNode(881, "msg-content-container container--others", children=(leaf,))
    message = ComposerDOMNode(882, "message", children=(content,))
    message.parent = transport.controls["bubbles"]
    rows.append(message)
    # A genuine decoded inbound has arrived, but the selected/header/role
    # identity and strict native latest-tail proof remain the original ones.
    snapshots = []
    snapshot = port.snapshot
    def capture_snapshot(current_target):
        value = snapshot(current_target)
        snapshots.append(value)
        return value
    port.snapshot = capture_snapshot
    result = worker.abort_draft(ticket, deadline_at=request.deadline_at)
    assert result.status == "cleaned" and result.error_code is None
    assert result.reservation_id == request.reservation_id and result.nonce == request.nonce
    assert worker._prepared is None and transport.body == "" and dom.clears == 1
    assert transport.inputs == original_inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
    assert events == ["profile", "owned-clear"] and transport.phases == phases + 2
    assert port._pending_fence is None and transport.active == 0
    assert record.request.model_dump_json() == request_json and ticket.model_dump_json() == ticket_json
    assert worker.config.bindings[0].model_dump_json() == original_binding
    assert len(snapshots) == 2 and len(record.snapshot.bubbles) == 1
    for value in snapshots:
        assert len(value.bubbles) == 2 and all(b.direction is BubbleDirection.INBOUND for b in value.bubbles)
        assert semantic_sequence_digest(value.bubbles) != ticket.expected_sequence_digest
        assert value.witness.latest_tail and value.witness.group_marker_count == 0
        assert all(getattr(value.witness, field) == getattr(record.snapshot.witness, field) for field in (
            "selected_row_runtime_id_hash", "header_digest", "active_chat_structure_digest"))
    assert leaf.reads["Name"] > 0  # The actual decoder read the new inbound, rather than a value stub.


def fake_cached_adjacency(controls, *, read, max_parents, max_edges):
    """Contract seam only: architecture's separate tests exercise raw COM."""
    assert len(controls) <= max_parents
    result, edges = [], 0
    for parent in controls:
        parent_id = tuple(read(parent.GetRuntimeId))
        children = read(parent.GetChildren)
        edges += len(children)
        assert edges <= max_edges
        result.append((parent_id, tuple(tuple(read(child.GetRuntimeId)) for child in children)))
    return tuple(result)


def test_cache_capability_is_closing_only_and_final_identity_is_independently_fresh(native):
    port, transport, target, *_ = native
    calls = []
    def producer(controls, **kwargs):
        calls.append((controls, kwargs["max_parents"], kwargs["max_edges"]))
        result = fake_cached_adjacency(controls, **kwargs)
        transport.controls["header"].Name = "changed while native cached scan completed"
        return result
    transport.cached_direct_adjacency = producer
    with pytest.raises(HybridWorkerError, match="target_drift"):
        port.snapshot(target)
    assert len(calls) == 1 and calls[0][1:] == (1024, 1024)
    assert transport.controls["composer"] not in calls[0][0]
    assert transport.inputs == [] and transport.phases == 1


def test_real_core_text_and_abort_pipeline_uses_optional_cached_producer(cold_text_dom):
    worker, port, transport, request, _, dom, _ = cold_text_dom
    calls = []
    def producer(controls, **kwargs):
        calls.append(controls)
        return fake_cached_adjacency(controls, **kwargs)
    transport.cached_direct_adjacency = producer
    ticket = worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert len(calls) == 9 and dom.adjacency_calls.count(False) == 9 and transport.phases == 4
    assert worker.abort_draft(ticket, deadline_at=request.deadline_at).status == "cleaned"
    assert len(calls) == 14 and dom.adjacency_calls.count(False) == 14 and transport.phases == 6
    assert dom.placeholder.reads["retired_access"] == 0 and dom.clears == 1 and transport.body == ""
    assert port._pending_fence is None


@pytest.mark.parametrize("error_kind", ["bounded", "native", "revoked"])
def test_cached_producer_failure_cannot_fall_back_or_reanchor(native, error_kind):
    from messenger_ai.adapters.qq.vm_driver.hybrid_worker import _BoundaryReads
    from messenger_ai.adapters.qq.vm_driver.raw_adjacency import RawAdjacencyError
    port, transport, target, _, _, revoked = native
    port.snapshot(target)
    fence, calls = port._pending_fence, []
    def producer(controls, *, read, **kwargs):
        calls.append(1)
        if error_kind == "bounded":
            raise RawAdjacencyError()
        if error_kind == "native":
            raise RetiredUIAElement()
        read(lambda: revoked.set())
        raise AssertionError("revoked read must not return")
    transport.cached_direct_adjacency = producer
    def no_slow_read(*_):
        raise AssertionError("native cache failure must not cause a slow traversal")
    with _BoundaryReads() as reads:
        reads.children = reads.runtime = no_slow_read
        if error_kind == "native":
            with pytest.raises(RetiredUIAElement):
                port._outside_adjacency(fence, reads)
        else:
            with pytest.raises(HybridWorkerError, match="hybrid_group_adjacency_unproven" if error_kind == "bounded" else "hybrid_revoked"):
                port._outside_adjacency(fence, reads)
    assert calls == [1] and transport.inputs == [] and transport.phases == 1


@pytest.mark.parametrize("stage", ["focus", "last_input"])
@pytest.mark.parametrize("change", ["utc", "monotonic", "revoked", "foreground", "control"])
def test_optimized_focus_and_terminal_postproof_preserve_native_fences(cold_text_dom, stage, change):
    worker, port, transport, request, _, dom, native = cold_text_dom
    def invalidate():
        if change == "utc":
            port.deadline = datetime.now(UTC) - timedelta(microseconds=1)
        elif change == "monotonic":
            port.stop_at = time.monotonic() - 1
        elif change == "revoked":
            native[-1].set()
        else:
            mutate_native(native, change)
    if stage == "focus":
        def focus(*_):
            invalidate()
            return True
        transport._composer_focused = focus
    else:
        dom.after_chunk = lambda count: invalidate() if count == 2 else None
    with pytest.raises(HybridWorkerError) as error:
        worker.prepare_draft(request, expected_sequence_digest=semantic_sequence_digest(tuple(transport.bubbles)))
    assert error.value.cleanup_required and worker._prepared.ticket is None and dom.clears == 0
    if stage == "focus":
        assert transport.inputs == [] and transport.body == ""
    else:
        assert transport.inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
        assert transport.body == request.body


def test_paused_health_is_readonly_and_does_not_admit_profile_or_observe(rig, native):
    worker, fake, _ = rig
    fake.paused = True
    command = WorkerCommand(kind=WorkerKind.HEALTH, deadline=BASE + timedelta(seconds=30))
    result = worker.execute(command)
    assert result.status is WorkerStatus.OK and result.evidence["process_started_at_100ns"] == PROCESS_START
    port, transport, target, guard, path, _ = native
    path.write_text(guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
    port.begin("health", deadline_at=guard.lease_expires_at)
    assert port.health()["window_handle"] == 456
    with pytest.raises(HybridWorkerError, match="ui_reserved"):
        port.frame(target)
    port.begin("idle", deadline_at=guard.lease_expires_at)
    with pytest.raises(HybridWorkerError, match="hybrid_paused"):
        port.frame(target)
    assert transport.phases == 0 and transport.inputs == []


def test_actual_paused_health_still_rejects_invalid_foreground(native):
    port, _, _, guard, path, _ = native
    path.write_text(guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
    port.begin("health", deadline_at=guard.lease_expires_at)
    def absent(_):
        raise HybridWorkerError("navigation_qq_not_foreground")
    port.handler.surface.snapshot = absent
    with pytest.raises(HybridWorkerError, match="navigation_qq_not_foreground"):
        port.health()


@pytest.mark.parametrize("code", [
    "composer_has_no_readable_pattern", "send_input_incomplete",
    "composer_focus_or_scope_drift", "composer_focus_drift",
    "composer_scope_rejected", "composer_not_empty",
    "composer_readback_mismatch", "composer_value_not_writable",
    "composer_clear_precondition_failed", "composer_clear_not_verified",
])
def test_worker_preserves_only_defined_guest_composer_codes(code):
    from messenger_ai.adapters.qq.vm_driver.guest_composer import GuestComposerError
    assert HybridQQWorker._error_code(GuestComposerError(code)) == code


@pytest.mark.parametrize("arguments", [
    (), (123,), ("private provider detail",), ("composer_unknown",),
    ("composer_readback_mismatch private detail",),
    ("composer_readback_mismatch", "private provider detail"),
])
def test_worker_never_exports_unknown_guest_composer_exception_text(arguments):
    from messenger_ai.adapters.qq.vm_driver.guest_composer import GuestComposerError
    assert HybridQQWorker._error_code(GuestComposerError(*arguments)) == "hybrid_ui_action_failed"


def test_native_exception_that_looks_like_composer_code_remains_generic():
    class COMError(RuntimeError):
        hresult = -2147220991
        def __str__(self):
            raise AssertionError("native exception text must never be read")
    assert HybridQQWorker._error_code(COMError("composer_not_empty")) == "hybrid_ui_action_failed"


def test_known_readback_failure_keeps_exact_owner_until_abort_without_replay(rig):
    from messenger_ai.adapters.qq.vm_driver.guest_composer import GuestComposerError
    worker, port, request = rig
    original_write = port.write
    def failed_readback(*args, **kwargs):
        original_write(*args, **kwargs)
        raise GuestComposerError("composer_readback_mismatch")
    port.write = failed_readback
    with pytest.raises(HybridWorkerError, match="composer_readback_mismatch") as error:
        prepare(rig)
    assert error.value.cleanup_required and worker._prepared.request == request
    assert worker._prepared.ticket is None and port.body == request.body
    assert port.writes == 1 and port.profiles == 1 and port.sends == 0
    cleanup = worker.abort_draft(request, deadline_at=request.deadline_at)
    assert cleanup.status == "cleaned" and port.body == "" and port.clears == 1
    with pytest.raises(HybridWorkerError, match="reservation_already_used"):
        prepare(rig)
    assert port.writes == 1 and port.profiles == 1 and port.sends == 0


def test_known_partial_input_failure_preserves_foreign_and_unmatched_draft(rig):
    from messenger_ai.adapters.qq.vm_driver.guest_composer import GuestComposerError
    worker, port, request = rig
    def partial_input(target, text, *, before_action):
        assert worker._prepared.request == request
        before_action()
        port.writes += 1
        port.body = text[:5]
        raise GuestComposerError("send_input_incomplete")
    port.write = partial_input
    with pytest.raises(HybridWorkerError, match="send_input_incomplete") as error:
        prepare(rig)
    assert error.value.cleanup_required and worker._prepared.ticket is None
    foreign = request.model_copy(update={"nonce": uuid4()})
    assert worker.abort_draft(foreign, deadline_at=request.deadline_at).status == "not_owned"
    cleanup = worker.abort_draft(request, deadline_at=request.deadline_at)
    assert cleanup.status == "cleanup_required" and cleanup.error_code == "needs_manual_cleanup"
    assert worker._prepared.request == request and port.body == request.body[:5]
    assert port.writes == 1 and port.clears == 0 and port.sends == 0
    with pytest.raises(HybridWorkerError, match="ui_reserved"):
        prepare(rig)
    assert port.writes == 1 and port.profiles == 1


@pytest.fixture
def cached_prepare_native(cold_text_dom):
    """Real core/port/raw cache producer; only native UIA values are synthetic."""
    from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility
    from tests.adapters.qq.vm_driver.test_raw_adjacency import CachedElement, scene
    worker, port, transport, request, events, dom, native = cold_text_dom
    cache = scene()
    state = SimpleNamespace(worker=worker, port=port, transport=transport, request=request,
        events=events, dom=dom, native=native, cache=cache, calls=[], snapshots=[],
        refreshes=[], expected_baselines=[], before_refresh=None, after_cache=None, promotion_owners=[])

    class NativeElement:
        def __init__(self, node):
            self.node = node

        def BuildUpdatedCache(self, cache_request):
            cache.trace.native("BuildUpdatedCache")
            assert cache_request.TreeScope == 3 and cache_request.AutomationElementMode == 0
            assert cache_request.TreeFilter is cache.condition
            assert cache_request.properties == [30000, 30012]
            # The native provider materializes one shallow cache atomically.
            # The actual producer never sees this retained control's getters.
            node = self.node
            rid, class_name = tuple(node.GetRuntimeId()), node.ClassName
            children = tuple(CachedElement(cache.trace, tuple(child.GetRuntimeId()))
                             for child in node.GetChildren())
            return CachedElement(cache.trace, rid, children, class_name=class_name)

    def attach(node):
        node._element = NativeElement(node)
        for child in node.GetChildren():
            attach(child)
    state.attach = attach
    attach(transport.root)

    def producer(controls, *, read, max_parents, max_edges):
        state.calls.append(tuple(controls))
        proof = WindowsUIAQQAccessibility.cached_outside_proof(cache.port, controls,
            read=lambda callback: read(lambda: cache.trace.read(callback)),
            max_parents=max_parents, max_edges=max_edges)
        if state.after_cache:
            state.after_cache()
        return proof
    transport.cached_outside_proof = producer

    roles = {"row": ("row",), "header": ("chat-header__contact-name",),
             "bubbles": ("ml-root",), "composer": ("ProseMirror", "ExEditor-qq-msg-editor"),
             "send": ("send",)}
    selectors = {}
    for role, tokens in roles.items():
        control = transport.controls[role]
        control.AutomationId = "flat-" + role
        patterns = ("TextPattern",) if role == "composer" else ("InvokePattern",) if role == "send" else ()
        selectors[role] = QQSelector(name=role, control_type="GroupControl", class_name_tokens=tokens,
            automation_id=None if role == "row" else control.AutomationId, required_patterns=patterns,
            selected_class_name_token="selected" if role == "row" else None)
    nav = port.config.navigation.model_copy(update={
        "row_selector": selectors["row"], "header_selector": selectors["header"],
        "message_selector": selectors["bubbles"], "composer_selector": selectors["composer"]})
    pack = port.config.selector_pack.model_copy(update={"selectors": tuple(
        selectors.get(item.name, item) for item in port.config.selector_pack.selectors)})
    port.config = worker.config = port.config.model_copy(update={"navigation": nav, "selector_pack": pack})
    port.handler.config = nav

    retain = port.retain_prepare_fence
    def retained(target, owner_request):
        assert worker._prepared is not None and worker._prepared.request == owner_request
        state.promotion_owners.append((owner_request.reservation_id, owner_request.nonce, target))
        return retain(target, owner_request)
    port.retain_prepare_fence = retained
    refresh = port._refresh_prepare_snapshot
    def refreshed(target):
        index = len(state.refreshes) + 1
        state.refreshes.append((port._prepare_owner, port._prepare_fence))
        fence = port._prepare_fence
        state.expected_baselines.append(tuple(fence[name] for name in (
            "properties", "edges", "outside_edges", "sequence")))
        if state.before_refresh:
            state.before_refresh(index)
        return refresh(target)
    port._refresh_prepare_snapshot = refreshed
    snapshot = port.snapshot
    def observed(target):
        phases = transport.phases
        value = snapshot(target)
        state.snapshots.append((port.mode, transport.phases - phases, value))
        return value
    port.snapshot = observed
    return state


def prepare_cached(state):
    return state.worker.prepare_draft(state.request,
        expected_sequence_digest=semantic_sequence_digest(tuple(state.transport.bubbles)))


def test_cached_prepare_real_core_saves_two_phases_but_abort_starts_two_fresh_phases(cached_prepare_native, monkeypatch):
    state = cached_prepare_native
    worker, port, transport, request = state.worker, state.port, state.transport, state.request
    clock_values, generated = [], []
    real_clock = time.monotonic_ns
    def observed_clock():
        value = real_clock()
        clock_values.append(value)
        return value
    monkeypatch.setattr(time, "monotonic_ns", observed_clock)
    build_snapshot = port._snapshot_from_fence
    def freshly_timestamped(*args):
        before = len(clock_values)
        value = build_snapshot(*args)
        assert len(clock_values) == before + 1
        assert value.witness.captured_monotonic_ns == clock_values[-1]
        generated.append(value)
        return value
    port._snapshot_from_fence = freshly_timestamped
    guard_bytes = state.native[-2].read_bytes()
    ticket = prepare_cached(state)
    assert state.promotion_owners == [(request.reservation_id, request.nonce, state.native[2])]
    assert len(state.refreshes) == 2 and all(owner == state.promotion_owners[0] for owner, _ in state.refreshes)
    assert [(mode, phases) for mode, phases, _ in state.snapshots] == [("idle", 1), ("idle", 1), ("owned", 0), ("owned", 0)]
    assert transport.phases == 2
    assert transport.body == request.body and transport.inputs == [("unicode", request.body[:32]), ("unicode", request.body[32:])]
    assert port._prepare_fence is port._pending_fence is port._prepare_owner is None
    first, second = (state.snapshots[i][2] for i in (2, 3))
    assert first.composer_text == "" and second.composer_text == request.body
    assert first.witness.captured_at < second.witness.captured_at
    # Windows can legitimately expose the same QPC tick. Both timestamps
    # must still have been independently collected by the actual builder.
    assert first.witness.captured_monotonic_ns <= second.witness.captured_monotonic_ns
    assert generated == [snapshot for _, _, snapshot in state.snapshots]
    assert first.bubbles is not second.bubbles
    baseline = state.refreshes[0][1]
    assert all(fence is baseline for _, fence in state.refreshes)
    assert state.expected_baselines[0] == state.expected_baselines[1]
    assert tuple(baseline[name] for name in ("properties", "edges", "outside_edges", "sequence")) == state.expected_baselines[0]
    assert baseline["sequence"] == ticket.expected_sequence_digest
    assert worker._prepared.portable.text_hash == request.body_hash
    assert state.native[-2].read_bytes() == guard_bytes
    assert worker.abort_draft(ticket, deadline_at=request.deadline_at).status == "cleaned"
    assert [(mode, phases) for mode, phases, _ in state.snapshots[-2:]] == [("abort", 1), ("abort", 1)]
    assert transport.phases == 4 and state.dom.clears == 1 and transport.body == ""
    assert state.events == ["profile", "owned-clear"] and port._prepare_fence is None
    assert all(request.properties == [30000, 30012] for request in state.cache.trace.requests)
    assert len(state.cache.trace.requests) == len(state.calls)
    assert state.cache.trace.events.count("BuildUpdatedCache") == sum(map(len, state.calls))
    assert state.dom.placeholder.reads["retired_access"] == 0


@pytest.mark.parametrize("role,field", [(role, field) for role in ("header", "send", "composer")
                                      for field in ("ControlTypeName", "AutomationId", "pattern")])
def test_cached_prepare_rechecks_same_id_role_metadata_before_first_input(cached_prepare_native, role, field):
    state = cached_prepare_native
    node = state.transport.controls[role]
    original_id = node.GetRuntimeId()
    def change(index):
        if index != 1:
            return
        if field == "ControlTypeName":
            node.ControlTypeName = "TextControl"
        elif field == "AutomationId":
            node.AutomationId = "changed-same-id"
        elif role == "send":
            node.GetInvokePattern = lambda: None
        elif role == "composer":
            node.GetTextPattern = lambda: None
        else:
            # Header normally has no required pattern. Add its real pattern
            # requirement before opening and remove support at refresh below.
            node.GetInvokePattern = lambda: None
    if role == "header" and field == "pattern":
        node.GetInvokePattern = lambda: SimpleNamespace(Invoke=lambda: pytest.fail("read-only test"))
        nav = state.port.config.navigation.model_copy(update={"header_selector":
            state.port.config.navigation.header_selector.model_copy(update={"required_patterns": ("InvokePattern",)})})
        state.port.config = state.worker.config = state.port.config.model_copy(update={"navigation": nav})
        state.port.handler.config = nav
    state.before_refresh = change
    with pytest.raises(HybridWorkerError) as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert node.GetRuntimeId() == original_id and state.transport.inputs == [] and state.transport.body == ""
    assert state.dom.clears == 0 and state.events == ["profile"]
    assert state.port._prepare_fence is state.port._prepare_owner is state.port._pending_fence is None


@pytest.mark.parametrize("change", ["second_selected", "second_header", "group", "outside_edge"])
def test_cached_prepare_all_original_parent_classes_and_edges_remain_authoritative(cached_prepare_native, change):
    state = cached_prepare_native
    def changed(index):
        if index != 1:
            return
        if change == "second_selected":
            state.dom.outside.class_name = "row selected"
        elif change == "second_header":
            state.dom.outside.class_name = "chat-header__contact-name"
            state.dom.outside.AutomationId = "flat-header"
        elif change == "group":
            state.dom.outside.class_name = "group-member-list"
        else:
            inserted = ComposerDOMNode(990, "unread inserted child")
            inserted.parent = state.dom.outside
            state.dom.outside.children.append(inserted)
    state.before_refresh = changed
    with pytest.raises(HybridWorkerError) as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert state.transport.inputs == [] and state.transport.body == "" and state.dom.clears == 0
    assert state.transport.phases == 2 and state.port._prepare_fence is None


@pytest.mark.parametrize("change", ["focus", "utc", "monotonic", "revoked", "foreground", "control", "process"])
def test_cached_prepare_native_and_original_budget_guards_block_input(cached_prepare_native, change):
    state = cached_prepare_native
    def changed(index):
        if index != 1:
            return
        if change == "focus":
            state.transport._composer_focused = lambda *_: False
        elif change == "utc":
            state.port.deadline = datetime.now(UTC) - timedelta(microseconds=1)
        elif change == "monotonic":
            state.port.stop_at = time.monotonic() - 1
        else:
            mutate_native(state.native, change)
    state.before_refresh = changed
    with pytest.raises(HybridWorkerError) as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert state.transport.inputs == [] and state.transport.body == "" and state.dom.clears == 0
    assert state.port._prepare_fence is state.port._pending_fence is None


@pytest.mark.parametrize("stage", [1, 2])
def test_cached_prepare_reads_current_same_key_message_content_before_and_after_write(cached_prepare_native, stage):
    state = cached_prepare_native
    original = state.transport.bubbles[0]
    def edited(index):
        if index == stage:
            state.transport.bubbles[0] = original.model_copy(update={"text": "edited same message key and count"})
    state.before_refresh = edited
    with pytest.raises(HybridWorkerError, match="stale_context") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert state.transport.bubbles[0].message_key == original.message_key
    assert state.transport.bubbles[0].text_hash != original.text_hash
    assert state.transport.body == ("" if stage == 1 else state.request.body)
    assert state.transport.inputs == ([] if stage == 1 else [("unicode", state.request.body[:32]), ("unicode", state.request.body[32:])])
    assert state.dom.clears == 0 and state.port._prepare_fence is None


def test_cached_prepare_post_write_exact_text_is_current_and_never_replayed(cached_prepare_native):
    state = cached_prepare_native
    state.before_refresh = lambda index: setattr(state.transport, "body", state.request.body + " foreign") if index == 2 else None
    with pytest.raises(HybridWorkerError, match="composer_drift") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert state.transport.body.endswith(" foreign") and state.dom.clears == 0
    assert state.transport.inputs == [("unicode", state.request.body[:32]), ("unicode", state.request.body[32:])]
    assert state.port._prepare_fence is state.port._prepare_owner is None


def test_cached_prepare_final_critical_fence_is_fresh_after_outside_cache(cached_prepare_native):
    state = cached_prepare_native
    def change_after_cache():
        if state.port.mode == "owned":
            state.transport.controls["header"].Name = "changed after completed cached outside proof"
    state.after_cache = change_after_cache
    with pytest.raises(HybridWorkerError, match="target_drift"):
        prepare_cached(state)
    assert state.transport.inputs == [] and state.dom.clears == 0 and state.transport.body == ""
    assert state.port._prepare_fence is None


@pytest.mark.parametrize("boundary", ["owned", "abort", "verify", "health", "idle", "discard", "close"])
def test_cached_prepare_fence_is_discarded_at_each_new_command_boundary(cached_prepare_native, boundary):
    state = cached_prepare_native
    def invalidate(index):
        if index != 1:
            return
        assert state.port._prepare_fence is not None
        if boundary == "discard":
            state.port.discard_fence()
        elif boundary == "close":
            state.port.close()
        else:
            state.port.begin(boundary, deadline_at=state.request.deadline_at)
        assert state.port._prepare_fence is state.port._prepare_owner is state.port._pending_fence is None
    state.before_refresh = invalidate
    with pytest.raises(HybridWorkerError, match="hybrid_input_fence_missing"):
        prepare_cached(state)
    assert state.transport.inputs == [] and state.dom.clears == 0


@pytest.mark.parametrize("constraint", ["no_class", "ancestor_id", "ancestor_type", "no_producer"])
def test_cached_prepare_requires_explicit_flat_selector_capability_or_keeps_legacy_phases(cached_prepare_native, constraint):
    state = cached_prepare_native
    if constraint == "no_producer":
        del state.transport.cached_outside_proof
    else:
        selector = state.port.config.navigation.header_selector
        changes = {"class_name_tokens": ()} if constraint == "no_class" else {"ancestor_automation_ids": ("parent",)} if constraint == "ancestor_id" else {"ancestor_control_types": ("PaneControl",)}
        nav = state.port.config.navigation.model_copy(update={"header_selector": selector.model_copy(update=changes)})
        state.port.config = state.worker.config = state.port.config.model_copy(update={"navigation": nav})
        state.port.handler.config = nav
    ticket = prepare_cached(state)
    assert state.refreshes == [] and state.transport.phases == 4
    assert state.worker.abort_draft(ticket, deadline_at=state.request.deadline_at).status == "cleaned"
    assert state.transport.phases == 6 and state.dom.clears == 1 and state.port._prepare_fence is None


def test_cached_prepare_standalone_owned_begin_cannot_promote_readonly_snapshot(cached_prepare_native):
    state = cached_prepare_native
    state.port.snapshot(state.native[2])
    assert state.port._pending_fence is not None and state.worker._prepared is None
    state.port.begin("owned", deadline_at=state.request.deadline_at)
    assert state.port._prepare_fence is state.port._pending_fence is state.port._prepare_owner is None
    state.port.snapshot(state.native[2])
    assert state.transport.phases == 2 and state.refreshes == []


def test_cached_prepare_com_failure_retains_owner_without_fallback_tree_or_input(cached_prepare_native):
    state = cached_prepare_native
    failures = []
    class CacheCOMError(RuntimeError):
        def __str__(self):
            raise AssertionError("must not read unknown provider exception text")
    def failed(name):
        if state.port.mode == "owned" and name == "BuildUpdatedCache":
            failures.append(name)
            raise CacheCOMError("private UI data")
    state.cache.trace.after_native = failed
    with pytest.raises(HybridWorkerError, match="hybrid_ui_action_failed") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert failures == ["BuildUpdatedCache"] and state.transport.phases == 2
    assert state.transport.inputs == [] and state.transport.body == "" and state.dom.clears == 0
    assert state.port._prepare_fence is state.port._pending_fence is state.port._prepare_owner is None


def test_cached_prepare_refresh_count_is_bounded_to_its_two_synchronous_snapshots(cached_prepare_native):
    state = cached_prepare_native
    def extra_post_read(index):
        if index == 2:
            # Spend the second permitted current observation. The original
            # caller's next observation cannot silently turn into a third one.
            state.port.snapshot(state.native[2])
            assert state.port._prepare_refreshes == 2
    state.before_refresh = extra_post_read
    with pytest.raises(HybridWorkerError, match="hybrid_input_fence_missing") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert len(state.refreshes) == 3 and state.transport.phases == 2
    assert state.transport.body == state.request.body and state.dom.clears == 0
    assert state.port._prepare_fence is state.port._prepare_owner is None


def test_cached_prepare_real_decoder_reads_changed_inbound_after_exact_body_write(cached_prepare_native):
    state = cached_prepare_native
    rows = _install_decoded_inbound_region(state.port, state.transport)
    state.attach(state.transport.controls["bubbles"])
    original = state.port.snapshot(state.native[2])
    leaf = rows[0].children[0].children[0]
    def edited(index):
        if index == 2:
            leaf.name = "fresh actual decoder content after owned write"
    state.before_refresh = edited
    with pytest.raises(HybridWorkerError, match="stale_context") as error:
        state.worker.prepare_draft(state.request,
            expected_sequence_digest=semantic_sequence_digest(original.bubbles))
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert leaf.reads["Name"] > 0 and state.transport.body == state.request.body
    assert state.transport.inputs == [("unicode", state.request.body[:32]), ("unicode", state.request.body[32:])]
    assert state.dom.clears == 0 and state.port._prepare_fence is None


def test_cached_prepare_commit_new_command_uses_full_fresh_phase_and_no_prepare_refresh(cached_prepare_native):
    state = cached_prepare_native
    ticket = prepare_cached(state)
    request = state.request
    prepare_command = WorkerCommand(kind=WorkerKind.PREPARE, binding_id=request.binding_id,
        binding_revision=request.binding_revision, conversation_revision=request.conversation_revision,
        operation_id=uuid4(), text=request.body, segment_ref=f"{request.pacing_plan_id}:{request.segment_index}",
        deadline=request.deadline_at)
    assert state.worker.adopt_prepared(ticket, prepare_command).status is WorkerStatus.OK
    before = len(state.refreshes)
    result = state.worker.execute(prepare_command.model_copy(update={"kind": WorkerKind.COMMIT}))
    assert result.status is WorkerStatus.OK
    assert state.transport.phases == 3 and state.snapshots[-1][1] == 1
    assert len(state.refreshes) == before == 2
    assert state.transport.inputs[-1] == ("send",)
    assert state.port._prepare_fence is state.port._prepare_owner is state.port._pending_fence is None


@pytest.mark.parametrize("method,property_name", [("property", "ControlTypeName"),
    ("property", "AutomationId"), ("property", "ProcessId"), ("pattern", "GetInvokePattern"), ("runtime", None)])
@pytest.mark.parametrize("change", ["utc", "monotonic", "revoked"])
def test_cached_prepare_each_selector_read_stops_immediately_on_original_budget_change(
        cached_prepare_native, monkeypatch, method, property_name, change):
    from messenger_ai.adapters.qq.vm_driver.hybrid_worker import _BoundaryReads
    state = cached_prepare_native
    active, calls, stopped = [False], [], []
    selected_node = state.transport.controls["send" if method == "pattern" else "header"]
    verify = state.port._verify_prepare_selectors
    def verification(*args):
        active[0] = True
        try:
            return verify(*args)
        finally:
            active[0] = False
    state.port._verify_prepare_selectors = verification
    original = getattr(_BoundaryReads, method)
    def read(boundary, node, *args, **kwargs):
        if active[0]:
            calls.append((node, args[0] if args else None))
        value = original(boundary, node, *args, **kwargs)
        if (active[0] and node is selected_node and (property_name is None or args[0] == property_name)
                and not stopped):
            stopped.append(len(calls))
            if change == "utc":
                state.port.deadline = datetime.now(UTC) - timedelta(microseconds=1)
            elif change == "monotonic":
                state.port.stop_at = time.monotonic() - 1
            else:
                state.native[-1].set()
        return value
    monkeypatch.setattr(_BoundaryReads, method, read)
    with pytest.raises(HybridWorkerError, match="hybrid_revoked") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and stopped == [len(calls)]
    assert calls[-1] == (selected_node, property_name)
    assert state.transport.inputs == [] and state.transport.body == "" and state.dom.clears == 0
    assert state.port._prepare_fence is state.port._pending_fence is None


@pytest.mark.parametrize("change", ["selected_id", "header_id", "header_class", "composer_id", "tail", "guard"])
def test_cached_prepare_final_critical_reads_do_not_inherit_selector_boundary_values(cached_prepare_native, change):
    state = cached_prepare_native
    verify = state.port._verify_prepare_selectors
    changes = []
    def verification(*args):
        result = verify(*args)
        if not changes:
            changes.append(change)
            if change.endswith("_id"):
                role = {"selected_id": "row", "header_id": "header", "composer_id": "composer"}[change]
                state.transport.controls[role].GetRuntimeId = lambda: [1, 995]
            elif change == "header_class":
                state.transport.controls["header"].ClassName = "changed-after-selector-check"
            elif change == "tail":
                mutate_native(state.native, "tail")
            else:
                mutate_native(state.native, "control")
        return result
    state.port._verify_prepare_selectors = verification
    with pytest.raises(HybridWorkerError) as error:
        prepare_cached(state)
    assert error.value.cleanup_required and changes == [change]
    assert state.transport.inputs == [] and state.transport.body == "" and state.dom.clears == 0
    assert state.port._prepare_fence is state.port._pending_fence is None


def test_cached_prepare_lost_producer_cannot_fall_back_to_legacy_tree(cached_prepare_native):
    state = cached_prepare_native
    adjacency = state.port._outside_adjacency
    def opening_only(fence, reads, *, opening=False):
        assert opening, "retained prepare proof must not fall back after producer loss"
        return adjacency(fence, reads, opening=opening)
    state.port._outside_adjacency = opening_only
    def loss(index):
        if index == 1:
            del state.transport.cached_outside_proof
    state.before_refresh = loss
    with pytest.raises(HybridWorkerError, match="hybrid_group_adjacency_unproven") as error:
        prepare_cached(state)
    assert error.value.cleanup_required and state.worker._prepared.ticket is None
    assert state.transport.inputs == [] and state.transport.phases == 2 and state.dom.clears == 0
    assert state.port._prepare_fence is None
