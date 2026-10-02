from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.domain import (
    AuthorizedSendCommand,
    ErrorCode,
    SendOperation,
    SendStatus,
)
from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage

from .contracts import (
    PreparedVerificationEvidence,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    observation_pause_reason,
)
from .message_cursor import MessageCursorStore
from .quarantine_release import ensure_quarantine_release_schema
from .visual_selection import runtime_id_digest
from .worker import QQVMWorkerProcess

_TERMINAL = {SendStatus.VERIFIED, SendStatus.FAILED, SendStatus.UNCERTAIN, SendStatus.CANCELLED}
_SELECTION_PROCESS_REFRESH_REQUIRED = "selection_process_refresh_required"
_SELECTION_PROCESS_REFRESH_EXHAUSTED = "selection_process_refresh_exhausted"
_SELECTION_REFRESH_KINDS = frozenset({WorkerKind.OBSERVE, WorkerKind.PREPARE})


class QQVMDriverBridge:
    """Durable runtime bridge. Hub owns IDs; worker performs fixed UI actions."""

    def __init__(self, *, worker: QQVMWorkerProcess, bindings: tuple[QQIdentityBinding, ...],
                 text_provider: Callable[[AuthorizedSendCommand], str], sqlite_path: str | Path,
                 timeout_seconds: float = 15,
                 bootstrap_last_inbound: tuple[str, ...] = (),
                 bootstrap_last_inbound_provenance: dict[str, dict[str, object]] | None = None,
                 selection_refresh_retry_enabled: bool = True,
                 recover_persistent_state: bool = True) -> None:
        if not isinstance(selection_refresh_retry_enabled, bool):
            raise TypeError("selection_refresh_retry_enabled must be a boolean")
        if not isinstance(recover_persistent_state, bool):
            raise TypeError("recover_persistent_state must be a boolean")
        self._worker, self._text_provider, self._timeout = worker, text_provider, timeout_seconds
        self._generation_lock = asyncio.Lock()
        self._worker_generation = 1
        self._pending_successor = None
        self._pending_successor_started = False
        self._selection_refresh_retry_enabled = selection_refresh_retry_enabled
        self._last_health: WorkerResult | None = None
        self._last_send_handoff: dict[str, object] | None = None
        self._pending_verify_command: WorkerCommand | None = None
        self._bindings = {item.hub_conversation_id: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("conversation bindings must be unique")
        self._by_id = {item.binding_id: item for item in bindings}
        self._bootstrap_last_inbound = set(bootstrap_last_inbound)
        self._bootstrap_last_inbound_provenance = dict(
            bootstrap_last_inbound_provenance or {}
        )
        if not set(self._bootstrap_last_inbound_provenance).issubset(
            self._bootstrap_last_inbound
        ):
            raise ValueError("bootstrap adoption provenance must target a configured bootstrap")
        for conversation_id, provenance in self._bootstrap_last_inbound_provenance.items():
            binding = self._bindings.get(conversation_id)
            if (
                binding is None
                or provenance.get("binding_id") != binding.binding_id
                or provenance.get("participant_signature") != binding.participant_signature
            ):
                raise ValueError("bootstrap adoption provenance does not match binding")
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
        CREATE TABLE IF NOT EXISTS qq_vm_prepared_evidence(
          operation_id TEXT PRIMARY KEY,
          evidence_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS qq_vm_receipts(
          operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          receipt_fingerprint TEXT NOT NULL, local_key TEXT,
          UNIQUE(conversation_id,local_key)
        );
        CREATE TABLE IF NOT EXISTS qq_vm_observation_quarantines(
          conversation_id TEXT PRIMARY KEY,
          failed_run_id TEXT,
          binding_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL,
          request_id TEXT NOT NULL,
          failed_generation INTEGER NOT NULL,
          error_code TEXT NOT NULL,
          parent_terminate_reason TEXT NOT NULL,
          worker_exit_code INTEGER,
          quarantined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS qq_vm_readonly_recoveries(
          recovery_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL UNIQUE,
          failed_generation INTEGER NOT NULL,
          successor_generation INTEGER,
          conversation_id TEXT NOT NULL,
          binding_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL,
          request_id TEXT NOT NULL,
          status TEXT NOT NULL,
          health_request_id TEXT,
          health_error_code TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );
        """)
        quarantine_columns = {
            row["name"] for row in self._db.execute(
                "PRAGMA table_info(qq_vm_observation_quarantines)"
            ).fetchall()
        }
        if "failed_run_id" not in quarantine_columns:
            self._db.execute(
                "ALTER TABLE qq_vm_observation_quarantines ADD COLUMN failed_run_id TEXT"
            )
        ensure_quarantine_release_schema(self._db)
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
        if recover_persistent_state:
            self._db.execute(
                "UPDATE qq_vm_ops SET status=?,error_code=? WHERE (status=? AND commit_intent=1) OR status=?",
                (SendStatus.UNCERTAIN.value, ErrorCode.SEND_UNCERTAIN.value,
                 SendStatus.PREPARED.value, SendStatus.COMMITTED.value),
            )
        cursor_path = Path(sqlite_path).with_suffix(".cursor.sqlite3") if str(sqlite_path) != ":memory:" else ":memory:"
        self._cursor = MessageCursorStore(cursor_path)
        if recover_persistent_state:
            self._cursor.recover()

    def probe_health(self) -> WorkerResult:
        """Perform an explicit worker/UIA probe and cache its result."""

        command = WorkerCommand(
            kind=WorkerKind.HEALTH,
            deadline=datetime.now(UTC) + timedelta(seconds=self._timeout),
        )
        result = self._worker.request(command, self._timeout)
        self._last_health = (
            result
            if self._health_matches_command(command, result)
            else WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNCERTAIN,
                worker_epoch=UUID(int=0),
                error_code="worker_response_mismatch",
            )
        )
        return self._last_health

    def health(self) -> WorkerResult:
        """Return the cached health snapshot without touching the worker pipe."""

        status = self.worker_status_snapshot()
        terminal = status.get("first_terminal_failure")
        if terminal is not None or status.get("worker_alive") is False:
            failure = terminal if isinstance(terminal, dict) else {}
            command = WorkerCommand(kind=WorkerKind.HEALTH)
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNAVAILABLE,
                worker_epoch=UUID(int=0),
                error_code=str(failure.get("error_code") or "worker_not_alive"),
                evidence={
                    "worker_process_id": status.get("worker_process_id"),
                    "worker_exit_code": status.get("worker_exit_code"),
                    "parent_terminate_reason": status.get("parent_terminate_reason"),
                },
            )
        if self._last_health is not None:
            return self._last_health
        command = WorkerCommand(kind=WorkerKind.HEALTH)
        return WorkerResult(
            request_id=command.request_id,
            kind=command.kind,
            status=WorkerStatus.UNAVAILABLE,
            worker_epoch=UUID(int=0),
            error_code="health_not_probed",
        )

    @property
    def observation_freshness_seconds(self) -> float:
        """Bounded allowance for one serialized round across configured bindings."""
        return min(600.0, max(30.0, self._timeout * max(2, len(self._bindings)) * 2))

    def worker_status_snapshot(self) -> dict[str, object]:
        """Read parent-owned worker lifecycle state without issuing IPC."""
        snapshot = getattr(self._worker, "status_snapshot", None)
        if callable(snapshot):
            current = snapshot()
        else:
            # Test/local workers predate the process façade.  Their cached health is
            # still usable by bridge callers, but never qualifies as a production
            # lifecycle witness because liveness remains unknown.
            current = {
                "run_id": None,
                "worker_process_id": None,
                "worker_alive": None,
                "worker_exit_code": None,
                "parent_terminate_reason": None,
                "startup_health": None,
                "last_request": None,
                "last_successful_observe": None,
                "first_terminal_failure": None,
            }
        result = dict(current)
        result["worker_generation"] = self._worker_generation
        recovery = self._db.execute(
            """SELECT run_id,failed_generation,successor_generation,conversation_id,
                      binding_id,binding_revision,request_id,status,
                      health_request_id,health_error_code,created_at,completed_at
               FROM qq_vm_readonly_recoveries
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        result["last_read_only_recovery"] = (
            dict(recovery) if recovery is not None else None
        )
        quarantines = self._db.execute(
            """SELECT conversation_id,failed_run_id,binding_id,binding_revision,request_id,
                      failed_generation,error_code,parent_terminate_reason,
                      worker_exit_code,quarantined_at
               FROM qq_vm_observation_quarantines
               ORDER BY quarantined_at DESC LIMIT 8"""
        ).fetchall()
        releases = self._db.execute(
            """SELECT release_id,failed_run_id,conversation_id,binding_id,
                      binding_revision,request_id,failed_generation,error_code,
                      parent_terminate_reason,worker_exit_code,quarantined_at,
                      operator_id,reason_code,released_at
               FROM qq_vm_observation_quarantine_releases
               ORDER BY released_at DESC LIMIT 8"""
        ).fetchall()
        result["observation_quarantine_count"] = int(self._db.execute(
            """SELECT COUNT(*) FROM qq_vm_observation_quarantines q
               WHERE NOT EXISTS(
                 SELECT 1 FROM qq_vm_observation_quarantine_releases r
                 WHERE r.conversation_id=q.conversation_id
                   AND r.request_id=q.request_id)"""
        ).fetchone()[0])
        history: dict[tuple[str, str], dict[str, object]] = {}
        for row in quarantines:
            history[(row["conversation_id"], row["request_id"])] = {
                "request_id": row["request_id"],
                "kind": WorkerKind.OBSERVE.value,
                "binding_id": row["binding_id"],
                "failed_run_id": row["failed_run_id"],
                "completed_at": row["quarantined_at"],
                "error_code": row["error_code"],
                "parent_terminate_reason": row["parent_terminate_reason"],
                "worker_exit_code": row["worker_exit_code"],
                "failed_generation": row["failed_generation"],
            }
        for row in releases:
            history[(row["conversation_id"], row["request_id"])] = {
                "request_id": row["request_id"],
                "kind": WorkerKind.OBSERVE.value,
                "binding_id": row["binding_id"],
                "completed_at": row["quarantined_at"],
                "error_code": row["error_code"],
                "parent_terminate_reason": row["parent_terminate_reason"],
                "worker_exit_code": row["worker_exit_code"],
                "failed_generation": row["failed_generation"],
                "failed_run_id": row["failed_run_id"],
                "release_id": row["release_id"],
                "released_at": row["released_at"],
                "release_operator_id": row["operator_id"],
                "release_reason_code": row["reason_code"],
            }
        result["historical_terminal_failures"] = sorted(
            history.values(), key=lambda item: str(item["completed_at"]), reverse=True,
        )[:8]
        return result

    def last_send_handoff(self) -> dict[str, object] | None:
        """Return content-free evidence for the latest COMMIT→VERIFY handoff."""

        return (
            dict(self._last_send_handoff)
            if self._last_send_handoff is not None
            else None
        )

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
        async with self._generation_lock:
            return await self._prepare_send_locked(
                command,
                operation_id=operation_id,
                segment_ref=segment_ref,
                binding_revision=binding_revision,
                conversation_revision=conversation_revision,
            )

    async def _prepare_send_locked(self, command: AuthorizedSendCommand, *, operation_id: UUID,
                                   segment_ref: str, binding_revision: int,
                                   conversation_revision: int) -> SendOperation:
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
            self._db.execute(
                """INSERT INTO qq_vm_ops(
                     operation_id,idempotency_key,draft_id,conversation_id,
                     binding_id,segment_ref,binding_revision,conversation_revision,
                     text_hash,status,commit_intent,error_code)
                   VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                (str(operation_id), command.idempotency_key, str(command.draft_id),
                 command.conversation_id, binding.binding_id, segment_ref,
                 binding_revision, conversation_revision, command.text_hash,
                 SendStatus.PENDING.value),
            )
        deadline = datetime.now(UTC) + timedelta(seconds=self._timeout)
        result = await self._request_with_selection_process_refresh(WorkerCommand(
            kind=WorkerKind.PREPARE, binding_id=binding.binding_id, operation_id=operation_id,
            segment_ref=segment_ref, binding_revision=binding_revision,
            conversation_revision=conversation_revision, text=text, deadline=deadline))
        requires_portable_evidence = bool(
            getattr(self._worker, "fresh_verify_capable", False)
        )
        if result.status is WorkerStatus.OK and requires_portable_evidence:
            try:
                prepared = PreparedVerificationEvidence.model_validate(
                    result.evidence.get("prepared_evidence")
                )
            except Exception:
                abort_command = WorkerCommand(
                    kind=WorkerKind.ABORT,
                    binding_id=binding.binding_id,
                    operation_id=operation_id,
                    binding_revision=binding_revision,
                    conversation_revision=conversation_revision,
                    deadline=datetime.now(UTC) + timedelta(seconds=self._timeout),
                )
                try:
                    abort_result = self._correlated_result(
                        abort_command,
                        await self._request_before_deadline(
                            self._worker, abort_command
                        ),
                    )
                except Exception:
                    abort_result = None
                operation.status = (
                    SendStatus.FAILED
                    if abort_result is not None
                    and abort_result.status is WorkerStatus.OK
                    else SendStatus.UNCERTAIN
                )
                operation.error_code = (
                    "prepared_evidence_invalid"
                    if operation.status is SendStatus.FAILED
                    else "needs_manual_cleanup"
                )
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO qq_vm_prepared_evidence VALUES(?,?)",
                    (str(operation_id), prepared.model_dump_json()),
                )
                operation.status = SendStatus.PREPARED
        elif result.status is WorkerStatus.OK:
            operation.status = SendStatus.PREPARED
        elif (
            result.status is WorkerStatus.FAILED_SAFE
            and result.evidence.get("cleanup_required") is True
        ):
            abort_command = WorkerCommand(
                kind=WorkerKind.ABORT,
                binding_id=binding.binding_id,
                operation_id=operation_id,
                binding_revision=binding_revision,
                conversation_revision=conversation_revision,
                deadline=datetime.now(UTC) + timedelta(seconds=self._timeout),
            )
            try:
                abort_result = self._correlated_result(
                    abort_command,
                    await self._request_before_deadline(
                        self._worker, abort_command
                    ),
                )
            except Exception:
                abort_result = None
            if abort_result is not None and abort_result.status is WorkerStatus.OK:
                operation.status = SendStatus.FAILED
                operation.error_code = (
                    result.error_code or ErrorCode.FAILED_SAFE.value
                )
            else:
                operation.status = SendStatus.UNCERTAIN
                operation.error_code = "needs_manual_cleanup"
        elif result.status is WorkerStatus.UNCERTAIN:
            operation.status = SendStatus.UNCERTAIN; operation.error_code = ErrorCode.SEND_UNCERTAIN.value
        else:
            # PREPARE has not crossed the commit boundary, so a worker's
            # classified failure is safe to expose and persist for diagnosis.
            # Keep the domain fallback for older/partial workers that omitted
            # an error code, while preserving SEND_UNCERTAIN above for any
            # result whose delivery state cannot be established.
            operation.status = SendStatus.FAILED
            operation.error_code = result.error_code or ErrorCode.FAILED_SAFE.value
        return self._persist(operation)

    async def commit_send(self, operation: SendOperation) -> SendOperation:
        async with self._generation_lock:
            return await self._commit_send_locked(operation)

    async def _commit_send_locked(self, operation: SendOperation) -> SendOperation:
        self._pending_verify_command = None
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
        commit_deadline = datetime.now(UTC) + timedelta(seconds=self._timeout)
        commit_command = WorkerCommand(
            kind=WorkerKind.COMMIT,
            binding_id=binding.binding_id,
            operation_id=stored.operation_id,
            binding_revision=row["binding_revision"],
            conversation_revision=row["conversation_revision"],
            deadline=commit_deadline,
        )
        try:
            result = self._correlated_result(
                commit_command,
                await self._request_before_deadline(self._worker, commit_command),
            )
        except Exception:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        commit_snapshot_method = getattr(self._worker, "status_snapshot", None)
        commit_snapshot = (
            commit_snapshot_method() if callable(commit_snapshot_method) else {}
        )
        self._last_send_handoff = {
            "operation_id": str(stored.operation_id),
            "commit_worker_epoch": str(result.worker_epoch),
            "commit_worker_process_id": commit_snapshot.get("worker_process_id"),
            "first_worker_retired": False,
        }
        if result.status is WorkerStatus.OK:
            stored.status = SendStatus.COMMITTED
            self._persist(stored)
            lifecycle_supported = bool(
                getattr(self._worker, "fresh_verify_capable", False)
            ) and callable(getattr(self._worker, "spawn_successor", None)) and callable(
                getattr(self._worker, "status_snapshot", None)
            )
            if lifecycle_supported:
                deadline = datetime.now(UTC) + timedelta(seconds=self._timeout)
                if not await self._activate_fresh_selection_worker(deadline):
                    stored.status = SendStatus.UNCERTAIN
                    stored.error_code = ErrorCode.SEND_UNCERTAIN.value
                else:
                    active_snapshot = self._worker.status_snapshot()
                    self._last_send_handoff.update({
                        "first_worker_retired": True,
                        "verify_worker_process_id": active_snapshot.get(
                            "worker_process_id"
                        ),
                        "verify_health_worker_epoch": str(
                            self._last_health.worker_epoch
                        ) if self._last_health is not None else None,
                    })
                    verify_deadline = datetime.now(UTC) + timedelta(
                        seconds=self._timeout
                    )
                    try:
                        prepared_row = self._db.execute(
                            "SELECT evidence_json FROM qq_vm_prepared_evidence "
                            "WHERE operation_id=?",
                            (str(stored.operation_id),),
                        ).fetchone()
                        prepared = PreparedVerificationEvidence.model_validate_json(
                            prepared_row["evidence_json"]
                            if prepared_row is not None else None
                        )
                        verify_command = WorkerCommand(
                            kind=WorkerKind.VERIFY,
                            binding_id=binding.binding_id,
                            operation_id=stored.operation_id,
                            binding_revision=row["binding_revision"],
                            conversation_revision=row["conversation_revision"],
                            prepared_evidence=prepared,
                            deadline=verify_deadline,
                        )
                        if self._last_health is None:
                            raise ValueError("verify successor health is unavailable")
                        issue_handoff = getattr(
                            self._worker, "mint_selection_handoff", None
                        )
                        if not callable(issue_handoff):
                            raise ValueError("verify successor issuer is unavailable")
                        handoff = issue_handoff(
                            predecessor_command=commit_command,
                            predecessor_result=result,
                            successor_command=verify_command,
                            source="commit_success",
                            successor_worker_epoch=self._last_health.worker_epoch,
                            target_runtime_id_digest=runtime_id_digest(
                                binding.platform_conversation_id
                            ),
                            expires_at=verify_deadline,
                        )
                    except (TypeError, ValueError):
                        stored.status = SendStatus.UNCERTAIN
                        stored.error_code = ErrorCode.SEND_UNCERTAIN.value
                    else:
                        self._pending_verify_command = verify_command.model_copy(
                            update={"selection_handoff": handoff}
                        )
        else:
            if callable(getattr(self._worker, "status_snapshot", None)):
                retired = await self._retire_untrusted_selection_worker(self._worker)
                self._last_send_handoff["first_worker_retired"] = retired
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist(stored)

    async def verify_send(self, operation: SendOperation) -> SendOperation:
        async with self._generation_lock:
            return await self._verify_send_locked(operation)

    async def _verify_send_locked(self, operation: SendOperation) -> SendOperation:
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            operation.status = SendStatus.FAILED; operation.error_code = ErrorCode.FAILED_SAFE.value; return operation
        stored = self._operation(row)
        if stored.status in _TERMINAL or stored.status is not SendStatus.COMMITTED:
            return stored
        binding = self._by_id.get(row["binding_id"])
        prepared = None
        if getattr(self._worker, "fresh_verify_capable", False):
            try:
                prepared_row = self._db.execute(
                    "SELECT evidence_json FROM qq_vm_prepared_evidence WHERE operation_id=?",
                    (str(stored.operation_id),),
                ).fetchone()
                prepared = PreparedVerificationEvidence.model_validate_json(
                    prepared_row["evidence_json"] if prepared_row is not None else None
                )
            except Exception:
                stored.status = SendStatus.UNCERTAIN
                stored.error_code = ErrorCode.SEND_UNCERTAIN.value
                return self._persist(stored)
        try:
            pending = self._pending_verify_command
            self._pending_verify_command = None
            if (
                pending is not None
                and binding is not None
                and pending.kind is WorkerKind.VERIFY
                and pending.binding_id == binding.binding_id
                and pending.operation_id == stored.operation_id
                and pending.binding_revision == row["binding_revision"]
                and pending.conversation_revision == row["conversation_revision"]
            ):
                verify_command = pending.model_copy(
                    update={"prepared_evidence": prepared}
                )
            else:
                verify_deadline = datetime.now(UTC) + timedelta(
                    seconds=self._timeout
                )
                verify_command = WorkerCommand(
                    kind=WorkerKind.VERIFY,
                    binding_id=binding.binding_id if binding else None,
                    operation_id=stored.operation_id,
                    binding_revision=row["binding_revision"],
                    conversation_revision=row["conversation_revision"],
                    prepared_evidence=prepared,
                    deadline=verify_deadline,
                )
            result = self._correlated_result(
                verify_command,
                await self._request_before_deadline(self._worker, verify_command),
            )
        except Exception:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        if self._last_send_handoff is not None:
            self._last_send_handoff.update({
                "verify_worker_epoch": str(result.worker_epoch),
                "verify_status": result.status.value,
            })
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
        async with self._generation_lock:
            return await self._abort_send_locked(operation)

    async def _abort_send_locked(self, operation: SendOperation) -> SendOperation:
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
        abort_command = WorkerCommand(
            kind=WorkerKind.ABORT,
            binding_id=row["binding_id"],
            operation_id=stored.operation_id,
            binding_revision=row["binding_revision"],
            conversation_revision=row["conversation_revision"],
            deadline=datetime.now(UTC) + timedelta(seconds=self._timeout),
        )
        try:
            result = self._correlated_result(
                abort_command,
                await self._request_before_deadline(self._worker, abort_command),
            )
        except Exception:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist(stored)
        if result.status is WorkerStatus.OK:
            stored.status = SendStatus.CANCELLED
        else:
            stored.status = SendStatus.UNCERTAIN; stored.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist(stored)

    async def observe_conversation(self, conversation_id: str, *, binding_revision: int,
                                   conversation_revision: int) -> ObservationBatch:
        async with self._generation_lock:
            return await self._observe_conversation_locked(
                conversation_id,
                binding_revision=binding_revision,
                conversation_revision=conversation_revision,
            )

    async def _observe_conversation_locked(self, conversation_id: str, *, binding_revision: int,
                                           conversation_revision: int) -> ObservationBatch:
        binding = self._bindings.get(conversation_id)
        if binding is None:
            raise KeyError(conversation_id)
        quarantine = self._db.execute(
            """SELECT q.binding_id,q.binding_revision
               FROM qq_vm_observation_quarantines q
               WHERE q.conversation_id=? AND NOT EXISTS(
                 SELECT 1 FROM qq_vm_observation_quarantine_releases r
                 WHERE r.conversation_id=q.conversation_id
                   AND r.request_id=q.request_id)""",
            (conversation_id,),
        ).fetchone()
        if quarantine is not None:
            reason = (
                "driver_quarantine:read_only_observe_timeout"
                if (
                    quarantine["binding_id"] == binding.binding_id
                    and int(quarantine["binding_revision"]) == binding_revision
                )
                else "driver_quarantine:binding_changed_requires_release"
            )
            return ObservationBatch(
                account_id=binding.account_id,
                contact_id=binding.contact_id,
                conversation_id=conversation_id,
                binding_revision=binding_revision,
                conversation_revision=conversation_revision,
                complete=False,
                gap_reason=reason,
                messages=(),
            )
        command = WorkerCommand(
            kind=WorkerKind.OBSERVE, binding_id=binding.binding_id, binding_revision=binding_revision,
            conversation_revision=conversation_revision,
            deadline=datetime.now(UTC) + timedelta(seconds=self._timeout),
        )
        result = await self._request_with_selection_process_refresh(command)
        quarantined = False
        if result.error_code == "worker_timeout_isolated":
            quarantined = await self._recover_after_read_only_timeout(
                conversation_id=conversation_id,
                binding=binding,
                binding_revision=binding_revision,
                command=command,
            )
        rows = result.evidence.get("bubbles", []) if result.status is WorkerStatus.OK else []
        messages: list[ObservedMessage] = []
        complete = result.status is WorkerStatus.OK
        gap_reason = (
            None
            if result.status is WorkerStatus.OK
            else (
                "driver_quarantine:read_only_observe_timeout"
                if quarantined else observation_pause_reason(result.status, result.error_code)
            )
        )
        if complete:
            try:
                if conversation_id in self._bootstrap_last_inbound:
                    if self._cursor.has_snapshot(conversation_id):
                        # A stale config may still carry its one-shot request
                        # after a clean adoption and restart.  The durable
                        # cursor is authoritative: process this observation as
                        # an ordinary increment without replaying the adopted
                        # row or rechecking now-stale bootstrap shape evidence.
                        self._cursor.ingest_snapshot(conversation_id, list(rows))
                    else:
                        provenance = self._bootstrap_last_inbound_provenance.get(conversation_id)
                        if provenance is not None:
                            self._verify_bootstrap_adoption(provenance, list(rows))
                        self._cursor.bootstrap_last_inbound_once(conversation_id, list(rows))
                    self._bootstrap_last_inbound.discard(conversation_id)
                    self._bootstrap_last_inbound_provenance.pop(conversation_id, None)
                else:
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

    async def _request_with_selection_process_refresh(
        self, command: WorkerCommand,
    ) -> WorkerResult:
        """Retry one pre-commit selection in a fresh OS process.

        The caller holds ``_generation_lock`` for this entire method.  The one
        UTC deadline belongs to the logical request, so worker startup and
        shutdown consume the same budget as both attempts.
        """

        if command.kind not in _SELECTION_REFRESH_KINDS or command.deadline is None:
            return self._correlated_result(
                command,
                await asyncio.to_thread(
                    self._worker.request, command, self._timeout,
                ),
            )
        result = self._correlated_result(
            command, await self._request_before_deadline(self._worker, command)
        )
        if not self._selection_refresh_retry_enabled:
            if self._is_selection_refresh_request(command, result):
                await self._retire_untrusted_selection_worker(self._worker)
            return result
        if not self._is_selection_refresh_request(command, result):
            return result
        if not self._selection_refresh_nonterminal_guard(command):
            await self._retire_untrusted_selection_worker(self._worker)
            return result
        if not await self._activate_fresh_selection_worker(command.deadline):
            return result

        retry = command.model_copy(update={"request_id": uuid4()})
        try:
            binding = self._by_id.get(command.binding_id or "")
            if binding is None or self._last_health is None:
                raise ValueError("selection successor scope is unavailable")
            issue_handoff = getattr(
                self._worker, "mint_selection_handoff", None
            )
            if not callable(issue_handoff):
                raise ValueError("selection successor issuer is unavailable")
            handoff = issue_handoff(
                predecessor_command=command,
                predecessor_result=result,
                successor_command=retry,
                source="selection_refresh",
                successor_worker_epoch=self._last_health.worker_epoch,
                target_runtime_id_digest=runtime_id_digest(
                    binding.platform_conversation_id
                ),
                expires_at=command.deadline,
            )
        except ValueError:
            await self._retire_untrusted_selection_worker(self._worker)
            return result
        retry = retry.model_copy(update={"selection_handoff": handoff})
        retried = self._correlated_result(
            retry, await self._request_before_deadline(self._worker, retry)
        )
        if self._is_selection_refresh_request(retry, retried):
            await self._retire_untrusted_selection_worker(self._worker)
            return retried.model_copy(update={
                "error_code": _SELECTION_PROCESS_REFRESH_EXHAUSTED,
            })
        return retried

    async def _request_before_deadline(
        self, worker: object, command: WorkerCommand,
    ) -> WorkerResult:
        remaining = self._remaining_seconds(command.deadline)
        if remaining <= 0:
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.FAILED_SAFE,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code="deadline_expired",
            )
        return await asyncio.to_thread(worker.request, command, remaining)

    @staticmethod
    def _result_correlates(command: WorkerCommand, result: WorkerResult) -> bool:
        """Require one worker response to name the exact accepted command.

        The guest pipe only correlates ``request_id``/``operation_id``; the
        bridge must additionally prove the response kind and identity
        coordinates belong to this exact command before any normal outcome is
        allowed to move durable state.
        """

        return bool(
            result.request_id == command.request_id
            and result.kind is command.kind
            and result.binding_id == command.binding_id
            and result.binding_revision == command.binding_revision
            and result.conversation_revision == command.conversation_revision
            and result.operation_id == command.operation_id
            and (
                result.status is not WorkerStatus.OK
                or result.worker_epoch != UUID(int=0)
            )
        )

    @classmethod
    def _correlated_result(
        cls, command: WorkerCommand, result: WorkerResult,
    ) -> WorkerResult:
        """Fail closed when any worker response is not exactly correlated."""

        if not cls._result_correlates(command, result):
            return result.model_copy(update={
                "status": WorkerStatus.UNCERTAIN,
                "error_code": "worker_response_mismatch",
            })
        return result

    @staticmethod
    def _remaining_seconds(deadline: datetime | None) -> float:
        if deadline is None:
            return 0.0
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    @staticmethod
    def _is_selection_refresh_request(
        command: WorkerCommand, result: WorkerResult,
    ) -> bool:
        """Accept the refresh signal only from the exact logical request."""

        return bool(
            command.kind in _SELECTION_REFRESH_KINDS
            and result.status is WorkerStatus.FAILED_SAFE
            and result.error_code == _SELECTION_PROCESS_REFRESH_REQUIRED
            and result.request_id == command.request_id
            and result.kind is command.kind
            and result.binding_id == command.binding_id
            and result.binding_revision == command.binding_revision
            and result.conversation_revision == command.conversation_revision
            and result.operation_id == command.operation_id
        )

    def _selection_refresh_nonterminal_guard(self, command: WorkerCommand) -> bool:
        rows = self._db.execute(
            """SELECT operation_id,status,commit_intent FROM qq_vm_ops
               WHERE status NOT IN (?,?,?,?)""",
            tuple(item.value for item in _TERMINAL),
        ).fetchall()
        if command.kind is WorkerKind.OBSERVE:
            return not rows
        if command.kind is not WorkerKind.PREPARE or command.operation_id is None:
            return False
        return bool(
            len(rows) == 1
            and rows[0]["operation_id"] == str(command.operation_id)
            and rows[0]["status"] == SendStatus.PENDING.value
            and int(rows[0]["commit_intent"]) == 0
        )

    async def _activate_fresh_selection_worker(self, deadline: datetime) -> bool:
        """Retire the stale process before starting and activating its successor."""

        old_worker = self._worker
        spawn = getattr(old_worker, "spawn_successor", None)
        successor = None
        activated = False
        try:
            if callable(spawn) and self._remaining_seconds(deadline) > 0:
                try:
                    successor = spawn()
                except Exception:
                    successor = None
                else:
                    self._pending_successor = successor
                    self._pending_successor_started = False
            # The selection action can rebuild QQ's Chromium UIA provider
            # asynchronously.  Never initialize a successor while the process
            # that performed Invoke is still alive: the new process could bind
            # to the same stale provider generation during its HEALTH request.
            if not await self._retire_untrusted_selection_worker(old_worker):
                return False
            if successor is None or self._remaining_seconds(deadline) <= 0:
                return False
            successor.start()
            self._pending_successor_started = True
            health_command = WorkerCommand(kind=WorkerKind.HEALTH, deadline=deadline)
            health = await self._request_before_deadline(successor, health_command)
            if not self._health_matches_command(health_command, health):
                return False
            self._worker = successor
            self._worker_generation += 1
            self._last_health = health
            self._pending_successor = None
            self._pending_successor_started = False
            activated = True
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            if successor is not None and not activated:
                try:
                    cleanup_timeout = min(1.0, max(0.05, self._timeout))
                    await self._stop_and_confirm(
                        successor,
                        deadline,
                        cleanup_timeout=cleanup_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                if self._pending_successor_confirmed_stopped():
                    self._pending_successor = None
                    self._pending_successor_started = False

    async def _retire_untrusted_selection_worker(self, worker: object) -> bool:
        """Boundedly stop a process that crossed the selection action boundary."""

        cleanup_timeout = min(1.0, max(0.05, self._timeout))
        try:
            return await self._stop_and_confirm(
                worker,
                datetime.now(UTC),
                cleanup_timeout=cleanup_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _health_matches_command(
        command: WorkerCommand, result: WorkerResult,
    ) -> bool:
        """Accept a successor HEALTH only when it is fully correlated.

        A successor may be activated by a HEALTH result, so it must prove it
        answered this exact probe with a live, nonzero worker epoch and no
        binding/operation coordinates of its own.
        """

        return bool(
            result.status is WorkerStatus.OK
            and result.request_id == command.request_id
            and result.kind is WorkerKind.HEALTH
            and result.worker_epoch != UUID(int=0)
            and result.binding_id is None
            and result.binding_revision == 0
            and result.conversation_revision == 0
            and result.operation_id is None
        )

    async def _stop_and_confirm(
        self,
        worker: object,
        deadline: datetime,
        *,
        cleanup_timeout: float | None = None,
    ) -> bool:
        stop = getattr(worker, "stop", None)
        snapshot = getattr(worker, "status_snapshot", None)
        if not callable(stop) or not callable(snapshot):
            return False
        remaining = (
            cleanup_timeout
            if cleanup_timeout is not None
            else self._remaining_seconds(deadline)
        )
        if remaining <= 0:
            return False

        def invoke_stop() -> None:
            try:
                parameters = inspect.signature(stop).parameters
            except (TypeError, ValueError):
                parameters = {"timeout_seconds": None}
            if parameters:
                # QQVMWorkerProcess.stop may perform up to three bounded joins
                # (graceful, terminate, kill).  Divide the remaining lifecycle
                # budget so the full stop sequence stays within this stage.
                stop(remaining / 3)
            else:
                stop()

        stop_task = asyncio.create_task(asyncio.to_thread(invoke_stop))
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            # Process retirement is a safety boundary.  Once stop has started,
            # do not return control while its thread can still mutate lifecycle
            # state in the background.
            await stop_task
            raise
        state = snapshot()
        exit_code = state.get("worker_exit_code")
        return bool(
            state.get("worker_alive") is False
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
        )

    async def _recover_after_read_only_timeout(
        self,
        *,
        conversation_id: str,
        binding: QQIdentityBinding,
        binding_revision: int,
        command: WorkerCommand,
    ) -> bool:
        """Replace a dead worker once; never replay the failed observation."""

        snapshot_method = getattr(self._worker, "status_snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else {}
        terminal = snapshot.get("first_terminal_failure")
        run_id = snapshot.get("run_id")
        exit_code = snapshot.get("worker_exit_code")
        predicate_ok = bool(
            isinstance(terminal, dict)
            and terminal.get("request_id") == str(command.request_id)
            and terminal.get("kind") == WorkerKind.OBSERVE.value
            and terminal.get("binding_id") == binding.binding_id
            and terminal.get("error_code") == "worker_timeout_isolated"
            and terminal.get("parent_terminate_reason") == "request_timeout"
            and snapshot.get("worker_alive") is False
            and isinstance(exit_code, int) and not isinstance(exit_code, bool)
            and terminal.get("worker_exit_code") == exit_code
            and snapshot.get("parent_terminate_reason") == "request_timeout"
            and isinstance(run_id, str)
            and run_id
        )
        if not predicate_ok:
            return False

        now = datetime.now(UTC).isoformat()
        recovery_id = str(uuid4())
        successor_generation = self._worker_generation + 1
        status = "starting"
        self._db.execute("BEGIN IMMEDIATE")
        try:
            released_slot = self._db.execute(
                """SELECT q.request_id
                   FROM qq_vm_observation_quarantines q
                   JOIN qq_vm_observation_quarantine_releases r
                     ON r.conversation_id=q.conversation_id
                    AND r.request_id=q.request_id
                   WHERE q.conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if released_slot is not None:
                self._db.execute(
                    """DELETE FROM qq_vm_observation_quarantines
                       WHERE conversation_id=? AND request_id=?""",
                    (conversation_id, released_slot["request_id"]),
                )
            self._db.execute(
                """INSERT OR IGNORE INTO qq_vm_observation_quarantines(
                     conversation_id,failed_run_id,binding_id,binding_revision,request_id,
                     failed_generation,error_code,parent_terminate_reason,
                     worker_exit_code,quarantined_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    conversation_id, run_id, binding.binding_id, binding_revision,
                    str(command.request_id), self._worker_generation,
                    "worker_timeout_isolated", "request_timeout",
                    exit_code if isinstance(exit_code, int) else None, now,
                ),
            )
            prior = self._db.execute(
                "SELECT 1 FROM qq_vm_readonly_recoveries WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if prior is not None:
                self._db.execute("COMMIT")
                return True
            nonterminal = self._db.execute(
                """SELECT 1 FROM qq_vm_ops
                   WHERE status NOT IN (?,?,?,?) LIMIT 1""",
                tuple(item.value for item in _TERMINAL),
            ).fetchone()
            if nonterminal is not None:
                status = "blocked_nonterminal_operation"
            self._db.execute(
                """INSERT INTO qq_vm_readonly_recoveries(
                     recovery_id,run_id,failed_generation,successor_generation,
                     conversation_id,binding_id,binding_revision,request_id,status,
                     health_request_id,health_error_code,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)""",
                (
                    recovery_id, run_id, self._worker_generation,
                    successor_generation if status == "starting" else None,
                    conversation_id, binding.binding_id, binding_revision,
                    str(command.request_id), status, now,
                    None if status == "starting" else now,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        if status != "starting":
            return True

        spawn = getattr(self._worker, "spawn_successor", None)
        if not callable(spawn):
            self._finish_recovery(
                recovery_id, status="successor_unsupported",
                health_request_id=None, health_error_code=None,
            )
            return True
        successor = None
        activated = False
        recovery_deadline = datetime.now(UTC) + timedelta(
            seconds=min(5.0, max(0.05, self._timeout))
        )
        try:
            successor = spawn()
            self._pending_successor = successor
            self._pending_successor_started = False
            successor.start()
            self._pending_successor_started = True
            health_command = WorkerCommand(
                kind=WorkerKind.HEALTH, deadline=recovery_deadline
            )
            health = await self._request_before_deadline(
                successor,
                health_command,
            )
        except asyncio.CancelledError:
            self._finish_recovery(
                recovery_id,
                status="successor_cancelled",
                health_request_id=None,
                health_error_code="CancelledError",
            )
            raise
        except Exception as exc:
            self._finish_recovery(
                recovery_id,
                status="successor_health_exception",
                health_request_id=None,
                health_error_code=type(exc).__name__,
            )
        else:
            if not self._health_matches_command(health_command, health):
                health_error = health.error_code or (
                    health.status.value
                    if health.status is not WorkerStatus.OK
                    else "health_correlation_mismatch"
                )
                self._finish_recovery(
                    recovery_id,
                    status="successor_health_failed",
                    health_request_id=str(health.request_id),
                    health_error_code=health_error,
                )
            else:
                self._worker = successor
                self._worker_generation = successor_generation
                self._last_health = health
                activated = True
                self._pending_successor = None
                self._pending_successor_started = False
                self._finish_recovery(
                    recovery_id,
                    status="successor_active",
                    health_request_id=str(health.request_id),
                    health_error_code=None,
                )
        finally:
            if successor is not None and not activated:
                try:
                    cleanup_timeout = min(1.0, max(0.05, self._timeout))
                    await self._stop_and_confirm(
                        successor,
                        recovery_deadline,
                        cleanup_timeout=cleanup_timeout,
                    )
                except BaseException as exc:
                    self._finish_recovery(
                        recovery_id,
                        status="successor_cleanup_failed",
                        health_request_id=None,
                        health_error_code=type(exc).__name__,
                    )
                else:
                    if self._pending_successor_confirmed_stopped():
                        self._pending_successor = None
                        self._pending_successor_started = False
                    else:
                        self._finish_recovery(
                            recovery_id,
                            status="successor_cleanup_unconfirmed",
                            health_request_id=None,
                            health_error_code="worker_exit_unknown",
                        )
        return True

    def _pending_successor_confirmed_stopped(self) -> bool:
        pending = self._pending_successor
        if pending is None:
            return True
        snapshot = getattr(pending, "status_snapshot", None)
        if not callable(snapshot):
            return False
        try:
            status = snapshot()
        except Exception:
            return False
        exit_code = status.get("worker_exit_code")
        return bool(
            status.get("worker_alive") is False
            and (
                not self._pending_successor_started
                or (isinstance(exit_code, int) and not isinstance(exit_code, bool))
            )
        )

    def _finish_recovery(
        self,
        recovery_id: str,
        *,
        status: str,
        health_request_id: str | None,
        health_error_code: str | None,
    ) -> None:
        self._db.execute(
            """UPDATE qq_vm_readonly_recoveries
               SET status=?,health_request_id=?,health_error_code=?,completed_at=?
               WHERE recovery_id=?""",
            (
                status, health_request_id, health_error_code,
                datetime.now(UTC).isoformat(), recovery_id,
            ),
        )

    @staticmethod
    def _verify_bootstrap_adoption(
        provenance: dict[str, object], rows: list[dict[str, object]]
    ) -> None:
        expected_count = provenance.get("bubble_count")
        expected_ordinal = provenance.get("last_ordinal")
        expected_hash = provenance.get("text_sha256")
        if (
            isinstance(expected_count, bool) or not isinstance(expected_count, int)
            or isinstance(expected_ordinal, bool) or not isinstance(expected_ordinal, int)
            or not isinstance(expected_hash, str)
            or len(rows) != expected_count or expected_ordinal != expected_count - 1
            or not rows or rows[-1].get("direction") != "inbound"
            or hashlib.sha256(str(rows[-1].get("text", "")).encode()).hexdigest()
               != expected_hash
        ):
            raise ValueError("bootstrap_last_inbound_evidence_mismatch")

    def acknowledge_observation(self, conversation_id: str, local_keys: tuple[str, ...]) -> int:
        """Called only after RuntimeState durably accepted the entire batch."""
        return self._cursor.acknowledge_keys(conversation_id, local_keys)

    def close(self) -> None:
        self._worker.stop()
        self._cursor.close()
        self._db.close()

    async def aclose(self) -> None:
        async with self._generation_lock:
            workers = []
            if self._pending_successor is not None:
                workers.append(self._pending_successor)
            if all(item is not self._worker for item in workers):
                workers.append(self._worker)
            first_error = None
            for worker in workers:
                try:
                    await asyncio.to_thread(worker.stop)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            self._pending_successor = None
            self._pending_successor_started = False
            self._cursor.close()
            self._db.close()
            if first_error is not None:
                raise first_error


def _receipt_fingerprint(row: dict[str, object]) -> str:
    stable = {key: row.get(key) for key in (
        "direction", "text", "message_key", "conversation_internal_id", "participant_signature"
    )}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
