"""Adversarial acceptance contracts for the VM worker.

These tests intentionally describe the safety boundary required by B3.  They
may fail against the current Terra implementation; a failure is evidence for
the worker/runtime repair and must not be fixed by weakening the assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from messenger_ai.adapters.qq import BubbleDirection, QQBubble
from messenger_ai.adapters.qq.vm_driver import (
    QQVMWorker,
    QQVMWorkerProcess,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
)

from test_worker import _worker


def _prepare(worker: QQVMWorker, binding_id: str, operation_id=None):
    return worker.execute(
        WorkerCommand(
            kind=WorkerKind.PREPARE,
            binding_id=binding_id,
            operation_id=operation_id or uuid4(),
            segment_ref="plan-acceptance:0",
            text="一次性测试文字",
            binding_revision=2,
            conversation_revision=3,
        )
    )


def test_observe_other_contact_cannot_interleave_prepared_send() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    prepared = _prepare(worker, binding_id, operation_id)
    assert prepared.status is WorkerStatus.OK

    # The desktop lease must cover prepare through commit.  An observation for
    # another contact cannot take the UI lane while this operation is pending.
    observed = worker.execute(
        WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    )
    assert observed.status is WorkerStatus.FAILED_SAFE
    assert observed.error_code in {"desktop_busy", "operation_in_progress", "ui_reserved"}
    assert fake.calls.count("select") == 1


def test_manual_composer_change_after_prepare_blocks_commit() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK
    fake.composer = "人工修改内容"
    result = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code in {"composer_changed", "composer_readback_mismatch", "composer_drift", "stale_context"}
    assert fake.calls.count("invoke-send") == 0


def test_new_inbound_after_prepare_makes_commit_stale() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK
    fake.bubbles.append(
        QQBubble(
            conversation_internal_id="qq-conv-1",
            message_key="new-inbound",
            direction=BubbleDirection.INBOUND,
            text="刚刚的新消息",
            observed_at=datetime.now(UTC),
            tree_digest=fake.digest,
        )
    )
    result = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code in {"stale_context", "new_inbound", "conversation_changed"}
    assert fake.calls.count("invoke-send") == 0


def test_duplicate_commit_invokes_send_at_most_once() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK
    first = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    second = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    assert first.status is WorkerStatus.OK
    assert second.status in {WorkerStatus.OK, WorkerStatus.FAILED_SAFE, WorkerStatus.UNCERTAIN}
    assert fake.calls.count("invoke-send") == 1


def test_invoke_exception_is_uncertain_and_never_retried() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK

    def raise_invoke(*_args):
        fake.calls.append("invoke-send")
        raise RuntimeError("UIA timeout after click")

    fake.invoke_send = raise_invoke
    result = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    assert result.status is WorkerStatus.UNCERTAIN
    retry = worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    assert retry.status is WorkerStatus.UNCERTAIN
    assert fake.calls.count("invoke-send") == 1


def test_old_same_text_bubble_after_scroll_is_not_receipt() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK
    fake.emit_receipt = False
    worker.execute(
        WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding_id, operation_id=operation_id)
    )
    # Simulate a virtualized list assigning a new local key to an old bubble.
    fake.bubbles = [
        QQBubble(
            conversation_internal_id="qq-conv-1",
            message_key="scroll-rekeyed-old",
            direction=BubbleDirection.OUTBOUND,
            text="一次性测试文字",
            observed_at=datetime.now(UTC),
            tree_digest=fake.digest,
        )
    ]
    result = worker.execute(
        WorkerCommand(kind=WorkerKind.VERIFY, binding_id=binding_id, operation_id=operation_id)
    )
    assert result.status is WorkerStatus.UNCERTAIN


def test_late_pipe_response_cannot_match_a_new_request() -> None:
    old_id = uuid4()
    new_id = uuid4()

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def is_alive(self):
            return True

        def terminate(self):
            self.terminated = True

        def join(self, _timeout):
            return None

    class LatePipe:
        def __init__(self):
            self.sent = []
            self.poll_count = 0

        def send(self, payload):
            self.sent.append(payload)

        def poll(self, _timeout):
            self.poll_count += 1
            return self.poll_count > 1

        def recv(self):
            return WorkerResult(
                request_id=old_id,
                kind=WorkerKind.OBSERVE,
                status=WorkerStatus.OK,
                worker_epoch=UUID(int=1),
            ).model_dump(mode="json")

    process = object.__new__(QQVMWorkerProcess)
    process._process = FakeProcess()
    process._parent = LatePipe()
    process._request_lock = __import__("threading").Lock()
    first = WorkerCommand(kind=WorkerKind.OBSERVE, request_id=old_id, binding_id="b")
    timed_out = process.request(first, timeout_seconds=0)
    assert timed_out.request_id == old_id
    assert timed_out.status is WorkerStatus.UNCERTAIN
    second = WorkerCommand(kind=WorkerKind.OBSERVE, request_id=new_id, binding_id="b")
    result = process.request(second, timeout_seconds=0)
    assert result.request_id == new_id
    assert result.status in {WorkerStatus.UNCERTAIN, WorkerStatus.UNAVAILABLE}


def test_abort_clears_only_unchanged_bot_owned_composer() -> None:
    worker, fake, binding_id = _worker()
    operation_id = uuid4()
    assert _prepare(worker, binding_id, operation_id).status is WorkerStatus.OK
    result = worker.execute(WorkerCommand(kind=WorkerKind.ABORT, binding_id=binding_id, operation_id=operation_id))
    assert result.status is WorkerStatus.OK
    assert fake.composer == ""

    second = uuid4()
    assert _prepare(worker, binding_id, second).status is WorkerStatus.OK
    fake.composer = "人工改过"
    refused = worker.execute(WorkerCommand(kind=WorkerKind.ABORT, binding_id=binding_id, operation_id=second))
    assert refused.status is WorkerStatus.FAILED_SAFE
    assert refused.error_code == "needs_manual_cleanup"
    assert fake.composer == "人工改过"
