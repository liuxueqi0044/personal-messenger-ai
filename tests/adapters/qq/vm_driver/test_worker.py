from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import messenger_ai.adapters.qq.vm_driver.worker as worker_module
from messenger_ai.adapters.qq import BubbleDirection, QQBubble
from messenger_ai.adapters.qq.models import (
    QQCertifiedDirectIdentity,
    QQConversation,
    QQSessionObservedDirectIdentity,
    QQWindow,
)
from messenger_ai.adapters.qq.vm_driver import (
    ConversationSelectionOutcome,
    ConversationSelectionStatus,
    QQVMWorker,
    QQVMWorkerProcess,
    SelectionHandoff,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
    session_identity,
)
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
from messenger_ai.adapters.qq.vm_driver.session_identity import (
    QQSessionCandidateLocator,
    QQSessionIdentityCertifier,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_qq_adapter import _adapter


def _worker():
    _adapter_instance, fake = _adapter()
    pack = _adapter_instance.selector_pack
    binding = next(iter(_adapter_instance.bindings.values()))
    return QQVMWorker(accessibility=fake, selector_pack=pack, bindings=(binding,)), fake, binding.binding_id


class _SelectionActuator:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def select(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


def test_worker_visual_actuator_is_only_the_selection_action_boundary() -> None:
    worker, fake, binding_id = _worker()
    actuator = _SelectionActuator(ConversationSelectionOutcome(
        status=ConversationSelectionStatus.ACTION_ATTEMPTED,
        frame_sha256="a" * 64,
    ))
    worker._selection_actuator = actuator
    binding = worker._bindings[binding_id]
    conversation = fake.conversations[0]

    changed = worker._select_conversation(fake.window, binding, conversation)

    assert changed is True
    assert len(actuator.calls) == 1
    assert actuator.calls[0]["binding_id"] == binding_id
    assert "select" not in fake.calls


def test_worker_visual_rejection_keeps_reply_and_send_path_unreachable() -> None:
    worker, fake, binding_id = _worker()
    worker._selection_actuator = _SelectionActuator(ConversationSelectionOutcome(
        status=ConversationSelectionStatus.REJECTED,
        error_code="visual_target_not_certified",
    ))
    binding = worker._bindings[binding_id]

    with pytest.raises(worker_module.UIAUnavailable, match="visual_target_not_certified"):
        worker._select_conversation(fake.window, binding, fake.conversations[0])

    assert "select" not in fake.calls
    assert fake.composer == ""
    assert "invoke-send" not in fake.calls


def test_worker_rejects_unknown_binding_without_touching_uia() -> None:
    worker, fake, _binding_id = _worker()
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="unknown"))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "unknown_binding"
    assert fake.calls == []


def test_select_only_is_closed_and_requires_a_known_binding() -> None:
    worker, fake, _binding_id = _worker()

    command = WorkerCommand(kind="select_only", binding_id="unknown")
    result = worker.execute(command)

    assert command.kind is WorkerKind.SELECT_ONLY
    assert command.requires_binding() is True
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "unknown_binding"
    assert fake.calls == []


def test_select_only_confirms_target_without_bubbles_composer_or_send() -> None:
    worker, fake, binding_id = _worker()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("select_only touched message or composer state")

    fake.list_bubbles = forbidden
    fake.read_composer = forbidden
    fake.write_composer = forbidden
    fake.invoke_send = forbidden

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.SELECT_ONLY, binding_id=binding_id)
    )

    assert result.status is WorkerStatus.OK
    assert result.evidence["selection_confirmed"] is True
    assert set(result.evidence) == {"selection_confirmed", "target_identity"}
    assert result.evidence["target_identity"] == {
        "binding_id": binding_id,
        "participant_signature": worker._bindings[binding_id].participant_signature,
        "conversation_type": worker._bindings[binding_id].conversation_type,
        "process_id": fake.window.process_id,
        "window_handle": fake.window.window_handle,
    }
    assert not {"bubbles", "read-composer", "write-composer", "invoke-send"} & set(
        fake.calls
    )


def test_select_only_session_proof_returns_only_bounded_identity_fields() -> None:
    worker, fake, binding_id = _worker()
    proof = QQSessionObservedDirectIdentity(
        binding_id=binding_id,
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version=worker._selectors.client_version,
        selector_pack_version=worker._selectors.fixture_suite_version,
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=fake.window.process_id,
        window_handle=fake.window.window_handle,
        process_started_at_100ns=123,
        vm_environment_fingerprint=worker._selectors.environment_fingerprint,
        selected_row_runtime_id_hash="b" * 64,
        header_digest="c" * 64,
    )
    resolve_calls = []
    worker._resolve = lambda command, binding, current_reader: (
        resolve_calls.append((command, binding, current_reader))
        or (fake.window, fake.conversations[0], proof, None)
    )

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.SELECT_ONLY, binding_id=binding_id)
    )

    assert result.status is WorkerStatus.OK
    assert len(resolve_calls) == 1
    assert resolve_calls[0][2] is None
    assert result.evidence == {
        "selection_confirmed": True,
        "target_identity": {
            "binding_id": binding_id,
            "conversation_type": "direct",
            "process_id": fake.window.process_id,
            "window_handle": fake.window.window_handle,
            "selected_row_runtime_id_hash": "b" * 64,
            "header_digest": "c" * 64,
            "group_marker_count": 0,
        },
    }


