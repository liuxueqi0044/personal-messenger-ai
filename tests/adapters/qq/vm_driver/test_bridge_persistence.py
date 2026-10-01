from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_worker import _worker

from messenger_ai.adapters.qq import (
    BubbleDirection,
    QQBubble,
    QQConversation,
    QQIdentityBinding,
)
from messenger_ai.adapters.qq.vm_driver import (
    QQVMDriverBridge,
    QQVMWorker,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
)
from messenger_ai.adapters.qq.vm_driver.message_cursor import (
    CURRENT_IDENTITY_SCHEMA,
    IDENTITY_SCHEMA_MIGRATION_REQUIRED,
    MessageCursorStore,
)
from messenger_ai.adapters.qq.vm_driver.quarantine_release import (
    release_observation_quarantine,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import runtime_id_digest
from messenger_ai.domain import (
    AuthorizationType,
    AuthorizedSendCommand,
    ErrorCode,
    SendStatus,
)
from messenger_ai.runtime.state import RuntimeState


class LocalWorker:
    def __init__(self, worker): self.worker, self.requests, self.stopped = worker, [], False
    def request(self, command, _timeout):
        self.requests.append(command)
        return self.worker.execute(command)
    def stop(self): self.stopped = True
    def mint_selection_handoff(self, **kwargs):
        return self.worker.mint_selection_handoff(**kwargs)


class FreshVerifyWorker(LocalWorker):
    fresh_verify_capable = True

    def __init__(self, worker, *, successor=None):
        super().__init__(worker)
        self.successor = successor
        self.started = False
        self._alive = True
        self._exit_code = None

    def start(self):
        self.started = True

    def spawn_successor(self):
        if self.successor is None:
            raise AssertionError("successor missing")
        return self.successor

    def stop(self, _timeout_seconds=5):
        self.stopped = True
        self._alive = False
        self._exit_code = 0

    def status_snapshot(self):
        return {
            "run_id": "fresh-verify-test",
            "worker_process_id": id(self),
            "worker_alive": self._alive,
            "worker_exit_code": self._exit_code,
            "parent_terminate_reason": None,
            "startup_health": None,
            "last_request": None,
            "last_successful_observe": None,
            "first_terminal_failure": None,
        }


class RecoverableWorker:
    """Parent-process fake exposing the exact terminal evidence recovery requires."""

    def __init__(self, *, run_id: str, fail_observe: bool, successor=None):
        self.run_id = run_id
        self.fail_observe = fail_observe
        self.successor = successor
        self.requests = []
        self.spawn_count = 0
        self.started = False
        self.stopped = False
        self._terminal = None
        self._alive = True
        self._exit_code = None
        self._last_observe = None

    def start(self): self.started = True

    def stop(self): self.stopped = True; self._alive = False

    def spawn_successor(self):
        self.spawn_count += 1
        if self.successor is None:
            raise AssertionError("unexpected successor request")
        return self.successor

    def request(self, command, _timeout):
        self.requests.append(command)
        if command.kind is WorkerKind.HEALTH:
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                status=WorkerStatus.OK, worker_epoch=uuid4(),
            )
        if command.kind is WorkerKind.OBSERVE and self.fail_observe:
            self._alive = False
            self._exit_code = -15
            self._terminal = {
                "request_id": str(command.request_id),
                "kind": command.kind.value,
                "binding_id": command.binding_id,
                "error_code": "worker_timeout_isolated",
                "parent_terminate_reason": "request_timeout",
                "worker_exit_code": -15,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.UNCERTAIN, worker_epoch=uuid4(),
                error_code="worker_timeout_isolated",
            )
        if command.kind is WorkerKind.OBSERVE:
            self._last_observe = {
                "request_id": str(command.request_id), "kind": "observe",
                "binding_id": command.binding_id, "status": "ok",
                "completed_at": datetime.now(UTC).isoformat(),
            }
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                operation_id=command.operation_id, binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK, worker_epoch=uuid4(),
                evidence={"bubbles": []},
            )
        return WorkerResult(
            request_id=command.request_id, kind=command.kind,
            operation_id=command.operation_id, binding_id=command.binding_id,
            binding_revision=command.binding_revision,
            conversation_revision=command.conversation_revision,
            status=WorkerStatus.UNCERTAIN, worker_epoch=uuid4(),
            error_code="worker_timeout_isolated",
        )

    def status_snapshot(self):
        return {
            "run_id": self.run_id,
            "worker_process_id": 88,
            "worker_alive": self._alive,
            "worker_exit_code": self._exit_code,
            "parent_terminate_reason": (
                "request_timeout" if self._terminal is not None else None
            ),
            "startup_health": None,
            "last_request": self._terminal,
            "last_successful_observe": self._last_observe,
            "first_terminal_failure": self._terminal,
        }


class SelectionRefreshWorker:
    """Live-process fake for the bounded selection process handoff."""

    def __init__(self, outcomes, *, successor=None, result_update=None,
                 confirm_stop=True, events=None, label="worker",
                 start_after=None):
        self.outcomes = list(outcomes)
        self.successor = successor
        self.result_update = dict(result_update or {})
        self.confirm_stop = confirm_stop
        self.events = events
        self.label = label
        self.start_after = start_after
        self.requests = []
        self.request_timeouts = []
        self.spawn_count = 0
        self.started = False
        self.stopped = False
        self._alive = True
        self._exit_code = None
        self._selection_handoff_signing_key = b"selection-refresh-test-key-32b!!"

    def mint_selection_handoff(self, **kwargs):
        return mint_selection_handoff(
            **kwargs, signing_key=self._selection_handoff_signing_key,
        )

    def start(self):
        if self.events is not None:
            self.events.append(f"{self.label}:start")
        if self.start_after is not None:
            assert self.start_after._alive is False
        self.started = True

    def spawn_successor(self):
        if self.events is not None:
            self.events.append(f"{self.label}:spawn")
        self.spawn_count += 1
        if self.successor is None:
            raise AssertionError("unexpected successor request")
        return self.successor

    def stop(self, _timeout_seconds=5):
        if self.events is not None:
            self.events.append(f"{self.label}:stop")
        self.stopped = True
        if self.confirm_stop:
            self._alive = False
            self._exit_code = 0

    def request(self, command, timeout):
        if self.events is not None:
            self.events.append(f"{self.label}:{command.kind.value}")
        self.requests.append(command)
        self.request_timeouts.append(timeout)
        if command.kind is WorkerKind.HEALTH:
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.OK,
                worker_epoch=uuid4(),
            )
        outcome = self.outcomes.pop(0)
        if outcome == "refresh":
            values = {
                "request_id": command.request_id,
                "kind": command.kind,
                "operation_id": command.operation_id,
                "binding_id": command.binding_id,
                "binding_revision": command.binding_revision,
                "conversation_revision": command.conversation_revision,
                "status": WorkerStatus.FAILED_SAFE,
                "worker_epoch": uuid4(),
                "error_code": "selection_process_refresh_required",
            }
            values.update(self.result_update)
            return WorkerResult(**values)
        if outcome == "identity":
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=uuid4(),
                error_code="profile_identity_mismatch",
            )
        return WorkerResult(
            request_id=command.request_id,
            kind=command.kind,
            operation_id=command.operation_id,
            binding_id=command.binding_id,
            binding_revision=command.binding_revision,
            conversation_revision=command.conversation_revision,
            status=WorkerStatus.OK,
            worker_epoch=uuid4(),
            evidence={"bubbles": []} if command.kind is WorkerKind.OBSERVE else {},
        )

    def status_snapshot(self):
        if self.events is not None:
            self.events.append(f"{self.label}:status")
        return {
            "run_id": "selection-refresh",
            "worker_process_id": 91,
            "worker_alive": self._alive,
            "worker_exit_code": self._exit_code,
            "parent_terminate_reason": None,
            "startup_health": None,
            "last_request": None,
            "last_successful_observe": None,
            "first_terminal_failure": None,
        }


def _two_bindings():
    worker, _, _ = _worker()
    first = next(iter(worker._bindings.values()))
    second = first.model_copy(update={
        "hub_conversation_id": "hub-conv-2",
        "contact_id": "contact-2",
        "platform_conversation_id": "qq-conv-2",
        "participant_signature": "profile-proof-2",
        "binding_id": "binding-2",
    })
    return first, second


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


def test_observation_classifies_temporary_and_identity_failures(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))

    class FailedWorker:
        error_code = "qq_window_ambiguous"
        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id,
                kind=WorkerKind.OBSERVE,
                status=WorkerStatus.UNAVAILABLE,
                worker_epoch=uuid4(),
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code=self.error_code,
            )
        def stop(self): pass

    failed = FailedWorker()
    bridge = QQVMDriverBridge(
        worker=failed,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "failure.sqlite3",
    )
    temporary = asyncio.run(bridge.observe_conversation(
        binding.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))
    assert temporary.gap_reason == "driver_temporary:qq_window_ambiguous"

    failed.error_code = "session_identity_process_restarted"
    identity = asyncio.run(bridge.observe_conversation(
        binding.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))
    assert identity.gap_reason == "identity_guard:session_identity_process_restarted"


def test_cached_health_does_not_issue_a_second_worker_request(tmp_path):
    worker, _, _ = _worker()
    local = LocalWorker(worker)
    binding = next(iter(worker._bindings.values()))
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "health.sqlite3",
    )
    first = bridge.probe_health()
    request_count = len(local.requests)
    assert first.status is WorkerStatus.OK
    assert bridge.health() is first
    assert len(local.requests) == request_count


