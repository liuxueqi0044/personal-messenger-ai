"""Explicitly recover one QQ cursor after a volatile locator migration.

The runtime must be stopped.  The tool performs one real read-only OBSERVE for
the exact configured direct binding, replaces only its settled cursor snapshot
through an opaque CAS token, then clears only an approved cursor gap.  Message
text is held in memory for hashing and is never written to this tool's report
or audit tables.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.vm_driver.contracts import (
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
)
from messenger_ai.adapters.qq.vm_driver.message_cursor import MessageCursorStore
from messenger_ai.adapters.qq.vm_driver.visual_selection import runtime_id_digest
from messenger_ai.adapters.qq.vm_driver.worker import QQVMWorkerProcess
from messenger_ai.runtime.settlement import verify_terminal_settlement

try:
    from run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _capability,
        load_config,
        validate_config,
    )
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _capability,
        load_config,
        validate_config,
    )


REPORT_SCHEMA = "pmai-qq-cursor-reanchor-result-v1"
APPROVED_PAUSE_REASONS = frozenset(
    {"message_anchor_gap", "message_cursor_schema_migration_required"}
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_AUDIT_OPERATOR = re.compile(r"[a-z][a-z0-9_-]{2,63}")
_AUDIT_REASON = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _identifier(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid identifier")
    return value


def _conversation_state(runtime_db: Path, conversation_id: str) -> dict[str, Any]:
    with sqlite3.connect(runtime_db) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT account_id,contact_id,binding_revision,conversation_revision,
                      conversation_type,paused,pause_reason
               FROM runtime_conversations WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("CURSOR_REANCHOR_CONVERSATION_MISSING")
    return dict(row)


def _validate_audit_source(operator_id: str, reason_code: str) -> None:
    if _AUDIT_OPERATOR.fullmatch(operator_id) is None:
        raise RuntimeError("CURSOR_REANCHOR_OPERATOR_INVALID")
    if _AUDIT_REASON.fullmatch(reason_code) is None:
        raise RuntimeError("CURSOR_REANCHOR_REASON_INVALID")


def _cursor_reanchor_provenance(
    cursor: MessageCursorStore, *, conversation_id: str, operator_id: str, reason_code: str,
) -> dict[str, object] | None:
    """Find an already-committed reanchor for the current snapshot only.

    The cursor audit stores SHA-256 values and source metadata, never bubbles
    or message text.  Matching the replacement against the current cursor CAS
    token prevents an old recovery from releasing a later anchor.
    """
    current = cursor.snapshot_token(conversation_id)
    if current is None:
        return None
    row = cursor.connection.execute(
        """SELECT expected_snapshot_sha256,replacement_snapshot_sha256,next_seq
           FROM cursor_reanchor_audit
           WHERE conversation_id=? AND operator_id=? AND reason_code=?
             AND replacement_snapshot_sha256=? AND outcome IN ('applied','idempotent')
           ORDER BY audit_id DESC LIMIT 1""",
        (conversation_id, operator_id, reason_code, current),
    ).fetchone()
    if row is None:
        return None
    return {
        "previous_snapshot_sha256": str(row["expected_snapshot_sha256"]),
        "current_snapshot_sha256": str(row["replacement_snapshot_sha256"]),
        "next_seq": int(row["next_seq"]),
    }


def _runtime_reanchor_completed(
    runtime_db: Path, *, conversation_id: str, operator_id: str, reason_code: str,
    provenance: dict[str, object], state: dict[str, Any],
) -> bool:
    """Recognize cursor+runtime completion after a crash before report write."""
    try:
        with sqlite3.connect(runtime_db) as db:
            columns = {
                item[1]
                for item in db.execute(
                    "PRAGMA table_info(runtime_cursor_reanchor_audit)"
                )
            }
            pause_projection = (
                "pause_reason"
                if "pause_reason" in columns
                else "'message_anchor_gap' AS pause_reason"
            )
            row = db.execute(
                f"""SELECT previous_conversation_revision,current_conversation_revision,
                          {pause_projection}
                   FROM runtime_cursor_reanchor_audit
                   WHERE conversation_id=? AND operator_id=? AND reason_code=?
                     AND previous_snapshot_sha256=? AND current_snapshot_sha256=?
                     AND next_seq=?""",
                (conversation_id, operator_id, reason_code,
                 provenance["previous_snapshot_sha256"], provenance["current_snapshot_sha256"],
                 provenance["next_seq"]),
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(
        row is not None
        and not bool(state["paused"])
        and state["pause_reason"] is None
        and int(row[1]) == int(state["conversation_revision"])
        and int(row[1]) == int(row[0]) + 1
        and row[2] in APPROVED_PAUSE_REASONS
    )


def _ensure_send_lanes_settled(data_dir: Path, conversation_id: str) -> None:
    """Reject every known queue that could continue work for the old anchor."""
    runtime_db = data_dir / "runtime.sqlite3"
    bridge_db = data_dir / "qq-vm-bridge.sqlite3"
    hub_db = data_dir / "hub.sqlite3"
    pacing_db = data_dir / "pacing.sqlite3"
    certified_operations: set[tuple[str, str]] = set()
    verified_operations: set[str] = set()
    with sqlite3.connect(runtime_db) as db:
        db.row_factory = sqlite3.Row
        global_control = db.execute(
            "SELECT paused FROM runtime_global_control WHERE singleton=1"
        ).fetchone()
        if global_control is None or not bool(global_control["paused"]):
            raise RuntimeError("CURSOR_REANCHOR_RUNTIME_NOT_PAUSED")
        queued = db.execute(
            """SELECT 1 FROM runtime_event_outbox WHERE aggregate_id=?
               AND status IN ('pending','dispatching') LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        planning = db.execute(
            """SELECT 1 FROM runtime_planning_jobs WHERE conversation_id=?
               AND status IN ('pending','running') LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        artifact = db.execute(
            """SELECT 1 FROM runtime_plan_artifacts WHERE conversation_id=?
               AND status='waiting' LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        segments = db.execute(
            """SELECT pacing_plan_id,segment_index,operation_id,status
               FROM runtime_segment_executions WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchall()
    if any(item is not None for item in (queued, planning, artifact)):
        raise RuntimeError("CURSOR_REANCHOR_RUNTIME_LANE_NOT_SETTLED")
    for segment in segments:
        status = str(segment["status"])
        operation_id = segment["operation_id"]
        if status == "verified":
            # A runtime "verified" projection is only trustworthy when the
            # exact bridge receipt (operation+conversation) and the exact
            # pacing segment receipt with verified=1 both exist.  Otherwise an
            # unproven echo would let the reanchor release the anchor.
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or not _verified_send_proof(
                    data_dir,
                    conversation_id=conversation_id,
                    pacing_plan_id=str(segment["pacing_plan_id"]),
                    segment_index=int(segment["segment_index"]),
                    operation_id=operation_id,
                )
            ):
                raise RuntimeError("CURSOR_REANCHOR_RUNTIME_LANE_NOT_SETTLED")
            verified_operations.add(operation_id)
            continue
        if (
            status in {"failed", "cancelled"}
            and isinstance(operation_id, str)
            and operation_id
            and verify_terminal_settlement(
                data_dir,
                conversation_id=conversation_id,
                pacing_plan_id=str(segment["pacing_plan_id"]),
                segment_index=int(segment["segment_index"]),
                operation_id=operation_id,
                terminal_status=status,
            )
        ):
            certified_operations.add((operation_id, status))
            continue
        raise RuntimeError("CURSOR_REANCHOR_RUNTIME_LANE_NOT_SETTLED")

    with sqlite3.connect(hub_db) as db:
        operations = db.execute(
            """SELECT s.operation_id,s.status FROM send_operations s
               JOIN drafts d ON d.draft_id=s.draft_id
               WHERE d.conversation_id=?""",
            (conversation_id,),
        ).fetchall()
        outbox = db.execute(
            """SELECT 1 FROM outbox o
               LEFT JOIN send_operations s ON s.operation_id=o.aggregate_id
               LEFT JOIN drafts d ON d.draft_id=s.draft_id
               WHERE o.status IN ('pending','dispatching')
                 AND (o.aggregate_id=? OR d.conversation_id=?) LIMIT 1""",
            (conversation_id, conversation_id),
        ).fetchone()
    if outbox is not None or any(
        (status == "verified" and operation_id not in verified_operations)
        or (status != "verified" and (operation_id, status) not in certified_operations)
        for operation_id, status in operations
    ):
        raise RuntimeError("CURSOR_REANCHOR_HUB_LANE_NOT_SETTLED")

    with sqlite3.connect(bridge_db) as db:
        operations = db.execute(
            """SELECT operation_id,status FROM qq_vm_ops WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchall()
    if any(
        (status == "verified" and operation_id not in verified_operations)
        or (status != "verified" and (operation_id, status) not in certified_operations)
        for operation_id, status in operations
    ):
        raise RuntimeError("CURSOR_REANCHOR_SEND_LANE_NOT_SETTLED")

    with sqlite3.connect(pacing_db) as db:
        plan = db.execute(
            """SELECT 1 FROM m10_plans WHERE conversation_id=?
               AND status IN ('waiting','due_for_revalidation') LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        due = db.execute(
            """SELECT 1 FROM m10_due_outbox o
               JOIN m10_plans p ON p.pacing_plan_id=o.pacing_plan_id
               WHERE p.conversation_id=?
                 AND o.status IN ('pending','dispatching','dispatching_nonrecoverable')
               LIMIT 1""",
            (conversation_id,),
        ).fetchone()
    if plan is not None or due is not None:
        raise RuntimeError("CURSOR_REANCHOR_PACING_LANE_NOT_SETTLED")


def _verified_send_proof(
    data_dir: Path,
    *,
    conversation_id: str,
    pacing_plan_id: str,
    segment_index: int,
    operation_id: str,
) -> bool:
    """Prove one runtime-verified segment from exact bridge and pacing receipts.

    A runtime "verified" projection alone is not evidence.  The QQ bridge must
    hold the same operation+conversation with status ``verified`` *and* an
    exact receipt row, and pacing must hold a segment receipt whose
    ``operation_id`` matches and whose ``verified`` flag is exactly ``1``.
    Missing tables, extra rows, or any mismatch fail closed.
    """

    try:
        with sqlite3.connect(data_dir / "qq-vm-bridge.sqlite3") as db:
            operation = db.execute(
                "SELECT conversation_id,status FROM qq_vm_ops WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if (
                operation is None
                or str(operation[0]) != conversation_id
                or str(operation[1]) != "verified"
            ):
                return False
            receipt = db.execute(
                """SELECT 1 FROM qq_vm_receipts
                   WHERE operation_id=? AND conversation_id=?""",
                (operation_id, conversation_id),
            ).fetchone()
            if receipt is None:
                return False
        with sqlite3.connect(data_dir / "pacing.sqlite3") as db:
            segment = db.execute(
                """SELECT operation_id,verified FROM m10_segment_receipts
                   WHERE pacing_plan_id=? AND segment_index=?""",
                (pacing_plan_id, segment_index),
            ).fetchone()
    except sqlite3.Error:
        return False
    if segment is None or str(segment[0]) != operation_id:
        return False
    return int(segment[1]) == 1


def _health_is_correlated(command: WorkerCommand, result: Any) -> bool:
    """Accept only the exact HEALTH probe answered by a live worker epoch.

    A HEALTH result may authorize the successor process, so status alone is
    never sufficient: the request id, kind, empty binding/operation scope, zero
    revisions and a nonzero worker epoch must all name this probe.
    """

    return bool(
        isinstance(result, WorkerResult)
        and result.status is WorkerStatus.OK
        and result.request_id == command.request_id
        and result.kind is WorkerKind.HEALTH
        and result.binding_id is None
        and result.binding_revision == 0
        and result.conversation_revision == 0
        and result.operation_id is None
        and result.worker_epoch != UUID(int=0)
    )


def _capture_current_bubbles(
    *, 
    pack: Any,
    bindings: tuple[Any, ...],
    evidence: tuple[Any, ...],
    binding: Any,
    state: dict[str, Any],
    timeout_seconds: float,
) -> list[dict[str, object]]:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)

    def remaining_seconds() -> float:
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    def start_worker() -> QQVMWorkerProcess:
        worker = QQVMWorkerProcess(
            pack, bindings, session_evidence=evidence, run_id=str(uuid4())
        )
        inherited_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            worker.start()
        finally:
            if inherited_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = inherited_key
        return worker

    def stop_worker(worker: QQVMWorkerProcess) -> None:
        worker.stop()
        status = worker.status_snapshot()
        if status.get("worker_alive") is not False:
            raise RuntimeError("CURSOR_REANCHOR_WORKER_NOT_RETIRED")

    def probe_health(worker: QQVMWorkerProcess) -> Any:
        remaining = remaining_seconds()
        if remaining <= 0:
            raise RuntimeError("CURSOR_REANCHOR_OBSERVE_DEADLINE_EXPIRED")
        command = WorkerCommand(kind=WorkerKind.HEALTH, deadline=deadline)
        health = worker.request(command, remaining)
        if not _health_is_correlated(command, health):
            raise RuntimeError("CURSOR_REANCHOR_WORKER_HEALTH_FAILED")
        return health

    def observe(
        worker: QQVMWorkerProcess,
        command: WorkerCommand | None = None,
    ) -> tuple[WorkerCommand, Any]:
        observe_command = command or WorkerCommand(
                kind=WorkerKind.OBSERVE,
                binding_id=binding.binding_id,
                binding_revision=int(state["binding_revision"]),
                conversation_revision=int(state["conversation_revision"]),
                deadline=deadline,
            )
        remaining = remaining_seconds()
        if remaining <= 0:
            raise RuntimeError("CURSOR_REANCHOR_OBSERVE_DEADLINE_EXPIRED")
        return observe_command, worker.request(
            observe_command,
            remaining,
        )

    worker = start_worker()
    try:
        probe_health(worker)
        observe_command, observed = observe(worker)
        refresh_required = (
            observed.status is WorkerStatus.FAILED_SAFE
            and observed.error_code == "selection_process_refresh_required"
        )
        if refresh_required:
            stop_worker(worker)
            worker = start_worker()
            successor_health = probe_health(worker)
            retry_command = observe_command.model_copy(
                update={"request_id": uuid4()}
            )
            issue_handoff = getattr(worker, "mint_selection_handoff", None)
            if not callable(issue_handoff):
                raise RuntimeError("CURSOR_REANCHOR_HANDOFF_ISSUER_UNAVAILABLE")
            handoff = issue_handoff(
                predecessor_command=observe_command,
                predecessor_result=observed,
                successor_command=retry_command,
                source="selection_refresh",
                successor_worker_epoch=successor_health.worker_epoch,
                target_runtime_id_digest=runtime_id_digest(
                    binding.platform_conversation_id
                ),
                expires_at=deadline,
            )
            retry_command = retry_command.model_copy(
                update={"selection_handoff": handoff}
            )
            _retry_command, observed = observe(worker, retry_command)
        if observed.status is not WorkerStatus.OK:
            raise RuntimeError("CURSOR_REANCHOR_OBSERVE_FAILED")
        raw = observed.evidence.get("bubbles")
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise RuntimeError("CURSOR_REANCHOR_OBSERVE_SHAPE_INVALID")
        return list(raw)
    finally:
        if worker.status_snapshot().get("worker_alive") is not False:
            stop_worker(worker)


def _clear_pause(
    runtime_db: Path,
    *,
    conversation_id: str,
    binding_id: str,
    state: dict[str, Any],
    previous_snapshot_sha256: str,
    current_snapshot_sha256: str,
    next_seq: int,
    operator_id: str,
    reason_code: str,
    expected_pause_reason: str,
) -> bool:
    """CAS-clear the exact gap after the cursor transaction has committed."""
    if binding_id != state.get("contact_id"):
        raise RuntimeError("CURSOR_REANCHOR_BINDING_ID_MISMATCH")
    with sqlite3.connect(runtime_db, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """CREATE TABLE IF NOT EXISTS runtime_cursor_reanchor_audit(
                     recovery_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
                     binding_id TEXT NOT NULL,binding_revision INTEGER NOT NULL,
                     previous_conversation_revision INTEGER NOT NULL,
                     current_conversation_revision INTEGER NOT NULL,
                     previous_snapshot_sha256 TEXT NOT NULL,
                     current_snapshot_sha256 TEXT NOT NULL,next_seq INTEGER NOT NULL,
                     operator_id TEXT NOT NULL,reason_code TEXT NOT NULL,
                     pause_reason TEXT NOT NULL,
                     released_at TEXT NOT NULL,
                     UNIQUE(conversation_id,previous_snapshot_sha256,current_snapshot_sha256))"""
            )
            audit_columns = {
                row[1] for row in db.execute("PRAGMA table_info(runtime_cursor_reanchor_audit)")
            }
            if "pause_reason" not in audit_columns:
                db.execute(
                    "ALTER TABLE runtime_cursor_reanchor_audit ADD COLUMN "
                    "pause_reason TEXT NOT NULL DEFAULT 'message_anchor_gap'"
                )
            existing = db.execute(
                """SELECT 1 FROM runtime_cursor_reanchor_audit
                   WHERE conversation_id=? AND operator_id=? AND reason_code=?
                     AND pause_reason=?
                     AND previous_snapshot_sha256=? AND current_snapshot_sha256=?""",
                (conversation_id, operator_id, reason_code, expected_pause_reason,
                 previous_snapshot_sha256, current_snapshot_sha256),
            ).fetchone()
            current = db.execute(
                """SELECT account_id,contact_id,binding_revision,conversation_revision,
                          conversation_type,paused,pause_reason
                   FROM runtime_conversations WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("CURSOR_REANCHOR_CONVERSATION_MISSING")
            coordinates = (
                current["account_id"], current["contact_id"],
                int(current["binding_revision"]), current["conversation_type"],
            )
            expected = (
                state["account_id"], state["contact_id"],
                int(state["binding_revision"]), state["conversation_type"],
            )
            if coordinates != expected:
                raise RuntimeError("CURSOR_REANCHOR_RUNTIME_CAS_MISMATCH")
            if not bool(current["paused"]):
                if (
                    existing is None
                    or int(current["conversation_revision"])
                    != int(state["conversation_revision"]) + 1
                ):
                    raise RuntimeError("CURSOR_REANCHOR_PAUSE_ALREADY_CHANGED")
                db.execute("COMMIT")
                return False
            if int(current["conversation_revision"]) != int(state["conversation_revision"]):
                raise RuntimeError("CURSOR_REANCHOR_RUNTIME_CAS_MISMATCH")
            if current["pause_reason"] != expected_pause_reason:
                raise RuntimeError("CURSOR_REANCHOR_PAUSE_REASON_CHANGED")
            changed = db.execute(
                """UPDATE runtime_conversations SET paused=0,pause_reason=NULL,
                     conversation_revision=conversation_revision+1
                   WHERE conversation_id=? AND binding_revision=?
                     AND conversation_revision=? AND paused=1 AND pause_reason=?""",
                (
                    conversation_id, int(state["binding_revision"]),
                    int(state["conversation_revision"]), expected_pause_reason,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("CURSOR_REANCHOR_RUNTIME_CAS_MISMATCH")
            db.execute(
                """INSERT INTO runtime_cursor_reanchor_audit(
                     recovery_id,conversation_id,binding_id,binding_revision,
                     previous_conversation_revision,current_conversation_revision,
                     previous_snapshot_sha256,
                     current_snapshot_sha256,next_seq,operator_id,reason_code,
                     pause_reason,released_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), conversation_id, binding_id,
                    int(state["binding_revision"]), int(state["conversation_revision"]),
                    int(state["conversation_revision"]) + 1,
                    previous_snapshot_sha256, current_snapshot_sha256, next_seq,
                    operator_id, reason_code, expected_pause_reason,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute("COMMIT")
            return True
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


def execute(
    *, config_path: Path, conversation_id: str, operator_id: str,
    reason_code: str,
) -> dict[str, object]:
    owner = QQRuntimeInstanceOwner()
    owner.acquire()
    try:
        _validate_audit_source(operator_id, reason_code)
        config = load_config(config_path)
        pack, bindings, evidence = validate_config(
            config, api_key="offline-cursor-reanchor-validation"
        )
        _capability(config, pack)
        binding = next(
            (item for item in bindings if item.hub_conversation_id == conversation_id),
            None,
        )
        if binding is None or binding.conversation_type != "direct":
            raise RuntimeError("CURSOR_REANCHOR_BINDING_NOT_DIRECT")
        data_dir = Path(str(config["data_dir"]))
        runtime_db = data_dir / "runtime.sqlite3"
        cursor_db = data_dir / "qq-vm-bridge.cursor.sqlite3"
        state = _conversation_state(runtime_db, conversation_id)
        if (
            state["account_id"] != binding.account_id
            or state["contact_id"] != binding.contact_id
            or int(state["binding_revision"]) != 1
            or state["conversation_type"] != "direct"
        ):
            raise RuntimeError("CURSOR_REANCHOR_BINDING_MISMATCH")
        cursor = MessageCursorStore(cursor_db)
        try:
            provenance = _cursor_reanchor_provenance(
                cursor, conversation_id=conversation_id, operator_id=operator_id,
                reason_code=reason_code,
            )
            if not bool(state["paused"]):
                if provenance is None or not _runtime_reanchor_completed(
                    runtime_db, conversation_id=conversation_id, operator_id=operator_id,
                    reason_code=reason_code, provenance=provenance, state=state,
                ):
                    raise RuntimeError("CURSOR_REANCHOR_NOT_EXACT_GAP")
                return {
                    "schema": REPORT_SCHEMA, "status": "succeeded",
                    "conversation_id": conversation_id, "binding_id": binding.binding_id,
                    "cursor_applied": False, "cursor_idempotent": True,
                    "pause_cleared": False, "next_seq": int(provenance["next_seq"]),
                }
            if state["pause_reason"] not in APPROVED_PAUSE_REASONS:
                raise RuntimeError("CURSOR_REANCHOR_NOT_EXACT_GAP")
            expected_pause_reason = str(state["pause_reason"])
            _ensure_send_lanes_settled(data_dir, conversation_id)
            if provenance is not None:
                previous = str(provenance["previous_snapshot_sha256"])
                current = str(provenance["current_snapshot_sha256"])
                next_seq = int(provenance["next_seq"])
                cursor_applied, cursor_idempotent = False, True
            else:
                previous = cursor.snapshot_token(conversation_id)
                if previous is None:
                    raise RuntimeError("CURSOR_REANCHOR_SNAPSHOT_MISSING")
                bubbles = _capture_current_bubbles(
                    pack=pack, bindings=bindings, evidence=evidence, binding=binding,
                    state=state, timeout_seconds=float(config.get("worker_timeout_seconds", 15)),
                )
                result = cursor.reanchor_snapshot(
                    conversation_id, bubbles, expected_snapshot_sha256=previous,
                    operator_id=operator_id, reason_code=reason_code,
                )
                current, next_seq = result.snapshot_sha256, result.next_seq
                cursor_applied, cursor_idempotent = result.applied, result.idempotent
        finally:
            cursor.close()
        pause_cleared = _clear_pause(
            runtime_db,
            conversation_id=conversation_id,
            binding_id=binding.binding_id,
            state=state,
            previous_snapshot_sha256=previous,
            current_snapshot_sha256=current,
            next_seq=next_seq,
            operator_id=operator_id,
            reason_code=reason_code,
            expected_pause_reason=expected_pause_reason,
        )
        return {
            "schema": REPORT_SCHEMA,
            "status": "succeeded",
            "conversation_id": conversation_id,
            "binding_id": binding.binding_id,
            "cursor_applied": cursor_applied,
            "cursor_idempotent": cursor_idempotent,
            "pause_cleared": pause_cleared,
            "next_seq": next_seq,
        }
    finally:
        owner.close()


def _error_code(exc: BaseException) -> str:
    value = str(exc)
    return value if _SAFE_CODE.fullmatch(value) else type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--conversation-id", type=_identifier, required=True)
    parser.add_argument("--operator-id", type=_identifier, required=True)
    parser.add_argument("--reason-code", type=_identifier, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = execute(
            config_path=args.config,
            conversation_id=args.conversation_id,
            operator_id=args.operator_id,
            reason_code=args.reason_code,
        )
        code = 0
    except Exception as exc:  # noqa: BLE001 - report all fail-closed outcomes
        report = {
            "schema": REPORT_SCHEMA,
            "status": "rejected",
            "error_code": _error_code(exc),
        }
        code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_name("." + args.report.name + ".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    temporary.replace(args.report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
