from __future__ import annotations

import hashlib
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
from messenger_ai.adapters.qq.vm_driver.contracts import (
    PreparedBubbleAnchor,
    PreparedTargetIdentity,
    PreparedVerificationEvidence,
)
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
from messenger_ai.adapters.qq.vm_driver.session_identity import (
    QQSessionCandidateLocator,
    QQSessionIdentityCertifier,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE,
    RowPaletteSummary,
    ScreenRect,
    SelectionVisualAttestation,
    runtime_id_digest,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_qq_adapter import _adapter


def _worker():
    _adapter_instance, fake = _adapter()
    pack = _adapter_instance.selector_pack
    binding = next(iter(_adapter_instance.bindings.values()))
    return QQVMWorker(accessibility=fake, selector_pack=pack, bindings=(binding,)), fake, binding.binding_id


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 0.5])
def test_worker_rejects_invalid_prepare_write_reserve(value) -> None:
    adapter, fake = _adapter()
    binding = next(iter(adapter.bindings.values()))

    with pytest.raises(ValueError, match="prepare write reserve"):
        QQVMWorker(
            accessibility=fake,
            selector_pack=adapter.selector_pack,
            bindings=(binding,),
            prepare_write_reserve_seconds=value,
        )

    with pytest.raises(ValueError, match="prepare write reserve"):
        QQVMWorkerProcess(
            adapter.selector_pack,
            (binding,),
            prepare_write_reserve_seconds=value,
        )


def test_worker_process_successor_preserves_prepare_write_reserve() -> None:
    original = QQVMWorkerProcess
    captured = {}

    class Successor(original):
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    facade = Successor.__new__(Successor)
    facade._selector_pack = object()
    facade._bindings = (object(),)
    facade._session_evidence = (object(),)
    facade._run_id = "run-id"
    facade._visual_selection = object()
    facade._visual_api_key = "visual-key"
    facade._prepare_write_reserve_seconds = 23.0

    successor = original.spawn_successor(facade)

    assert isinstance(successor, Successor)
    assert captured["kwargs"]["prepare_write_reserve_seconds"] == 23.0


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

    changed = worker._select_conversation(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id),
        fake.window,
        binding,
        conversation,
    )

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
        worker._select_conversation(
            WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id),
            fake.window,
            binding,
            fake.conversations[0],
        )

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
        try:
            yield object()
        finally:
            if opened >= 2:
                time.sleep(0.02)
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


@pytest.mark.parametrize("existing_draft", ["", "operator draft"])
def test_prepare_without_read_phase_keeps_composer_check_for_ordinary_proof(
    existing_draft: str,
) -> None:
    adapter, fake = _adapter()
    binding = next(iter(adapter.bindings.values())).model_copy(update={
        "conversation_type": "direct",
        "participant_signature": "qq-profile-hmac:" + "c" * 64,
    })
    worker = QQVMWorker(
        accessibility=fake,
        selector_pack=adapter.selector_pack,
        bindings=(binding,),
        identity_certifier=Certifier(binding.participant_signature),
        candidate_locator=Locator(),
    )
    assert not callable(getattr(fake, "read_phase", None))
    assert binding.authorization_scope == "legacy_explicit_contacts"
    fake.composer = existing_draft
    command = WorkerCommand(
        kind=WorkerKind.PREPARE, binding_id=binding.binding_id,
        operation_id=uuid4(), segment_ref="fallback:0", text="prepared reply",
    )

    result = worker.execute(command)

    content_calls = [call for call in fake.calls if call in {
        "bubbles", "read-composer", "write-composer", "invoke-send",
    }]
    if existing_draft:
        assert result.status is WorkerStatus.FAILED_SAFE
        assert result.error_code == "composer_not_empty"
        assert content_calls == ["bubbles", "read-composer"]
        assert fake.composer == existing_draft
        assert worker._prepared == {}
        assert worker._reservation is None
    else:
        assert result.status is WorkerStatus.OK
        assert content_calls == ["bubbles", "read-composer", "write-composer", "read-composer"]
        assert fake.composer == command.text
        assert worker._reservation == command.operation_id