def test_cached_health_reports_known_terminal_worker_without_ipc(tmp_path):
    worker, _, _ = _worker()
    local = LocalWorker(worker)
    status = {
        "run_id": "run-1",
        "worker_process_id": 42,
        "worker_alive": True,
        "worker_exit_code": None,
        "parent_terminate_reason": None,
        "first_terminal_failure": None,
    }
    local.status_snapshot = lambda: status
    binding = next(iter(worker._bindings.values()))
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "terminal-health.sqlite3",
    )
    assert bridge.probe_health().status is WorkerStatus.OK
    request_count = len(local.requests)
    status.update({
        "worker_alive": False,
        "worker_exit_code": -15,
        "parent_terminate_reason": "request_timeout",
        "first_terminal_failure": {"error_code": "worker_timeout_isolated"},
    })

    current = bridge.health()

    assert current.status is WorkerStatus.UNAVAILABLE
    assert current.error_code == "worker_timeout_isolated"
    assert len(local.requests) == request_count


def test_explicit_latest_inbound_adoption_rejects_same_text_with_changed_count(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    text = "你好，你好"
    bubbles = [
        {
            "conversation_internal_id": "visible-current-conversation",
            "message_key": f"in-{index}",
            "direction": "inbound",
            "text": text,
            "observed_at": datetime.now(UTC).isoformat(),
            "tree_digest": "tree",
        }
        for index in (1, 2)
    ]

    class SnapshotWorker:
        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK,
                worker_epoch=uuid4(),
                evidence={"bubbles": bubbles},
            )
        def stop(self): pass

    conversation_id = binding.hub_conversation_id
    bridge = QQVMDriverBridge(
        worker=SnapshotWorker(), bindings=(binding,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "adoption-guard.sqlite3",
        bootstrap_last_inbound=(conversation_id,),
        bootstrap_last_inbound_provenance={conversation_id: {
            "binding_id": binding.binding_id,
            "participant_signature": binding.participant_signature,
            "bubble_count": 1,
            "last_ordinal": 0,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }},
    )

    batch = asyncio.run(bridge.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=1,
    ))

    assert batch.complete is False
    assert batch.gap_reason == "bootstrap_last_inbound_evidence_mismatch"
    assert bridge._cursor.connection.execute(
        "SELECT 1 FROM cursor_state WHERE conversation_id=?", (conversation_id,)
    ).fetchone() is None


def test_restart_with_stale_adoption_config_uses_durable_cursor_without_replay(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    conversation_id = binding.hub_conversation_id
    initial_text, fresh_text = "你好，你好", "重启后的新消息"

    def bubble(key: str, text: str) -> dict[str, object]:
        return {
            "conversation_internal_id": "visible-current-conversation",
            "message_key": key,
            "direction": "inbound",
            "text": text,
            "observed_at": datetime.now(UTC).isoformat(),
            "tree_digest": "tree",
        }

    class SnapshotWorker:
        def __init__(self): self.bubbles = [bubble("in-1", initial_text)]
        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK, worker_epoch=uuid4(),
                evidence={"bubbles": list(self.bubbles)},
            )
        def stop(self): pass

    local = SnapshotWorker()
    path = tmp_path / "restart-adoption.sqlite3"
    provenance = {conversation_id: {
        "binding_id": binding.binding_id,
        "participant_signature": binding.participant_signature,
        "bubble_count": 1,
        "last_ordinal": 0,
        "text_sha256": hashlib.sha256(initial_text.encode()).hexdigest(),
    }}
    first = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "x", sqlite_path=path,
        bootstrap_last_inbound=(conversation_id,),
        bootstrap_last_inbound_provenance=provenance,
    )
    adopted = asyncio.run(first.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=1,
    ))
    assert [item.text for item in adopted.messages] == [initial_text]
    first.acknowledge_observation(
        conversation_id, tuple(item.local_message_key for item in adopted.messages)
    )
    first.close()

    local.bubbles.append(bubble("in-2", fresh_text))
    restarted = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "x", sqlite_path=path,
        bootstrap_last_inbound=(conversation_id,),
        bootstrap_last_inbound_provenance=provenance,
    )
    observed = asyncio.run(restarted.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=2,
    ))

    assert observed.complete is True
    assert [item.text for item in observed.messages] == [fresh_text]
    assert [item.local_message_key for item in observed.messages] == ["2"]


