"""Certify and close one exact, definitively failed QQ send lane.

The command is dry-run by default.  It never starts a worker, observes QQ,
calls a model, authorizes a draft, or sends a message.  ``--apply`` only
changes the stale runtime projection and appends an immutable certificate after
all four SQLite authorities prove a matching no-commit terminal outcome.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from messenger_ai.runtime.settlement import (
    SettlementRefused,
    TerminalSettlementTarget,
    settle_terminal_send,
)

try:
    from run_vm_runtime import QQRuntimeInstanceOwner
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.run_vm_runtime import QQRuntimeInstanceOwner


REPORT_SCHEMA = "pmai-terminal-send-settlement-result-v2"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")

# The exact target contract the host binds field-by-field on a validated result.
# It deliberately excludes operator_id, reason_code, and any message body.
TARGET_CONTRACT_FIELDS = (
    "mode",
    "settlement_id",
    "conversation_id",
    "pacing_plan_id",
    "segment_index",
    "operation_id",
    "authorization_id",
    "draft_id",
    "hub_send_outbox_id",
    "hub_stable_outbox_id",
    "hub_draft_outbox_id",
    "binding_id",
    "binding_revision",
    "conversation_revision",
    "body_hash",
    "terminal_status",
    "pacing_cancel_reason",
)


def _identifier(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid identifier")
    return value


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid sha256")
    return value


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _target_contract(
    target: TerminalSettlementTarget, apply: bool
) -> dict[str, object]:
    """Echo the exact host target without operator, reason, or message body."""
    return {
        "mode": "apply" if apply else "dry_run",
        "settlement_id": target.settlement_id,
        "conversation_id": target.conversation_id,
        "pacing_plan_id": target.pacing_plan_id,
        "segment_index": target.segment_index,
        "operation_id": target.operation_id,
        "authorization_id": target.authorization_id,
        "draft_id": target.draft_id,
        "hub_send_outbox_id": target.hub_send_outbox_id,
        "hub_stable_outbox_id": target.hub_stable_outbox_id,
        "hub_draft_outbox_id": target.hub_draft_outbox_id,
        "binding_id": target.binding_id,
        "binding_revision": target.binding_revision,
        "conversation_revision": target.conversation_revision,
        "body_hash": target.body_hash,
        "terminal_status": target.terminal_status,
        "pacing_cancel_reason": target.pacing_cancel_reason,
    }


def execute(
    *,
    data_dir: Path,
    target: TerminalSettlementTarget,
    apply: bool,
) -> dict[str, object]:
    owner = QQRuntimeInstanceOwner()
    owner.acquire()
    try:
        result = settle_terminal_send(data_dir, target, apply=apply)
    finally:
        owner.close()
    return {
        "schema": REPORT_SCHEMA,
        **_target_contract(target, apply),
        "status": "succeeded" if result.applied or result.idempotent else "validated",
        "artifact_status": result.artifact_status,
        "applied": result.applied,
        "idempotent": result.idempotent,
        "evidence_sha256": result.evidence_sha256,
        "legacy_due_operation_id_missing": result.legacy_due_operation_id_missing,
    }


def _error_code(exc: BaseException) -> str:
    value = str(exc)
    if isinstance(exc, SettlementRefused) and _SAFE_CODE.fullmatch(value):
        return value
    return type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--settlement-id", type=_identifier, required=True)
    parser.add_argument("--operator-id", type=_identifier, required=True)
    parser.add_argument("--reason-code", type=_identifier, required=True)
    parser.add_argument("--conversation-id", type=_identifier, required=True)
    parser.add_argument("--pacing-plan-id", type=_identifier, required=True)
    parser.add_argument("--segment-index", type=int, required=True)
    parser.add_argument("--operation-id", type=_identifier, required=True)
    parser.add_argument("--authorization-id", type=_identifier, required=True)
    parser.add_argument("--draft-id", type=_identifier, required=True)
    parser.add_argument("--hub-send-outbox-id", type=_identifier, required=True)
    parser.add_argument("--hub-stable-outbox-id", type=_identifier, required=True)
    parser.add_argument("--hub-draft-outbox-id", type=_identifier, required=True)
    parser.add_argument("--binding-id", type=_identifier, required=True)
    parser.add_argument("--binding-revision", type=int, required=True)
    parser.add_argument("--conversation-revision", type=int, required=True)
    parser.add_argument("--body-hash", type=_sha256, required=True)
    parser.add_argument(
        "--terminal-status", choices=("failed", "cancelled"), default="failed"
    )
    parser.add_argument(
        "--pacing-cancel-reason", type=_identifier, default="revalidation_rejected"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        target = TerminalSettlementTarget(
            settlement_id=args.settlement_id,
            operator_id=args.operator_id,
            reason_code=args.reason_code,
            conversation_id=args.conversation_id,
            pacing_plan_id=args.pacing_plan_id,
            segment_index=args.segment_index,
            operation_id=args.operation_id,
            authorization_id=args.authorization_id,
            draft_id=args.draft_id,
            hub_send_outbox_id=args.hub_send_outbox_id,
            hub_stable_outbox_id=args.hub_stable_outbox_id,
            hub_draft_outbox_id=args.hub_draft_outbox_id,
            binding_id=args.binding_id,
            binding_revision=args.binding_revision,
            conversation_revision=args.conversation_revision,
            body_hash=args.body_hash,
            terminal_status=args.terminal_status,
            pacing_cancel_reason=args.pacing_cancel_reason,
        )
        report = execute(data_dir=args.data_dir, target=target, apply=args.apply)
        code = 0
    except Exception as exc:  # noqa: BLE001 - report all fail-closed outcomes
        report = {
            "schema": REPORT_SCHEMA,
            "status": "rejected",
            "mode": "apply" if args.apply else "dry_run",
            "error_code": _error_code(exc),
        }
        code = 2
    _atomic_json(args.report, report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
