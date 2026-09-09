from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
from uuid import uuid4

from messenger_ai.adapters.qq import BubbleDirection, QQBubble
from messenger_ai.adapters.qq.vm_driver import QQVMWorker, WorkerCommand, WorkerKind, WorkerStatus
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_qq_adapter import _adapter


def _worker():
    _adapter_instance, fake = _adapter()
    pack = _adapter_instance.selector_pack
    binding = next(iter(_adapter_instance.bindings.values()))
    return QQVMWorker(accessibility=fake, selector_pack=pack, bindings=(binding,)), fake, binding.binding_id


def test_worker_rejects_unknown_binding_without_touching_uia() -> None:
    worker, fake, _binding_id = _worker()
    result = worker.execute(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="unknown"))
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "unknown_binding"
    assert fake.calls == []


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
