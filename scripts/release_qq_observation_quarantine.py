from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Callable
from uuid import UUID

from messenger_ai.adapters.qq.vm_driver.quarantine_release import (
    release_observation_quarantine,
)
try:
    from run_vm_runtime import QQRuntimeInstanceOwner
except ModuleNotFoundError:  # imported as a module from the repository root
    from scripts.run_vm_runtime import QQRuntimeInstanceOwner


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REASON = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,79}")


def _id(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid identifier")
    return value


def _reason(value: str) -> str:
    if _REASON.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid reason code")
    return value


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid UUID") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Release one exact read-only QQ observation quarantine.",
    )
    result.add_argument("--sqlite-path", type=Path, required=True)
    result.add_argument("--expected-run-id", type=_uuid, required=True)
    result.add_argument("--conversation-id", type=_id, required=True)
    result.add_argument("--binding-id", type=_id, required=True)
    result.add_argument("--binding-revision", type=_positive, required=True)
    result.add_argument("--request-id", type=_uuid, required=True)
    result.add_argument("--failed-generation", type=_positive, required=True)
    result.add_argument("--operator-id", type=_id, required=True)
    result.add_argument("--reason-code", type=_reason, required=True)
    return result


def execute_release(
    args: argparse.Namespace,
    *,
    owner_factory: Callable[[], QQRuntimeInstanceOwner] = QQRuntimeInstanceOwner,
) -> dict[str, object]:
    owner = owner_factory()
    owner.acquire()
    try:
        return release_observation_quarantine(
            args.sqlite_path,
            expected_run_id=args.expected_run_id,
            conversation_id=args.conversation_id,
            binding_id=args.binding_id,
            binding_revision=args.binding_revision,
            request_id=args.request_id,
            failed_generation=args.failed_generation,
            operator_id=args.operator_id,
            reason_code=args.reason_code,
        )
    finally:
        owner.close()


def main() -> int:
    args = parser().parse_args()
    try:
        result = execute_release(args)
    except ValueError as exc:
        print(json.dumps({
            "schema": "pmai-qq-observation-quarantine-release-result-v1",
            "status": "rejected",
            "error_code": str(exc),
        }))
        return 2
    except Exception as exc:
        print(json.dumps({
            "schema": "pmai-qq-observation-quarantine-release-result-v1",
            "status": "failed",
            "error_code": type(exc).__name__,
        }))
        return 2
    print(json.dumps({
        "schema": "pmai-qq-observation-quarantine-release-result-v1",
        **result,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
