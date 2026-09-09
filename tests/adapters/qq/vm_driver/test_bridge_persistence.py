from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq import BubbleDirection, QQBubble, QQConversation, QQIdentityBinding
from messenger_ai.adapters.qq.vm_driver import QQVMDriverBridge, QQVMWorker
from messenger_ai.adapters.qq.vm_driver.message_cursor import MessageCursorStore
from messenger_ai.domain import AuthorizationType, AuthorizedSendCommand, SendStatus

from test_worker import _worker


class LocalWorker:
    def __init__(self, worker): self.worker, self.requests, self.stopped = worker, [], False
    def request(self, command, _timeout):
        self.requests.append(command)
        return self.worker.execute(command)
    def stop(self): self.stopped = True


def command(conversation_id="hub-conv-1", key="send-1", text="固定测试文字"):
    return AuthorizedSendCommand(
        draft_id=uuid4(), conversation_id=conversation_id,
        expected_last_message_key="incoming-1", text_hash=hashlib.sha256(text.encode()).hexdigest(),
        idempotency_key=key, authorization_type=AuthorizationType.HUMAN,
        authorization_id=uuid4(), policy_version="v1",
        expires_at=datetime.now(UTC) + timedelta(minutes=1))


def test_bridge_aclose_stops_worker_then_closes_owner_thread_databases(tmp_path):
    worker, _, binding_id = _worker()
    local = LocalWorker(worker)
    binding = next(iter(worker._bindings.values()))
    bridge = QQVMDriverBridge(worker=local, bindings=(binding,), text_provider=lambda _: "x",
                              sqlite_path=tmp_path / "bridge.sqlite3")
    asyncio.run(bridge.aclose())
    assert local.stopped
    with pytest.raises(Exception):
        bridge._cursor.connection.execute("SELECT 1")


def test_cursor_baseline_sequence_duplicate_text_and_recovery(tmp_path):
    path = tmp_path / "cursor.sqlite3"
    store = MessageCursorStore(path)
    first = [{"direction": "inbound", "text": "嗯", "message_key": "a", "observed_at": datetime.now(UTC).isoformat()}]
    assert store.ingest_snapshot("c", first) == ()
    second = first + [
        {"direction": "inbound", "text": "嗯", "message_key": "b", "observed_at": datetime.now(UTC).isoformat()},
        {"direction": "inbound", "text": "嗯", "message_key": "c", "observed_at": datetime.now(UTC).isoformat()},
    ]
    assert store.ingest_snapshot("c", second) == ("1", "2")
    claimed = store.claim("c")
    assert [row["local_key"] for row in claimed] == ["1", "2"]
    assert store.recover() == 2
    assert [row["local_key"] for row in store.claim("c")] == ["1", "2"]


