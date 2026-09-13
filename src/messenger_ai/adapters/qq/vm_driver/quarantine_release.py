from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from messenger_ai.domain import SendStatus


_TERMINAL_SEND_STATUSES = tuple(
    item.value for item in (
        SendStatus.VERIFIED,
        SendStatus.FAILED,
        SendStatus.UNCERTAIN,
        SendStatus.CANCELLED,
    )
)


def ensure_quarantine_release_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS qq_vm_observation_quarantine_releases(
             release_id TEXT PRIMARY KEY,
             failed_run_id TEXT NOT NULL,
             conversation_id TEXT NOT NULL,
             binding_id TEXT NOT NULL,
             binding_revision INTEGER NOT NULL,
             request_id TEXT NOT NULL,
             failed_generation INTEGER NOT NULL,
             error_code TEXT NOT NULL,
             parent_terminate_reason TEXT NOT NULL,
             worker_exit_code INTEGER,
             quarantined_at TEXT NOT NULL,
             operator_id TEXT NOT NULL,
             reason_code TEXT NOT NULL,
             released_at TEXT NOT NULL,
             UNIQUE(conversation_id,request_id)
           )"""
    )


def release_observation_quarantine(
    sqlite_path: str | Path,
    *,
    expected_run_id: str,
    conversation_id: str,
    binding_id: str,
    binding_revision: int,
    request_id: str,
    failed_generation: int,
    operator_id: str,
    reason_code: str,
) -> dict[str, object]:
    """Release one exact read-only quarantine without touching message state."""

    path = Path(sqlite_path)
    if not path.is_file():
        raise ValueError("bridge_database_missing")
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"qq_vm_ops", "qq_vm_observation_quarantines"}.issubset(tables):
            raise ValueError("bridge_database_schema_invalid")
        quarantine_columns = {
            row["name"] for row in db.execute(
                "PRAGMA table_info(qq_vm_observation_quarantines)"
            ).fetchall()
        }
        if "failed_run_id" not in quarantine_columns:
            raise ValueError("quarantine_run_provenance_unavailable")
        ensure_quarantine_release_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                """SELECT failed_run_id,conversation_id,binding_id,binding_revision,
                          request_id,failed_generation,error_code,
                          parent_terminate_reason,worker_exit_code,quarantined_at
                   FROM qq_vm_observation_quarantines
                   WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quarantine_not_found")
            expected = (
                expected_run_id,
                conversation_id,
                binding_id,
                binding_revision,
                request_id,
                failed_generation,
            )
            actual = (
                row["failed_run_id"],
                row["conversation_id"],
                row["binding_id"],
                int(row["binding_revision"]),
                row["request_id"],
                int(row["failed_generation"]),
            )
            if actual != expected:
                raise ValueError("quarantine_cas_mismatch")
            released = db.execute(
                """SELECT release_id,released_at FROM qq_vm_observation_quarantine_releases
                   WHERE conversation_id=? AND request_id=?""",
                (conversation_id, request_id),
            ).fetchone()
            if released is not None:
                db.execute("COMMIT")
                return {
                    "status": "already_released",
                    "release_id": released["release_id"],
                    "released_at": released["released_at"],
                }
            nonterminal = db.execute(
                """SELECT 1 FROM qq_vm_ops
                   WHERE status NOT IN (?,?,?,?) LIMIT 1""",
                _TERMINAL_SEND_STATUSES,
            ).fetchone()
            if nonterminal is not None:
                raise ValueError("nonterminal_send_operation_present")
            release_id = str(uuid4())
            released_at = datetime.now(UTC).isoformat()
            db.execute(
                """INSERT INTO qq_vm_observation_quarantine_releases(
                     release_id,failed_run_id,conversation_id,binding_id,
                     binding_revision,request_id,failed_generation,error_code,
                     parent_terminate_reason,worker_exit_code,quarantined_at,
                     operator_id,reason_code,released_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    release_id,
                    row["failed_run_id"],
                    row["conversation_id"],
                    row["binding_id"],
                    row["binding_revision"],
                    row["request_id"],
                    row["failed_generation"],
                    row["error_code"],
                    row["parent_terminate_reason"],
                    row["worker_exit_code"],
                    row["quarantined_at"],
                    operator_id,
                    reason_code,
                    released_at,
                ),
            )
            db.execute("COMMIT")
            return {
                "status": "released",
                "release_id": release_id,
                "released_at": released_at,
            }
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    finally:
        db.close()