def test_restart_bounded_snapshot_without_persisted_anchor_reports_message_anchor_gap(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    conversation_id = binding.hub_conversation_id
    anchor_text = "持久化锚点"

    def bubble(key: str, text: str) -> dict[str, object]:
        return {
            "conversation_internal_id": "recreated-locator",
            "message_key": key,
            "direction": "inbound",
            "text": text,
            "observed_at": datetime.now(UTC).isoformat(),
            "tree_digest": "tree",
        }

    class SnapshotWorker:
        def __init__(self, bubbles): self.bubbles = list(bubbles)
        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK, worker_epoch=uuid4(),
                evidence={"bubbles": list(self.bubbles)},
            )
        def stop(self): pass

    path = tmp_path / "anchor-gap.sqlite3"
    first = QQVMDriverBridge(
        worker=SnapshotWorker([bubble("anchor", anchor_text)]),
        bindings=(binding,), text_provider=lambda _: "x", sqlite_path=path,
    )
    assert asyncio.run(first.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=1,
    )).complete is True
    persisted = first._cursor.snapshot_token(conversation_id)
    assert persisted is not None
    first.close()

    # A restarted client can only surface a bounded tail whose rows no longer
    # overlap the durable anchor, so alignment must fail closed.
    limited = SnapshotWorker([bubble("new-1", "重启后一条"), bubble("new-2", "重启后两条")])
    restarted = QQVMDriverBridge(
        worker=limited, bindings=(binding,), text_provider=lambda _: "x", sqlite_path=path,
    )
    gapped = asyncio.run(restarted.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=2,
    ))

    assert gapped.complete is False
    assert gapped.gap_reason == "message_anchor_gap"
    assert gapped.messages == ()
    # No adoption and no replay: the durable cursor is untouched and nothing was
    # queued from the unrelated bounded window.
    assert restarted._cursor.snapshot_token(conversation_id) == persisted
    assert restarted._cursor.connection.execute(
        "SELECT COUNT(*) FROM observation_outbox WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()[0] == 0

    # The runtime persists exactly the pause reason the reanchor tool clears.
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    state.register(
        account_id=binding.account_id, contact_id=binding.contact_id,
        conversation_id=conversation_id, binding_revision=1, conversation_type="direct",
    )
    state.connection.execute(
        "UPDATE runtime_conversations SET paused=0,pause_reason=NULL WHERE conversation_id=?",
        (conversation_id,),
    )
    state.connection.commit()
    assert state.apply_observation(gapped) == ()
    row = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert (row["paused"], row["pause_reason"]) == (1, "message_anchor_gap")
    state.close()

    # Once the durable anchor reappears, only the genuine tail is emitted.
    limited.bubbles = [
        bubble("anchor", anchor_text), bubble("new-1", "重启后一条"),
        bubble("new-2", "重启后两条"),
    ]
    resumed = asyncio.run(restarted.observe_conversation(
        conversation_id, binding_revision=1, conversation_revision=3,
    ))
    assert resumed.complete is True
    assert [item.text for item in resumed.messages] == ["重启后一条", "重启后两条"]
    restarted.close()


def test_bridge_refuses_legacy_cursor_until_schema_reanchor(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    conversation_id = binding.hub_conversation_id

    def bubble(key: str, text: str, locator: str) -> dict[str, object]:
        return {
            "conversation_internal_id": locator,
            "message_key": key,
            "direction": "inbound",
            "text": text,
            "observed_at": datetime.now(UTC).isoformat(),
            "tree_digest": "tree",
        }

    class SnapshotWorker:
        def __init__(self, bubbles):
            self.bubbles = list(bubbles)

        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK,
                worker_epoch=uuid4(),
                evidence={"bubbles": list(self.bubbles)},
            )

        def stop(self):
            pass

    legacy_anchor = bubble("anchor", "legacy anchor", "old-runtime-locator")
    legacy_payload = {
        key: legacy_anchor.get(key)
        for key in (
            "direction",
            "text",
            "message_key",
            "conversation_internal_id",
        )
    }
    legacy_identity = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    path = tmp_path / "legacy-schema.sqlite3"
    cursor_path = path.with_suffix(".cursor.sqlite3")
    with sqlite3.connect(cursor_path) as connection:
        connection.execute(
            """CREATE TABLE cursor_state(
                   conversation_id TEXT PRIMARY KEY,
                   next_seq INTEGER NOT NULL,
                   snapshot_json TEXT NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO cursor_state VALUES(?,?,?)",
            (conversation_id, 9, json.dumps([legacy_identity])),
        )

    visible_anchor = bubble("anchor", "legacy anchor", "new-runtime-locator")
    local = SnapshotWorker([visible_anchor])
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=path,
    )
    before = tuple(
        bridge._cursor.connection.execute(
            """SELECT next_seq,snapshot_json,identity_schema FROM cursor_state
               WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
    )
    refused = asyncio.run(
        bridge.observe_conversation(
            conversation_id,
            binding_revision=1,
            conversation_revision=1,
        )
    )
    assert refused.complete is False
    assert refused.gap_reason == IDENTITY_SCHEMA_MIGRATION_REQUIRED
    assert refused.messages == ()
    assert tuple(
        bridge._cursor.connection.execute(
            """SELECT next_seq,snapshot_json,identity_schema FROM cursor_state
               WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
    ) == before
    assert bridge._cursor.connection.execute(
        "SELECT COUNT(*) FROM observation_outbox WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()[0] == 0

    legacy_token = bridge._cursor.snapshot_token(conversation_id)
    assert legacy_token is not None
    reanchored = bridge._cursor.reanchor_snapshot(
        conversation_id,
        [visible_anchor],
        expected_snapshot_sha256=legacy_token,
        operator_id="operator-1",
        reason_code="IDENTITY_SCHEMA_MIGRATION",
    )
    assert reanchored.applied is True
    assert bridge._cursor.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()[0] == CURRENT_IDENTITY_SCHEMA
    aligned = asyncio.run(
        bridge.observe_conversation(
            conversation_id,
            binding_revision=1,
            conversation_revision=2,
        )
    )
    assert aligned.complete is True
    assert aligned.messages == ()
    bridge.close()

    restarted = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=path,
    )
    same_schema = asyncio.run(
        restarted.observe_conversation(
            conversation_id,
            binding_revision=1,
            conversation_revision=3,
        )
    )
    assert same_schema.complete is True
    assert same_schema.messages == ()
    restarted.close()


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


def test_one_shot_bridge_initialization_does_not_recover_shared_state(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    path = tmp_path / "one-shot-no-global-recovery.sqlite3"
    first = QQVMDriverBridge(
        worker=LocalWorker(worker),
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=path,
    )
    operation_id = uuid4()
    first._db.execute(
        "INSERT INTO qq_vm_ops VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(operation_id),
            "one-shot-no-recovery",
            str(uuid4()),
            binding.hub_conversation_id,
            binding.binding_id,
            "one-shot-no-recovery:0",
            1,
            1,
            "a" * 64,
            SendStatus.PREPARED.value,
            1,
            None,
        ),
    )
    baseline = {
        "direction": "inbound",
        "text": "baseline",
        "message_key": "a",
        "observed_at": datetime.now(UTC).isoformat(),
    }
    first._cursor.ingest_snapshot(binding.hub_conversation_id, [baseline])
    first._cursor.ingest_snapshot(
        binding.hub_conversation_id,
        [
            baseline,
            {
                "direction": "inbound",
                "text": "new",
                "message_key": "b",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        ],
    )
    assert len(first._cursor.claim(binding.hub_conversation_id)) == 1
    first.close()

    restarted = QQVMDriverBridge(
        worker=LocalWorker(worker),
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=path,
        recover_persistent_state=False,
    )

    assert restarted._db.execute(
        "SELECT status FROM qq_vm_ops WHERE operation_id=?", (str(operation_id),)
    ).fetchone()["status"] == SendStatus.PREPARED.value
    assert restarted._cursor.connection.execute(
        "SELECT status FROM observation_outbox"
    ).fetchone()["status"] == "dispatching"
    restarted.close()


def test_bridge_retires_commit_worker_and_verifies_with_fresh_worker(tmp_path):
    first_worker, fake, _ = _worker()
    fake.bubbles = [QQBubble(
        conversation_internal_id="qq-conv-1",
        message_key="anchor",
        direction=BubbleDirection.INBOUND,
        text="anchor",
        observed_at=datetime.now(UTC),
        tree_digest=fake.digest,
    )]
    binding = next(iter(first_worker._bindings.values()))
    second_worker = QQVMWorker(
        accessibility=fake,
        selector_pack=first_worker._selectors,
        bindings=(binding,),
    )
    successor = FreshVerifyWorker(second_worker)
    first = FreshVerifyWorker(first_worker, successor=successor)
    bridge = QQVMDriverBridge(
        worker=first,
        bindings=(binding,),
        text_provider=lambda _: "one shot reply",
        sqlite_path=tmp_path / "fresh-verify.sqlite3",
    )
    item = command(text="one shot reply")
    operation = asyncio.run(bridge.prepare_send(
        item,
        operation_id=uuid4(),
        segment_ref="fresh-verify:0",
        binding_revision=1,
        conversation_revision=1,
    ))
    assert operation.status is SendStatus.PREPARED

    operation = asyncio.run(bridge.commit_send(operation))
    assert operation.status is SendStatus.COMMITTED
    assert first.stopped is True
    assert successor.started is True
    assert bridge._worker_generation == 2

    operation = asyncio.run(bridge.verify_send(operation))
    assert operation.status is SendStatus.VERIFIED
    verify = [item for item in successor.requests if item.kind is WorkerKind.VERIFY]
    assert len(verify) == 1
    assert verify[0].prepared_evidence is not None


def test_prepare_failure_persists_worker_error_code_across_restart(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))

    class FailedPrepareWorker:
        def __init__(self, error_code):
            self.error_code = error_code
            self.requests = []

        def request(self, item, _timeout):
            self.requests.append(item)
            return WorkerResult(
                request_id=item.request_id,
                kind=item.kind,
                operation_id=item.operation_id,
                binding_id=item.binding_id,
                binding_revision=item.binding_revision,
                conversation_revision=item.conversation_revision,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=uuid4(),
                error_code=self.error_code,
            )

        def stop(self):
            pass

    path = tmp_path / "prepare-failure.sqlite3"
    local = FailedPrepareWorker("composer_not_empty")
    bridge = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=path,
    )
    cmd = command()
    operation_id = uuid4()
    failed = asyncio.run(bridge.prepare_send(
        cmd, operation_id=operation_id, segment_ref="prepare-failure:0",
        binding_revision=1, conversation_revision=1,
    ))
    assert failed.status is SendStatus.FAILED
    assert failed.error_code == "composer_not_empty"
    assert bridge._db.execute(
        "SELECT error_code FROM qq_vm_ops WHERE operation_id=?", (str(operation_id),)
    ).fetchone()[0] == "composer_not_empty"

    restarted = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=path,
    )
    recovered = asyncio.run(restarted.prepare_send(
        cmd, operation_id=operation_id, segment_ref="prepare-failure:0",
        binding_revision=1, conversation_revision=1,
    ))
    assert recovered.status is SendStatus.FAILED
    assert recovered.error_code == "composer_not_empty"
    assert len(local.requests) == 1


def test_prepare_failure_without_worker_error_code_falls_back_to_failed_safe(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))

    class FailedPrepareWorker:
        def request(self, item, _timeout):
            return WorkerResult(
                request_id=item.request_id,
                kind=item.kind,
                operation_id=item.operation_id,
                binding_id=item.binding_id,
                binding_revision=item.binding_revision,
                conversation_revision=item.conversation_revision,
                status=WorkerStatus.UNAVAILABLE,
                worker_epoch=uuid4(),
            )

        def stop(self):
            pass

    bridge = QQVMDriverBridge(
        worker=FailedPrepareWorker(), bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "prepare-failure-fallback.sqlite3",
    )
    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref="prepare-failure-fallback:0",
        binding_revision=1, conversation_revision=1,
    ))
    assert operation.status is SendStatus.FAILED
    assert operation.error_code == ErrorCode.FAILED_SAFE.value


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


def test_observe_selection_refresh_replaces_process_and_retries_once(tmp_path):
    first, _ = _two_bindings()
    events = []
    successor = SelectionRefreshWorker(
        ["ok"], events=events, label="successor",
    )
    failed = SelectionRefreshWorker(
        ["refresh"], successor=successor, events=events, label="old",
    )
    successor.start_after = failed
    bridge = QQVMDriverBridge(
        worker=failed,
        bindings=(first,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-observe.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=4,
        conversation_revision=9,
    ))

    assert batch.complete is True
    assert failed.spawn_count == 1
    assert failed.stopped is True
    assert successor.started is True
    assert [item.kind for item in successor.requests] == [
        WorkerKind.HEALTH, WorkerKind.OBSERVE,
    ]
    initial = failed.requests[0]
    retried = successor.requests[1]
    assert retried.request_id != initial.request_id
    assert (
        retried.kind, retried.binding_id, retried.binding_revision,
        retried.conversation_revision, retried.operation_id, retried.text,
        retried.deadline,
    ) == (
        initial.kind, initial.binding_id, initial.binding_revision,
        initial.conversation_revision, initial.operation_id, initial.text,
        initial.deadline,
    )
    assert successor.requests[0].deadline == initial.deadline
    assert failed.request_timeouts[0] >= successor.request_timeouts[0]
    assert successor.request_timeouts[0] >= successor.request_timeouts[1]
    assert events == [
        "old:observe",
        "old:spawn",
        "old:stop",
        "old:status",
        "successor:start",
        "successor:health",
        "successor:observe",
    ]
    assert bridge.worker_status_snapshot()["worker_generation"] == 2


def test_selection_refresh_second_signal_is_exhausted_without_third_process(tmp_path):
    first, _ = _two_bindings()
    forbidden = SelectionRefreshWorker(["ok"])
    successor = SelectionRefreshWorker(["refresh"], successor=forbidden)
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-exhausted.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.complete is False
    assert batch.gap_reason == "driver_temporary:selection_process_refresh_exhausted"
    assert failed.spawn_count == 1
    assert successor.spawn_count == 0
    assert successor.stopped is True
    assert successor.status_snapshot()["worker_alive"] is False
    assert forbidden.started is False


@pytest.mark.parametrize(
    "result_update",
    [
        {"request_id": uuid4()},
        {"kind": WorkerKind.PREPARE},
        {"binding_id": "wrong-binding"},
        {"binding_revision": 99},
        {"conversation_revision": 99},
        {"operation_id": uuid4()},
    ],
)
def test_selection_refresh_requires_exact_response_correlation(tmp_path, result_update):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(
        ["refresh"], successor=successor, result_update=result_update,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / f"selection-refresh-correlation-{uuid4()}.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=2,
    ))

    assert batch.complete is False
    assert failed.spawn_count == 0
    assert successor.started is False


def test_prepare_selection_refresh_keeps_pending_until_retry_result(tmp_path):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "selection-refresh-prepare.sqlite3",
    )
    operation_id = uuid4()
    cmd = command()

    operation = asyncio.run(bridge.prepare_send(
        cmd,
        operation_id=operation_id,
        segment_ref="selection-refresh-prepare:0",
        binding_revision=3,
        conversation_revision=7,
    ))

    assert operation.status is SendStatus.PREPARED
    stored = bridge._db.execute(
        "SELECT status,error_code FROM qq_vm_ops WHERE operation_id=?",
        (str(operation_id),),
    ).fetchone()
    assert (stored["status"], stored["error_code"]) == (
        SendStatus.PREPARED.value, None,
    )
    initial = failed.requests[0]
    retried = successor.requests[1]
    assert retried.request_id != initial.request_id
    assert (
        retried.operation_id, retried.segment_ref, retried.binding_revision,
        retried.conversation_revision, retried.text, retried.deadline,
    ) == (
        initial.operation_id, initial.segment_ref, initial.binding_revision,
        initial.conversation_revision, initial.text, initial.deadline,
    )

    repeated = asyncio.run(bridge.prepare_send(
        cmd,
        operation_id=operation_id,
        segment_ref="selection-refresh-prepare:0",
        binding_revision=3,
        conversation_revision=7,
    ))
    assert repeated.status is SendStatus.PREPARED
    assert len(successor.requests) == 2
    assert successor.spawn_count == 0


def test_prepare_selection_refresh_is_blocked_by_another_nonterminal_operation(tmp_path):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "selection-refresh-prepare-blocked.sqlite3",
    )
    bridge._db.execute(
        """INSERT INTO qq_vm_ops(
             operation_id,idempotency_key,draft_id,conversation_id,binding_id,
             segment_ref,binding_revision,conversation_revision,text_hash,status,
             commit_intent,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
        (
            str(uuid4()), str(uuid4()), str(uuid4()), first.hub_conversation_id,
            first.binding_id, str(uuid4()), 1, 1, "hash", SendStatus.PREPARED.value,
        ),
    )

    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref="selection-refresh-blocked:0",
        binding_revision=1, conversation_revision=1,
    ))

    assert operation.status is SendStatus.FAILED
    assert operation.error_code == "selection_process_refresh_required"
    assert failed.spawn_count == 0
    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is False


def test_identity_failure_is_preserved_without_selection_refresh(tmp_path):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["identity"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-identity.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "identity_guard:profile_identity_mismatch"
    assert failed.spawn_count == 0
    assert successor.started is False


def test_failed_successor_health_still_cleans_unactivated_successor(tmp_path):
    first, _ = _two_bindings()

    class SlowHealthSuccessor(SelectionRefreshWorker):
        def request(self, item, timeout):
            if item.kind is WorkerKind.HEALTH:
                self.requests.append(item)
                self.request_timeouts.append(timeout)
                time.sleep(0.02)
                return WorkerResult(
                    request_id=item.request_id,
                    kind=item.kind,
                    status=WorkerStatus.FAILED_SAFE,
                    worker_epoch=uuid4(),
                    error_code="deadline_expired",
                )
            return super().request(item, timeout)

    successor = SlowHealthSuccessor(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-cleanup.sqlite3",
        timeout_seconds=1,
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.complete is False
    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert successor.stopped is True
    assert successor.status_snapshot()["worker_alive"] is False
    assert bridge._pending_successor is None
    assert bridge._worker is failed
    assert bridge.worker_status_snapshot()["worker_generation"] == 1


def test_deadline_consumed_stopping_old_worker_never_starts_successor(
    tmp_path, monkeypatch,
):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-stop-deadline.sqlite3",
    )
    # Initial request, activation precheck and stop all have budget.  The fixed
    # logical deadline becomes exhausted only after the completed old-worker
    # stop, immediately before successor.start().
    remaining = iter((1.0, 1.0, 0.0))
    monkeypatch.setattr(bridge, "_remaining_seconds", lambda _deadline: next(remaining))

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is False
    assert successor.stopped is True
    assert bridge._worker is failed
    assert bridge.worker_status_snapshot()["worker_generation"] == 1


def test_expired_deadline_after_refresh_still_retires_old_worker(
    tmp_path, monkeypatch,
):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-expired-before-spawn.sqlite3",
    )
    # The initial request is admitted, but no logical budget remains when the
    # bridge reaches the successor construction gate.
    remaining = iter((1.0, 0.0))
    monkeypatch.setattr(bridge, "_remaining_seconds", lambda _deadline: next(remaining))

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert failed.spawn_count == 0
    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is False
    assert bridge._worker is failed


def test_old_worker_stop_once_started_is_awaited_before_bridge_returns(tmp_path):
    first, _ = _two_bindings()

    class BlockingStopWorker(SelectionRefreshWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stop_started = threading.Event()
            self.stop_release = threading.Event()

        def stop(self, timeout_seconds=5):
            self.stop_started.set()
            assert self.stop_release.wait(timeout=1)
            super().stop(timeout_seconds)

    successor = SelectionRefreshWorker(["ok"])
    failed = BlockingStopWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-stop-awaited.sqlite3",
    )

    async def scenario():
        task = asyncio.create_task(bridge.observe_conversation(
            first.hub_conversation_id,
            binding_revision=1,
            conversation_revision=1,
        ))
        assert await asyncio.to_thread(failed.stop_started.wait, 1)
        assert task.done() is False
        assert successor.started is False
        failed.stop_release.set()
        return await task

    batch = asyncio.run(scenario())

    assert batch.complete is True
    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is True


def test_cancelled_refresh_waits_for_started_old_worker_stop(tmp_path):
    first, _ = _two_bindings()

    class BlockingStopWorker(SelectionRefreshWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stop_started = threading.Event()
            self.stop_release = threading.Event()

        def stop(self, timeout_seconds=5):
            self.stop_started.set()
            assert self.stop_release.wait(timeout=1)
            super().stop(timeout_seconds)

    successor = SelectionRefreshWorker(["ok"])
    failed = BlockingStopWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-stop-cancelled.sqlite3",
    )

    async def scenario():
        task = asyncio.create_task(bridge.observe_conversation(
            first.hub_conversation_id,
            binding_revision=1,
            conversation_revision=1,
        ))
        assert await asyncio.to_thread(failed.stop_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        failed.stop_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is False
    assert successor.stopped is True
    assert bridge._pending_successor is None


def test_unconfirmed_old_worker_exit_refuses_activation_and_cleans_successor(tmp_path):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(
        ["refresh"], successor=successor, confirm_stop=False,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-old-exit.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is True
    assert successor.stopped is True
    assert successor.started is False
    assert successor.requests == []
    assert bridge._worker is failed
    assert bridge.worker_status_snapshot()["worker_generation"] == 1


def test_successor_health_exception_is_failed_safe_and_successor_is_cleaned(tmp_path):
    first, _ = _two_bindings()

    class RaisingHealthSuccessor(SelectionRefreshWorker):
        def request(self, item, timeout):
            if item.kind is WorkerKind.HEALTH:
                self.requests.append(item)
                self.request_timeouts.append(timeout)
                raise OSError("health failed")
            return super().request(item, timeout)

    successor = RaisingHealthSuccessor(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-health-exception.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is True
    assert successor.stopped is True
    assert bridge._pending_successor is None
    assert bridge._worker is failed


def test_successor_start_exception_occurs_only_after_old_exit_and_is_cleaned(tmp_path):
    first, _ = _two_bindings()
    events = []

    class RaisingStartSuccessor(SelectionRefreshWorker):
        def start(self):
            self.events.append(f"{self.label}:start")
            assert self.start_after._alive is False
            raise OSError("start failed")

    successor = RaisingStartSuccessor(
        ["ok"], events=events, label="successor",
    )
    failed = SelectionRefreshWorker(
        ["refresh"], successor=successor, events=events, label="old",
    )
    successor.start_after = failed
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "selection-refresh-start-exception.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert batch.gap_reason == "driver_temporary:selection_process_refresh_required"
    assert events[:5] == [
        "old:observe", "old:spawn", "old:stop", "old:status",
        "successor:start",
    ]
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.stopped is True
    assert successor.requests == []
    assert bridge._pending_successor is None
    assert bridge._worker is failed


@pytest.mark.parametrize("kind", [WorkerKind.COMMIT, WorkerKind.VERIFY, WorkerKind.ABORT])
def test_commit_verify_abort_selection_signal_never_rotates(tmp_path, kind):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / f"selection-refresh-{kind.value}.sqlite3",
    )
    command = WorkerCommand(
        kind=kind,
        binding_id=first.binding_id,
        binding_revision=1,
        conversation_revision=2,
        operation_id=uuid4(),
        deadline=datetime.now(UTC) + timedelta(seconds=5),
    )

    result = asyncio.run(bridge._request_with_selection_process_refresh(command))

    assert result.error_code == "selection_process_refresh_required"
    assert failed.spawn_count == 0
    assert successor.started is False


def test_one_shot_mode_retires_selection_worker_without_retry(tmp_path):
    first, _ = _two_bindings()
    successor = SelectionRefreshWorker(["ok"])
    failed = SelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed,
        bindings=(first,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "one-shot-no-selection-retry.sqlite3",
        selection_refresh_retry_enabled=False,
    )
    command = WorkerCommand(
        kind=WorkerKind.OBSERVE,
        binding_id=first.binding_id,
        binding_revision=1,
        conversation_revision=2,
        deadline=datetime.now(UTC) + timedelta(seconds=5),
    )

    result = asyncio.run(bridge._request_with_selection_process_refresh(command))

    assert result.error_code == "selection_process_refresh_required"
    assert len(failed.requests) == 1
    assert failed.spawn_count == 0
    assert failed.stopped is True
    assert failed.status_snapshot()["worker_alive"] is False
    assert successor.started is False


def test_observe_timeout_quarantines_once_and_successor_serves_other_contact(tmp_path):
    first, second = _two_bindings()
    successor = RecoverableWorker(run_id="run-recovery", fail_observe=False)
    failed = RecoverableWorker(
        run_id="run-recovery", fail_observe=True, successor=successor,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first, second), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "observe-recovery.sqlite3",
    )

    initial = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=4, conversation_revision=9,
    ))
    assert initial.complete is False
    assert initial.gap_reason == "driver_quarantine:read_only_observe_timeout"
    assert bridge._cursor.has_snapshot(first.hub_conversation_id) is False
    request_count = len(failed.requests)

    repeated = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=4, conversation_revision=10,
    ))
    changed_binding = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=5, conversation_revision=10,
    ))
    assert repeated.gap_reason == "driver_quarantine:read_only_observe_timeout"
    assert changed_binding.gap_reason == "driver_quarantine:binding_changed_requires_release"
    assert len(failed.requests) == request_count

    other = asyncio.run(bridge.observe_conversation(
        second.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    assert other.complete is True
    assert [item.kind for item in successor.requests[:2]] == [
        WorkerKind.HEALTH, WorkerKind.OBSERVE,
    ]
    status = bridge.worker_status_snapshot()
    assert status["worker_generation"] == 2
    assert status["first_terminal_failure"] is None
    assert status["observation_quarantine_count"] == 1
    assert status["historical_terminal_failures"][0]["request_id"] == str(
        failed.requests[0].request_id
    )
    assert status["last_read_only_recovery"]["status"] == "successor_active"


def test_observe_recovery_health_uses_bounded_lifecycle_budget(tmp_path):
    first, _ = _two_bindings()

    class RecordingSuccessor(RecoverableWorker):
        def __init__(self):
            super().__init__(run_id="run-deadline", fail_observe=False)
            self.request_timeouts = []

        def request(self, command, timeout):
            self.request_timeouts.append(timeout)
            return super().request(command, timeout)

    successor = RecordingSuccessor()
    failed = RecoverableWorker(
        run_id="run-deadline", fail_observe=True, successor=successor,
    )
    bridge = QQVMDriverBridge(
        worker=failed,
        bindings=(first,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "observe-recovery-deadline.sqlite3",
        timeout_seconds=90,
    )
    asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id,
        binding_revision=1,
        conversation_revision=1,
    ))

    assert len(successor.request_timeouts) == 1
    assert 0 < successor.request_timeouts[0] <= 5


def test_observe_recovery_budget_is_one_per_run_and_never_retries_failed_request(tmp_path):
    first, second = _two_bindings()
    forbidden = RecoverableWorker(run_id="run-budget", fail_observe=False)
    successor = RecoverableWorker(
        run_id="run-budget", fail_observe=True, successor=forbidden,
    )
    failed = RecoverableWorker(
        run_id="run-budget", fail_observe=True, successor=successor,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first, second), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "observe-budget.sqlite3",
    )

    asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    second_result = asyncio.run(bridge.observe_conversation(
        second.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))

    assert second_result.gap_reason == "driver_quarantine:read_only_observe_timeout"
    assert failed.spawn_count == 1
    assert successor.spawn_count == 0
    assert [item.kind for item in failed.requests] == [WorkerKind.OBSERVE]
    assert bridge._db.execute(
        "SELECT COUNT(*) FROM qq_vm_readonly_recoveries"
    ).fetchone()[0] == 1
    assert bridge._db.execute(
        "SELECT COUNT(*) FROM qq_vm_observation_quarantines"
    ).fetchone()[0] == 2