def test_verify_selection_only_is_read_only_even_when_selection_is_unsettled() -> None:
    worker, fake, binding_id = _worker()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verification-only command attempted an action")

    fake.select_conversation = forbidden
    worker._selection_actuator = forbidden
    fake.confirm_conversation_selected = lambda *_args: None
    fake.list_bubbles = forbidden
    fake.read_composer = forbidden
    fake.write_composer = forbidden
    fake.invoke_send = forbidden

    result = worker.execute(
        WorkerCommand(
            kind=WorkerKind.VERIFY_SELECTION_ONLY,
            binding_id=binding_id,
        )
    )

    assert result.status is WorkerStatus.OK
    assert result.evidence["selection_confirmed"] is True
    assert not {
        "select",
        "bubbles",
        "read-composer",
        "write-composer",
        "invoke-send",
    } & set(fake.calls)


def test_verify_selection_only_fails_without_performing_selection() -> None:
    worker, fake, binding_id = _worker()
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("must not select")
    )
    fake.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(
        worker_module.UIAUnavailable(
            "conversation selection could not be independently confirmed"
        )
    )
    worker._SELECTION_SETTLE_SECONDS = 0

    result = worker.execute(
        WorkerCommand(
            kind=WorkerKind.VERIFY_SELECTION_ONLY,
            binding_id=binding_id,
        )
    )

    assert result.status is WorkerStatus.FAILED_SAFE
    assert "select" not in fake.calls


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (
            ConversationSelectionStatus.ACTION_ATTEMPTED,
            "selection_process_refresh_required",
        ),
        (ConversationSelectionStatus.REJECTED, "visual_target_not_certified"),
    ],
)
def test_select_only_visual_failure_is_closed_and_keeps_only_safe_summary(
    status, expected_error, capsys
) -> None:
    worker, fake, binding_id = _worker()
    worker._run_id = "visual-summary-test"
    actuator = _SelectionActuator(
        ConversationSelectionOutcome(
            status=status,
            error_code=(
                "visual_target_not_certified"
                if status is ConversationSelectionStatus.REJECTED
                else None
            ),
            frame_sha256="d" * 64,
            model="deepseek-v4-flash-vision-exp",
            latency_ms=37,
            visual_decision="match",
            visual_reason="exact_label",
            visual_confidence=0.97,
            normalized_label_match=True,
        )
    )
    actuator.raw_png = b"sensitive-png"
    actuator.target_label = "sensitive-target-label"
    actuator.api_key = "sensitive-api-key"
    worker._selection_actuator = actuator

    def forbidden(*_args, **_kwargs):
        raise AssertionError("visual failure continued into message state")

    fake.list_bubbles = forbidden
    fake.read_composer = forbidden
    fake.write_composer = forbidden
    fake.invoke_send = forbidden
    fake.confirm_conversation_selected = forbidden
    worker._confirm_identity = forbidden

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.SELECT_ONLY, binding_id=binding_id)
    )
    logs = capsys.readouterr().out

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == expected_error
    assert {
        key: result.evidence[key]
        for key in (
            "frame_sha256",
            "model",
            "latency_ms",
            "visual_decision",
            "visual_reason",
            "visual_confidence",
            "normalized_label_match",
        )
    } == {
        "frame_sha256": "d" * 64,
        "model": "deepseek-v4-flash-vision-exp",
        "latency_ms": 37,
        "visual_decision": "match",
        "visual_reason": "exact_label",
        "visual_confidence": 0.97,
        "normalized_label_match": True,
    }
    serialized = json.dumps(result.model_dump(mode="json")) + logs
    assert "sensitive-png" not in serialized
    assert "sensitive-target-label" not in serialized
    assert "sensitive-api-key" not in serialized
    assert not {"bubbles", "read-composer", "write-composer", "invoke-send"} & set(
        fake.calls
    )


def test_observe_uses_two_fresh_read_phases_and_emits_bounded_stages(capsys) -> None:
    worker, fake, binding_id = _worker()
    worker._run_id = "run-phase-test"
    active_phase = 0
    opened = 0
    bubble_phase = None

    @contextmanager
    def read_phase(_window):
        nonlocal active_phase, opened
        assert active_phase == 0
        opened += 1
        active_phase = opened
        try:
            yield object()
        finally:
            active_phase = 0

    original_conversations = fake.list_conversations
    original_select = fake.select_conversation
    original_bubbles = fake.list_bubbles
    fake.read_phase = read_phase
    foreground_phases: list[int] = []
    fake.ensure_guest_foreground = lambda _window: foreground_phases.append(active_phase)
    fake.list_conversations = lambda window, selector: (
        (_ for _ in ()).throw(AssertionError("conversation read outside phase"))
        if active_phase == 0 else original_conversations(window, selector)
    )
    fake.select_conversation = lambda window, conversation, selector: (
        (_ for _ in ()).throw(AssertionError("selection retained a read phase"))
        if active_phase != 0 else original_select(window, conversation, selector)
    )
    fake.confirm_conversation_selected = lambda *_args: (
        None if active_phase == 2 else
        (_ for _ in ()).throw(AssertionError("confirmation outside fresh phase"))
    )

    def bubbles(window, selector):
        nonlocal bubble_phase
        bubble_phase = active_phase
        return original_bubbles(window, selector)

    fake.list_bubbles = bubbles
    result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )

    assert result.status is WorkerStatus.OK
    assert foreground_phases == [0]
    assert opened == 2
    assert bubble_phase == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(item["schema"] == "pmai-qq-worker-stage-event-v1" for item in events)
    assert all(item["run_id"] == "run-phase-test" for item in events)
    assert {item["stage"] for item in events} >= {
        "target_window", "discovery_phase", "select",
        "guest_foreground", "verification_phase", "bubbles",
    }
    assert "anchor" not in json.dumps(events)


