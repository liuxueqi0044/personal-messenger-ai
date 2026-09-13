"""Atomically promote an accepted visual-selection block into canonical config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from activate_default_rulepack_guest import _require_guest_context
    from build_visual_selection_acceptance_config_guest import (
        _parse_labels,
        build,
    )
    from run_vm_runtime import QQRuntimeInstanceOwner, load_config, validate_config
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.deployment.activate_default_rulepack_guest import (
        _require_guest_context,
    )
    from scripts.deployment.build_visual_selection_acceptance_config_guest import (
        _parse_labels,
        build,
    )
    from scripts.run_vm_runtime import QQRuntimeInstanceOwner, load_config, validate_config


CANONICAL_CONFIG = Path(r"C:\PMAI\data\runtime-session-1.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPORT_SCHEMA = "pmai-visual-selection-promotion-v2"
Journal = Callable[[dict[str, Any]], None]


class PromotionFailure(RuntimeError):
    """A failed promotion with a privacy-safe, transaction-state report."""

    def __init__(self, error_code: str, report: dict[str, Any]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.report = report


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exclusive_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("exclusive write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_report(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    _exclusive_write(temporary, payload)
    os.replace(temporary, path)


def _validate_payload(payload: bytes, *, api_key: str) -> dict[str, Any]:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("visual promotion config must be an object")
    validate_config(value, api_key=api_key)
    return value


def _safe_error_code(exc: BaseException) -> str:
    value = str(exc)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", value):
        return value
    return "VISUAL_PROMOTION_FAILED"


def _failure_report(
    *,
    state: str,
    error_code: str,
    accepted_config_sha256: str,
    committed: bool,
    rolled_back: bool,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "state": state,
        "succeeded": False,
        "committed": committed,
        "rolled_back": rolled_back,
        "accepted_config_sha256": accepted_config_sha256,
        "error_code": error_code,
    }
    if evidence:
        report.update(evidence)
    return report


def _rollback(
    *,
    config_path: Path,
    candidate_path: Path,
    source: bytes,
    candidate: bytes,
) -> None:
    if config_path.read_bytes() != candidate:
        raise RuntimeError("ROLLBACK_CURRENT_CONFIG_MISMATCH")
    if candidate_path.exists():
        raise RuntimeError("ROLLBACK_CANDIDATE_ALREADY_EXISTS")
    _exclusive_write(candidate_path, source)
    if candidate_path.read_bytes() != source:
        raise RuntimeError("ROLLBACK_CANDIDATE_MISMATCH")
    if config_path.read_bytes() != candidate:
        raise RuntimeError("ROLLBACK_CURRENT_CONFIG_CHANGED")
    os.replace(candidate_path, config_path)
    if config_path.read_bytes() != source:
        raise RuntimeError("ROLLBACK_DID_NOT_PERSIST")


def promote(
    *, config_path: Path, backup_path: Path, candidate_path: Path,
    labels: dict[int, str], accepted_config_sha256: str,
    journal: Journal | None = None,
) -> dict[str, Any]:
    config_path = config_path.resolve()
    backup_path = backup_path.resolve()
    candidate_path = candidate_path.resolve()
    if SHA256.fullmatch(accepted_config_sha256) is None:
        raise ValueError("ACCEPTED_CONFIG_SHA256_INVALID")
    source = config_path.read_bytes()
    current = _validate_payload(
        source, api_key="offline-canonical-visual-promotion"
    )
    data_dir = Path(str(current["data_dir"])).resolve()
    if backup_path.parent != config_path.parent:
        raise ValueError("visual promotion backup must stay beside canonical config")
    if candidate_path.parent != data_dir:
        raise ValueError("visual promotion candidate must stay in runtime data directory")
    if candidate_path.drive.casefold() != config_path.drive.casefold():
        raise ValueError("visual promotion candidate must stay on canonical volume")
    if len({config_path, backup_path, candidate_path}) != 3:
        raise ValueError("visual promotion paths must be distinct")
    if backup_path.exists() or candidate_path.exists():
        raise RuntimeError("visual promotion output already exists")
    if "visual_selection" in current:
        raise RuntimeError("canonical visual selection is already configured")

    build(source_config=config_path, output_config=candidate_path, labels=labels)
    candidate = candidate_path.read_bytes()
    installed = _validate_payload(
        candidate, api_key="offline-canonical-visual-promotion"
    )
    before_sha256 = _digest(source)
    after_sha256 = _digest(candidate)
    if after_sha256 != accepted_config_sha256:
        raise RuntimeError("ACCEPTED_CONFIG_SHA256_MISMATCH")
    semantic_source = dict(installed)
    visual = semantic_source.pop("visual_selection", None)
    if semantic_source != current or not isinstance(visual, dict):
        raise RuntimeError("PROMOTION_SEMANTIC_DIFF_INVALID")
    _exclusive_write(backup_path, source)
    backup = backup_path.read_bytes()
    backup_sha256 = _digest(backup)
    if backup != source or backup_sha256 != before_sha256:
        raise RuntimeError("PROMOTION_BACKUP_MISMATCH")
    evidence = {
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "backup_sha256": backup_sha256,
        "binding_count": len(installed["bindings"]),
        "visual_model": visual["model"],
        "min_confidence": visual["min_confidence"],
        "semantic_change": "visual_selection_only",
    }
    if journal is not None:
        journal(
            _failure_report(
                state="backup_verified",
                error_code="PROMOTION_NOT_COMMITTED",
                accepted_config_sha256=accepted_config_sha256,
                committed=False,
                rolled_back=False,
                evidence=evidence,
            )
        )
    if config_path.read_bytes() != source:
        raise RuntimeError("PROMOTION_SOURCE_CHANGED")

    committed = False
    try:
        os.replace(candidate_path, config_path)
        committed = True
        if journal is not None:
            journal(
                _failure_report(
                    state="committed",
                    error_code="PROMOTION_POST_COMMIT_VALIDATION_PENDING",
                    accepted_config_sha256=accepted_config_sha256,
                    committed=True,
                    rolled_back=False,
                    evidence=evidence,
                )
            )
        persisted = config_path.read_bytes()
        if persisted != candidate:
            raise RuntimeError("PROMOTION_CANONICAL_MISMATCH")
        verified = _validate_payload(
            persisted, api_key="offline-canonical-visual-promotion"
        )
        if verified != installed:
            raise RuntimeError("PROMOTION_CANONICAL_VALIDATION_MISMATCH")
        result = {
            "schema": REPORT_SCHEMA,
            "state": "succeeded",
            "succeeded": True,
            "committed": True,
            "rolled_back": False,
            "accepted_config_sha256": accepted_config_sha256,
            **evidence,
        }
        if journal is not None:
            journal(result)
        return result
    except BaseException as exc:
        if not committed:
            raise
        try:
            _rollback(
                config_path=config_path,
                candidate_path=candidate_path,
                source=source,
                candidate=candidate,
            )
            failure = _failure_report(
                state="rolled_back",
                error_code=_safe_error_code(exc),
                accepted_config_sha256=accepted_config_sha256,
                committed=True,
                rolled_back=True,
                evidence=evidence,
            )
            if journal is not None:
                journal(failure)
            raise PromotionFailure(failure["error_code"], failure) from exc
        except PromotionFailure:
            raise
        except BaseException as rollback_exc:
            failure = _failure_report(
                state="rollback_failed",
                error_code=_safe_error_code(rollback_exc),
                accepted_config_sha256=accepted_config_sha256,
                committed=True,
                rolled_back=False,
                evidence=evidence,
            )
            if journal is not None:
                try:
                    journal(failure)
                except BaseException:
                    pass
            raise PromotionFailure(
                "PROMOTION_ROLLBACK_FAILED", failure
            ) from rollback_exc

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--accepted-config-sha256", required=True)
    parser.add_argument("--visual-label", action="append", default=[])
    args = parser.parse_args(argv)
    if args.config.resolve() != CANONICAL_CONFIG.resolve():
        parser.error("promotion target must be the canonical runtime config")
    owner = QQRuntimeInstanceOwner()
    owner.acquire()
    transaction: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "state": "preparing",
        "succeeded": False,
        "committed": False,
        "rolled_back": False,
        "accepted_config_sha256": args.accepted_config_sha256,
        "error_code": "PROMOTION_NOT_COMMITTED",
    }

    def record(value: dict[str, Any]) -> None:
        nonlocal transaction
        transaction = dict(value)
        _atomic_report(args.report, transaction)

    try:
        _require_guest_context()
        record(transaction)
        try:
            promote(
                config_path=args.config,
                backup_path=args.backup,
                candidate_path=args.candidate,
                labels=_parse_labels(args.visual_label),
                accepted_config_sha256=args.accepted_config_sha256,
                journal=record,
            )
            return 0
        except PromotionFailure:
            return 2
        except BaseException as exc:
            failure = _failure_report(
                state="precommit_failed",
                error_code=_safe_error_code(exc),
                accepted_config_sha256=args.accepted_config_sha256,
                committed=False,
                rolled_back=False,
            )
            try:
                record(failure)
            except BaseException:
                pass
            return 2
    finally:
        owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
