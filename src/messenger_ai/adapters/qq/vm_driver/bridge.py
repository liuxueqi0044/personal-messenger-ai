from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.domain import AuthorizedSendCommand, ErrorCode, SendOperation, SendStatus
from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage

from .contracts import WorkerCommand, WorkerKind, WorkerStatus
from .message_cursor import MessageCursorStore
from .worker import QQVMWorkerProcess


_TERMINAL = {SendStatus.VERIFIED, SendStatus.FAILED, SendStatus.UNCERTAIN, SendStatus.CANCELLED}


class QQVMDriverBridge:
    """Durable runtime bridge. Hub owns IDs; worker performs fixed UI actions."""

    def __init__(self, *, worker: QQVMWorkerProcess, bindings: tuple[QQIdentityBinding, ...],
                 text_provider: Callable[[AuthorizedSendCommand], str], sqlite_path: str | Path,
                 timeout_seconds: float = 15) -> None:
        self._worker, self._text_provider, self._timeout = worker, text_provider, timeout_seconds
        self._bindings = {item.hub_conversation_id: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("conversation bindings must be unique")
        self._by_id = {item.binding_id: item for item in bindings}
        if str(sqlite_path) != ":memory:":
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(sqlite_path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS qq_vm_ops(
          operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
          draft_id TEXT NOT NULL, conversation_id TEXT NOT NULL, binding_id TEXT NOT NULL,
          segment_ref TEXT NOT NULL, binding_revision INTEGER NOT NULL,
          conversation_revision INTEGER NOT NULL, text_hash TEXT NOT NULL,
          status TEXT NOT NULL, commit_intent INTEGER NOT NULL DEFAULT 0,
          error_code TEXT, UNIQUE(segment_ref)
        );
        CREATE TABLE IF NOT EXISTS qq_vm_receipts(
          operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          receipt_fingerprint TEXT NOT NULL, local_key TEXT,
          UNIQUE(conversation_id,local_key)
        );
        """)
        receipt_sql = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='qq_vm_receipts'"
        ).fetchone()[0]
        if "local_key TEXT UNIQUE" in receipt_sql:
            self._db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE qq_vm_receipts RENAME TO qq_vm_receipts_old;
            CREATE TABLE qq_vm_receipts(
              operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
              receipt_fingerprint TEXT NOT NULL, local_key TEXT,
              UNIQUE(conversation_id,local_key));
            INSERT INTO qq_vm_receipts SELECT * FROM qq_vm_receipts_old;
            DROP TABLE qq_vm_receipts_old;
            COMMIT;
            """)
        self._db.execute(
            "UPDATE qq_vm_ops SET status=?,error_code=? WHERE (status=? AND commit_intent=1) OR status=?",
            (SendStatus.UNCERTAIN.value, ErrorCode.SEND_UNCERTAIN.value,
             SendStatus.PREPARED.value, SendStatus.COMMITTED.value),
        )
        cursor_path = Path(sqlite_path).with_suffix(".cursor.sqlite3") if str(sqlite_path) != ":memory:" else ":memory:"
        self._cursor = MessageCursorStore(cursor_path)
        self._cursor.recover()

    def _operation(self, row: sqlite3.Row) -> SendOperation:
        return SendOperation(operation_id=UUID(row["operation_id"]), idempotency_key=row["idempotency_key"],
                             draft_id=UUID(row["draft_id"]), status=SendStatus(row["status"]), error_code=row["error_code"])

    def _persist(self, operation: SendOperation, *, commit_intent: bool | None = None) -> SendOperation:
        fields = "status=?,error_code=?"; values: list[object] = [operation.status.value, operation.error_code]
        if commit_intent is not None:
            fields += ",commit_intent=?"; values.append(int(commit_intent))
        values.append(str(operation.operation_id))
        self._db.execute(f"UPDATE qq_vm_ops SET {fields} WHERE operation_id=?", values)
        return operation

    async def prepare_send(self, command: AuthorizedSendCommand, *, operation_id: UUID, segment_ref: str,
                           binding_revision: int, conversation_revision: int) -> SendOperation:
        existing = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=? OR idempotency_key=? OR segment_ref=?",
                                    (str(operation_id), command.idempotency_key, segment_ref)).fetchone()
        retry_pending = False
        if existing:
            binding = self._bindings.get(command.conversation_id)
            if (
                existing["operation_id"] != str(operation_id)
                or existing["idempotency_key"] != command.idempotency_key
                or existing["draft_id"] != str(command.draft_id)
                or existing["conversation_id"] != command.conversation_id
                or binding is None or existing["binding_id"] != binding.binding_id
                or existing["segment_ref"] != segment_ref
                or int(existing["binding_revision"]) != binding_revision
                or int(existing["conversation_revision"]) != conversation_revision
                or existing["text_hash"] != command.text_hash
            ):
                return SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key,
                                     draft_id=command.draft_id, status=SendStatus.FAILED,
                                     error_code=ErrorCode.FAILED_SAFE.value)
            operation = self._operation(existing)
            if operation.status is not SendStatus.PENDING:
                return operation
            retry_pending = True
        else:
            operation = SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key, draft_id=command.draft_id)
        binding = self._bindings.get(command.conversation_id)
        text = self._text_provider(command)
        if binding is None or hashlib.sha256(text.encode()).hexdigest() != command.text_hash:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value
            return operation
        if not retry_pending:
            self._db.execute("""INSERT INTO qq_vm_ops VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                             (str(operation_id), command.idempotency_key, str(command.draft_id), command.conversation_id,
                              binding.binding_id, segment_ref, binding_revision, conversation_revision,
                              command.text_hash, SendStatus.PENDING.value))
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(
            kind=WorkerKind.PREPARE, binding_id=binding.binding_id, operation_id=operation_id,
            segment_ref=segment_ref, binding_revision=binding_revision,
            conversation_revision=conversation_revision, text=text), self._timeout)
        if result.status is WorkerStatus.OK:
            operation.status = SendStatus.PREPARED
        elif result.status is WorkerStatus.UNCERTAIN:
            operation.status = SendStatus.UNCERTAIN; operation.error_code = ErrorCode.SEND_UNCERTAIN.value
        else:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value
        return self._persist(operation)

    async def commit_send(self, operation: SendOperation) -> SendOperation:
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value; return operation
        stored = self._operation(row)
        if stored.status in _TERMINAL:
            return stored
        if row["commit_intent"]:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        if stored.status is not SendStatus.PREPARED:
            return stored
        self._persist(stored, commit_intent=True)
        binding = self._by_id.get(row["binding_id"])
        if binding is None:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value; return self._persist(stored)
        try:
            result = await asyncio.to_thread(self._worker.request, WorkerCommand(
                kind=WorkerKind.COMMIT, binding_id=binding.binding_id, operation_id=stored.operation_id,
                binding_revision=row["binding_revision"], conversation_revision=row["conversation_revision"]), self._timeout)
        except Exception:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        if result.status is WorkerStatus.OK:
            stored.status = SendStatus.COMMITTED
        else:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist(stored)

    async def verify_send(self, operation: SendOperation) -> SendOperation:
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value; return operation
        stored = self._operation(row)
        if stored.status in _TERMINAL or stored.status is not SendStatus.COMMITTED:
            return stored
        binding = self._by_id.get(row["binding_id"])
        try:
            result = await asyncio.to_thread(self._worker.request, WorkerCommand(
                kind=WorkerKind.VERIFY, binding_id=binding.binding_id if binding else None,
                operation_id=stored.operation_id, binding_revision=row["binding_revision"],
                conversation_revision=row["conversation_revision"]), self._timeout)
        except Exception:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        if result.status is WorkerStatus.OK:
            receipt = result.evidence.get("receipt")
            if isinstance(receipt, dict):
                stored.status = SendStatus.VERIFIED
                fingerprint = _receipt_fingerprint(receipt)
                self._db.execute(
                    "INSERT OR IGNORE INTO qq_vm_receipts(operation_id,conversation_id,receipt_fingerprint) VALUES(?,?,?)",
                    (str(stored.operation_id), row["conversation_id"], fingerprint),
                )
            else:
                stored.status = SendStatus.UNCERTAIN
                stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        else:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist(stored)

    async def abort_send(self, operation: SendOperation) -> SendOperation:
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value
            return operation
        stored = self._operation(row)
        if stored.status in _TERMINAL:
            return stored
        if row["commit_intent"]:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(
            kind=WorkerKind.ABORT, binding_id=row["binding_id"], operation_id=stored.operation_id), self._timeout)
        if result.status is WorkerStatus.OK:
            stored.status = SendStatus.CANCELLED
        else:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist(stored)

    async def observe_conversation(self, conversation_id: str, *, binding_revision: int,
                                   conversation_revision: int) -> ObservationBatch:
        binding = self._bindings.get(conversation_id)
        if binding is None:
            raise KeyError(conversation_id)
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(
            kind=WorkerKind.OBSERVE, binding_id=binding.binding_id, binding_revision=binding_revision,
            conversation_revision=conversation_revision), self._timeout)
        rows = result.evidence.get("bubbles", []) if result.status is WorkerStatus.OK else []
        messages: list[ObservedMessage] = []
        complete = result.status is WorkerStatus.OK
        gap_reason = result.error_code
        if complete:
            try:
                self._cursor.ingest_snapshot(conversation_id, list(rows))
                for claimed in self._cursor.claim(conversation_id):
                    raw = json.loads(claimed["payload_json"])
                    receipt = self._db.execute(
                        """SELECT operation_id FROM qq_vm_receipts
                           WHERE conversation_id=? AND (local_key=? OR (local_key IS NULL AND receipt_fingerprint=?))
                           ORDER BY CASE WHEN local_key=? THEN 0 ELSE 1 END""",
                        (conversation_id, claimed["local_key"], _receipt_fingerprint(raw), claimed["local_key"]),
                    ).fetchall()
                    operation_id = UUID(receipt[0]["operation_id"]) if len(receipt) == 1 else None
                    if operation_id is not None:
                        self._db.execute("UPDATE qq_vm_receipts SET local_key=? WHERE operation_id=? AND local_key IS NULL",
                                         (claimed["local_key"], str(operation_id)))
                    messages.append(ObservedMessage(
                        local_message_key=claimed["local_key"], direction=Direction(raw.get("direction", "unknown")),
                        text=str(raw.get("text", "")), observed_at=datetime.fromisoformat(str(raw["observed_at"])),
                        operation_id=operation_id,
                        evidence_ref=f"qq-vm:{conversation_id}:{claimed['local_key']}"))
            except ValueError as exc:
                complete = False; gap_reason = str(exc)
        return ObservationBatch(account_id=binding.account_id, contact_id=binding.contact_id,
                                conversation_id=conversation_id, binding_revision=binding_revision,
                                conversation_revision=conversation_revision, complete=complete,
                                gap_reason=gap_reason, messages=tuple(messages))

    def acknowledge_observation(self, conversation_id: str, local_keys: tuple[str, ...]) -> int:
        """Called only after RuntimeState durably accepted the entire batch."""
        return self._cursor.acknowledge_keys(conversation_id, local_keys)

    def close(self) -> None:
        self._worker.stop()
        self._cursor.close()
        self._db.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._worker.stop)
        self._cursor.close()
        self._db.close()


def _receipt_fingerprint(row: dict[str, object]) -> str:
    stable = {key: row.get(key) for key in (
        "direction", "text", "message_key", "conversation_internal_id", "participant_signature"
    )}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