@pytest.mark.parametrize("kind", [WorkerKind.OBSERVE, WorkerKind.PREPARE])
def test_selection_action_requires_fresh_process_before_verification_or_composer(
    kind,
) -> None:
    worker, fake, binding_id = _worker()
    opened = 0

    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()

    fake.read_phase = read_phase
    fake.select_conversation = lambda *_args: True
    fake.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(
        AssertionError("selection action must end before verification")
    )
    fake.list_bubbles = lambda *_args: (_ for _ in ()).throw(
        AssertionError("selection action must end before current reads")
    )
    fake.read_composer = lambda *_args: (_ for _ in ()).throw(
        AssertionError("selection action must end before composer access")
    )
    fake.write_composer = lambda *_args: (_ for _ in ()).throw(
        AssertionError("selection action must end before composer access")
    )
    command = (
        WorkerCommand(kind=kind, binding_id=binding_id)
        if kind is WorkerKind.OBSERVE
        else WorkerCommand(
            kind=kind,
            binding_id=binding_id,
            operation_id=uuid4(),
            segment_ref="plan:0",
            text="draft",
        )
    )

    result = worker.execute(command)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_process_refresh_required"
    assert opened == 1
    assert worker._reservation is None
    assert worker._prepared == {}


def test_preselected_row_continues_through_fresh_verification_phase() -> None:
    worker, fake, binding_id = _worker()
    opened = 0
    confirmed = 0

    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()

    def confirm(*_args):
        nonlocal confirmed
        confirmed += 1

    fake.read_phase = read_phase
    fake.select_conversation = lambda *_args: False
    fake.confirm_conversation_selected = confirm

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )

    assert result.status is WorkerStatus.OK
    assert opened == 2
    assert confirmed == 1
    assert "bubbles" in fake.calls



def test_selection_confirmation_retries_in_fresh_phase_then_certifies_and_reads() -> None:
    worker, fake, binding_id = _worker()
    phases: list[int] = []
    active = 0
    @contextmanager
    def read_phase(_window):
        nonlocal active
        active = len(phases) + 1; phases.append(active)
        try: yield object()
        finally: active = 0
    fake.read_phase = read_phase
    calls = 0
    def confirm(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise worker_module.UIAUnavailable("conversation selection could not be independently confirmed")
    fake.confirm_conversation_selected = confirm
    certified_phase: list[int] = []
    worker._confirm_identity = lambda *_args: certified_phase.append(active) or None
    current_phase: list[int] = []
    binding = worker._bindings[binding_id]
    _proof, current = worker._confirm_after_selection(
        command=WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id), binding=binding,
        window=worker._target_window(), conversation=fake.conversations[0], read_phase=read_phase,
        current_reader=lambda *_args: current_phase.append(active) or "current",
    )
    assert current == "current"
    assert phases == [1, 2]
    assert certified_phase == [2] and current_phase == [2]


def test_selection_confirmation_exhaustion_fails_closed_without_certifier(monkeypatch) -> None:
    worker, fake, binding_id = _worker()
    monkeypatch.setattr(worker, "_SELECTION_SETTLE_SECONDS", 0.0)
    opened = 0
    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()
    fake.read_phase = read_phase
    fake.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(worker_module.UIAUnavailable("conversation selection could not be independently confirmed"))
    worker._confirm_identity = lambda *_args: (_ for _ in ()).throw(AssertionError("must not certify"))
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id))
    assert result.status is WorkerStatus.FAILED_SAFE and opened == 1


def test_selection_confirmation_does_not_open_a_phase_past_command_deadline() -> None:
    worker, fake, binding_id = _worker()
    opened = 0
    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()
    fake.read_phase = read_phase
    fake.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(worker_module.UIAUnavailable("conversation selection could not be independently confirmed"))
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id, deadline=datetime.now(UTC) + timedelta(milliseconds=15))
    result = worker.execute(command)
    assert result.status is WorkerStatus.FAILED_SAFE
    assert opened == 2


def test_selection_confirmation_rejects_result_that_finishes_past_deadline() -> None:
    worker, fake, binding_id = _worker()

    @contextmanager
    def read_phase(_window):
        yield object()

    fake.read_phase = read_phase
    fake.confirm_conversation_selected = lambda *_args: None
    worker._confirm_identity = lambda *_args: None
    binding = worker._bindings[binding_id]

    with pytest.raises(RuntimeError, match="deadline_expired"):
        worker._confirm_after_selection(
            command=WorkerCommand(
                kind=WorkerKind.OBSERVE,
                binding_id=binding_id,
                deadline=datetime.now(UTC) + timedelta(milliseconds=5),
            ),
            binding=binding,
            window=worker._target_window(),
            conversation=fake.conversations[0],
            read_phase=read_phase,
            current_reader=lambda *_args: time.sleep(0.01),
        )


def test_identity_uia_error_is_not_retried_as_selection_settle() -> None:
    worker, fake, binding_id = _worker()
    opened = 0
    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()
    fake.read_phase = read_phase
    fake.confirm_conversation_selected = lambda *_args: None
    worker._confirm_identity = lambda *_args: (_ for _ in ()).throw(worker_module.UIAUnavailable("conversation selection could not be independently confirmed"))
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert opened == 2

def test_other_uia_confirmation_failure_is_not_retried() -> None:
    worker, fake, binding_id = _worker()
    opened = 0
    @contextmanager
    def read_phase(_window):
        nonlocal opened
        opened += 1
        yield object()
    fake.read_phase = read_phase
    fake.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(worker_module.UIAUnavailable("QQ window no longer matches the worker target"))
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id))
    assert result.status is WorkerStatus.FAILED_SAFE and opened == 2