def test_prepare_session_proof_with_narrow_scope_keeps_all_full_revalidations(
    monkeypatch,
) -> None:
    worker, fake, binding, proof = _already_current_fixture(monkeypatch)
    binding = binding.model_copy(update={"authorization_scope": "legacy_explicit_contacts"})
    worker._bindings[binding.binding_id] = binding
    assert isinstance(proof, QQSessionObservedDirectIdentity)
    assert callable(fake.read_phase)
    fake.confirm_conversation_selected = lambda *_args: None
    events = []
    full_revalidations = []
    revalidate = worker._revalidate_expected_target
    certify = worker._certify_visual_header_current
    list_bubbles = fake.list_bubbles
    read_composer = fake.read_composer
    write_composer = fake.write_composer
    attest = fake.certify_conversation_selected_visual
    certify_header = worker._identity_certifier.try_certify_already_current

    def spy_revalidate(**kwargs):
        events.append("revalidate")
        return revalidate(**kwargs)

    def spy_certify(**kwargs):
        # A narrow-scope resolution cannot issue a certified PREPARE snapshot.
        # Every call here must still execute the complete identity/visual check.
        full_revalidations.append(kwargs)
        return certify(**kwargs)

    def spy_attest(*args, **kwargs):
        events.append("attest")
        return attest(*args, **kwargs)

    def spy_header(*args):
        events.append("header")
        return certify_header(*args)

    def spy_bubbles(*args):
        events.append("bubbles")
        return list_bubbles(*args)

    def spy_read(*args):
        events.append("read-composer")
        return read_composer(*args)

    def spy_write(*args):
        events.append("write-composer")
        return write_composer(*args)

    monkeypatch.setattr(worker, "_revalidate_expected_target", spy_revalidate)
    monkeypatch.setattr(worker, "_certify_visual_header_current", spy_certify)
    monkeypatch.setattr(fake, "list_bubbles", spy_bubbles)
    monkeypatch.setattr(fake, "read_composer", spy_read)
    monkeypatch.setattr(fake, "write_composer", spy_write)
    monkeypatch.setattr(fake, "certify_conversation_selected_visual", spy_attest)
    monkeypatch.setattr(worker._identity_certifier, "try_certify_already_current", spy_header)
    command = WorkerCommand(
        kind=WorkerKind.PREPARE, binding_id=binding.binding_id,
        operation_id=uuid4(), segment_ref="fallback:0", text="prepared reply",
    )

    result = worker.execute(command)

    assert result.status is WorkerStatus.OK
    full_check = ["revalidate", "attest", "header", "attest"]
    assert events == (
        ["bubbles"] + full_check + ["read-composer"] + full_check
        + ["write-composer", "revalidate", "attest", "header", "read-composer", "attest"]
    )
    assert len(full_revalidations) == 3
    assert all(call["current_reader"] is None for call in full_revalidations[:2])
    assert callable(full_revalidations[2]["current_reader"])
    assert all(call["binding"] is binding for call in full_revalidations)
    assert fake.composer == command.text
    assert worker._reservation == command.operation_id
    assert "invoke-send" not in fake.calls


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

    def try_certify_already_current(self, window, candidate):
        return self.certify_current(window, candidate)


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
    @contextmanager
    def read_phase(_window):
        yield object()
    fake.read_phase = read_phase
    worker = QQVMWorker(accessibility=fake, selector_pack=adapter.selector_pack,
                        bindings=(binding,), identity_certifier=certifier,
                        candidate_locator=Locator())
    _install_visual_attestation(worker, fake)
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
    @contextmanager
    def read_phase(_window):
        yield object()
    fake.read_phase = read_phase
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
    _install_visual_attestation(worker, fake)

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
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
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
    @contextmanager
    def read_phase(_window):
        yield object()
    fake.read_phase = read_phase
    worker = QQVMWorker(
        accessibility=fake, selector_pack=adapter.selector_pack, bindings=(binding,),
        identity_certifier=certifier,
        candidate_locator=QQSessionCandidateLocator((proof,)),
        expected_window=certifier.window_scope,
        window_validator=certifier.validate_window,
    )
    _install_visual_attestation(worker, fake)
    return worker, fake, binding, proof