def test_bridge_persists_terminal_and_never_recommits(tmp_path):
    worker, fake, binding_id = _worker()
    fake.bubbles = [QQBubble(conversation_internal_id="qq-conv-1", message_key="anchor",
                             direction=BubbleDirection.INBOUND, text="anchor",
                             observed_at=datetime.now(UTC), tree_digest=fake.digest)]
    local = LocalWorker(worker)
    binding = next(iter(worker._bindings.values()))
    path = tmp_path / "bridge.sqlite3"
    bridge = QQVMDriverBridge(worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字", sqlite_path=path)
    cmd = command(); opid = uuid4()
    op = asyncio.run(bridge.prepare_send(cmd, operation_id=opid, segment_ref="plan:0", binding_revision=2, conversation_revision=3))
    assert op.status is SendStatus.PREPARED
    op = asyncio.run(bridge.commit_send(op)); assert op.status is SendStatus.COMMITTED
    op = asyncio.run(bridge.verify_send(op)); assert op.status is SendStatus.VERIFIED
    commits = len([item for item in local.requests if item.kind.value == "commit"])
    restarted = QQVMDriverBridge(worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字", sqlite_path=path)
    replay = asyncio.run(restarted.commit_send(op))
    assert replay.status is SendStatus.VERIFIED
    assert len([item for item in local.requests if item.kind.value == "commit"]) == commits


def test_restart_after_persisted_commit_intent_quarantines_without_worker_replay(tmp_path):
    worker, fake, _ = _worker()
    fake.bubbles = [QQBubble(conversation_internal_id="qq-conv-1", message_key="anchor",
                             direction=BubbleDirection.INBOUND, text="anchor",
                             observed_at=datetime.now(UTC), tree_digest=fake.digest)]
    local = LocalWorker(worker); binding = next(iter(worker._bindings.values()))
    path = tmp_path / "bridge.sqlite3"
    bridge = QQVMDriverBridge(worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字", sqlite_path=path)
    cmd = command(); op = asyncio.run(bridge.prepare_send(cmd, operation_id=uuid4(), segment_ref="plan:0",
                                                          binding_revision=2, conversation_revision=3))
    bridge._db.execute("UPDATE qq_vm_ops SET commit_intent=1 WHERE operation_id=?", (str(op.operation_id),))
    commits = sum(item.kind.value == "commit" for item in local.requests)
    restarted = QQVMDriverBridge(worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字", sqlite_path=path)
    quarantined = asyncio.run(restarted.commit_send(op))
    assert quarantined.status is SendStatus.UNCERTAIN
    assert sum(item.kind.value == "commit" for item in local.requests) == commits


def test_bridge_three_contacts_keep_binding_and_cursor_namespaces(tmp_path):
    worker, fake, _ = _worker()
    pack = worker._selectors
    bindings = []
    fake.conversations = []
    for number in range(3):
        fake.conversations.append(QQConversation(internal_id=f"qq-{number}", display_name="same",
            participant_signature=f"proof-{number}", last_message_key=f"in-{number}", tree_digest=fake.digest))
        bindings.append(QQIdentityBinding(hub_conversation_id=f"hub-{number}", contact_id=f"contact-{number}",
            account_id="account", platform_conversation_id=f"qq-{number}", participant_signature=f"proof-{number}",
            binding_id=f"binding-{number}"))
    multi = QQVMWorker(accessibility=fake, selector_pack=pack, bindings=tuple(bindings))
    bridge = QQVMDriverBridge(worker=LocalWorker(multi), bindings=tuple(bindings), text_provider=lambda _: "x",
                              sqlite_path=tmp_path / "multi.sqlite3")
    for number in range(3):
        batch = asyncio.run(bridge.observe_conversation(f"hub-{number}", binding_revision=1, conversation_revision=1))
        assert (batch.contact_id, batch.conversation_id, batch.messages) == (f"contact-{number}", f"hub-{number}", ())


def test_three_contacts_each_complete_bridge_send_with_distinct_target(tmp_path):
    worker, fake, _ = _worker()
    bindings = []
    fake.conversations = []
    for number in range(3):
        fake.conversations.append(QQConversation(
            internal_id=f"qq-{number}", display_name="same", participant_signature=f"proof-{number}",
            last_message_key=f"in-{number}", tree_digest=fake.digest))
        bindings.append(QQIdentityBinding(
            hub_conversation_id=f"hub-{number}", contact_id=f"contact-{number}", account_id="account",
            platform_conversation_id=f"qq-{number}", participant_signature=f"proof-{number}",
            binding_id=f"binding-{number}"))
    selected = {"id": ""}
    histories = {
        f"qq-{number}": [QQBubble(
            conversation_internal_id=f"qq-{number}", message_key=f"anchor-{number}",
            direction=BubbleDirection.INBOUND, text=f"anchor-{number}",
            observed_at=datetime.now(UTC), tree_digest=fake.digest,
        )]
        for number in range(3)
    }
    composers = {f"qq-{number}": "" for number in range(3)}
    def select(_window, conversation, _selector):
        fake.calls.append("select"); selected["id"] = conversation.internal_id
    fake.select_conversation = select
    def list_bubbles(_window, _selector):
        return list(histories[selected["id"]])
    def write_composer(_window, text, _selector):
        composers[selected["id"]] = text
    def read_composer(_window, _selector):
        return composers[selected["id"]]
    fake.list_bubbles = list_bubbles
    fake.write_composer = write_composer
    fake.read_composer = read_composer
    def invoke(_window, _selector):
        fake.calls.append("invoke-send")
        histories[selected["id"]].append(QQBubble(
            conversation_internal_id=selected["id"], message_key=f"out-{len(histories[selected['id']])}",
            direction=BubbleDirection.OUTBOUND, text=composers[selected["id"]],
            observed_at=datetime.now(UTC), tree_digest=fake.digest))
        composers[selected["id"]] = ""
    fake.invoke_send = invoke
    multi = QQVMWorker(accessibility=fake, selector_pack=worker._selectors, bindings=tuple(bindings))
    local = LocalWorker(multi)
    texts = {}; commands = []
    for number in range(3):
        item = command(f"hub-{number}", f"send-{number}", f"文字-{number}")
        texts[item.draft_id] = f"文字-{number}"; commands.append(item)
    bridge = QQVMDriverBridge(worker=local, bindings=tuple(bindings), text_provider=lambda item: texts[item.draft_id],
                              sqlite_path=tmp_path / "three.sqlite3")
    for number, item in enumerate(commands):
        operation = asyncio.run(bridge.prepare_send(
            item, operation_id=uuid4(), segment_ref=f"plan-{number}:0",
            binding_revision=1, conversation_revision=1))
        assert operation.status is SendStatus.PREPARED
        operation = asyncio.run(bridge.commit_send(operation)); assert operation.status is SendStatus.COMMITTED
        operation = asyncio.run(bridge.verify_send(operation)); assert operation.status is SendStatus.VERIFIED
    assert fake.calls.count("invoke-send") == 3
    assert all(len(history) == 2 for history in histories.values())