def test_nonterminal_send_blocks_recovery_but_verified_commit_intent_does_not(tmp_path):
    first, _ = _two_bindings()

    def insert_op(bridge, *, status, commit_intent):
        bridge._db.execute(
            """INSERT INTO qq_vm_ops(
                 operation_id,idempotency_key,draft_id,conversation_id,binding_id,
                 segment_ref,binding_revision,conversation_revision,text_hash,status,
                 commit_intent,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                str(uuid4()), str(uuid4()), str(uuid4()), first.hub_conversation_id,
                first.binding_id, str(uuid4()), 1, 1, "hash", status, commit_intent,
            ),
        )

    blocked_successor = RecoverableWorker(run_id="run-blocked", fail_observe=False)
    blocked_worker = RecoverableWorker(
        run_id="run-blocked", fail_observe=True, successor=blocked_successor,
    )
    blocked = QQVMDriverBridge(
        worker=blocked_worker, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "blocked.sqlite3",
    )
    insert_op(blocked, status=SendStatus.PREPARED.value, commit_intent=0)
    result = asyncio.run(blocked.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    assert result.gap_reason == "driver_quarantine:read_only_observe_timeout"
    assert blocked_worker.spawn_count == 0
    assert blocked.worker_status_snapshot()["last_read_only_recovery"]["status"] == (
        "blocked_nonterminal_operation"
    )

    allowed_successor = RecoverableWorker(run_id="run-verified", fail_observe=False)
    allowed_worker = RecoverableWorker(
        run_id="run-verified", fail_observe=True, successor=allowed_successor,
    )
    allowed = QQVMDriverBridge(
        worker=allowed_worker, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "verified.sqlite3",
    )
    insert_op(allowed, status=SendStatus.VERIFIED.value, commit_intent=1)
    asyncio.run(allowed.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    assert allowed_worker.spawn_count == 1
    assert allowed.worker_status_snapshot()["last_read_only_recovery"]["status"] == (
        "successor_active"
    )


def test_send_timeout_never_spawns_read_only_successor(tmp_path):
    first, _ = _two_bindings()
    successor = RecoverableWorker(run_id="run-send", fail_observe=False)
    worker = RecoverableWorker(
        run_id="run-send", fail_observe=True, successor=successor,
    )
    bridge = QQVMDriverBridge(
        worker=worker, bindings=(first,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "send-no-recovery.sqlite3",
    )
    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref="send-no-recovery:0",
        binding_revision=1, conversation_revision=1,
    ))
    assert operation.status is SendStatus.UNCERTAIN
    assert worker.spawn_count == 0
    assert bridge._db.execute(
        "SELECT COUNT(*) FROM qq_vm_readonly_recoveries"
    ).fetchone()[0] == 0


def test_cancelled_successor_health_is_stopped_and_released_from_pending_owner(tmp_path):
    first, _ = _two_bindings()

    class BlockingSuccessor(RecoverableWorker):
        def __init__(self):
            super().__init__(run_id="run-cancel", fail_observe=False)
            self.health_started = threading.Event()
            self.health_release = threading.Event()

        def request(self, command, timeout):
            if command.kind is WorkerKind.HEALTH:
                self.requests.append(command)
                self.health_started.set()
                self.health_release.wait(timeout=2)
                return WorkerResult(
                    request_id=command.request_id, kind=command.kind,
                    status=WorkerStatus.OK, worker_epoch=uuid4(),
                )
            return super().request(command, timeout)

        def stop(self):
            self.stopped = True
            self.health_release.set()
            self._alive = False
            self._exit_code = 0

    successor = BlockingSuccessor()
    failed = RecoverableWorker(
        run_id="run-cancel", fail_observe=True, successor=successor,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "cancelled-successor.sqlite3",
    )

    async def scenario():
        task = asyncio.create_task(bridge.observe_conversation(
            first.hub_conversation_id,
            binding_revision=1,
            conversation_revision=1,
        ))
        started = await asyncio.to_thread(successor.health_started.wait, 1)
        assert started is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert successor.stopped is True
    assert bridge._pending_successor is None
    assert bridge._worker is failed
    assert bridge._db.execute(
        "SELECT status FROM qq_vm_readonly_recoveries"
    ).fetchone()["status"] == "successor_cancelled"


def test_explicit_release_requires_exact_cas_and_full_observation_resumes_without_replay(tmp_path):
    first, _ = _two_bindings()
    old = {
        "conversation_internal_id": "qq-conv-1", "message_key": "old",
        "direction": "inbound", "text": "你好，你好",
        "observed_at": datetime.now(UTC).isoformat(), "tree_digest": "tree",
    }
    fresh = {
        "conversation_internal_id": "qq-conv-1", "message_key": "fresh",
        "direction": "inbound", "text": "你好呀",
        "observed_at": datetime.now(UTC).isoformat(), "tree_digest": "tree",
    }

    class SnapshotWorker:
        def __init__(self): self.bubbles = [old]
        def request(self, item, _timeout):
            return WorkerResult(
                request_id=item.request_id, kind=item.kind,
                operation_id=item.operation_id, binding_id=item.binding_id,
                binding_revision=item.binding_revision,
                conversation_revision=item.conversation_revision,
                status=WorkerStatus.OK,
                worker_epoch=uuid4(), evidence={"bubbles": list(self.bubbles)},
            )
        def stop(self): pass

    path = tmp_path / "release.sqlite3"
    local = SnapshotWorker()
    bridge = QQVMDriverBridge(
        worker=local, bindings=(first,), text_provider=lambda _: "x", sqlite_path=path,
    )
    bridge._cursor.ingest_snapshot(first.hub_conversation_id, [old])
    run_id = str(uuid4())
    request_id = str(uuid4())
    bridge._db.execute(
        """INSERT INTO qq_vm_observation_quarantines(
             conversation_id,failed_run_id,binding_id,binding_revision,request_id,
             failed_generation,error_code,parent_terminate_reason,worker_exit_code,
             quarantined_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            first.hub_conversation_id, run_id, first.binding_id, 1, request_id,
            1, "worker_timeout_isolated", "request_timeout", -15,
            datetime.now(UTC).isoformat(),
        ),
    )
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    state.register(
        account_id=first.account_id, contact_id=first.contact_id,
        conversation_id=first.hub_conversation_id, binding_revision=1,
        conversation_type="direct",
    )
    blocked = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    state.apply_observation(blocked)
    assert state.connection.execute(
        "SELECT pause_reason FROM runtime_conversations WHERE conversation_id=?",
        (first.hub_conversation_id,),
    ).fetchone()[0] == "driver_quarantine:read_only_observe_timeout"

    with pytest.raises(ValueError, match="quarantine_cas_mismatch"):
        release_observation_quarantine(
            path, expected_run_id=run_id,
            conversation_id=first.hub_conversation_id, binding_id=first.binding_id,
            binding_revision=1, request_id=str(uuid4()), failed_generation=1,
            operator_id="operator", reason_code="host_power_suspend_confirmed",
        )
    released = release_observation_quarantine(
        path, expected_run_id=run_id,
        conversation_id=first.hub_conversation_id, binding_id=first.binding_id,
        binding_revision=1, request_id=request_id, failed_generation=1,
        operator_id="operator", reason_code="host_power_suspend_confirmed",
    )
    assert released["status"] == "released"
    repeated = release_observation_quarantine(
        path, expected_run_id=run_id,
        conversation_id=first.hub_conversation_id, binding_id=first.binding_id,
        binding_revision=1, request_id=request_id, failed_generation=1,
        operator_id="operator", reason_code="host_power_suspend_confirmed",
    )
    assert repeated == {
        "status": "already_released",
        "release_id": released["release_id"],
        "released_at": released["released_at"],
    }
    local.bubbles.append(fresh)
    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))
    assert [item.text for item in batch.messages] == ["你好呀"]
    assert state.apply_observation(batch) == ("new_message",)
    conversation = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations WHERE conversation_id=?",
        (first.hub_conversation_id,),
    ).fetchone()
    assert (conversation["paused"], conversation["pause_reason"]) == (0, None)
    assert state.connection.execute(
        "SELECT status FROM runtime_planning_jobs WHERE conversation_id=?",
        (first.hub_conversation_id,),
    ).fetchone()[0] == "pending"
    audit = bridge._db.execute(
        "SELECT * FROM qq_vm_observation_quarantine_releases"
    ).fetchone()
    assert (audit["failed_run_id"], audit["request_id"], audit["reason_code"]) == (
        run_id, request_id, "host_power_suspend_confirmed",
    )
    status = bridge.worker_status_snapshot()
    assert status["observation_quarantine_count"] == 0
    assert status["historical_terminal_failures"][0]["released_at"] == (
        released["released_at"]
    )