def test_prepare_commit_verify_preserves_main_operation_id() -> None:
    worker, fake, binding_id = _worker()
    fake.bubbles = [QQBubble(conversation_internal_id="qq-conv-1", message_key="anchor", direction=BubbleDirection.INBOUND, text="anchor", observed_at=datetime.now(UTC), tree_digest=fake.digest)]
    operation_id = uuid4()
    prepared = worker.execute(WorkerCommand(kind=WorkerKind.PREPARE, binding_id=binding_id, operation_id=operation_id, segment_ref="plan-1:0", text="固定测试文字", binding_revision=2, conversation_revision=3))
    assert prepared.status is WorkerStatus.OK
    assert prepared.operation_id == operation_id
    committed = worker.execute(WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id, binding_revision=2, conversation_revision=3))
    assert committed.status is WorkerStatus.OK
    verified = worker.execute(WorkerCommand(kind=WorkerKind.VERIFY, binding_id=binding_id, operation_id=operation_id, binding_revision=2, conversation_revision=3))
    assert verified.status is WorkerStatus.OK
    assert verified.operation_id == operation_id


def test_fresh_worker_verifies_from_content_free_prepared_evidence() -> None:
    worker, fake, binding_id = _worker()
    fake.bubbles = [QQBubble(
        conversation_internal_id="qq-conv-1",
        message_key="anchor",
        direction=BubbleDirection.INBOUND,
        text="private inbound text",
        observed_at=datetime.now(UTC),
        tree_digest=fake.digest,
    )]
    operation_id = uuid4()
    prepared = worker.execute(WorkerCommand(
        kind=WorkerKind.PREPARE,
        binding_id=binding_id,
        operation_id=operation_id,
        segment_ref="fresh-verify:0",
        text="private reply text",
    ))
    assert prepared.status is WorkerStatus.OK
    portable = prepared.evidence["prepared_evidence"]
    serialized = __import__("json").dumps(portable, ensure_ascii=False)
    assert "private inbound text" not in serialized
    assert "private reply text" not in serialized

    committed = worker.execute(WorkerCommand(
        kind=WorkerKind.COMMIT,
        binding_id=binding_id,
        operation_id=operation_id,
    ))
    assert committed.status is WorkerStatus.OK
    successor = QQVMWorker(
        accessibility=fake,
        selector_pack=worker._selectors,
        bindings=tuple(worker._bindings.values()),
    )
    verified = successor.execute(WorkerCommand(
        kind=WorkerKind.VERIFY,
        binding_id=binding_id,
        operation_id=operation_id,
        prepared_evidence=portable,
    ))

    assert verified.status is WorkerStatus.OK
    assert verified.worker_epoch != prepared.worker_epoch
    assert fake.calls.count("invoke-send") == 1


def test_preexisting_draft_is_never_overwritten() -> None:
    worker, fake, binding_id = _worker()
    fake.composer = "人工草稿"
    result = worker.execute(WorkerCommand(kind=WorkerKind.PREPARE, binding_id=binding_id, operation_id=uuid4(), segment_ref="p:0", text="不应覆盖"))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "composer_not_empty"
    assert fake.composer == "人工草稿"


def test_verify_requires_a_new_unique_outbound_bubble() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    fake.bubbles = [QQBubble(conversation_internal_id="visible-current-conversation", message_key="old", direction=BubbleDirection.OUTBOUND, text="相同文字", observed_at=datetime.now(UTC), tree_digest=fake.digest)]
    assert worker.execute(WorkerCommand(kind=WorkerKind.PREPARE, binding_id=binding_id, operation_id=operation_id, segment_ref="p:0", text="相同文字")).status is WorkerStatus.OK
    fake.emit_receipt = False
    worker.execute(WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id))
    result = worker.execute(WorkerCommand(kind=WorkerKind.VERIFY, binding_id=binding_id, operation_id=operation_id))
    assert result.status is WorkerStatus.UNCERTAIN


def test_selector_validation_requires_the_full_v5_pack() -> None:
    worker, _fake, _binding_id = _worker()
    validate_guest_selector_pack(worker._selectors)


class Certifier:
    def __init__(self, signature: str):
        self.signature = signature
        self.calls = 0

    def certify_current(self, window, candidate):
        self.calls += 1
        return QQCertifiedDirectIdentity(
            profile_id_hmac=self.signature.removeprefix("qq-profile-hmac:"),
            conversation_type="direct",
            client_version="9.9.26.44343",
            selector_pack_version="qq-fake-v1",
            group_marker_probe_complete=True,
            group_marker_count=0,
            process_id=window.process_id,
            window_handle=window.window_handle,
            header_digest="a" * 64,
            right_region_digest="b" * 64,
        )


class Locator:
    def locate_candidates(self, binding, visible):
        return [item for item in visible if item.internal_id == binding.platform_conversation_id]


def test_production_scope_requires_injected_current_identity_certifier() -> None:
    adapter, fake = _adapter()
    binding = next(iter(adapter.bindings.values())).model_copy(update={
        "authorization_scope": "all_direct_including_temporary",
        "conversation_type": "direct",
        "participant_signature": "qq-profile-hmac:" + "c" * 64,
    })
    with pytest.raises(ValueError, match="identity certifier"):
        QQVMWorker(accessibility=fake, selector_pack=adapter.selector_pack, bindings=(binding,))

    certifier = Certifier(binding.participant_signature)
    worker = QQVMWorker(accessibility=fake, selector_pack=adapter.selector_pack,
                        bindings=(binding,), identity_certifier=certifier,
                        candidate_locator=Locator())
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding.binding_id))
    assert result.status is WorkerStatus.OK
    assert certifier.calls == 1


