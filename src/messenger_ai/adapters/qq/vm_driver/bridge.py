from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from uuid import UUID

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.domain import AuthorizedSendCommand, ErrorCode, SendOperation, SendStatus
from messenger_ai.runtime.contracts import ObservationBatch, ObservedMessage, Direction
from messenger_ai.domain.state_machines import SendStateMachine

from .contracts import WorkerCommand, WorkerKind, WorkerStatus
from .worker import QQVMWorkerProcess


class QQVMDriverBridge:
    """Runtime-facing V5 driver; Hub owns operation ids and durable state."""
    def __init__(self, *, worker: QQVMWorkerProcess, bindings: tuple[QQIdentityBinding, ...], text_provider: Callable[[AuthorizedSendCommand], str], sqlite_path: str, timeout_seconds: float = 15) -> None:
        self._worker, self._text_provider, self._timeout = worker, text_provider, timeout_seconds
        self._bindings = {item.hub_conversation_id: item for item in bindings}
        self._operations: dict[UUID, SendOperation] = {}
        self._db = sqlite3.connect(sqlite_path)
        self._db.execute("create table if not exists qq_vm_ops (op text primary key, binding text, segment text, br integer, cr integer, status text)")
        self._db.execute("create table if not exists qq_vm_seen (binding text, fingerprint text, primary key(binding,fingerprint))")
        self._db.commit()

    async def prepare_send(self, command: AuthorizedSendCommand, *, operation_id: UUID, segment_ref: str, binding_revision: int, conversation_revision: int) -> SendOperation:
        operation = self._operations.setdefault(operation_id, SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key, draft_id=command.draft_id))
        binding = self._bindings.get(command.conversation_id)
        text = self._text_provider(command)
        if binding is None or hashlib.sha256(text.encode()).hexdigest() != command.text_hash:
            return self._failed(operation)
        self._db.execute("insert or replace into qq_vm_ops values(?,?,?,?,?,?)", (str(operation_id), binding.binding_id, segment_ref, binding_revision, conversation_revision, 'prepared')); self._db.commit()
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(kind=WorkerKind.PREPARE, binding_id=binding.binding_id, operation_id=operation_id, segment_ref=segment_ref, binding_revision=binding_revision, conversation_revision=conversation_revision, text=text), self._timeout)
        if result.status is WorkerStatus.OK: SendStateMachine.transition(operation, SendStatus.PREPARED)
        elif result.status is WorkerStatus.UNCERTAIN: SendStateMachine.transition(operation, SendStatus.UNCERTAIN, ErrorCode.SEND_UNCERTAIN.value)
        else: self._failed(operation)
        return operation

    async def commit_send(self, operation: SendOperation) -> SendOperation:
        if operation.status is not SendStatus.PREPARED: return operation
        binding = self._binding_for(operation)
        if binding is None: return self._failed(operation)
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(kind=WorkerKind.COMMIT, binding_id=binding.binding_id, operation_id=operation.operation_id), self._timeout)
        if result.status is WorkerStatus.OK: SendStateMachine.transition(operation, SendStatus.COMMITTED)
        elif result.status is WorkerStatus.UNCERTAIN: SendStateMachine.transition(operation, SendStatus.UNCERTAIN, ErrorCode.SEND_UNCERTAIN.value)
        else: self._failed(operation)
        return operation

    async def verify_send(self, operation: SendOperation) -> SendOperation:
        if operation.status is not SendStatus.COMMITTED: return operation
        binding = self._binding_for(operation)
        if binding is None: return self._failed(operation)
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(kind=WorkerKind.VERIFY, binding_id=binding.binding_id, operation_id=operation.operation_id), self._timeout)
        if result.status is WorkerStatus.OK: SendStateMachine.transition(operation, SendStatus.VERIFIED)
        else: SendStateMachine.transition(operation, SendStatus.UNCERTAIN, ErrorCode.SEND_UNCERTAIN.value)
        return operation

    def _binding_for(self, operation: SendOperation) -> QQIdentityBinding | None:
        row = self._db.execute("select binding from qq_vm_ops where op=?", (str(operation.operation_id),)).fetchone()
        return next((b for b in self._bindings.values() if row and b.binding_id == row[0]), None)

    async def observe_conversation(self, conversation_id: str, *, binding_revision: int, conversation_revision: int) -> ObservationBatch:
        binding = self._bindings[conversation_id]
        result = await asyncio.to_thread(self._worker.request, WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding.binding_id, binding_revision=binding_revision, conversation_revision=conversation_revision), self._timeout)
        rows = result.evidence.get("bubbles", [])
        initial = self._db.execute("select count(*) from qq_vm_seen where binding=?", (binding.binding_id,)).fetchone()[0] == 0
        messages=[]
        for index, row in enumerate(rows):
            fingerprint=hashlib.sha256(repr(row).encode()).hexdigest()
            existed=self._db.execute("select 1 from qq_vm_seen where binding=? and fingerprint=?", (binding.binding_id,fingerprint)).fetchone()
            self._db.execute("insert or ignore into qq_vm_seen values(?,?)",(binding.binding_id,fingerprint))
            if not initial and not existed:
                direction=Direction(str(row.get('direction','unknown')))
                messages.append(ObservedMessage(local_message_key=str(index), direction=direction, text=str(row.get('text','')), observed_at=row['observed_at']))
        self._db.commit()
        return ObservationBatch(account_id=binding.account_id,contact_id=binding.contact_id,conversation_id=conversation_id,binding_revision=binding_revision,conversation_revision=conversation_revision,complete=result.status is WorkerStatus.OK,gap_reason=result.error_code,messages=tuple(messages))
    @staticmethod
    def _failed(operation: SendOperation) -> SendOperation:
        if operation.status in {SendStatus.PENDING, SendStatus.PREPARED}: SendStateMachine.transition(operation, SendStatus.FAILED, ErrorCode.FAILED_SAFE.value)
        return operation
