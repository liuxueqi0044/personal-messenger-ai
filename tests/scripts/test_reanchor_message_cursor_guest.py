from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from messenger_ai.adapters.qq import QQIdentityBinding
from messenger_ai.adapters.qq.vm_driver.contracts import (
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
)
from messenger_ai.adapters.qq.vm_driver.message_cursor import MessageCursorStore

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "reanchor_message_cursor_guest.py"
)
SPEC = importlib.util.spec_from_file_location("reanchor_message_cursor_guest", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _runtime_db(path: Path) -> dict[str, object]:
    state = {
        "account_id": "qq-default-account",
        "contact_id": "session-contact-1",
        "binding_revision": 1,
        "conversation_revision": 4,
        "conversation_type": "direct",
        "paused": 1,
        "pause_reason": "message_anchor_gap",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE runtime_conversations(
                 conversation_id TEXT PRIMARY KEY,account_id TEXT,contact_id TEXT,
                 binding_revision INTEGER,conversation_revision INTEGER,
                 conversation_type TEXT,paused INTEGER,pause_reason TEXT)"""
        )
        db.execute(
            "INSERT INTO runtime_conversations VALUES(?,?,?,?,?,?,?,?)",
            ("qq-session-conversation-1", *state.values()),
        )
    return state


def test_clear_pause_is_cas_audited_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    state = _runtime_db(database)
    arguments = {
        "conversation_id": "qq-session-conversation-1",
        "binding_id": "session-contact-1",
        "state": state,
        "previous_snapshot_sha256": "a" * 64,
        "current_snapshot_sha256": "b" * 64,
        "next_seq": 9,
        "operator_id": "codex_architect",
        "reason_code": "QQ_LOCATOR_IDENTITY_REMOVED",
        "expected_pause_reason": "message_anchor_gap",
    }

    assert MODULE._clear_pause(database, **arguments) is True
    assert MODULE._clear_pause(database, **arguments) is False
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT paused,pause_reason,conversation_revision FROM runtime_conversations"
        ).fetchone()
        audit = db.execute(
            """SELECT binding_id,next_seq,operator_id,reason_code,released_at
               FROM runtime_cursor_reanchor_audit"""
        ).fetchall()
    assert row == (0, None, 5)
    assert len(audit) == 1
    assert audit[0][:4] == (
        "session-contact-1", 9, "codex_architect",
        "QQ_LOCATOR_IDENTITY_REMOVED",
    )
    assert "+00:00" in audit[0][4]


def test_clear_pause_rejects_revision_change_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    state = _runtime_db(database)
    state["conversation_revision"] = 5
    with pytest.raises(RuntimeError, match="RUNTIME_CAS_MISMATCH"):
        MODULE._clear_pause(
            database,
            conversation_id="qq-session-conversation-1",
            binding_id="session-contact-1",
            state=state,
            previous_snapshot_sha256="a" * 64,
            current_snapshot_sha256="b" * 64,
            next_seq=9,
            operator_id="codex_architect",
            reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
            expected_pause_reason="message_anchor_gap",
        )
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT paused,pause_reason FROM runtime_conversations"
        ).fetchone() == (1, "message_anchor_gap")
        assert db.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type='table' AND name='runtime_cursor_reanchor_audit'"""
        ).fetchone()[0] == 0


def test_error_report_never_returns_arbitrary_exception_text() -> None:
    assert MODULE._error_code(RuntimeError("CURSOR_REANCHOR_OBSERVE_FAILED")) == (
        "CURSOR_REANCHOR_OBSERVE_FAILED"
    )
    assert MODULE._error_code(RuntimeError("private message: hello")) == "RuntimeError"


def test_clear_pause_rejects_mismatched_binding_and_changed_gap_reason(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    state = _runtime_db(database)
    arguments = {
        "conversation_id": "qq-session-conversation-1",
        "state": state,
        "previous_snapshot_sha256": "a" * 64,
        "current_snapshot_sha256": "b" * 64,
        "next_seq": 9,
        "operator_id": "codex_architect",
        "reason_code": "QQ_LOCATOR_IDENTITY_REMOVED",
        "expected_pause_reason": "message_anchor_gap",
    }

    with pytest.raises(RuntimeError, match="CURSOR_REANCHOR_BINDING_ID_MISMATCH"):
        MODULE._clear_pause(database, binding_id="other-contact", **arguments)
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT paused,pause_reason FROM runtime_conversations"
        ).fetchone() == (1, "message_anchor_gap")

    # The persisted gap reason itself is part of the CAS, not just the pause flag.
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE runtime_conversations SET pause_reason=?",
            ("driver_quarantine:read_only_observe_timeout",),
        )
    with pytest.raises(RuntimeError, match="CURSOR_REANCHOR_PAUSE_REASON_CHANGED"):
        MODULE._clear_pause(database, binding_id="session-contact-1", **arguments)
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT paused,pause_reason FROM runtime_conversations"
        ).fetchone() == (1, "driver_quarantine:read_only_observe_timeout")
        assert db.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type='table' AND name='runtime_cursor_reanchor_audit'"""
        ).fetchone()[0] == 0


def _lane_databases(root: Path) -> None:
    with sqlite3.connect(root / "qq-vm-bridge.sqlite3") as db:
        db.execute(
            "CREATE TABLE qq_vm_ops(operation_id TEXT,conversation_id TEXT,status TEXT)"
        )
        db.execute(
            """CREATE TABLE qq_vm_receipts(
                 operation_id TEXT,conversation_id TEXT,receipt_fingerprint TEXT)"""
        )
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute(
            "CREATE TABLE runtime_global_control(singleton INTEGER,paused INTEGER)"
        )
        db.execute("INSERT INTO runtime_global_control VALUES(1,1)")
        db.execute("CREATE TABLE runtime_event_outbox(aggregate_id TEXT,status TEXT)")
        db.execute("CREATE TABLE runtime_planning_jobs(conversation_id TEXT,status TEXT)")
        db.execute("CREATE TABLE runtime_plan_artifacts(conversation_id TEXT,status TEXT)")
        db.execute(
            """CREATE TABLE runtime_segment_executions(
                 pacing_plan_id TEXT,segment_index INTEGER,operation_id TEXT,
                 conversation_id TEXT,status TEXT)"""
        )
    with sqlite3.connect(root / "hub.sqlite3") as db:
        db.execute("CREATE TABLE drafts(draft_id TEXT,conversation_id TEXT)")
        db.execute("CREATE TABLE send_operations(operation_id TEXT,draft_id TEXT,status TEXT)")
        db.execute("CREATE TABLE outbox(aggregate_id TEXT,status TEXT)")
    with sqlite3.connect(root / "pacing.sqlite3") as db:
        db.execute("CREATE TABLE m10_plans(pacing_plan_id TEXT,conversation_id TEXT,status TEXT)")
        db.execute("CREATE TABLE m10_due_outbox(pacing_plan_id TEXT,status TEXT)")
        db.execute(
            """CREATE TABLE m10_segment_receipts(
                 pacing_plan_id TEXT,segment_index INTEGER,operation_id TEXT,
                 verified INTEGER)"""
        )


def test_send_lane_guard_covers_runtime_hub_and_bridge(tmp_path: Path) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    MODULE._ensure_send_lanes_settled(tmp_path, conversation)
    with sqlite3.connect(tmp_path / "hub.sqlite3") as db:
        db.execute("INSERT INTO drafts VALUES('draft',?)", (conversation,))
        db.execute("INSERT INTO send_operations VALUES('operation','draft','prepared')")
    with pytest.raises(RuntimeError, match="HUB_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)
    with sqlite3.connect(tmp_path / "hub.sqlite3") as db:
        db.execute("DELETE FROM send_operations")
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        db.execute("INSERT INTO runtime_event_outbox VALUES(?,'pending')", (conversation,))
    with pytest.raises(RuntimeError, match="RUNTIME_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        db.execute("DELETE FROM runtime_event_outbox")
    with sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3") as db:
        db.execute(
            "INSERT INTO qq_vm_ops VALUES('operation',?,'committed')", (conversation,)
        )
    with pytest.raises(RuntimeError, match="SEND_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)


@pytest.mark.parametrize("status", ["waiting", "due_for_revalidation"])
def test_send_lane_guard_blocks_every_active_pacing_plan(tmp_path: Path, status: str) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    with sqlite3.connect(tmp_path / "pacing.sqlite3") as db:
        db.execute("INSERT INTO m10_plans VALUES('plan',?,?)", (conversation, status))
    with pytest.raises(RuntimeError, match="PACING_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)


def _insert_verified_segment(
    root: Path, *, conversation_id: str, plan_id: str, operation_id: str
) -> None:
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute(
            "INSERT INTO runtime_segment_executions VALUES(?,?,?,?,?)",
            (plan_id, 0, operation_id, conversation_id, "verified"),
        )
    with sqlite3.connect(root / "hub.sqlite3") as db:
        db.execute("INSERT INTO drafts VALUES('draft',?)", (conversation_id,))
        db.execute(
            "INSERT INTO send_operations VALUES(?, 'draft', 'verified')",
            (operation_id,),
        )
    with sqlite3.connect(root / "qq-vm-bridge.sqlite3") as db:
        db.execute(
            "INSERT INTO qq_vm_ops VALUES(?,?, 'verified')", (operation_id, conversation_id)
        )


def test_verified_proof_requires_exact_operation_conversation_and_verified_flag(
    tmp_path: Path,
) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    assert not MODULE._verified_send_proof(
        tmp_path, conversation_id=conversation, pacing_plan_id="plan", segment_index=0,
        operation_id="operation",
    )
    with sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3") as db:
        db.execute("INSERT INTO qq_vm_ops VALUES('operation',?, 'verified')", (conversation,))
        db.execute(
            "INSERT INTO qq_vm_receipts VALUES('operation',?, 'fingerprint')", (conversation,)
        )
    # Bridge proof alone is not enough without the pacing verified=1 receipt.
    assert not MODULE._verified_send_proof(
        tmp_path, conversation_id=conversation, pacing_plan_id="plan", segment_index=0,
        operation_id="operation",
    )
    with sqlite3.connect(tmp_path / "pacing.sqlite3") as db:
        db.execute("INSERT INTO m10_segment_receipts VALUES('plan',0,'operation',1)")
    assert MODULE._verified_send_proof(
        tmp_path, conversation_id=conversation, pacing_plan_id="plan", segment_index=0,
        operation_id="operation",
    )
    # A receipt bound to another conversation must not certify this operation.
    with sqlite3.connect(tmp_path / "pacing.sqlite3") as db:
        db.execute("DELETE FROM m10_segment_receipts")
        db.execute("INSERT INTO m10_segment_receipts VALUES('plan',0,'operation',0)")
    assert not MODULE._verified_send_proof(
        tmp_path, conversation_id=conversation, pacing_plan_id="plan", segment_index=0,
        operation_id="operation",
    )


def test_send_lane_guard_accepts_only_receipt_backed_verified_state(tmp_path: Path) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    _insert_verified_segment(
        tmp_path, conversation_id=conversation, plan_id="plan", operation_id="operation"
    )
    # Runtime says verified but no bridge receipt exists yet: fail closed.
    with pytest.raises(RuntimeError, match="RUNTIME_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)
    with sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3") as db:
        db.execute("INSERT INTO qq_vm_receipts VALUES('operation',?, 'fp')", (conversation,))
    # Bridge receipt present, pacing receipt missing: still fail closed.
    with pytest.raises(RuntimeError, match="RUNTIME_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)
    with sqlite3.connect(tmp_path / "pacing.sqlite3") as db:
        db.execute("INSERT INTO m10_segment_receipts VALUES('plan',0,'operation',1)")
    # Exact bridge + pacing receipts for this operation/conversation: accepted.
    MODULE._ensure_send_lanes_settled(tmp_path, conversation)


def test_send_lane_guard_rejects_verified_bridge_operation_without_runtime_proof(
    tmp_path: Path,
) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    with sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3") as db:
        db.execute("INSERT INTO qq_vm_ops VALUES('orphan',?, 'verified')", (conversation,))
        db.execute("INSERT INTO qq_vm_receipts VALUES('orphan',?, 'fp')", (conversation,))
    with pytest.raises(RuntimeError, match="SEND_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)


def test_send_lane_guard_rejects_verified_hub_operation_without_runtime_proof(
    tmp_path: Path,
) -> None:
    _lane_databases(tmp_path)
    conversation = "qq-session-conversation-1"
    with sqlite3.connect(tmp_path / "hub.sqlite3") as db:
        db.execute("INSERT INTO drafts VALUES('draft',?)", (conversation,))
        db.execute("INSERT INTO send_operations VALUES('orphan','draft','verified')")
    with pytest.raises(RuntimeError, match="HUB_LANE_NOT_SETTLED"):
        MODULE._ensure_send_lanes_settled(tmp_path, conversation)


def _committed_cursor_reanchor(path: Path) -> tuple[MessageCursorStore, dict[str, object]]:
    cursor = MessageCursorStore(path)
    bubbles = [{"direction": "inbound", "text": "private-body", "message_key": "anchor"}]
    assert cursor.ingest_snapshot("qq-session-conversation-1", bubbles) == ()
    previous = cursor.snapshot_token("qq-session-conversation-1")
    assert previous is not None
    result = cursor.reanchor_snapshot(
        "qq-session-conversation-1",
        [{"direction": "inbound", "text": "private-replacement", "message_key": "fresh"}],
        expected_snapshot_sha256=previous,
        operator_id="codex_architect",
        reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
    )
    provenance = MODULE._cursor_reanchor_provenance(
        cursor, conversation_id="qq-session-conversation-1", operator_id="codex_architect",
        reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
    )
    assert provenance is not None
    assert provenance["previous_snapshot_sha256"] == previous
    assert provenance["current_snapshot_sha256"] == result.snapshot_sha256
    assert provenance["next_seq"] == result.next_seq
    return cursor, provenance


def test_cursor_committed_crash_recovers_pause_with_original_audit_hashes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.sqlite3"
    state = _runtime_db(runtime)
    cursor, provenance = _committed_cursor_reanchor(tmp_path / "cursor.sqlite3")
    try:
        # Simulates the crash after cursor SQLite committed and before runtime
        # SQLite cleared the pause.  No bubble recapture or second reanchor is
        # required; the original audit pair is reused verbatim.
        assert MODULE._clear_pause(
            runtime, conversation_id="qq-session-conversation-1", binding_id="session-contact-1",
            state=state, operator_id="codex_architect", reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
            expected_pause_reason="message_anchor_gap",
            **provenance,
        ) is True
        with sqlite3.connect(runtime) as db:
            pair = db.execute(
                """SELECT previous_snapshot_sha256,current_snapshot_sha256
                   FROM runtime_cursor_reanchor_audit"""
            ).fetchone()
        assert pair == (provenance["previous_snapshot_sha256"], provenance["current_snapshot_sha256"])
    finally:
        cursor.close()


def test_runtime_committed_crash_is_recognized_as_completed_recovery(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.sqlite3"
    state = _runtime_db(runtime)
    cursor, provenance = _committed_cursor_reanchor(tmp_path / "cursor.sqlite3")
    try:
        assert MODULE._clear_pause(
            runtime, conversation_id="qq-session-conversation-1", binding_id="session-contact-1",
            state=state, operator_id="codex_architect", reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
            expected_pause_reason="message_anchor_gap",
            **provenance,
        ) is True
        current_state = MODULE._conversation_state(runtime, "qq-session-conversation-1")
        # This is the exact predicate execute() uses before returning its
        # idempotent success report when a crash happened before report write.
        assert MODULE._runtime_reanchor_completed(
            runtime, conversation_id="qq-session-conversation-1", operator_id="codex_architect",
            reason_code="QQ_LOCATOR_IDENTITY_REMOVED", provenance=provenance,
            state=current_state,
        ) is True
    finally:
        cursor.close()


def test_reanchor_provenance_release_is_bound_to_current_exact_snapshot(tmp_path: Path) -> None:
    cursor = MessageCursorStore(tmp_path / "cursor.sqlite3")
    conversation = "qq-session-conversation-1"
    try:
        assert cursor.ingest_snapshot(
            conversation,
            [{"direction": "inbound", "text": "old-anchor", "message_key": "old"}],
        ) == ()
        previous = cursor.snapshot_token(conversation)
        cursor.reanchor_snapshot(
            conversation,
            [{"direction": "inbound", "text": "replacement", "message_key": "fresh"}],
            expected_snapshot_sha256=previous,
            operator_id="codex_architect",
            reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
        )
        assert MODULE._cursor_reanchor_provenance(
            cursor, conversation_id=conversation, operator_id="codex_architect",
            reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
        ) is not None

        # Once the anchor moves on, the committed reanchor no longer matches the
        # current snapshot: it cannot release a later anchor, and the captured
        # replacement is not replayed as a new observation.
        assert cursor.ingest_snapshot(
            conversation,
            [
                {"direction": "inbound", "text": "replacement", "message_key": "fresh"},
                {"direction": "inbound", "text": "newer", "message_key": "newer"},
            ],
        ) == ("1",)
        assert MODULE._cursor_reanchor_provenance(
            cursor, conversation_id=conversation, operator_id="codex_architect",
            reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
        ) is None
    finally:
        cursor.close()


def _binding(**overrides: object) -> QQIdentityBinding:
    values: dict[str, object] = {
        "hub_conversation_id": "qq-session-conversation-1",
        "contact_id": "session-contact-1",
        "account_id": "qq-default-account",
        "platform_conversation_id": "qq-platform-1",
        "participant_signature": "signature-1",
        # Production binds the platform identity one-to-one, so the configured
        # binding id must equal the runtime contact id _clear_pause re-checks.
        "binding_id": "session-contact-1",
        "conversation_type": "direct",
    }
    values.update(overrides)
    return QQIdentityBinding(**values)


class _FakeOwner:
    def acquire(self) -> None: pass
    def close(self) -> None: pass


def _arrange_execute(monkeypatch, tmp_path: Path, *, binding: QQIdentityBinding,
                     captured: list[dict[str, object]]) -> Path:
    _runtime_db(tmp_path / "runtime.sqlite3")
    cursor = MessageCursorStore(tmp_path / "qq-vm-bridge.cursor.sqlite3")
    assert cursor.ingest_snapshot(
        "qq-session-conversation-1",
        [{"direction": "inbound", "text": "old-anchor", "message_key": "old"}],
    ) == ()
    cursor.close()
    config_path = tmp_path / "runtime.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(MODULE, "QQRuntimeInstanceOwner", _FakeOwner)
    monkeypatch.setattr(
        MODULE, "load_config",
        lambda path: {"data_dir": str(tmp_path), "worker_timeout_seconds": 5},
    )
    monkeypatch.setattr(
        MODULE, "validate_config", lambda config, api_key: (object(), (binding,), ()),
    )
    monkeypatch.setattr(MODULE, "_capability", lambda config, pack: None)
    monkeypatch.setattr(
        MODULE, "_ensure_send_lanes_settled", lambda data_dir, conversation_id: None,
    )
    monkeypatch.setattr(MODULE, "_capture_current_bubbles", lambda **kwargs: list(captured))
    return config_path


def test_execute_refuses_binding_mismatch_without_touching_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _arrange_execute(
        monkeypatch, tmp_path, binding=_binding(contact_id="other-contact"), captured=[],
    )
    with pytest.raises(RuntimeError, match="CURSOR_REANCHOR_BINDING_MISMATCH"):
        MODULE.execute(
            config_path=config_path, conversation_id="qq-session-conversation-1",
            operator_id="codex_architect", reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
        )
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert db.execute(
            "SELECT paused,pause_reason FROM runtime_conversations"
        ).fetchone() == (1, "message_anchor_gap")
    cursor = MessageCursorStore(tmp_path / "qq-vm-bridge.cursor.sqlite3")
    try:
        assert cursor.snapshot_token("qq-session-conversation-1") is not None
        assert cursor.connection.execute(
            "SELECT COUNT(*) FROM cursor_reanchor_audit"
        ).fetchone()[0] == 0
    finally:
        cursor.close()


def test_execute_reanchors_current_snapshot_without_replaying_captured_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = [{"direction": "inbound", "text": "private-replacement", "message_key": "fresh"}]
    config_path = _arrange_execute(
        monkeypatch, tmp_path, binding=_binding(), captured=captured,
    )
    report = MODULE.execute(
        config_path=config_path, conversation_id="qq-session-conversation-1",
        operator_id="codex_architect", reason_code="QQ_LOCATOR_IDENTITY_REMOVED",
    )
    assert report["status"] == "succeeded"
    assert report["cursor_applied"] is True and report["pause_cleared"] is True
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert db.execute(
            "SELECT paused,pause_reason,conversation_revision FROM runtime_conversations"
        ).fetchone() == (0, None, 5)
    cursor = MessageCursorStore(tmp_path / "qq-vm-bridge.cursor.sqlite3")
    try:
        # The captured snapshot is the new durable baseline: re-observing it is
        # a no-op rather than a replay of the recovery capture.
        assert cursor.ingest_snapshot("qq-session-conversation-1", captured) == ()
    finally:
        cursor.close()


class _CaptureWorker:
    scripts: ClassVar[list[list[tuple[WorkerKind, WorkerResult]]]] = []
    events: ClassVar[list[str]] = []
    instances: ClassVar[list[_CaptureWorker]] = []
    instance_count = 0

    def __init__(self, *args, **kwargs) -> None:
        self.replies = self.scripts.pop(0)
        self.alive = False
        self.commands: list[WorkerCommand] = []
        self.responses: list[WorkerResult] = []
        self.label = f"worker-{self.instance_count}"
        type(self).instance_count += 1
        type(self).instances.append(self)
        self.events.append(f"{self.label}:constructed")

    def start(self) -> None:
        self.alive = True
        self.events.append(f"{self.label}:started")

    def request(self, command, timeout_seconds):
        del timeout_seconds
        expected_kind, result = self.replies.pop(0)
        assert command.kind is expected_kind
        self.commands.append(command)
        result = result.model_copy(update={
            "request_id": command.request_id,
            "kind": command.kind,
            "binding_id": command.binding_id,
            "binding_revision": command.binding_revision,
            "conversation_revision": command.conversation_revision,
            "operation_id": command.operation_id,
        })
        self.responses.append(result)
        self.events.append(f"{self.label}:{command.kind.value}")
        return result

    def stop(self) -> None:
        self.alive = False
        self.events.append(f"{self.label}:stopped")

    def status_snapshot(self) -> dict[str, object]:
        return {"worker_alive": self.alive}


def _worker_result(
    status: WorkerStatus,
    *,
    error_code: str | None = None,
    bubbles: list[dict[str, object]] | None = None,
    worker_epoch: UUID | None = None,
) -> WorkerResult:
    return WorkerResult(
        request_id=uuid4(),
        kind=WorkerKind.HEALTH,
        status=status,
        worker_epoch=worker_epoch or uuid4(),
        error_code=error_code,
        evidence={} if bubbles is None else {"bubbles": bubbles},
    )


def test_capture_retires_selection_worker_then_observes_once_in_fresh_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bubbles = [{"direction": "inbound", "text": "private", "message_key": "m1"}]
    _CaptureWorker.events = []
    _CaptureWorker.instance_count = 0
    _CaptureWorker.scripts = [
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (
                WorkerKind.OBSERVE,
                _worker_result(
                    WorkerStatus.FAILED_SAFE,
                    error_code="selection_process_refresh_required",
                ),
            ),
        ],
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (WorkerKind.OBSERVE, _worker_result(WorkerStatus.OK, bubbles=bubbles)),
        ],
    ]
    monkeypatch.setattr(MODULE, "QQVMWorkerProcess", _CaptureWorker)

    assert MODULE._capture_current_bubbles(
        pack=object(),
        bindings=(_binding(),),
        evidence=(),
        binding=_binding(),
        state={"binding_revision": 1, "conversation_revision": 4},
        timeout_seconds=5,
    ) == bubbles
    first_stop = _CaptureWorker.events.index("worker-0:stopped")
    second_start = _CaptureWorker.events.index("worker-1:started")
    assert first_stop < second_start
    assert _CaptureWorker.events[-1] == "worker-1:stopped"


def test_capture_does_not_retry_uncertain_selection_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CaptureWorker.events = []
    _CaptureWorker.instance_count = 0
    _CaptureWorker.scripts = [
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (
                WorkerKind.OBSERVE,
                _worker_result(
                    WorkerStatus.UNCERTAIN,
                    error_code="selection_process_refresh_required",
                ),
            ),
        ]
    ]
    monkeypatch.setattr(MODULE, "QQVMWorkerProcess", _CaptureWorker)

    with pytest.raises(RuntimeError, match="CURSOR_REANCHOR_OBSERVE_FAILED"):
        MODULE._capture_current_bubbles(
            pack=object(),
            bindings=(_binding(),),
            evidence=(),
            binding=_binding(),
            state={"binding_revision": 1, "conversation_revision": 4},
            timeout_seconds=5,
        )
    assert sum(event.endswith(":constructed") for event in _CaptureWorker.events) == 1
    assert _CaptureWorker.events[-1].endswith(":stopped")


def test_capture_handoff_is_exactly_bound_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bubbles = [{"direction": "inbound", "text": "private", "message_key": "m1"}]
    _CaptureWorker.events = []
    _CaptureWorker.instances = []
    _CaptureWorker.instance_count = 0
    _CaptureWorker.scripts = [
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (
                WorkerKind.OBSERVE,
                _worker_result(
                    WorkerStatus.FAILED_SAFE,
                    error_code="selection_process_refresh_required",
                ),
            ),
        ],
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (WorkerKind.OBSERVE, _worker_result(WorkerStatus.OK, bubbles=bubbles)),
        ],
    ]
    monkeypatch.setattr(MODULE, "QQVMWorkerProcess", _CaptureWorker)

    assert MODULE._capture_current_bubbles(
        pack=object(),
        bindings=(_binding(),),
        evidence=(),
        binding=_binding(),
        state={"binding_revision": 1, "conversation_revision": 4},
        timeout_seconds=5,
    ) == bubbles

    first_worker, second_worker = _CaptureWorker.instances
    first = next(
        item for item in first_worker.commands if item.kind is WorkerKind.OBSERVE
    )
    second = next(
        item for item in second_worker.commands if item.kind is WorkerKind.OBSERVE
    )
    handoff = second.selection_handoff

    assert first.selection_handoff is None
    assert handoff is not None
    assert handoff.source == "selection_refresh"
    assert handoff.source_kind is WorkerKind.OBSERVE
    assert handoff.target_kind is WorkerKind.OBSERVE
    assert handoff.predecessor_request_id == first.request_id
    assert handoff.successor_request_id == second.request_id
    assert handoff.binding_id == "session-contact-1"
    assert handoff.binding_revision == 1
    assert handoff.conversation_revision == 4
    assert handoff.operation_id is None
    assert handoff.expires_at.tzinfo is not None

    # Exactly one token exists, it never reaches a HEALTH probe, and it is
    # never handed back as part of the captured (content-free) result.
    tokens = [
        command.selection_handoff
        for worker in (first_worker, second_worker)
        for command in worker
        if command.selection_handoff is not None
    ]
    assert tokens == [handoff]
    assert all(
        command.selection_handoff is None
        for worker in (first_worker, second_worker)
        for command in worker.commands
        if command.kind is not WorkerKind.OBSERVE
    )


def test_capture_does_not_retry_unavailable_selection_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CaptureWorker.events = []
    _CaptureWorker.instances = []
    _CaptureWorker.instance_count = 0
    _CaptureWorker.scripts = [
        [
            (WorkerKind.HEALTH, _worker_result(WorkerStatus.OK)),
            (
                WorkerKind.OBSERVE,
                _worker_result(
                    WorkerStatus.UNAVAILABLE,
                    error_code="selection_process_refresh_required",
                ),
            ),
        ]
    ]
    monkeypatch.setattr(MODULE, "QQVMWorkerProcess", _CaptureWorker)

    with pytest.raises(RuntimeError, match="CURSOR_REANCHOR_OBSERVE_FAILED"):
        MODULE._capture_current_bubbles(
            pack=object(),
            bindings=(_binding(),),
            evidence=(),
            binding=_binding(),
            state={"binding_revision": 1, "conversation_revision": 4},
            timeout_seconds=5,
        )

    assert sum(event.endswith(":constructed") for event in _CaptureWorker.events) == 1
    assert _CaptureWorker.events[-1].endswith(":stopped")
    assert all(
        command.selection_handoff is None
        for worker in _CaptureWorker.instances
        for command in worker.commands
    )