def test_explicit_release_refuses_nonterminal_send_operation(tmp_path):
    first, _ = _two_bindings()
    path = tmp_path / "release-blocked.sqlite3"
    bridge = QQVMDriverBridge(
        worker=RecoverableWorker(run_id="run", fail_observe=False),
        bindings=(first,), text_provider=lambda _: "x", sqlite_path=path,
    )
    run_id, request_id = str(uuid4()), str(uuid4())
    bridge._db.execute(
        """INSERT INTO qq_vm_observation_quarantines(
             conversation_id,failed_run_id,binding_id,binding_revision,request_id,
             failed_generation,error_code,parent_terminate_reason,worker_exit_code,
             quarantined_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            first.hub_conversation_id, run_id, first.binding_id, 1, request_id,
            1, "worker_timeout_isolated", "request_timeout", -15,
            datetime.now(UTC).isoformat(),
        ),
    )
    bridge._db.execute(
        """INSERT INTO qq_vm_ops(
             operation_id,idempotency_key,draft_id,conversation_id,binding_id,
             segment_ref,binding_revision,conversation_revision,text_hash,status,
             commit_intent,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
        (
            str(uuid4()), str(uuid4()), str(uuid4()), first.hub_conversation_id,
            first.binding_id, str(uuid4()), 1, 1, "hash", SendStatus.PREPARED.value,
        ),
    )

    with pytest.raises(ValueError, match="nonterminal_send_operation_present"):
        release_observation_quarantine(
            path, expected_run_id=run_id,
            conversation_id=first.hub_conversation_id, binding_id=first.binding_id,
            binding_revision=1, request_id=request_id, failed_generation=1,
            operator_id="operator", reason_code="host_power_suspend_confirmed",
        )
    assert bridge._db.execute(
        "SELECT COUNT(*) FROM qq_vm_observation_quarantine_releases"
    ).fetchone()[0] == 0


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


# --------------------------------------------------------------------------
# SelectionHandoff minting: exact correlation, one retry, private diagnostics.
# --------------------------------------------------------------------------


class RecordingSelectionRefreshWorker(SelectionRefreshWorker):
    """Capture every command/result pair, including the minted handoffs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exchanges: list[tuple[WorkerCommand, WorkerResult]] = []

    def request(self, command, timeout):
        result = super().request(command, timeout)
        self.exchanges.append((command, result))
        return result


class RecordingFreshVerifyWorker(FreshVerifyWorker):
    """Capture every command/result pair of a fresh-verify capable worker."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exchanges: list[tuple[WorkerCommand, WorkerResult]] = []

    def request(self, command, timeout):
        result = super().request(command, timeout)
        self.exchanges.append((command, result))
        return result


def _observe_exchange(worker, kind=WorkerKind.OBSERVE):
    return next(
        pair for pair in worker.exchanges if pair[0].kind is kind
    )


def test_observe_refresh_retry_mints_one_exactly_bound_handoff(tmp_path):
    first, _ = _two_bindings()
    successor = RecordingSelectionRefreshWorker(["ok"])
    failed = RecordingSelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "refresh-handoff-observe.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=4, conversation_revision=9,
    ))

    assert batch.complete is True
    predecessor, refresh_result = _observe_exchange(failed)
    retried, _retry_result = _observe_exchange(successor)
    _health_command, successor_health = _observe_exchange(
        successor, WorkerKind.HEALTH,
    )
    handoff = retried.selection_handoff

    assert handoff is not None
    assert predecessor.selection_handoff is None
    assert handoff.source == "selection_refresh"
    assert handoff.source_kind is WorkerKind.OBSERVE
    assert handoff.target_kind is WorkerKind.OBSERVE
    assert handoff.predecessor_request_id == predecessor.request_id
    assert handoff.successor_request_id == retried.request_id
    assert handoff.predecessor_worker_epoch == refresh_result.worker_epoch
    assert handoff.successor_worker_epoch == successor_health.worker_epoch
    assert handoff.target_runtime_id_digest == runtime_id_digest(
        first.platform_conversation_id
    )
    assert handoff.binding_id == first.binding_id
    assert handoff.binding_revision == 4
    assert handoff.conversation_revision == 9
    assert handoff.operation_id is None
    assert handoff.expires_at.tzinfo is not None
    assert handoff.expires_at > datetime.now(UTC)

    # Exactly one token exists across both process generations.
    tokens = [
        item.selection_handoff
        for item, _ in (*failed.exchanges, *successor.exchanges)
        if item.selection_handoff is not None
    ]
    assert tokens == [handoff]