def test_session_worker_uses_bootstrapped_window_when_qq_has_an_auxiliary_window(
    monkeypatch,
) -> None:
    adapter, fake = _adapter()
    target = fake.window
    auxiliary = QQWindow(
        process_id=target.process_id,
        window_handle=target.window_handle + 1,
        class_name=target.class_name,
        title="QQ auxiliary announcement",
    )
    windows = [auxiliary, target]
    fake.find_main_windows = lambda _selector: list(windows)
    fake._window = lambda _window: object()
    fake._descendants = lambda _root: []
    proof = QQSessionObservedDirectIdentity(
        binding_id="session-contact-1",
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version=adapter.selector_pack.client_version,
        selector_pack_version=adapter.selector_pack.fixture_suite_version,
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=target.process_id,
        window_handle=target.window_handle,
        process_started_at_100ns=123,
        vm_environment_fingerprint=adapter.selector_pack.environment_fingerprint,
        selected_row_runtime_id_hash="b" * 64,
        header_digest="c" * 64,
    )
    fake.conversations = [QQConversation(
        internal_id="runtime:" + proof.selected_row_runtime_id_hash,
        participant_signature="uncertified:session-row",
        tree_digest=fake.digest,
    )]
    binding = next(iter(adapter.bindings.values())).model_copy(update={
        "binding_id": proof.binding_id,
        "platform_conversation_id": fake.conversations[0].internal_id,
        "participant_signature": proof.participant_signature,
        "conversation_type": "direct",
        "authorization_scope": "all_direct_including_temporary",
    })
    monkeypatch.setattr(
        session_identity, "_process_started_at_100ns", lambda _pid: 123
    )
    monkeypatch.setattr(
        session_identity, "_header_digest", lambda *_args: proof.header_digest
    )
    certifier = QQSessionIdentityCertifier(
        accessibility=fake, selector_pack=adapter.selector_pack, evidence=(proof,)
    )
    worker = QQVMWorker(
        accessibility=fake,
        selector_pack=adapter.selector_pack,
        bindings=(binding,),
        identity_certifier=certifier,
        candidate_locator=QQSessionCandidateLocator((proof,)),
        expected_window=certifier.window_scope,
        window_validator=certifier.validate_window,
    )

    health = worker.execute(WorkerCommand(kind=WorkerKind.HEALTH))
    observed = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding.binding_id)
    )
    assert health.status is WorkerStatus.OK
    assert health.evidence["window_handle"] == target.window_handle
    assert observed.status is WorkerStatus.OK

    windows[:] = [auxiliary]
    missing = worker.execute(WorkerCommand(kind=WorkerKind.HEALTH))
    assert missing.status is WorkerStatus.UNAVAILABLE
    assert missing.error_code == "qq_session_window_unavailable"


def test_reserved_operation_cannot_cross_binding() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    prepared = worker.execute(WorkerCommand(kind=WorkerKind.PREPARE, binding_id=binding_id,
        operation_id=operation_id, segment_ref="p:0", text="draft"))
    assert prepared.status is WorkerStatus.OK
    result = worker.execute(WorkerCommand(kind=WorkerKind.COMMIT, binding_id=None,
        operation_id=operation_id))
    assert result.error_code == "operation_binding_mismatch"


def test_child_initialization_failure_is_reported_through_health(monkeypatch) -> None:
    _existing, _fake, _binding_id = _worker()
    binding = next(iter(_existing._bindings.values()))
    command = WorkerCommand(kind=WorkerKind.HEALTH)

    class Connection:
        sent = None
        def recv(self): return command.model_dump(mode="json")
        def send(self, value): self.sent = value

    connection = Connection()
    monkeypatch.setattr(
        worker_module,
        "WindowsUIAQQAccessibility",
        lambda: (_ for _ in ()).throw(
            RuntimeError("guest machine identity could not be certified")
        ),
    )
    worker_module._serve(connection, _existing._selectors, (binding,))
    result = worker_module.WorkerResult.model_validate(connection.sent)
    assert result.kind is WorkerKind.HEALTH
    assert result.status is WorkerStatus.UNAVAILABLE
    assert result.error_code == "guest_machine_identity_unavailable"
    assert result.evidence["failure_stage"] == "initialize"


