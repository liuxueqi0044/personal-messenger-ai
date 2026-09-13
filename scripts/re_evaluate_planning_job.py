"""Inspect or prepare one explicit offline planner re-evaluation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Callable

try:
    from run_vm_runtime import QQRuntimeInstanceOwner
except ModuleNotFoundError:  # pragma: no cover - package-style test import
    from scripts.run_vm_runtime import QQRuntimeInstanceOwner

from messenger_ai.runtime.reevaluation import (
    inspect_operator_reevaluation,
    prepare_operator_reevaluation,
)
from messenger_ai.runtime.state import RuntimeState


def _existing_data_dir(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    required = (
        path / "runtime.sqlite3",
        path / "hub.sqlite3",
        path / "qq-vm-bridge.sqlite3",
    )
    if not path.is_dir() or any(not item.is_file() for item in required):
        raise argparse.ArgumentTypeError("data directory is missing runtime authorities")
    return path


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _connections(data_dir: Path) -> tuple[sqlite3.Connection, sqlite3.Connection, sqlite3.Connection]:
    return (
        _read_only(data_dir / "runtime.sqlite3"),
        _read_only(data_dir / "hub.sqlite3"),
        _read_only(data_dir / "qq-vm-bridge.sqlite3"),
    )


def execute_inspect(
    args: argparse.Namespace,
    *,
    owner_factory: Callable[[], QQRuntimeInstanceOwner] = QQRuntimeInstanceOwner,
) -> dict[str, object]:
    owner = owner_factory()
    owner.acquire()
    runtime = hub = bridge = None
    try:
        runtime, hub, bridge = _connections(args.data_dir)
        return inspect_operator_reevaluation(
            runtime=runtime,
            hub=hub,
            bridge=bridge,
            original_request_id=args.original_request_id,
        )
    finally:
        for connection in (bridge, hub, runtime):
            if connection is not None:
                connection.close()
        owner.close()


def execute_prepare(
    args: argparse.Namespace,
    *,
    owner_factory: Callable[[], QQRuntimeInstanceOwner] = QQRuntimeInstanceOwner,
) -> dict[str, object]:
    owner = owner_factory()
    owner.acquire()
    state = None
    hub = bridge = None
    try:
        state = RuntimeState(args.data_dir / "runtime.sqlite3")
        hub = _read_only(args.data_dir / "hub.sqlite3")
        bridge = _read_only(args.data_dir / "qq-vm-bridge.sqlite3")
        result = prepare_operator_reevaluation(
            state=state,
            hub=hub,
            bridge=bridge,
            reevaluation_id=args.reevaluation_id,
            original_request_id=args.original_request_id,
            expected_conversation_id=args.conversation_id,
            expected_binding_revision=args.expected_binding_revision,
            expected_conversation_revision=args.expected_conversation_revision,
            expected_current_global_revision=args.expected_current_global_revision,
            expected_source_keys_sha256=args.expected_source_keys_sha256,
            operator_id=args.operator_id,
            reason_code=args.reason_code,
        )
        return {
            "schema": "pmai-planning-reevaluation-prepare-result-v1",
            **result.__dict__,
        }
    finally:
        if bridge is not None:
            bridge.close()
        if hub is not None:
            hub.close()
        if state is not None:
            state.close()
        owner.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--data-dir", type=_existing_data_dir, required=True)
    inspect.add_argument("--original-request-id", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--data-dir", type=_existing_data_dir, required=True)
    prepare.add_argument("--original-request-id", required=True)
    prepare.add_argument("--reevaluation-id", required=True)
    prepare.add_argument("--conversation-id", required=True)
    prepare.add_argument("--expected-binding-revision", type=int, required=True)
    prepare.add_argument("--expected-conversation-revision", type=int, required=True)
    prepare.add_argument("--expected-current-global-revision", type=int, required=True)
    prepare.add_argument("--expected-source-keys-sha256", required=True)
    prepare.add_argument("--operator-id", required=True)
    prepare.add_argument("--reason-code", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = execute_inspect(args) if args.command == "inspect" else execute_prepare(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **output}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