@pytest.mark.parametrize(
    "result_update",
    [
        {"request_id": uuid4()},
        {"kind": WorkerKind.PREPARE},
        {"binding_id": "wrong-binding"},
        {"binding_revision": 99},
        {"conversation_revision": 99},
        {"operation_id": uuid4()},
        {"status": WorkerStatus.UNAVAILABLE},
        {"status": WorkerStatus.UNCERTAIN},
        {"error_code": "profile_identity_mismatch"},
    ],
)
def test_only_the_exact_failed_safe_refresh_earns_a_retry_token(
    tmp_path, result_update,
):
    """Any other outcome must fail closed without a second process or token."""

    first, _ = _two_bindings()
    successor = RecordingSelectionRefreshWorker(["ok"])
    failed = RecordingSelectionRefreshWorker(
        ["refresh"], successor=successor, result_update=result_update,
    )
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / f"no-retry-token-{uuid4()}.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        first.hub_conversation_id, binding_revision=1, conversation_revision=2,
    ))

    assert batch.complete is False
    assert failed.spawn_count == 0
    assert successor.started is False
    assert all(
        command.selection_handoff is None for command, _ in failed.exchanges
    )


def test_prepare_refresh_retry_handoff_binds_operation_and_plan_target(tmp_path):
    first, _ = _two_bindings()
    successor = RecordingSelectionRefreshWorker(["ok"])
    failed = RecordingSelectionRefreshWorker(["refresh"], successor=successor)
    bridge = QQVMDriverBridge(
        worker=failed, bindings=(first,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "refresh-handoff-prepare.sqlite3",
    )
    operation_id = uuid4()

    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=operation_id, segment_ref="refresh-handoff:0",
        binding_revision=3, conversation_revision=7,
    ))

    assert operation.status is SendStatus.PREPARED
    predecessor, refresh_result = _observe_exchange(failed, WorkerKind.PREPARE)
    retried, _retry_result = _observe_exchange(successor, WorkerKind.PREPARE)
    _health_command, successor_health = _observe_exchange(
        successor, WorkerKind.HEALTH,
    )
    handoff = retried.selection_handoff

    assert handoff is not None
    assert handoff.source == "selection_refresh"
    assert handoff.source_kind is WorkerKind.PREPARE
    assert handoff.target_kind is WorkerKind.PREPARE
    assert handoff.operation_id == operation_id
    assert handoff.binding_id == first.binding_id
    assert handoff.binding_revision == 3
    assert handoff.conversation_revision == 7
    assert handoff.predecessor_request_id == predecessor.request_id
    assert handoff.successor_request_id == retried.request_id
    assert handoff.predecessor_worker_epoch == refresh_result.worker_epoch
    assert handoff.successor_worker_epoch == successor_health.worker_epoch
    assert handoff.target_runtime_id_digest == runtime_id_digest(
        first.platform_conversation_id
    )


