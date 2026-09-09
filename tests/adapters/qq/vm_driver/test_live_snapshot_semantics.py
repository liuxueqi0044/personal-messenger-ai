"""Acceptance expectations for live QQ UIA snapshots; deliberately no driver edits."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from uuid import uuid4

from messenger_ai.adapters.qq import BubbleDirection, QQBubble
from messenger_ai.adapters.qq.vm_driver import QQVMWorker, WorkerCommand, WorkerKind, WorkerStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_qq_adapter import _adapter


class LiveLikeAccessibility:
    """Same rendered bubbles, but each UIA observation refreshes volatile fields."""
    def __init__(self, base):
        self.base, self.tick = base, 0
    def __getattr__(self, name): return getattr(self.base, name)
    def tree_digest(self, window):
        self.tick += 1
        return f"tree-{self.tick}" # composer/layout changes must not invalidate target
    def list_bubbles(self, window, selector):
        self.tick += 1
        now=datetime.now(UTC)+timedelta(seconds=self.tick)
        return [row.model_copy(update={"observed_at":now,"tree_digest":f"tree-{self.tick}"}) for row in self.base.bubbles]


def _worker():
    adapter, fake = _adapter()
    return QQVMWorker(accessibility=LiveLikeAccessibility(fake), selector_pack=adapter.selector_pack, bindings=tuple(adapter.bindings.values())), fake, next(iter(adapter.bindings.values())).binding_id


def test_volatile_observed_at_and_tree_digest_do_not_make_commit_stale():
    worker, fake, binding = _worker()
    fake.bubbles=[QQBubble(conversation_internal_id='qq-conv-1',message_key='anchor',direction=BubbleDirection.INBOUND,text='hi',observed_at=datetime.now(UTC),tree_digest='old')]
    op=uuid4()
    assert worker.execute(WorkerCommand(kind=WorkerKind.PREPARE,binding_id=binding,operation_id=op,segment_ref='p:0',text='reply')).status is WorkerStatus.OK
    assert worker.execute(WorkerCommand(kind=WorkerKind.COMMIT,binding_id=binding,operation_id=op)).status is WorkerStatus.OK
    assert fake.calls.count('invoke-send') == 1


def test_real_new_inbound_still_blocks_commit_under_volatile_snapshot():
    worker, fake, binding = _worker(); op=uuid4()
    assert worker.execute(WorkerCommand(kind=WorkerKind.PREPARE,binding_id=binding,operation_id=op,segment_ref='p:0',text='reply')).status is WorkerStatus.OK
    fake.bubbles.append(QQBubble(conversation_internal_id='qq-conv-1',message_key='new',direction=BubbleDirection.INBOUND,text='new',observed_at=datetime.now(UTC),tree_digest='x'))
    assert worker.execute(WorkerCommand(kind=WorkerKind.COMMIT,binding_id=binding,operation_id=op)).status is WorkerStatus.FAILED_SAFE


def test_outbound_operation_correlation_is_single_use_not_text_based(tmp_path):
    import asyncio
    from test_bridge_persistence import LocalWorker, command
    from messenger_ai.adapters.qq.vm_driver import QQVMDriverBridge
    worker, fake, binding = _worker()
    fake.bubbles=[QQBubble(conversation_internal_id='qq-conv-1',message_key='anchor',direction=BubbleDirection.INBOUND,text='a',observed_at=datetime.now(UTC),tree_digest='x')]
    bridge=QQVMDriverBridge(worker=LocalWorker(worker),bindings=tuple(worker._bindings.values()),text_provider=lambda _:'固定测试文字',sqlite_path=tmp_path/'b.sqlite3')
    asyncio.run(bridge.observe_conversation('hub-conv-1',binding_revision=1,conversation_revision=1))
    opid=uuid4(); op=asyncio.run(bridge.prepare_send(command(),operation_id=opid,segment_ref='p:0',binding_revision=1,conversation_revision=1)); op=asyncio.run(bridge.commit_send(op)); asyncio.run(bridge.verify_send(op))
    batch=asyncio.run(bridge.observe_conversation('hub-conv-1',binding_revision=1,conversation_revision=1))
    assert any(m.operation_id == opid for m in batch.messages)
    fake.bubbles.append(QQBubble(conversation_internal_id='qq-conv-1',message_key='human-same',direction=BubbleDirection.OUTBOUND,text='固定测试文字',observed_at=datetime.now(UTC),tree_digest='x'))
    later=asyncio.run(bridge.observe_conversation('hub-conv-1',binding_revision=1,conversation_revision=1))
    assert any(m.operation_id is None for m in later.messages)