def test_process_serializes_complete_pipe_request_response_exchanges() -> None:
    class Process:
        terminated = False
        def is_alive(self): return True
        def terminate(self): self.terminated = True
        def join(self, _timeout): pass

    class Pipe:
        current = None
        overlapped = False
        closed = False
        def send(self, value):
            if self.current is not None:
                self.overlapped = True
            self.current = value
            time.sleep(0.01)
        def poll(self, _timeout): return True
        def recv(self):
            time.sleep(0.01)
            value = self.current
            self.current = None
            command = WorkerCommand.model_validate(value)
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.OK,
                worker_epoch=uuid4(),
                operation_id=command.operation_id,
            ).model_dump(mode="json")
        def close(self): self.closed = True

    process = QQVMWorkerProcess.__new__(QQVMWorkerProcess)
    process._process = Process()
    process._parent = Pipe()
    process._request_lock = worker_module.threading.Lock()
    commands = (
        WorkerCommand(kind=WorkerKind.HEALTH),
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding"),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda command: process.request(command, 1), commands))

    assert [item.request_id for item in results] == [item.request_id for item in commands]
    assert process._parent.overlapped is False
    assert process._process.terminated is False


def _process_facade(process, pipe):
    facade = QQVMWorkerProcess.__new__(QQVMWorkerProcess)
    facade._process = process
    facade._parent = pipe
    facade._request_lock = worker_module.threading.Lock()
    return facade


def test_process_stop_raises_when_kill_cannot_retire_child() -> None:
    class Process:
        pid = 90
        exitcode = None

        def is_alive(self):
            return True

        def join(self, _timeout):
            pass

        def terminate(self):
            pass

        def kill(self):
            pass

    class Pipe:
        def send(self, _value):
            pass

        def close(self):
            pass

    facade = _process_facade(Process(), Pipe())

    with pytest.raises(RuntimeError, match="worker_process_did_not_exit"):
        facade.stop(timeout_seconds=0)

    assert facade.status_snapshot()["worker_alive"] is True


def test_process_freezes_timeout_as_first_terminal_cause() -> None:
    class Process:
        alive = True
        pid = 91
        exitcode = None

        def is_alive(self): return self.alive
        def terminate(self): self.alive = False; self.exitcode = -15
        def join(self, _timeout): pass

    class Pipe:
        def send(self, _value): pass
        def poll(self, _timeout): return False

    facade = _process_facade(Process(), Pipe())
    first = facade.request(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding"), .01)
    assert first.error_code == "worker_timeout_isolated"
    assert facade.status_snapshot()["first_terminal_failure"]["error_code"] == "worker_timeout_isolated"

    second = facade.request(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding"), .01)
    assert second.error_code == "worker_not_alive"
    terminal = facade.status_snapshot()["first_terminal_failure"]
    assert terminal["error_code"] == "worker_timeout_isolated"
    assert terminal["parent_terminate_reason"] == "request_timeout"


def test_process_invalid_response_is_terminal_without_payload_echo() -> None:
    class Process:
        alive = True
        pid = 92
        exitcode = None

        def is_alive(self): return self.alive
        def terminate(self): self.alive = False; self.exitcode = -15
        def join(self, _timeout): pass

    class Pipe:
        def send(self, _value): pass
        def poll(self, _timeout): return True
        def recv(self): return {"unexpected_secret_payload": "must-not-appear"}

    facade = _process_facade(Process(), Pipe())
    result = facade.request(WorkerCommand(kind=WorkerKind.HEALTH), 1)
    assert result.error_code == "worker_response_invalid"
    terminal = facade.status_snapshot()["first_terminal_failure"]
    assert terminal["error_code"] == "worker_response_invalid"
    assert "must-not-appear" not in json.dumps(terminal)


def test_process_keeps_only_bounded_child_action_diagnostics() -> None:
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding")

    class Process:
        pid = 93
        exitcode = None
        def is_alive(self): return True

    class Pipe:
        def send(self, _value): pass
        def poll(self, _timeout): return True
        def recv(self):
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                binding_id=command.binding_id,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=uuid4(),
                error_code="worker_action_failed",
                evidence={
                    "com_hresult": -2147418111,
                    "frame_sha256": "e" * 64,
                    "model": "deepseek-v4-flash-vision-exp",
                    "latency_ms": 41,
                    "project_frames": [{
                        "function": "list_conversations",
                        "file": "messenger_ai/adapters/qq/vm_driver/transport.py",
                        "line": 700,
                    }],
                    "bubbles": ["sensitive text"],
                    "png_bytes": "sensitive png",
                    "target_label": "sensitive target",
                    "api_key": "sensitive api key",
                },
            ).model_dump(mode="json")

    facade = _process_facade(Process(), Pipe())
    result = facade.request(command, 1)
    record = facade.status_snapshot()["last_request"]

    assert result.error_code == "worker_action_failed"
    assert record["com_hresult"] == -2147418111
    assert record["frame_sha256"] == "e" * 64
    assert record["model"] == "deepseek-v4-flash-vision-exp"
    assert record["latency_ms"] == 41
    assert record["project_frames"][0]["function"] == "list_conversations"
    assert "sensitive text" not in json.dumps(record)
    assert "sensitive png" not in json.dumps(record)
    assert "sensitive target" not in json.dumps(record)
    assert "sensitive api key" not in json.dumps(record)


def test_safe_failure_keeps_com_hresult_and_project_frame_without_message() -> None:
    namespace: dict[str, object] = {}
    exec(compile(
        "def list_conversations():\n"
        "    error = RuntimeError(-2147418111, 'sensitive UI text')\n"
        "    error.hresult = -2147418111\n"
        "    raise error\n",
        "C:/project/src/messenger_ai/adapters/qq/vm_driver/transport.py",
        "exec",
    ), namespace)
    try:
        namespace["list_conversations"]()
    except RuntimeError as exc:
        code, evidence = worker_module._safe_failure(exc, fallback="worker_action_failed")

    assert code == "worker_action_failed"
    assert evidence["com_hresult"] == -2147418111
    assert evidence["project_frames"] == [{
        "function": "list_conversations",
        "file": "messenger_ai/adapters/qq/vm_driver/transport.py",
        "line": 4,
    }]
    assert "sensitive UI text" not in json.dumps(evidence)


def _already_current_fixture(monkeypatch, *, descendants=(), live_header_digest=None):
    """Bootstrapped direct session whose registered target may already be current."""

    adapter, fake = _adapter()
    proof = QQSessionObservedDirectIdentity(
        binding_id="session-contact-1",
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version=adapter.selector_pack.client_version,
        selector_pack_version=adapter.selector_pack.fixture_suite_version,
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=fake.window.process_id,
        window_handle=fake.window.window_handle,
        process_started_at_100ns=123,
        vm_environment_fingerprint=adapter.selector_pack.environment_fingerprint,
        selected_row_runtime_id_hash="b" * 64,
        header_digest="c" * 64,
    )
    fake.conversations = [QQConversation(
        internal_id="runtime:" + proof.selected_row_runtime_id_hash,
        participant_signature="uncertified:session-row",
        tree_digest=fake.digest,
    )]
    binding = next(iter(adapter.bindings.values())).model_copy(update={
        "binding_id": proof.binding_id,
        "platform_conversation_id": fake.conversations[0].internal_id,
        "participant_signature": proof.participant_signature,
        "conversation_type": "direct",
        "authorization_scope": "all_direct_including_temporary",
    })
    fake._window = lambda _window: object()
    fake._descendants = lambda _root: list(descendants)
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    digest = proof.header_digest if live_header_digest is None else live_header_digest
    monkeypatch.setattr(session_identity, "_header_digest", lambda *_args: digest)

    @contextmanager
    def read_phase(_window):
        yield object()

    fake.read_phase = read_phase
    certifier = QQSessionIdentityCertifier(
        accessibility=fake, selector_pack=adapter.selector_pack, evidence=(proof,)
    )
    worker = QQVMWorker(
        accessibility=fake, selector_pack=adapter.selector_pack, bindings=(binding,),
        identity_certifier=certifier,
        candidate_locator=QQSessionCandidateLocator((proof,)),
        expected_window=certifier.window_scope,
        window_validator=certifier.validate_window,
    )
    return worker, fake, binding, proof


def _already_current_observe(binding) -> WorkerCommand:
    """One exact OBSERVE command carrying a valid selection-refresh handoff.

    A plain command may never shortcut to the certified target; only a token
    minted from an exact predecessor refresh outcome (or a binding this worker
    already established as trusted-current) unlocks the header shortcut.
    """

    predecessor = WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id=binding.binding_id
    )
    result = WorkerResult(
        request_id=predecessor.request_id,
        kind=predecessor.kind,
        status=WorkerStatus.FAILED_SAFE,
        worker_epoch=uuid4(),
        binding_id=predecessor.binding_id,
        error_code="selection_process_refresh_required",
    )
    successor = WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id=binding.binding_id
    )
    handoff = mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=successor,
        source="selection_refresh",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    return successor.model_copy(update={"selection_handoff": handoff})