def test_commit_success_handoff_binds_fresh_verify_without_leaking_diagnostics(
    tmp_path,
):
    first_worker, fake, _ = _worker()
    fake.bubbles = [QQBubble(
        conversation_internal_id="qq-conv-1",
        message_key="anchor",
        direction=BubbleDirection.INBOUND,
        text="anchor",
        observed_at=datetime.now(UTC),
        tree_digest=fake.digest,
    )]
    binding = next(iter(first_worker._bindings.values()))
    second_worker = QQVMWorker(
        accessibility=fake,
        selector_pack=first_worker._selectors,
        bindings=(binding,),
    )
    successor = RecordingFreshVerifyWorker(second_worker)
    first = RecordingFreshVerifyWorker(first_worker, successor=successor)
    bridge = QQVMDriverBridge(
        worker=first,
        bindings=(binding,),
        text_provider=lambda _: "one shot reply",
        sqlite_path=tmp_path / "commit-success-handoff.sqlite3",
    )

    operation = asyncio.run(bridge.prepare_send(
        command(text="one shot reply"), operation_id=uuid4(),
        segment_ref="commit-success:0", binding_revision=1,
        conversation_revision=1,
    ))
    assert operation.status is SendStatus.PREPARED
    operation = asyncio.run(bridge.commit_send(operation))
    assert operation.status is SendStatus.COMMITTED
    operation = asyncio.run(bridge.verify_send(operation))
    assert operation.status is SendStatus.VERIFIED

    commit_command, commit_result = _observe_exchange(first, WorkerKind.COMMIT)
    verify_command, _verify_result = _observe_exchange(successor, WorkerKind.VERIFY)
    _health_command, successor_health = _observe_exchange(
        successor, WorkerKind.HEALTH,
    )
    handoff = verify_command.selection_handoff

    assert handoff is not None
    assert handoff.source == "commit_success"
    assert handoff.source_kind is WorkerKind.COMMIT
    assert handoff.target_kind is WorkerKind.VERIFY
    assert handoff.predecessor_request_id == commit_command.request_id
    assert handoff.successor_request_id == verify_command.request_id
    assert handoff.predecessor_worker_epoch == commit_result.worker_epoch
    assert handoff.successor_worker_epoch == successor_health.worker_epoch
    assert handoff.target_runtime_id_digest == runtime_id_digest(
        binding.platform_conversation_id
    )
    assert handoff.operation_id == commit_command.operation_id
    assert handoff.binding_id == binding.binding_id
    assert handoff.binding_revision == 1
    assert handoff.conversation_revision == 1
    assert handoff.expires_at.tzinfo is not None

    diagnostics = bridge.last_send_handoff()
    assert diagnostics is not None
    serialized = json.dumps(diagnostics, sort_keys=True, default=str)
    assert "handoff" not in serialized
    assert str(handoff.handoff_id) not in serialized
    assert handoff.expires_at.isoformat() not in serialized
    assert handoff.target_runtime_id_digest not in serialized


def test_abort_send_carries_db_revisions_and_clears_real_composer(tmp_path):
    worker, fake, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    local = LocalWorker(worker)
    bridge = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "abort-cleanup.sqlite3",
    )
    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref="abort-cleanup:0",
        binding_revision=2, conversation_revision=3,
    ))
    assert operation.status is SendStatus.PREPARED
    assert fake.composer == "固定测试文字"

    aborted = asyncio.run(bridge.abort_send(operation))

    assert aborted.status is SendStatus.CANCELLED
    assert fake.composer == ""
    abort_command = next(
        item for item in local.requests if item.kind is WorkerKind.ABORT
    )
    assert abort_command.binding_id == binding.binding_id
    assert abort_command.operation_id == operation.operation_id
    assert abort_command.binding_revision == 2
    assert abort_command.conversation_revision == 3
    assert abort_command.deadline is not None
    assert abort_command.deadline.tzinfo is not None


def test_abort_send_expired_deadline_never_reaches_worker(tmp_path):
    worker, fake, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    local = LocalWorker(worker)
    bridge = QQVMDriverBridge(
        worker=local, bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "abort-expired.sqlite3",
    )
    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref="abort-expired:0",
        binding_revision=2, conversation_revision=3,
    ))
    assert operation.status is SendStatus.PREPARED
    # A zero budget is already consumed, so the abort deadline is in the past.
    bridge._timeout = 0.0

    aborted = asyncio.run(bridge.abort_send(operation))

    assert aborted.status is SendStatus.UNCERTAIN
    assert aborted.error_code == ErrorCode.SEND_UNCERTAIN.value
    assert [item.kind for item in local.requests] == [WorkerKind.PREPARE]
    assert fake.composer == "固定测试文字"
    assert bridge._db.execute(
        "SELECT status FROM qq_vm_ops WHERE operation_id=?",
        (str(operation.operation_id),),
    ).fetchone()["status"] == SendStatus.UNCERTAIN.value


@pytest.mark.parametrize("kind", [WorkerKind.COMMIT, WorkerKind.VERIFY])
def test_uncorrelated_commit_verify_ok_fails_closed_uncertain(tmp_path, kind):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))

    class DriftWorker:
        """A worker whose terminal OK response names the wrong revision."""

        def request(self, command, _timeout):
            values = {
                "request_id": command.request_id,
                "kind": command.kind,
                "operation_id": command.operation_id,
                "binding_id": command.binding_id,
                "binding_revision": command.binding_revision,
                "conversation_revision": command.conversation_revision,
                "status": WorkerStatus.OK,
                "worker_epoch": uuid4(),
            }
            if command.kind is kind:
                values["conversation_revision"] += 1
            return WorkerResult(**values)

        def stop(self): pass

    bridge = QQVMDriverBridge(
        worker=DriftWorker(), bindings=(binding,), text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / f"drift-{kind.value}.sqlite3",
    )
    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=uuid4(), segment_ref=f"drift-{kind.value}:0",
        binding_revision=1, conversation_revision=1,
    ))
    assert operation.status is SendStatus.PREPARED
    if kind is WorkerKind.VERIFY:
        operation = asyncio.run(bridge.commit_send(operation))
        assert operation.status is SendStatus.COMMITTED

    result = asyncio.run(getattr(bridge, f"{kind.value}_send")(operation))

    assert result.status is SendStatus.UNCERTAIN
    assert result.error_code == ErrorCode.SEND_UNCERTAIN.value
    assert bridge._db.execute(
        "SELECT status FROM qq_vm_ops WHERE operation_id=?",
        (str(operation.operation_id),),
    ).fetchone()["status"] == SendStatus.UNCERTAIN.value


def test_uncorrelated_observe_ok_fails_closed_without_ingesting(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))

    class DriftWorker:
        def request(self, command, _timeout):
            return WorkerResult(
                request_id=command.request_id, kind=command.kind,
                operation_id=command.operation_id, binding_id=command.binding_id,
                binding_revision=command.binding_revision + 1,
                conversation_revision=command.conversation_revision,
                status=WorkerStatus.OK, worker_epoch=uuid4(),
                evidence={"bubbles": [
                    {"direction": "inbound", "text": "leaked", "message_key": "x"},
                ]},
            )

        def stop(self): pass

    bridge = QQVMDriverBridge(
        worker=DriftWorker(), bindings=(binding,), text_provider=lambda _: "x",
        sqlite_path=tmp_path / "drift-observe.sqlite3",
    )

    batch = asyncio.run(bridge.observe_conversation(
        binding.hub_conversation_id, binding_revision=1, conversation_revision=1,
    ))

    assert batch.complete is False
    assert batch.messages == ()
    assert batch.gap_reason == "driver_temporary:worker_response_mismatch"
    assert bridge._cursor.has_snapshot(binding.hub_conversation_id) is False


class ScriptedCleanupWorker:
    """Records commands and scripts only the ABORT half of the cleanup path."""

    def __init__(self, *, abort):
        self.abort = abort
        self.requests = []

    def request(self, item, _timeout):
        self.requests.append(item)
        if item.kind is WorkerKind.PREPARE:
            return WorkerResult(
                request_id=item.request_id,
                kind=item.kind,
                operation_id=item.operation_id,
                binding_id=item.binding_id,
                binding_revision=item.binding_revision,
                conversation_revision=item.conversation_revision,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=uuid4(),
                error_code="composer_readback_mismatch",
                evidence={"cleanup_required": True},
            )
        # A COMMIT escaping here would mean PREPARE cleanup crossed the commit
        # boundary; fail loudly instead of answering.
        assert item.kind is WorkerKind.ABORT
        if self.abort == "exception":
            raise RuntimeError("abort_pipe_failed")
        if self.abort == "non_ok":
            return WorkerResult(
                request_id=item.request_id,
                kind=item.kind,
                operation_id=item.operation_id,
                binding_id=item.binding_id,
                binding_revision=item.binding_revision,
                conversation_revision=item.conversation_revision,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=uuid4(),
                error_code="needs_manual_cleanup",
            )
        drift = (
            {"conversation_revision": item.conversation_revision + 1}
            if self.abort == "mismatch"
            else {}
        )
        values = {
            "request_id": item.request_id,
            "kind": item.kind,
            "operation_id": item.operation_id,
            "binding_id": item.binding_id,
            "binding_revision": item.binding_revision,
            "conversation_revision": item.conversation_revision,
            "status": WorkerStatus.OK,
            "worker_epoch": uuid4(),
        }
        values.update(drift)
        return WorkerResult(**values)

    def stop(self):
        pass


