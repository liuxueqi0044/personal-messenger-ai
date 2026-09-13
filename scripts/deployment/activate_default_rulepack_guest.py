"""Validate and idempotently activate the approved default RulePack in the guest.

The normal command is intentionally guest-only and targets the fixed runtime
database. ``--self-test`` is a local, non-activation test mode that uses an
explicit temporary database and still emits only redacted status fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from messenger_ai.rules.compiler import RuleCompilationError, RulePackCompiler
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore


EXPECTED_COMPUTER = "PMAI-QQVM"
EXPECTED_USER = "qqbot"
EXPECTED_RULEPACK_ID = "personal-default"
APPROVED_SOURCE_HASH = "b63c24aaa58bb30c4d05cd4297b562f1164ee932b6b61224c6c4035f65c2d6a3"
FIXED_DB = Path(r"C:\PMAI\data\rules.sqlite3")


def _status(
    *,
    rulepack_id: str | None = None,
    version: str | None = None,
    source_hash: str | None = None,
    active: bool = False,
    error_code: str | None = None,
) -> dict[str, Any]:
    # Keep this schema deliberately free of source text, persona, chat text,
    # exception details, credentials, and keys.
    return {
        "rulepack_id": rulepack_id,
        "version": version,
        "source_hash": source_hash,
        "active": active,
        "error_code": error_code,
    }


def _emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _require_guest_context() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("guest_context_required")
    if os.environ.get("COMPUTERNAME", "").upper() != EXPECTED_COMPUTER:
        raise RuntimeError("guest_context_required")
    if os.environ.get("USERNAME", "").casefold() != EXPECTED_USER:
        raise RuntimeError("guest_context_required")
    try:
        import win32com.client

        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        service = locator.ConnectServer(".", "root\\cimv2")
        rows = service.ExecQuery(
            "SELECT Manufacturer, Model FROM Win32_ComputerSystem"
        )
        matched = any(
            "virtualbox" in str(row.Model).casefold()
            and (
                "oracle" in str(row.Manufacturer).casefold()
                or "innotek" in str(row.Manufacturer).casefold()
            )
            for row in rows
        )
    except Exception as exc:
        raise RuntimeError("guest_context_probe_failed") from exc
    if not matched:
        raise RuntimeError("guest_context_required")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.expanduser().resolve())) == os.path.normcase(
        str(right.expanduser().resolve())
    )


def _draft_for(store: AtomicRulePackStore, rulepack_id: str, version: str):
    row = store.connection.execute(
        "SELECT draft_id FROM m7_rulepack_sources WHERE rulepack_id=? AND version=?",
        (rulepack_id, version),
    ).fetchone()
    return store.draft(row["draft_id"]) if row else None


def activate(source_path: Path, db_path: Path, *, self_test: bool) -> dict[str, Any]:
    try:
        if not self_test:
            _require_guest_context()
            if not _same_path(db_path, FIXED_DB):
                return _status(error_code="fixed_database_required")
    except RuntimeError as exc:
        return _status(error_code=str(exc))
    try:
        content = source_path.read_bytes()
    except OSError:
        return _status(error_code="source_unreadable")

    source_hash = hashlib.sha256(content).hexdigest()
    if source_hash != APPROVED_SOURCE_HASH:
        return _status(source_hash=source_hash, error_code="source_not_approved")
    try:
        draft = RulePackCompiler().ingest(
            RuleSource(name=source_path.name, content=content)
        )
    except (RuleCompilationError, ValueError, TypeError):
        return _status(source_hash=source_hash, error_code="source_invalid")
    if draft.rulepack_id != EXPECTED_RULEPACK_ID:
        return _status(
            rulepack_id=draft.rulepack_id,
            version=draft.version,
            source_hash=source_hash,
            error_code="unexpected_rulepack_id",
        )
    if not draft.report.valid or draft.report.requires_human_review:
        return _status(
            rulepack_id=draft.rulepack_id,
            version=draft.version,
            source_hash=source_hash,
            error_code="source_requires_review",
        )

    try:
        store = AtomicRulePackStore(str(db_path))
    except (OSError, ValueError, sqlite3.Error):
        return _status(
            rulepack_id=draft.rulepack_id,
            version=draft.version,
            source_hash=source_hash,
            error_code="database_unavailable",
        )
    try:
        active_row = store.connection.execute(
            "SELECT version,source_hash FROM m7_rulepack_sources "
            "WHERE rulepack_id=? AND status='active'",
            (draft.rulepack_id,),
        ).fetchone()
        if active_row is not None:
            if (
                active_row["version"] == draft.version
                and active_row["source_hash"] == source_hash
            ):
                return _status(
                    rulepack_id=draft.rulepack_id,
                    version=draft.version,
                    source_hash=source_hash,
                    active=True,
                )
            return _status(
                rulepack_id=draft.rulepack_id,
                version=draft.version,
                source_hash=source_hash,
                error_code="different_active_version",
            )

        existing = _draft_for(store, draft.rulepack_id, draft.version)
        if existing is None:
            existing = store.save_draft(draft, source_blob=content)
        elif existing.source_hash != source_hash:
            return _status(
                rulepack_id=draft.rulepack_id,
                version=draft.version,
                source_hash=source_hash,
                error_code="source_hash_mismatch",
            )
        activated = store.activate(
            existing.draft_id,
            HumanApproval(
                approver_id="user-approved-default-persona",
                reason="User approved original personal-default persona and relationship rules for QQ direct-friend conversations",
                approved_at=datetime.now(UTC),
            ),
        )
        return _status(
            rulepack_id=activated.rulepack_id,
            version=activated.version,
            source_hash=activated.source_hash,
            active=True,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return _status(
            rulepack_id=draft.rulepack_id,
            version=draft.version,
            source_hash=source_hash,
            error_code="activation_failed",
        )
    finally:
        store.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=FIXED_DB)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="local validation/idempotency test; never targets the fixed guest DB",
    )
    args = parser.parse_args(argv)
    try:
        same_as_fixed = _same_path(args.db, FIXED_DB)
    except OSError:
        return _emit(_status(error_code="database_path_invalid"), 2)
    if args.self_test and same_as_fixed:
        return _emit(_status(error_code="self_test_requires_temp_database"), 2)
    try:
        result = activate(args.source, args.db, self_test=args.self_test)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        result = _status(error_code="activation_failed")
    return _emit(result, 0 if result.get("active") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