def test_observe_skips_selection_when_target_is_already_current(monkeypatch) -> None:
    worker, fake, binding, proof = _already_current_fixture(monkeypatch)
    actuator = _SelectionActuator(ConversationSelectionOutcome(
        status=ConversationSelectionStatus.ACTION_ATTEMPTED, frame_sha256="a" * 64,
    ))
    worker._selection_actuator = actuator

    def forbidden(*_args, **_kwargs):
        raise AssertionError("already-current observe must not select or confirm")

    fake.select_conversation = forbidden
    fake.confirm_conversation_selected = forbidden

    result = worker.execute(_already_current_observe(binding))

    assert result.status is WorkerStatus.OK
    assert result.error_code is None
    assert "bubbles" in fake.calls
    assert "select" not in fake.calls
    assert actuator.calls == []
    assert result.evidence["target"]["internal_id"] == fake.conversations[0].internal_id
    assert proof.selected_row_runtime_id_hash in fake.conversations[0].internal_id


def test_observe_selects_when_certifier_cannot_prove_target_already_current(monkeypatch) -> None:
    worker, fake, binding, _proof = _already_current_fixture(
        monkeypatch, live_header_digest="d" * 64,
    )
    select_calls = []

    def select(*_args):
        select_calls.append(_args)
        return True

    fake.select_conversation = select

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding.binding_id)
    )

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_process_refresh_required"
    assert len(select_calls) == 1
    assert "bubbles" not in fake.calls


def test_observe_fails_closed_without_selection_on_non_header_identity_error(monkeypatch) -> None:
    worker, fake, binding, _proof = _already_current_fixture(
        monkeypatch, descendants=(SimpleNamespace(ClassName="group-member-list"),),
    )
    actuator = _SelectionActuator(ConversationSelectionOutcome(
        status=ConversationSelectionStatus.ACTION_ATTEMPTED, frame_sha256="a" * 64,
    ))
    worker._selection_actuator = actuator
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("identity failure must not fall through to selection")
    )

    result = worker.execute(_already_current_observe(binding))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "group_marker_detected"
    assert actuator.calls == []
    assert "select" not in fake.calls
    assert "bubbles" not in fake.calls


# --------------------------------------------------------------------------
# SelectionHandoff: the one-use authority for the already-current shortcut.
# --------------------------------------------------------------------------

# A rejected token may either get its own bounded code or fall through to the
# ordinary pre-commit refresh signal.  A silent OK, however, is never allowed.
_REJECTED_HANDOFF_CODES = frozenset({
    "selection_process_refresh_required",
    "selection_handoff_mismatch",
    "selection_handoff_expired",
    "selection_handoff_replayed",
})


class _CertifyingStub:
    """Stand-in current-identity certifier that records every consultation."""

    def __init__(self, outcome: object = "certified-current") -> None:
        self.outcome = outcome
        self.calls: list[tuple[object, object]] = []

    def try_certify_already_current(self, window, conversation):
        self.calls.append((window, conversation))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _shortcut_worker(*, certified: object = "certified-current"):
    """A worker whose certifier could prove the target already current."""

    worker, fake, binding_id = _worker()

    class Locator:
        def locate_candidates(self, binding, visible):
            return list(visible)

    @contextmanager
    def read_phase(_window):
        yield object()

    certifier = _CertifyingStub(certified)
    worker._identity_certifier = certifier
    worker._candidate_locator = Locator()
    fake.read_phase = read_phase
    return worker, fake, binding_id, certifier


def _refresh_handoff(
    command: WorkerCommand, *, expires_at: datetime | None = None,
) -> SelectionHandoff:
    """Mint the exact selection-refresh authority for ``command``."""

    predecessor = command.model_copy(
        update={"selection_handoff": None, "request_id": uuid4()}
    )
    result = WorkerResult(
        request_id=predecessor.request_id,
        kind=predecessor.kind,
        status=WorkerStatus.FAILED_SAFE,
        worker_epoch=uuid4(),
        operation_id=predecessor.operation_id,
        binding_id=predecessor.binding_id,
        binding_revision=predecessor.binding_revision,
        conversation_revision=predecessor.conversation_revision,
        error_code="selection_process_refresh_required",
    )
    return mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=command,
        source="selection_refresh",
        expires_at=expires_at or datetime.now(UTC) + timedelta(seconds=30),
    )