def test_prepare_cleanup_required_aborts_exactly_then_fails_with_original_error(
    tmp_path,
):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    local = ScriptedCleanupWorker(abort="ok")
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / "prepare-cleanup-abort.sqlite3",
    )
    operation_id = uuid4()
    issued_at = datetime.now(UTC)

    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=operation_id, segment_ref="prepare-cleanup:0",
        binding_revision=2, conversation_revision=3,
    ))
    finished_at = datetime.now(UTC)

    assert operation.status is SendStatus.FAILED
    assert operation.error_code == "composer_readback_mismatch"
    assert [item.kind for item in local.requests] == [
        WorkerKind.PREPARE, WorkerKind.ABORT,
    ]
    prepare_command, abort_command = local.requests
    assert prepare_command.deadline is not None
    assert abort_command.operation_id == prepare_command.operation_id == operation_id
    assert abort_command.binding_id == prepare_command.binding_id == binding.binding_id
    assert abort_command.binding_revision == prepare_command.binding_revision == 2
    assert abort_command.conversation_revision == prepare_command.conversation_revision == 3
    assert abort_command.segment_ref is None
    assert abort_command.text is None
    assert abort_command.prepared_evidence is None
    assert abort_command.selection_handoff is None
    # The cleanup ABORT carries its own live deadline drawn from the same
    # bounded request budget as PREPARE; it can only be later because it is
    # created after PREPARE returned.
    assert abort_command.deadline is not None
    assert abort_command.deadline.tzinfo is not None
    assert prepare_command.deadline >= issued_at
    assert abort_command.deadline >= prepare_command.deadline
    assert abort_command.deadline <= finished_at + timedelta(
        seconds=bridge._timeout
    )

    row = bridge._db.execute(
        "SELECT status,error_code,commit_intent FROM qq_vm_ops WHERE operation_id=?",
        (str(operation_id),),
    ).fetchone()
    assert row["status"] == SendStatus.FAILED.value
    assert row["error_code"] == "composer_readback_mismatch"
    assert row["commit_intent"] == 0

    # A terminal cleanup failure must never be recommitted, even when the
    # runtime keeps driving the same operation object.
    replayed = asyncio.run(bridge.commit_send(operation))
    assert replayed.status is SendStatus.FAILED
    assert [item.kind for item in local.requests] == [
        WorkerKind.PREPARE, WorkerKind.ABORT,
    ]


@pytest.mark.parametrize("abort", ["exception", "mismatch", "non_ok"])
def test_prepare_cleanup_abort_without_correlated_ok_requires_manual_cleanup(
    tmp_path, abort,
):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    local = ScriptedCleanupWorker(abort=abort)
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "固定测试文字",
        sqlite_path=tmp_path / f"prepare-cleanup-{abort}.sqlite3",
    )
    operation_id = uuid4()

    operation = asyncio.run(bridge.prepare_send(
        command(), operation_id=operation_id, segment_ref=f"prepare-cleanup-{abort}:0",
        binding_revision=2, conversation_revision=3,
    ))

    assert operation.status is SendStatus.UNCERTAIN
    assert operation.error_code == "needs_manual_cleanup"
    assert [item.kind for item in local.requests] == [
        WorkerKind.PREPARE, WorkerKind.ABORT,
    ]
    abort_command = local.requests[-1]
    assert abort_command.operation_id == operation_id
    assert abort_command.binding_id == binding.binding_id
    row = bridge._db.execute(
        "SELECT status,error_code,commit_intent FROM qq_vm_ops WHERE operation_id=?",
        (str(operation_id),),
    ).fetchone()
    assert row["status"] == SendStatus.UNCERTAIN.value
    assert row["error_code"] == "needs_manual_cleanup"
    assert row["commit_intent"] == 0

    replayed = asyncio.run(bridge.commit_send(operation))
    assert replayed.status is SendStatus.UNCERTAIN
    assert replayed.error_code == "needs_manual_cleanup"
    assert [item.kind for item in local.requests] == [
        WorkerKind.PREPARE, WorkerKind.ABORT,
    ]


class ScriptedHealthWorker:
    def __init__(self, build):
        self._build = build
        self.requests = []

    def request(self, item, _timeout):
        self.requests.append(item)
        return self._build(item)

    def stop(self):
        pass


def _probe_response(item, **overrides):
    values = {
        "request_id": item.request_id,
        "kind": WorkerKind.HEALTH,
        "status": WorkerStatus.OK,
        "worker_epoch": uuid4(),
    }
    values.update(overrides)
    return WorkerResult(**values)


_PROBE_REJECTION_MUTATIONS = {
    "request_id": {"request_id": uuid4()},
    "kind": {"kind": WorkerKind.OBSERVE},
    "binding_id": {"binding_id": "binding-1"},
    "binding_revision": {"binding_revision": 1},
    "conversation_revision": {"conversation_revision": 1},
    "operation_id": {"operation_id": uuid4()},
    "zero_epoch": {"worker_epoch": UUID(int=0)},
}


@pytest.mark.parametrize("mutation", list(_PROBE_REJECTION_MUTATIONS))
def test_probe_health_rejects_uncorrelated_or_scoped_ok_results(tmp_path, mutation):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    overrides = dict(_PROBE_REJECTION_MUTATIONS[mutation])
    local = ScriptedHealthWorker(
        lambda item: _probe_response(item, **overrides)
    )
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / f"probe-{mutation}.sqlite3",
    )

    probe = bridge.probe_health()

    assert probe.status is WorkerStatus.UNCERTAIN
    assert probe.error_code == "worker_response_mismatch"
    assert probe.worker_epoch == UUID(int=0)
    assert len(local.requests) == 1
    assert probe.request_id == local.requests[0].request_id
    # The fail-closed verdict is what gets cached, not the raw response.
    assert bridge.health() is probe
    assert len(local.requests) == 1


def test_probe_health_accepts_only_exact_nonzero_epoch_result(tmp_path):
    worker, _, _ = _worker()
    binding = next(iter(worker._bindings.values()))
    local = ScriptedHealthWorker(_probe_response)
    bridge = QQVMDriverBridge(
        worker=local,
        bindings=(binding,),
        text_provider=lambda _: "x",
        sqlite_path=tmp_path / "probe-exact.sqlite3",
    )

    probe = bridge.probe_health()

    assert probe.status is WorkerStatus.OK
    assert probe.kind is WorkerKind.HEALTH
    assert probe.request_id == local.requests[0].request_id
    assert probe.worker_epoch != UUID(int=0)
    assert (
        probe.binding_id,
        probe.binding_revision,
        probe.conversation_revision,
        probe.operation_id,
    ) == (None, 0, 0, None)
    assert bridge.health() is probe
    assert len(local.requests) == 1


def test_unproven_message_tail_does_not_advance_cursor_or_outbox(tmp_path):
    worker, fake, _binding = _worker()
    fake.bubbles = [QQBubble(
        conversation_internal_id="qq-conv-1", message_key="anchor",
        direction=BubbleDirection.INBOUND, text="synthetic anchor",
        observed_at=datetime.now(UTC), tree_digest="synthetic-tree",
    )]
    bridge = QQVMDriverBridge(
        worker=LocalWorker(worker), bindings=tuple(worker._bindings.values()),
        text_provider=lambda _: "", sqlite_path=tmp_path / "tail.sqlite3",
    )
    try:
        first = asyncio.run(bridge.observe_conversation(
            "hub-conv-1", binding_revision=1, conversation_revision=1
        ))
        assert first.complete and first.messages == ()
        before = tuple(bridge._cursor.connection.execute("SELECT * FROM cursor_state").fetchone())
        fake.message_tail_is_latest = lambda *_: False
        failed = asyncio.run(bridge.observe_conversation(
            "hub-conv-1", binding_revision=1, conversation_revision=1
        ))
        assert not failed.complete and failed.messages == ()
        assert failed.gap_reason == "driver_temporary:message_tail_scroll_unavailable"
        assert tuple(bridge._cursor.connection.execute("SELECT * FROM cursor_state").fetchone()) == before
        assert bridge._cursor.connection.execute("SELECT COUNT(*) FROM observation_outbox").fetchone()[0] == 0
    finally:
        bridge.close()


@pytest.mark.parametrize("preserve_anchor", [True, False])
def test_latest_tail_scroll_emits_only_new_suffix_or_preserves_anchor_gap(
    tmp_path, preserve_anchor
):
    worker, fake, _binding = _worker()
    anchor = QQBubble(
        conversation_internal_id="qq-conv-1", message_key="anchor",
        direction=BubbleDirection.INBOUND, text="synthetic anchor",
        observed_at=datetime.now(UTC), tree_digest="synthetic-tree",
    )
    new = anchor.model_copy(update={"message_key": "new", "text": "synthetic new"})
    fake.bubbles = [anchor]
    bridge = QQVMDriverBridge(
        worker=LocalWorker(worker), bindings=tuple(worker._bindings.values()),
        text_provider=lambda _: "", sqlite_path=tmp_path / "tail.sqlite3",
    )
    try:
        asyncio.run(bridge.observe_conversation("hub-conv-1", binding_revision=1, conversation_revision=1))
        before = tuple(bridge._cursor.connection.execute("SELECT * FROM cursor_state").fetchone())
        latest = False
        scrolls = []
        fake.message_tail_is_latest = lambda *_: latest

        def scroll(*args, before_action):
            nonlocal latest
            before_action()
            scrolls.append("scroll")
            latest = True
            fake.bubbles = ([anchor] if preserve_anchor else []) + [new]

        fake.scroll_message_tail_to_latest = scroll
        batch = asyncio.run(bridge.observe_conversation("hub-conv-1", binding_revision=1, conversation_revision=1))
        if preserve_anchor:
            assert batch.complete
            assert [message.text for message in batch.messages] == ["synthetic new"]
            repeated = asyncio.run(bridge.observe_conversation("hub-conv-1", binding_revision=1, conversation_revision=1))
            assert repeated.complete and repeated.messages == ()
            assert bridge._cursor.connection.execute("SELECT COUNT(*) FROM observation_outbox").fetchone()[0] == 1
        else:
            assert not batch.complete and batch.gap_reason == "message_anchor_gap"
            assert batch.messages == ()
            assert tuple(bridge._cursor.connection.execute("SELECT * FROM cursor_state").fetchone()) == before
            assert bridge._cursor.connection.execute("SELECT COUNT(*) FROM observation_outbox").fetchone()[0] == 0
        assert scrolls == ["scroll"]
        assert not {"write-composer", "read-composer", "invoke-send"} & set(fake.calls)
    finally:
        bridge.close()
