"""Bounded QQ one-shot reply entry point for a frozen guest release.

This entry point never starts the long-running runtime loop.  It targets one
explicit direct binding, durably claims one source batch, permits at most one
reply segment, and requires COMMIT and VERIFY to occur in different workers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from messenger_ai.observability import WindowsDPAPISecretStore
from messenger_ai.runtime.one_shot import OneShotAttemptLedger, run_one_shot_reply

try:
    from run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _shutdown,
        build_runtime,
        load_config,
        validate_config,
    )
except ModuleNotFoundError:  # repository-root test/import path
    from scripts.run_vm_runtime import (
        QQRuntimeInstanceOwner,
        _shutdown,
        build_runtime,
        load_config,
        validate_config,
    )


REPORT_SCHEMA = "pmai-qq-one-shot-reply-v1"
INTENT_SCHEMA = "pmai-qq-one-shot-attempt-v1"
_BINDING_ID = re.compile(r"session-contact-[1-9][0-9]{0,3}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{1,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("ONE_SHOT_REPORT_WRITE_FAILED")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _safe_code(value: object, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return fallback


def _config_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_intent(
    path: Path, *, attempt_id: str, binding_id: str, config_sha256: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("ONE_SHOT_ATTEMPT_REPLAY") from exc
    payload = json.dumps(
        {
            "schema": INTENT_SCHEMA,
            "attempt_id": attempt_id,
            "binding_id": binding_id,
            "config_sha256": config_sha256,
            "created_at": _now(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("ONE_SHOT_INTENT_WRITE_FAILED")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_guest() -> None:
    try:
        from activate_default_rulepack_guest import _require_guest_context
    except ModuleNotFoundError:  # repository-root test/import path
        from scripts.deployment.activate_default_rulepack_guest import (
            _require_guest_context,
        )

    _require_guest_context()


def _safe_positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _safe_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value)) if str(UUID(value)) == value else None
    except ValueError:
        return None


def _project_handoff(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    clean: dict[str, object] = {}
    operation_id = _safe_uuid(value.get("operation_id"))
    if operation_id is not None:
        clean["operation_id"] = operation_id
    for key in ("commit_worker_process_id", "verify_worker_process_id"):
        item = _safe_positive_int(value.get(key))
        if item is not None:
            clean[key] = item
    for key in (
        "commit_worker_epoch",
        "verify_health_worker_epoch",
        "verify_worker_epoch",
    ):
        item = _safe_uuid(value.get(key))
        if item is not None:
            clean[key] = item
    if isinstance(value.get("first_worker_retired"), bool):
        clean["first_worker_retired"] = value["first_worker_retired"]
    if value.get("verify_status") in {"ok", "failed_safe", "uncertain", "unavailable"}:
        clean["verify_status"] = value["verify_status"]
    return clean or None


def _worker_retirement(worker: object | None) -> tuple[bool, int | None]:
    snapshot = getattr(worker, "status_snapshot", None)
    if not callable(snapshot):
        return False, None
    try:
        value = snapshot()
    except Exception:  # noqa: BLE001 - shutdown witness must fail closed
        return False, None
    if not isinstance(value, dict):
        return False, None
    exit_code = value.get("worker_exit_code")
    valid_exit = (
        exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else None
    )
    return value.get("worker_alive") is False and valid_exit is not None, valid_exit


def _single_binding_config(
    config: dict[str, Any], *, binding_id: str
) -> dict[str, Any]:
    """Create an ephemeral execution config containing exactly one binding."""

    scoped = json.loads(json.dumps(config, ensure_ascii=False))
    bindings = scoped.get("bindings")
    if not isinstance(bindings, list):
        raise TypeError("ONE_SHOT_BINDINGS_INVALID")
    selected = [
        item
        for item in bindings
        if isinstance(item, dict) and item.get("binding_id") == binding_id
    ]
    if len(selected) != 1:
        raise RuntimeError("ONE_SHOT_BINDING_NOT_FOUND")
    contact_id = selected[0].get("contact_id")
    conversation_id = selected[0].get("hub_conversation_id")
    if not isinstance(contact_id, str) or not isinstance(conversation_id, str):
        raise TypeError("ONE_SHOT_BINDING_INVALID")
    contacts = scoped.get("contacts")
    if not isinstance(contacts, list):
        raise TypeError("ONE_SHOT_CONTACTS_INVALID")
    selected_contacts = [
        item
        for item in contacts
        if isinstance(item, dict) and item.get("contact_id") == contact_id
    ]
    if len(selected_contacts) != 1:
        raise RuntimeError("ONE_SHOT_CONTACT_NOT_UNIQUE")
    scoped["contacts"] = selected_contacts
    scoped["bindings"] = selected

    if scoped.get("identity_mode") == "session_observed_direct":
        evidence = scoped.get("session_observed_evidence")
        if not isinstance(evidence, list):
            raise RuntimeError("ONE_SHOT_SESSION_EVIDENCE_INVALID")
        selected_evidence = [
            item
            for item in evidence
            if isinstance(item, dict) and item.get("binding_id") == binding_id
        ]
        if len(selected_evidence) != 1:
            raise RuntimeError("ONE_SHOT_SESSION_EVIDENCE_NOT_UNIQUE")
        scoped["session_observed_evidence"] = selected_evidence

    visual = scoped.get("visual_selection")
    if visual is not None:
        if not isinstance(visual, dict) or not isinstance(visual.get("labels"), dict):
            raise RuntimeError("ONE_SHOT_VISUAL_SELECTION_INVALID")
        if binding_id not in visual["labels"]:
            raise RuntimeError("ONE_SHOT_VISUAL_LABEL_MISSING")
        visual["labels"] = {binding_id: visual["labels"][binding_id]}

    migrations = scoped.get("session_identity_migrations", [])
    if not isinstance(migrations, list):
        raise TypeError("ONE_SHOT_MIGRATIONS_INVALID")
    scoped["session_identity_migrations"] = [
        item
        for item in migrations
        if isinstance(item, dict) and item.get("binding_id") == binding_id
    ]
    bootstrap = scoped.get("bootstrap_last_inbound_once", [])
    if not isinstance(bootstrap, list):
        raise TypeError("ONE_SHOT_BOOTSTRAP_INVALID")
    scoped["bootstrap_last_inbound_once"] = [
        item for item in bootstrap if item == conversation_id
    ]
    provenance = scoped.get("bootstrap_last_inbound_provenance", {})
    if not isinstance(provenance, dict):
        raise TypeError("ONE_SHOT_BOOTSTRAP_PROVENANCE_INVALID")
    scoped["bootstrap_last_inbound_provenance"] = {
        key: value for key, value in provenance.items() if key == conversation_id
    }
    return scoped


def execute(
    *,
    config_path: Path,
    binding_id: str,
    attempt_id: str,
    report_path: Path,
    max_wait_seconds: float,
) -> dict[str, object]:
    # Keep async transports on their owning loop through failure cleanup too.
    with asyncio.Runner() as runner:
        return _execute_with_runner(
            config_path=config_path,
            binding_id=binding_id,
            attempt_id=attempt_id,
            report_path=report_path,
            max_wait_seconds=max_wait_seconds,
            runner=runner,
        )


def _execute_with_runner(
    *,
    config_path: Path,
    binding_id: str,
    attempt_id: str,
    report_path: Path,
    max_wait_seconds: float,
    runner: asyncio.Runner,
) -> dict[str, object]:
    canonical_attempt = str(UUID(attempt_id))
    if _BINDING_ID.fullmatch(binding_id) is None:
        raise RuntimeError("ONE_SHOT_BINDING_ID_INVALID")
    if not 1 <= max_wait_seconds <= 300:
        raise RuntimeError("ONE_SHOT_WAIT_INVALID")

    owner = QQRuntimeInstanceOwner()
    owner.acquire()
    app = None
    active_worker = None
    result = None
    run_started = False
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "attempt_id": canonical_attempt,
        "binding_id": binding_id,
        "status": "initializing",
        "succeeded": False,
        "provider_called": False,
        "action_attempted": False,
        "send_action_attempted": False,
        "selection_action_attempted": None,
        "intent_recorded": False,
        "active_worker_retired": False,
        "started_at": _now(),
    }
    try:
        _atomic_json(report_path, report)
        _require_guest()
        os.environ["PERSONAL_MESSENGER_VM_GUEST"] = "1"
        config = load_config(config_path)
        secrets = WindowsDPAPISecretStore(Path(str(config["secret_vault"])))
        api_key = secrets.get_secret("deepseek.api_key").decode("utf-8")
        _pack, bindings, _evidence = validate_config(config, api_key=api_key)
        binding = next((item for item in bindings if item.binding_id == binding_id), None)
        if binding is None:
            raise RuntimeError("ONE_SHOT_BINDING_NOT_FOUND")
        if binding.conversation_type != "direct":
            raise RuntimeError("ONE_SHOT_BINDING_NOT_DIRECT")
        digest = _config_digest(config)
        report["config_sha256"] = digest
        execution_config = _single_binding_config(config, binding_id=binding_id)
        _scoped_pack, scoped_bindings, _scoped_evidence = validate_config(
            execution_config, api_key=api_key
        )
        if len(scoped_bindings) != 1 or scoped_bindings[0].binding_id != binding_id:
            raise RuntimeError("ONE_SHOT_EXECUTION_SCOPE_INVALID")
        binding = scoped_bindings[0]
        report["execution_config_sha256"] = _config_digest(execution_config)
        data_dir = Path(str(config["data_dir"]))
        _record_intent(
            data_dir / "one-shot-intents" / f"{canonical_attempt}.json",
            attempt_id=canonical_attempt,
            binding_id=binding_id,
            config_sha256=digest,
        )
        report["intent_recorded"] = True
        report["status"] = "runtime_initializing"
        _atomic_json(report_path, report)

        app = build_runtime(
            execution_config,
            api_key=api_key,
            authorization_signing_key=secrets.get_or_create_hmac_key(
                "runtime.authorization.signing"
            ),
            run_id=canonical_attempt,
            selection_refresh_retry_enabled=False,
            recover_persistent_state=False,
        )
        ledger = OneShotAttemptLedger(data_dir / "one-shot-attempts.sqlite3")
        report["status"] = "running_once"
        _atomic_json(report_path, report)
        run_started = True
        try:
            stopped_scope = getattr(
                app.state, "one_shot_stopped_runtime_scope", None
            )
            if not callable(stopped_scope):
                raise TypeError("ONE_SHOT_STOP_SCOPE_UNAVAILABLE")
            with stopped_scope(
                attempt_id=UUID(canonical_attempt),
                conversation_id=binding.hub_conversation_id,
            ):
                result = runner.run(
                    run_one_shot_reply(
                        app=app,
                        ledger=ledger,
                        binding=binding,
                        attempt_id=UUID(canonical_attempt),
                        max_wait_seconds=max_wait_seconds,
                    )
                )
        finally:
            ledger.close()

        report["status"] = result.state
        report["provider_called"] = result.provider_called
        report["action_attempted"] = result.action_attempted
        report["send_action_attempted"] = result.send_action_attempted
        report["selection_action_attempted"] = (
            result.selection_action_attempted
        )
        report["source_key_hashes"] = list(result.source_key_hashes)
        for key, value in (
            ("pacing_plan_id", result.pacing_plan_id),
            ("operation_id", result.operation_id),
        ):
            if value is not None:
                report[key] = str(value)
        if result.send_status is not None:
            report["send_status"] = result.send_status
        if result.error_code is not None:
            report["error_code"] = _safe_code(
                result.error_code, "ONE_SHOT_RESULT_ERROR"
            )
        handoff = _project_handoff(result.handoff)
        if handoff is not None:
            report["handoff"] = handoff
    except Exception as exc:  # noqa: BLE001 - report uncertainty without content
        report["status"] = "uncertain" if run_started else "failed"
        report["succeeded"] = False
        report["action_attempted"] = None if run_started else False
        report["send_action_attempted"] = None if run_started else False
        report["selection_action_attempted"] = None
        report["error_code"] = _safe_code(str(exc), "ONE_SHOT_GUEST_FAILED")
    finally:
        if app is not None:
            active_worker = getattr(getattr(app, "driver", None), "_worker", None)
            try:
                _shutdown(app, runner=runner)
            except Exception as exc:  # noqa: BLE001 - action state becomes uncertain
                # Keep the report schema stable; retain the original bounded
                # outcome in a separate diagnostic, never raw exception text.
                try:
                    print(json.dumps({
                        "schema": "pmai-one-shot-shutdown-failure-v1",
                        "attempt_id": canonical_attempt,
                        "result_status": report["status"],
                        "result_error_code": report.get("error_code"),
                        "exception_type": type(exc).__name__,
                    }, sort_keys=True), flush=True)
                except (OSError, ValueError):
                    # Unavailable stdout must not suppress the durable report.
                    pass
                report["status"] = "uncertain"
                report["succeeded"] = False
                report["action_attempted"] = None if run_started else False
                report["send_action_attempted"] = None if run_started else False
                report["selection_action_attempted"] = None
                report["error_code"] = "ONE_SHOT_RUNTIME_SHUTDOWN_FAILED"
        retired, exit_code = _worker_retirement(active_worker)
        report["active_worker_retired"] = retired
        if exit_code is not None:
            report["active_worker_exit_code"] = exit_code
        if (
            result is not None and result.state == "verified"
            and report["status"] == "verified"
        ):
            if retired:
                report["succeeded"] = True
            else:
                report["status"] = "uncertain"
                report["succeeded"] = False
                report["error_code"] = "ONE_SHOT_VERIFY_WORKER_NOT_RETIRED"
        owner.close()
        report["completed_at"] = _now()
        _atomic_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-wait-seconds", type=float, default=90)
    args = parser.parse_args(argv)
    try:
        report = execute(
            config_path=args.config,
            binding_id=args.binding_id,
            attempt_id=args.attempt_id,
            report_path=args.report,
            max_wait_seconds=args.max_wait_seconds,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"blocked: {_safe_code(str(exc), 'ONE_SHOT_STARTUP_FAILED')}")
        return 2
    if report["succeeded"] is True:
        return 0
    if report["status"] == "no_new_inbound":
        return 4
    return 3 if report["status"] == "uncertain" else 2


if __name__ == "__main__":
    raise SystemExit(main())