def _already_current_observe(worker, binding) -> WorkerCommand:
    """One exact OBSERVE command carrying a valid selection-refresh handoff.

    A plain command may never shortcut to the certified target; only a token
    minted from an exact predecessor refresh outcome (or a binding this worker
    already established as trusted-current) unlocks the header shortcut.
    """

    deadline = datetime.now(UTC) + timedelta(seconds=30)
    predecessor = WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id=binding.binding_id, deadline=deadline
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
        kind=WorkerKind.OBSERVE, binding_id=binding.binding_id, deadline=deadline
    )
    handoff = mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=successor,
        source="selection_refresh",
        successor_worker_epoch=worker._epoch,
        target_runtime_id_digest=runtime_id_digest(
            binding.platform_conversation_id
        ),
        expires_at=deadline,
        signing_key=worker._selection_handoff_signing_key,
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

    result = worker.execute(_already_current_observe(worker, binding))

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

    result = worker.execute(_already_current_observe(worker, binding))

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
    "selection_handoff_invalid",
})


_DEFAULT_CERTIFIED = object()


class _CertifyingStub:
    """Stand-in current-identity certifier that records every consultation."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[object, object]] = []

    def try_certify_already_current(self, window, conversation):
        self.calls.append((window, conversation))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _install_visual_attestation(worker, fake):
    profile = QQ_VM_ROW_PALETTE_PROFILE.model_copy(update={
        "client_version": worker._selectors.client_version,
        "selector_pack_version": worker._selectors.fixture_suite_version,
        "environment_fingerprint": worker._selectors.environment_fingerprint,
    })
    worker._selection_visual_profile = profile

    def certify(window, conversation, _selector, actual_profile, *, deadline=None):
        assert actual_profile == profile
        assert deadline is None or deadline.tzinfo is not None
        return SelectionVisualAttestation(
            profile_id=profile.profile_id,
            client_version=profile.client_version,
            selector_pack_version=profile.selector_pack_version,
            environment_fingerprint=profile.environment_fingerprint,
            process_id=window.process_id,
            window_handle=window.window_handle,
            target_runtime_id_digest=runtime_id_digest(conversation.internal_id),
            row_rect=ScreenRect(left=56, top=100, right=306, bottom=164),
            sample_count=2,
            stable_sample_count=2,
            unselected_control_count=2,
            selected=RowPaletteSummary(
                dominant_rgb=(225, 225, 225), ratio=0.779592,
                unique_count=12, pixel_count=5880,
            ),
            unselected=RowPaletteSummary(
                dominant_rgb=(245, 245, 245), ratio=1.0,
                unique_count=1, pixel_count=5880,
            ),
        )

    fake.certify_conversation_selected_visual = certify


def _shortcut_worker(*, certified: object = _DEFAULT_CERTIFIED):
    """A worker whose certifier could prove the target already current."""

    worker, fake, binding_id = _worker()

    class Locator:
        def locate_candidates(self, binding, visible):
            return list(visible)

    @contextmanager
    def read_phase(_window):
        yield object()

    outcome = (
        SimpleNamespace(participant_signature=worker._bindings[binding_id].participant_signature)
        if certified is _DEFAULT_CERTIFIED
        else certified
    )
    certifier = _CertifyingStub(outcome)
    worker._identity_certifier = certifier
    worker._candidate_locator = Locator()
    fake.read_phase = read_phase
    _install_visual_attestation(worker, fake)
    return worker, fake, binding_id, certifier


def _refresh_handoff(
    worker, command: WorkerCommand, *, expires_at: datetime | None = None,
) -> SelectionHandoff:
    """Mint the exact selection-refresh authority for ``command``."""

    successor = command.model_copy(update={
        "deadline": command.deadline or datetime.now(UTC) + timedelta(seconds=30)
    })
    predecessor = successor.model_copy(
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
        successor_command=successor,
        source="selection_refresh",
        successor_worker_epoch=worker._epoch,
        target_runtime_id_digest=runtime_id_digest(
            worker._bindings[command.binding_id or ""].platform_conversation_id
        ),
        expires_at=expires_at or successor.deadline,
        signing_key=worker._selection_handoff_signing_key,
    )


def _with_handoff(command: WorkerCommand, handoff: SelectionHandoff) -> WorkerCommand:
    return command.model_copy(update={
        "selection_handoff": handoff,
        "deadline": handoff.successor_deadline,
    })


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
    assert worker._trusted_operation_lease is None


def test_valid_handoff_authorises_already_current_observe_without_selection() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a valid handoff must not perform a selection action")
    )

    result = worker.execute(_with_handoff(command, _refresh_handoff(worker, command)))

    assert result.status is WorkerStatus.OK
    assert len(certifier.calls) == 1
    assert certifier.calls[0][0] is fake.window
    assert certifier.calls[0][1] == fake.conversations[0]
    assert "bubbles" in fake.calls
    assert "select" not in fake.calls


def test_valid_handoff_does_not_establish_generic_binding_trust() -> None:
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
        _with_handoff(first, _refresh_handoff(worker, first))
    ).status is WorkerStatus.OK
    assert len(certifier.calls) == 1

    _selecting_accessibility(fake)
    fake.calls.clear()
    repeated = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )
    assert repeated.status is WorkerStatus.FAILED_SAFE
    assert repeated.error_code == "selection_process_refresh_required"
    assert len(certifier.calls) == 1
    assert "select" in fake.calls
    assert "bubbles" not in fake.calls

    fake.calls.clear()
    other_result = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=other.binding_id)
    )
    assert len(certifier.calls) == 1
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
        {"successor_worker_epoch": uuid4()},
        {"target_runtime_id_digest": "d" * 64},
    ],
)
def test_cross_wired_handoffs_never_authorise_the_shortcut(mutation) -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id=binding_id,
        binding_revision=2, conversation_revision=3,
    )
    handoff = _refresh_handoff(worker, command).model_copy(update=mutation)
    _selecting_accessibility(fake)

    result = worker.execute(_with_handoff(command, handoff))

    assert certifier.calls == []
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code in _REJECTED_HANDOFF_CODES
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    assert worker._trusted_operation_lease is None


def test_expired_handoff_never_authorises_the_shortcut() -> None:
    worker, fake, binding_id, certifier = _shortcut_worker()
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    # Minted while valid, then observed after its expiry elapsed.
    handoff = _refresh_handoff(worker, command).model_copy(
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
    command = _with_handoff(command, _refresh_handoff(worker, command))

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
    command = _with_handoff(command, _refresh_handoff(worker, command))
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
    command = _with_handoff(command, _refresh_handoff(worker, command))
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
    handoff = _refresh_handoff(_worker_instance, command)

    diagnostics = QQVMWorkerProcess._request_metadata(_with_handoff(command, handoff))
    serialized = json.dumps(diagnostics, sort_keys=True)

    assert set(diagnostics) == {"request_id", "kind", "binding_id", "operation_id",
                                "binding_revision", "conversation_revision"}
    assert str(handoff.handoff_id) not in serialized
    assert str(handoff.predecessor_request_id) not in serialized
    assert str(handoff.predecessor_worker_epoch) not in serialized
    assert handoff.expires_at.isoformat() not in serialized


# --------------------------------------------------------------------------
# Bounded adversarial coverage: a minted capability is welded to exactly one
# successor request, and an uncorrelated child reply must retire the process.
# --------------------------------------------------------------------------


_PREPARE_TAMPER_CASES = (
    "forged_auth_tag",
    "request_text",
    "token_text_sha256",
    "request_segment_ref",
    "token_segment_ref",
    "request_deadline",
    "token_successor_deadline",
    "request_successor_request_id",
)


def _full_proof_worker():
    """Shortcut worker whose certifier returns a complete stable-target proof.

    ``_shortcut_worker`` proves the participant signature only, which is enough
    for OBSERVE.  PREPARE and VERIFY additionally derive the prepared target
    from the proof, so the stub must expose the same bounded identity fields
    the real certifier returns.
    """

    worker, fake, binding_id, certifier = _shortcut_worker(certified=None)
    binding = worker._bindings[binding_id]
    certifier.outcome = SimpleNamespace(
        participant_signature=binding.participant_signature,
        conversation_type=(
            "direct" if binding.conversation_type == "direct" else "unknown"
        ),
        process_id=fake.window.process_id,
        window_handle=fake.window.window_handle,
    )
    return worker, fake, binding_id, certifier


def _prepare_request(binding_id: str, *, text: str = "prepare-body-1") -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.PREPARE,
        binding_id=binding_id,
        operation_id=uuid4(),
        text=text,
        segment_ref="segment-1",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _flip_handoff_auth_tag(handoff: SelectionHandoff) -> SelectionHandoff:
    """Forge one nibble of a genuine tag without touching the signed payload."""

    first = "0" if handoff.auth_tag[0] != "0" else "1"
    return handoff.model_copy(update={"auth_tag": first + handoff.auth_tag[1:]})


def _tampered_prepare_request(
    command: WorkerCommand, handoff: SelectionHandoff, case: str,
) -> WorkerCommand:
    """One PREPARE request a post-mint adversary could present to the worker."""

    if case == "forged_auth_tag":
        return _with_handoff(command, _flip_handoff_auth_tag(handoff))
    if case == "request_text":
        return _with_handoff(
            command.model_copy(update={"text": f"{command.text}-tampered"}), handoff
        )
    if case == "token_text_sha256":
        return _with_handoff(
            command, handoff.model_copy(update={"text_sha256": "f" * 64})
        )
    if case == "request_segment_ref":
        return _with_handoff(
            command.model_copy(update={"segment_ref": "segment-tampered"}), handoff
        )
    if case == "token_segment_ref":
        return _with_handoff(
            command, handoff.model_copy(update={"segment_ref": "segment-tampered"})
        )
    if case == "request_deadline":
        # The token still carries the original deadline; only the request moved.
        return _with_handoff(command, handoff).model_copy(
            update={"deadline": handoff.successor_deadline + timedelta(seconds=5)}
        )
    if case == "token_successor_deadline":
        later = handoff.successor_deadline + timedelta(seconds=5)
        return _with_handoff(
            command, handoff.model_copy(update={"successor_deadline": later})
        )
    if case == "request_successor_request_id":
        return _with_handoff(
            command.model_copy(update={"request_id": uuid4()}), handoff
        )
    raise AssertionError(f"unknown tamper case {case!r}")


@pytest.mark.parametrize("tamper", _PREPARE_TAMPER_CASES)
def test_prepare_handoff_tampering_after_mint_fails_closed_without_authority(
    tamper: str,
) -> None:
    worker, fake, binding_id, certifier = _full_proof_worker()
    command = _prepare_request(binding_id)
    handoff = _refresh_handoff(worker, command)

    result = worker.execute(_tampered_prepare_request(command, handoff, tamper))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_handoff_invalid"
    assert certifier.calls == []
    assert "select" not in fake.calls
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    assert fake.composer == ""
    assert fake.bubbles == []
    assert worker._prepared == {}
    assert worker._reservation is None
    assert worker._trusted_operation_lease is None
    diagnostics = json.dumps(result.evidence, sort_keys=True)
    assert command.text not in diagnostics
    assert f"{command.text}-tampered" not in diagnostics
    assert str(handoff.handoff_id) not in diagnostics
    assert handoff.auth_tag not in diagnostics


def test_valid_prepare_handoff_remains_the_only_shortcut_authority() -> None:
    worker, fake, binding_id, certifier = _full_proof_worker()
    command = _prepare_request(binding_id)
    command = _with_handoff(command, _refresh_handoff(worker, command))
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a valid PREPARE handoff must never select")
    )

    result = worker.execute(command)

    assert result.status is WorkerStatus.OK
    assert result.error_code is None
    assert len(certifier.calls) == 1
    assert "bubbles" in fake.calls
    assert "write-composer" in fake.calls
    assert "invoke-send" not in fake.calls
    assert "select" not in fake.calls
    assert fake.composer == command.text
    assert worker._prepared[command.operation_id]["composer_text"] == command.text
    assert worker._reservation == command.operation_id
    assert worker._trusted_operation_lease == (
        command.operation_id,
        binding_id,
        command.binding_revision,
        command.conversation_revision,
    )


def _verify_request(
    binding_id: str, evidence: PreparedVerificationEvidence,
) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.VERIFY,
        binding_id=binding_id,
        binding_revision=2,
        conversation_revision=3,
        operation_id=uuid4(),
        prepared_evidence=evidence,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _commit_success_handoff(worker, verify_command: WorkerCommand) -> SelectionHandoff:
    """Mint the commit-success authority issued to a fresh VERIFY successor."""

    predecessor = WorkerCommand(
        kind=WorkerKind.COMMIT,
        binding_id=verify_command.binding_id,
        binding_revision=verify_command.binding_revision,
        conversation_revision=verify_command.conversation_revision,
        operation_id=verify_command.operation_id,
        deadline=verify_command.deadline,
    )
    result = WorkerResult(
        request_id=predecessor.request_id,
        kind=predecessor.kind,
        status=WorkerStatus.OK,
        worker_epoch=uuid4(),
        operation_id=predecessor.operation_id,
        binding_id=predecessor.binding_id,
        binding_revision=predecessor.binding_revision,
        conversation_revision=predecessor.conversation_revision,
    )
    return worker.mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=verify_command,
        source="commit_success",
        successor_worker_epoch=worker._epoch,
        target_runtime_id_digest=runtime_id_digest(
            worker._bindings[verify_command.binding_id or ""].platform_conversation_id
        ),
        expires_at=verify_command.deadline,
    )


def _bubble_anchors(bubbles) -> tuple[PreparedBubbleAnchor, ...]:
    return tuple(
        PreparedBubbleAnchor(
            direction=item.direction.value,
            message_key=item.message_key,
            conversation_internal_id=item.conversation_internal_id,
            text_hash=item.text_hash,
        )
        for item in bubbles
    )


def _prepared_evidence(
    binding, fake, *, text: str, before_bubbles: tuple[PreparedBubbleAnchor, ...],
) -> PreparedVerificationEvidence:
    return PreparedVerificationEvidence(
        owner_binding_id=binding.binding_id,
        target_identity=PreparedTargetIdentity(
            binding_id=binding.binding_id,
            participant_signature=binding.participant_signature,
            conversation_type=(
                "direct" if binding.conversation_type == "direct" else "unknown"
            ),
            process_id=fake.window.process_id,
            window_handle=fake.window.window_handle,
        ),
        before_bubbles=before_bubbles,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        segment_ref="segment-1",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"text_hash": "f" * 64},
        {"segment_ref": "segment-2"},
        {"before_bubbles": ()},
    ],
    ids=["text_hash", "segment_ref", "before_bubbles"],
)
def test_verify_evidence_changed_after_mint_fails_closed(change) -> None:
    worker, fake, binding_id, certifier = _full_proof_worker()
    binding = worker._bindings[binding_id]
    anchored = (
        PreparedBubbleAnchor(
            direction="inbound",
            message_key="incoming-1",
            conversation_internal_id=fake.conversations[0].internal_id,
            text_hash="b" * 64,
        ),
    )
    original = _prepared_evidence(
        binding, fake, text="verify-body-1", before_bubbles=anchored,
    )
    verify = _verify_request(binding_id, original)
    handoff = _commit_success_handoff(worker, verify)
    changed = original.model_copy(update=change)
    assert changed != original

    result = worker.execute(
        _with_handoff(verify.model_copy(update={"prepared_evidence": changed}), handoff)
    )

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "selection_handoff_invalid"
    assert "receipt" not in result.evidence
    assert certifier.calls == []
    assert "select" not in fake.calls
    assert "bubbles" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    assert worker._prepared == {}
    assert worker._reservation is None
    assert worker._trusted_operation_lease is None
    diagnostics = json.dumps(result.evidence, sort_keys=True)
    assert "verify-body-1" not in diagnostics
    assert str(handoff.handoff_id) not in diagnostics


def test_valid_commit_success_handoff_reads_the_exact_receipt_without_sending() -> None:
    worker, fake, binding_id, certifier = _full_proof_worker()
    binding = worker._bindings[binding_id]
    fake.bubbles = [
        QQBubble(
            conversation_internal_id=fake.conversations[0].internal_id,
            message_key="incoming-1",
            direction=BubbleDirection.INBOUND,
            text="incoming one",
            observed_at=datetime.now(UTC),
            tree_digest=fake.digest,
        )
    ]
    evidence = _prepared_evidence(
        binding, fake, text="verify-body-1",
        before_bubbles=_bubble_anchors(fake.bubbles),
    )
    verify = _verify_request(binding_id, evidence)
    command = _with_handoff(verify, _commit_success_handoff(worker, verify))
    fake.select_conversation = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a valid VERIFY handoff must never select")
    )
    fake.bubbles.append(QQBubble(
        conversation_internal_id=fake.conversations[0].internal_id,
        message_key="out-0",
        direction=BubbleDirection.OUTBOUND,
        text="verify-body-1",
        observed_at=datetime.now(UTC) + timedelta(seconds=1),
        tree_digest=fake.digest,
    ))

    result = worker.execute(command)

    assert result.status is WorkerStatus.OK
    assert len(certifier.calls) == 1
    assert "select" not in fake.calls
    assert "write-composer" not in fake.calls
    assert "invoke-send" not in fake.calls
    receipt = result.evidence["receipt"]
    assert receipt["message_key"] == "out-0"
    assert receipt["direction"] == "outbound"
    assert result.evidence["operation_id"] == str(command.operation_id)
    assert worker._reservation is None
    assert worker._trusted_operation_lease is None


# Each mutation makes the child reply disagree with its request in exactly one
# correlated field; the parent may never adopt the reply as a usable outcome.
_PROCESS_RESPONSE_MISMATCHES = {
    "request_id": lambda command, response: response.model_copy(
        update={"request_id": uuid4()}
    ),
    "kind": lambda command, response: response.model_copy(
        update={"kind": WorkerKind.HEALTH}
    ),
    "binding_id": lambda command, response: response.model_copy(
        update={"binding_id": "other-binding"}
    ),
    "binding_revision": lambda command, response: response.model_copy(
        update={"binding_revision": command.binding_revision + 1}
    ),
    "conversation_revision": lambda command, response: response.model_copy(
        update={"conversation_revision": command.conversation_revision + 1}
    ),
    "operation_id": lambda command, response: response.model_copy(
        update={"operation_id": uuid4()}
    ),
    "ok_with_zero_worker_epoch": lambda command, response: response.model_copy(
        update={"status": WorkerStatus.OK, "worker_epoch": UUID(int=0)}
    ),
}


@pytest.mark.parametrize("mismatch", tuple(_PROCESS_RESPONSE_MISMATCHES))
def test_process_terminates_and_fails_uncertain_on_mismatched_child_response(
    mismatch: str,
) -> None:
    command = WorkerCommand(
        kind=WorkerKind.OBSERVE,
        binding_id="approved-binding-1",
        binding_revision=2,
        conversation_revision=3,
        operation_id=uuid4(),
    )
    response = WorkerResult(
        request_id=command.request_id,
        kind=WorkerKind.OBSERVE,
        status=WorkerStatus.OK,
        worker_epoch=uuid4(),
        operation_id=command.operation_id,
        binding_id=command.binding_id,
        binding_revision=command.binding_revision,
        conversation_revision=command.conversation_revision,
        evidence={"target_label": "must-not-appear"},
    )
    mismatched = _PROCESS_RESPONSE_MISMATCHES[mismatch](command, response)
    assert mismatched != response

    class Process:
        pid = 93
        exitcode = None

        def __init__(self) -> None:
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            self.exitcode = -15

        def join(self, _timeout):
            pass

    class Pipe:
        def send(self, _value):
            pass

        def poll(self, _timeout):
            return True

        def recv(self):
            return mismatched.model_dump(mode="json")

    process = Process()
    facade = _process_facade(process, Pipe())

    result = facade.request(command, 1)
    snapshot = facade.status_snapshot()

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "worker_response_mismatch"
    assert result.worker_epoch == UUID(int=0)
    assert result.evidence == {}
    assert process.alive is False
    assert snapshot["worker_alive"] is False
    assert snapshot["parent_terminate_reason"] == "response_mismatch"
    terminal = snapshot["first_terminal_failure"]
    assert terminal["error_code"] == "worker_response_mismatch"
    assert terminal["request_id"] == str(command.request_id)
    assert "must-not-appear" not in json.dumps(snapshot, sort_keys=True)


def test_observe_reads_only_after_one_latest_tail_scroll_and_fresh_resolution():
    worker, fake, binding = _worker()
    latest = False
    trace = []
    fake.message_tail_is_latest = lambda *_: latest
    original_resolve = worker._resolve
    original_bubbles = fake.list_bubbles

    def resolve(*args, **kwargs):
        trace.append("resolve")
        return original_resolve(*args, **kwargs)

    def bubbles(*args):
        assert latest
        trace.append("bubbles")
        return original_bubbles(*args)

    def scroll(*args, before_action):
        nonlocal latest
        before_action()
        trace.append("scroll")
        latest = True

    worker._resolve = resolve
    fake.list_bubbles = bubbles
    fake.scroll_message_tail_to_latest = scroll
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding))
    assert result.status is WorkerStatus.OK
    assert trace == ["resolve", "scroll", "resolve", "bubbles"]


@pytest.mark.parametrize("mode,code", [
    ("missing", "message_tail_unproven"),
    ("unknown", "message_tail_unproven"),
    ("no_action", "message_tail_scroll_unavailable"),
])
def test_observe_unproven_tail_is_failed_safe_without_bubbles(mode, code):
    worker, fake, binding = _worker()
    fake.message_tail_is_latest = None if mode == "missing" else lambda *_: (None if mode == "unknown" else False)
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding))
    assert result.status is WorkerStatus.FAILED_SAFE and result.error_code == code
    assert "bubbles" not in fake.calls and "bubbles" not in result.evidence


def test_observe_tail_settle_is_bounded_without_repeating_scroll(monkeypatch):
    worker, fake, binding = _worker()
    fake.message_tail_is_latest = lambda *_: False
    calls = []
    fake.scroll_message_tail_to_latest = lambda *args, **kwargs: calls.append("scroll")
    ticks = iter([0.0, 3.0])
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(ticks))
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "message_tail_not_latest"
    assert calls == ["scroll"] and "bubbles" not in fake.calls


def test_observe_tail_drift_after_read_never_publishes_snapshot():
    worker, fake, binding = _worker()
    states = iter([True, False])
    fake.message_tail_is_latest = lambda *_: next(states)
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "message_tail_changed"
    assert "bubbles" not in result.evidence


def test_observe_scroll_keeps_identity_validation_fail_closed():
    worker, fake, binding = _worker()
    fake.message_tail_is_latest = lambda *_: False

    def scroll(*args, before_action):
        before_action()
        fake.conversations[0] = fake.conversations[0].model_copy(
            update={"participant_signature": "different-counterparty"}
        )

    fake.scroll_message_tail_to_latest = scroll
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert "bubbles" not in result.evidence and "bubbles" not in fake.calls