def _with_handoff(command: WorkerCommand, handoff: SelectionHandoff) -> WorkerCommand:
    return command.model_copy(update={"selection_handoff": handoff})


def _selecting_accessibility(fake) -> None:
    """Record a real selection action that must only run without authority."""

    def select(*_args):
        fake.calls.append("select")
        return True

    fake.select_conversation = select


def test_a_plain_command_cannot_shortcut_to_the_already_current_target() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    _selecting_accessibility(fake)

    result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )

    assert certifier.calls == []
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_process_refresh_required"
    assert "select" in fake.calls
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    assert worker._trusted_current_binding_id is None


def test_valid_handoff_authorises_already_current_observe_without_selection() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a valid handoff must not perform a selection action")
    )

    result = worker.execute(_with_handoff(command, _refresh_handoff(command)))

    assert result.status is WorkerStatus.OK
    assert len(certifier.calls) == 1
    assert certifier.calls[0][0] is fake.window
    assert certifier.calls[0][1] == fake.conversations[0]
    assert "bubbles" in fake.calls
    assert "select" not in fake.calls


def test_valid_handoff_establishes_trusted_current_for_that_binding_only() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    other = worker._bindings[binding_id].model_copy(update={
        "binding_id": "approved-binding-2",
        "hub_conversation_id": "hub-conv-2",
        "platform_conversation_id": "qq-conv-2",
        "participant_signature": "contact-proof-2",
    })
    worker._bindings[other.binding_id] = other

    first = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    assert worker.execute(
        _with_handoff(first, _refresh_handoff(first))
    ).status is WorkerStatus.OK
    assert len(certifier.calls) == 1

    trusted = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )
    assert trusted.status is WorkerStatus.OK
    assert len(certifier.calls) == 2

    _selecting_accessibility(fake)
    fake.calls.clear()
    other_result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=other.binding_id)
    )
    assert len(certifier.calls) == 2
    assert other_result.status is WorkerStatus.FAILED_SAFE
    assert other_result.error_code == "selection_process_refresh_required"
    assert "select" in fake.calls
    assert "bubbles" not in fake.calls


@pytest.mark.parametrize(
    "mutation",
    [
        {"binding_id": "other-binding"},
        {"binding_revision": 9},
        {"conversation_revision": 9},
        {"operation_id": uuid4()},
        {"successor_request_id": uuid4()},
        {"target_kind": WorkerKind.VERIFY},
        {"source": "commit_success"},
        {"source_kind": WorkerKind.PREPARE},
        {"predecessor_worker_epoch": UUID(int=0)},
    ],
)
def test_cross_wired_handoffs_never_authorise_the_shortcut(mutation) -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id=binding_id,
        binding_revision=2, conversation_revision=3,
    )
    handoff = _refresh_handoff(command).model_copy(update=mutation)
    _selecting_accessibility(fake)

    result = worker.execute(_with_handoff(command, handoff))

    assert certifier.calls == []
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code in _REJECTED_HANDOFF_CODES
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    assert worker._trusted_current_binding_id is None


def test_expired_handoff_never_authorises_the_shortcut() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    # Minted while valid, then observed after its expiry elapsed.
    handoff = _refresh_handoff(command).model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    _selecting_accessibility(fake)

    result = worker.execute(_with_handoff(command, handoff))

    assert certifier.calls == []
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code in _REJECTED_HANDOFF_CODES
    assert "bubbles" not in fake.calls


def test_consumed_handoff_is_never_replayed_for_a_second_observe() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    command = _with_handoff(command, _refresh_handoff(command))

    assert worker.execute(command).status is WorkerStatus.OK
    assert len(certifier.calls) == 1

    _selecting_accessibility(fake)
    replayed = worker.execute(command)

    assert len(certifier.calls) == 1
    assert replayed.status is WorkerStatus.FAILED_SAFE
    assert replayed.error_code in _REJECTED_HANDOFF_CODES


def test_handoff_is_spent_even_when_the_live_header_drifted() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker(certified=None)
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    command = _with_handoff(command, _refresh_handoff(command))
    _selecting_accessibility(fake)

    drifted = worker.execute(command)

    assert drifted.status is WorkerStatus.FAILED_SAFE
    assert len(certifier.calls) == 1
    assert "bubbles" not in fake.calls

    # The same authority can never be spent a second time in this worker.
    again = worker.execute(command)
    assert again.status is WorkerStatus.FAILED_SAFE
    assert len(certifier.calls) == 1


def test_group_marker_identity_failure_with_handoff_fails_closed() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker(
        certified=RuntimeError("group_marker_detected"),
    )
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    command = _with_handoff(command, _refresh_handoff(command))
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("identity failure must not fall through to selection")
    )

    result = worker.execute(command)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "group_marker_detected"
    assert len(certifier.calls) == 1
    assert "select" not in fake.calls
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls


def test_handoff_never_enters_the_worker_request_diagnostics() -> None:
    _worker_instance, _fake, binding_id, _certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    handoff = _refresh_handoff(command)

    diagnostics = QQVMWorkerProcess._request_metadata(_with_handoff(command, handoff))
    serialized = json.dumps(diagnostics, sort_keys=True)

    assert set(diagnostics) == {"request_id", "kind", "binding_id"}
    assert str(handoff.handoff_id) not in serialized
    assert str(handoff.predecessor_request_id) not in serialized
    assert str(handoff.predecessor_worker_epoch) not in serialized
    assert handoff.expires_at.isoformat() not in serialized
