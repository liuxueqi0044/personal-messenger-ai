"""Trigger the M10 audit redaction migration on one installed guest runtime.

This is a maintenance-only path.  It never starts the runtime, a worker, a
model, a provider, an observation, or a send; it imports only the pacing
scheduler *after* every safety gate has passed.  The command refuses unless the
durable global runtime control is exactly paused and it holds the same per-user
automation ownership mutex the live runtime uses, then it invokes the isolated
audit migration directly so only ``m10_audit.payload_json`` and its version
marker can change.

The written report is privacy-minimal: status, operator, counts, migration
version and deterministic SHA-256 evidence digests only.  It never contains
``payload_json``, message/body text, or any raw sensitive value.
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

REPORT_SCHEMA = "pmai-pacing-audit-sanitization-result-v1"
RUNTIME_DB_NAME = "runtime.sqlite3"
PACING_DB_NAME = "pacing.sqlite3"
AUDIT_TABLE = "m10_audit"
AUDIT_MIGRATION_KEY = "m10_audit_payload_redaction"
AUDIT_MIGRATION_VERSION = "redacted-v1"

_OPERATOR = re.compile(r"[a-z][a-z0-9_-]{2,63}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


class _RuntimeOwner:
    """Same per-user automation ownership mutex as ``run_vm_runtime``.

    Reimplemented locally so this maintenance path never imports the worker,
    model, provider, observation, or send modules that live behind
    ``run_vm_runtime``.  The mutex name is byte-for-byte identical so it still
    contends with the live runtime for the same Windows user.
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, *, user_scope: str | None = None) -> None:
        if os.name != "nt":
            raise RuntimeError("QQ runtime instance ownership requires Windows")
        identity = user_scope or "\\".join(
            part for part in (os.environ.get("USERDOMAIN"), getpass.getuser()) if part
        )
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
        self.name = f"Local\\PersonalMessengerAI.QQRuntime.{digest}"
        self._handle: int | None = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        )
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        self._kernel32.ReleaseMutex.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def acquire(self) -> None:
        if self._handle is not None:
            return
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError(
                ctypes.get_last_error(), "cannot create QQ runtime ownership mutex"
            )
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            raise RuntimeError("QQ runtime is already running for this Windows user")
        self._handle = int(handle)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._kernel32.ReleaseMutex(handle)
        self._kernel32.CloseHandle(handle)


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RuntimeError("PACING_AUDIT_DB_MISSING")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None
    )
    try:
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        # A hot WAL left by an abruptly stopped runtime has no shared-memory
        # file, which a strictly read-only handle cannot initialise.  The owner
        # mutex is already held and the runtime is stopped, so a query-only
        # handle is safe here and still cannot mutate a single row.
        connection.close()
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _require_global_pause(runtime_db: Path) -> int:
    """Fail closed unless the durable global control is exactly paused."""

    if not runtime_db.is_file():
        raise RuntimeError("PACING_AUDIT_RUNTIME_DB_MISSING")
    connection = _connect_readonly(runtime_db)
    try:
        if not _table_exists(connection, "runtime_global_control"):
            raise RuntimeError("PACING_AUDIT_GLOBAL_PAUSE_REQUIRED")
        row = connection.execute(
            "SELECT revision,paused,reason FROM runtime_global_control "
            "WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    if row is None or not bool(row["paused"]):
        raise RuntimeError("PACING_AUDIT_GLOBAL_PAUSE_REQUIRED")
    reason = row["reason"]
    if not isinstance(reason, str) or not reason:
        raise RuntimeError("PACING_AUDIT_GLOBAL_PAUSE_REQUIRED")
    return int(row["revision"])


def _read_payloads(
    pacing_db: Path, table: str, key_column: str
) -> list[tuple[str, str]]:
    connection = _connect_readonly(pacing_db)
    try:
        if not _table_exists(connection, table):
            return []
        rows = connection.execute(
            f"SELECT {key_column} AS key,payload_json AS payload FROM {table} "
            f"ORDER BY {key_column}"
        ).fetchall()
    finally:
        connection.close()
    return [(str(row["key"]), str(row["payload"])) for row in rows]


def _read_migration_marker(pacing_db: Path) -> str | None:
    connection = _connect_readonly(pacing_db)
    try:
        if not _table_exists(connection, "m10_metadata"):
            return None
        row = connection.execute(
            "SELECT value FROM m10_metadata WHERE key=?", (AUDIT_MIGRATION_KEY,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row["value"])


def _aggregate_sha256(rows: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for key, payload in rows:
        digest.update(key.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(payload.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _business_table_evidence(
    pacing_db: Path, table: str, key_column: str
) -> tuple[str, int]:
    """Hash complete rows plus table/index/trigger SQL without exposing values."""

    connection = _connect_readonly(pacing_db)
    try:
        if not _table_exists(connection, table):
            raise RuntimeError("PACING_AUDIT_BUSINESS_SCHEMA_MISSING")
        schema = [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? "
                "AND type IN ('table','index','trigger') ORDER BY type,name",
                (table,),
            ).fetchall()
        ]
        columns = [
            tuple(row)
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        rows = [
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY {key_column}"
            ).fetchall()
        ]
    finally:
        connection.close()
    encoded = json.dumps(
        {"schema": schema, "columns": columns, "rows": rows},
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: {"bytes_sha256": hashlib.sha256(value).hexdigest()},
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(rows)


def _acquire_owner(owner_factory: object | None) -> object:
    factory = _RuntimeOwner if owner_factory is None else owner_factory
    try:
        owner = factory()
        owner.acquire()
    except Exception as exc:  # any contention fails closed
        raise RuntimeError("PACING_AUDIT_OWNER_LOCK_REQUIRED") from exc
    return owner


def execute(
    *,
    runtime_root: Path,
    operator_id: str,
    owner_factory: object | None = None,
) -> dict[str, object]:
    if _OPERATOR.fullmatch(operator_id) is None:
        raise RuntimeError("PACING_AUDIT_OPERATOR_INVALID")
    root = Path(runtime_root)
    runtime_db = root / RUNTIME_DB_NAME
    pacing_db = root / PACING_DB_NAME

    # Safety gates first: exclusive ownership, exact global pause, DB present.
    owner = _acquire_owner(owner_factory)
    try:
        _require_global_pause(runtime_db)
        if not pacing_db.is_file():
            raise RuntimeError("PACING_AUDIT_PACING_DB_MISSING")

        # Imported only past every gate so no worker/model/provider/observation/
        # send code is loaded, let alone executed, on a refused run.
        from messenger_ai.pacing.scheduler import (
            AUDIT_REDACTION_MIGRATION_VERSION,
            migrate_audit_payload_redaction,
        )

        audit_before = _read_payloads(pacing_db, AUDIT_TABLE, "audit_id")
        plans_before_hash, plans_before_count = _business_table_evidence(
            pacing_db, "m10_plans", "pacing_plan_id"
        )
        due_before_hash, due_before_count = _business_table_evidence(
            pacing_db, "m10_due_outbox", "outbox_id"
        )
        marker_before = _read_migration_marker(pacing_db)

        connection = sqlite3.connect(str(pacing_db), isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            migrate_audit_payload_redaction(connection)
        finally:
            connection.close()

        audit_after = _read_payloads(pacing_db, AUDIT_TABLE, "audit_id")
        plans_after_hash, plans_after_count = _business_table_evidence(
            pacing_db, "m10_plans", "pacing_plan_id"
        )
        due_after_hash, due_after_count = _business_table_evidence(
            pacing_db, "m10_due_outbox", "outbox_id"
        )
        marker_after = _read_migration_marker(pacing_db)
    finally:
        owner.close()

    if (
        plans_before_hash != plans_after_hash
        or due_before_hash != due_after_hash
        or plans_before_count != plans_after_count
        or due_before_count != due_after_count
    ):
        raise RuntimeError("PACING_AUDIT_BUSINESS_MUTATED")
    if marker_after is None:
        raise RuntimeError("PACING_AUDIT_MIGRATION_MARKER_MISSING")

    before_map = dict(audit_before)
    after_map = dict(audit_after)
    rewritten = sum(1 for key, value in before_map.items() if after_map.get(key) != value)
    rewritten += sum(1 for key in after_map if key not in before_map)
    audit_before_hash = _aggregate_sha256(audit_before)
    audit_after_hash = _aggregate_sha256(audit_after)

    if _VERSION.fullmatch(marker_after) is None or (
        marker_after != AUDIT_REDACTION_MIGRATION_VERSION
    ):
        raise RuntimeError("PACING_AUDIT_MIGRATION_VERSION_INVALID")

    return {
        "schema": REPORT_SCHEMA,
        "status": "applied" if marker_before is None else "noop",
        "operator_id": operator_id,
        "migration_version": marker_after,
        "migration_applied": marker_before is None,
        "idempotent": rewritten == 0 and audit_before_hash == audit_after_hash,
        "business_byte_identical": True,
        "audit_before_sha256": audit_before_hash,
        "audit_after_sha256": audit_after_hash,
        "plans_before_sha256": plans_before_hash,
        "plans_after_sha256": plans_after_hash,
        "due_outbox_before_sha256": due_before_hash,
        "due_outbox_after_sha256": due_after_hash,
        "counts": {
            "audit_rows": len(audit_after),
            "audit_rows_rewritten": rewritten,
            "plans": plans_after_count,
            "due_outbox": due_after_count,
        },
    }


def _error_code(exc: BaseException) -> str:
    value = str(exc)
    if _SAFE_CODE.fullmatch(value):
        return value
    return type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = execute(runtime_root=args.runtime_root, operator_id=args.operator_id)
        code = 0
    except Exception as exc:  # noqa: BLE001 - report all fail-closed outcomes
        report = {
            "schema": REPORT_SCHEMA,
            "status": "rejected",
            "error_code": _error_code(exc),
        }
        code = 2
    _atomic_json(args.report, report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
