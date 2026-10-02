"""Start one runtime and report Web reachability separately from QQ health.

`web_reachable` records the spawned PID's uniquely assigned local WebUI port.
`ready` additionally requires fresh, same-run successful QQ observation
evidence. HTTP 200 alone never establishes driver readiness.
"""
from __future__ import annotations

import argparse
import http.client
import hashlib
import json
import os
import sqlite3
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from contextlib import closing
from messenger_ai.runtime.config_publication import assert_publication_complete
from messenger_ai.runtime.session_revision import (
    session_revision_number,
    validate_session_revision,
)


OUTPUT = Path(r"C:\PMAI\data\qq-session-runtime-status.json")
CONFIG = Path(r"C:\PMAI\data\runtime-session-1.json")
CONTROL_RESULT = Path(r"C:\PMAI\data\qq-session-runtime-control-result.json")
WORKER_WITNESS = Path(r"C:\PMAI\data\qq-session-runtime-worker-status.json")
WORKER_WITNESS_SCHEMA = "pmai-qq-runtime-worker-status-v1"
MAX_WITNESS_WRITE_AGE_SECONDS = 10
MIN_OBSERVE_FRESHNESS_SECONDS = 30.0
MAX_OBSERVE_FRESHNESS_SECONDS = 600.0
HYBRID_WITNESS_SCHEMA = "qq_hybrid_runtime_status_v2"
HYBRID_OBSERVE_FRESHNESS_SECONDS = 90.0
_TRANSIENT_WINDOWS_FILE_ERRORS = frozenset({5, 32, 33})
_PUBLISH_RETRY_DELAYS = (0.05, 0.10, 0.15)
SUPERVISOR_EVENT_LOG = Path(r"C:\PMAI\data\logs\qq-session-runtime-supervisor.stderr.log")
LOG_DIR = Path(r"C:\PMAI\data\logs")


def _runtime_config_snapshot() -> tuple[dict[str, object], Path, str]:
    assert_publication_complete(CONFIG)
    canonical_bytes = CONFIG.read_bytes()
    value = json.loads(canonical_bytes.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime config must be an object")
    runtime_config = CONFIG
    session_revision = validate_session_revision(value)
    if session_revision is not None:
        runtime_config = session_revision.snapshot_path
        if runtime_config.read_bytes() != canonical_bytes:
            raise ValueError("canonical config does not match isolated session revision snapshot")
    elif value.get("runtime_generation") is not None:
        data_dir = value.get("data_dir")
        if not isinstance(data_dir, str) or not data_dir:
            raise ValueError("isolated runtime config has no data directory")
        runtime_config = Path(data_dir) / "runtime-config.json"
        generation_bytes = runtime_config.read_bytes()
        if generation_bytes != canonical_bytes:
            raise ValueError("canonical config does not match isolated generation snapshot")
    return value, runtime_config, hashlib.sha256(canonical_bytes).hexdigest()


def _validate_requested_snapshot(
    config: dict[str, object],
    config_sha256: str,
    *,
    expected_config_sha256: str | None,
    expected_generation_id: str | None,
    expected_session_binding_revision: int | None = None,
) -> None:
    if (
        expected_session_binding_revision is not None
        and session_revision_number(config) != expected_session_binding_revision
    ):
        raise ValueError("session binding revision does not match requested build")
    if (
        expected_config_sha256 is not None
        and config_sha256 != expected_config_sha256.casefold()
    ):
        raise ValueError("runtime config does not match requested build")
    if expected_generation_id is not None:
        generation = config.get("runtime_generation")
        if (
            not isinstance(generation, dict)
            or generation.get("generation_id") != expected_generation_id
        ):
            raise ValueError("runtime generation does not match requested build")


def _hybrid_settings_snapshot(path: str, *, expected_sha256: str | None,
                              active_binding_ids: list[str], config: dict) -> tuple[Path, str, tuple[dict, ...]]:
    """Freeze launch bytes and existing business scope; never enroll identity."""
    settings_path = Path(path)
    if not settings_path.is_absolute() or not settings_path.is_file() or settings_path.is_symlink():
        raise ValueError("hybrid settings must be an absolute regular snapshot")
    if (not active_binding_ids or len(set(active_binding_ids)) != len(active_binding_ids)
            or any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in active_binding_ids)):
        raise ValueError("explicit unique hybrid binding IDs are required")
    with settings_path.open("rb") as stream:
        payload = stream.read(262145)
    if not payload or len(payload) > 262144:
        raise ValueError("hybrid settings snapshot is not bounded")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and (
            len(expected_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
            or digest != expected_sha256.casefold()):
        raise ValueError("hybrid settings do not match requested build")
    settings = json.loads(payload.decode("utf-8"))
    if (not isinstance(settings, dict) or settings.get("schema_version") != "qq_hybrid_runtime_v2"
            or settings.get("enabled") is not True or not isinstance(settings.get("inputs"), list)):
        raise ValueError("explicit hybrid settings are required")
    bindings = config.get("bindings")
    if bindings is None:
        contacts = config.get("contacts")
        if not isinstance(contacts, list) or any(not isinstance(item, dict) for item in contacts):
            raise ValueError("hybrid business registry is unavailable")
        bindings = [item.get("binding") for item in contacts]
    if not isinstance(bindings, list) or any(not isinstance(item, dict) for item in bindings):
        raise ValueError("hybrid business registry is unavailable")
    registered = {item.get("binding_id"): item for item in bindings}
    if (len(registered) != len(bindings) or any(key not in registered for key in active_binding_ids)
            or any(not isinstance(registered[key].get(field), str) or not registered[key][field]
                   for key in active_binding_ids for field in (
                       "account_id", "contact_id", "hub_conversation_id"))):
        raise ValueError("hybrid binding is not registered")
    targets = {}
    for item in settings["inputs"]:
        if not isinstance(item, dict) or not isinstance(item.get("target"), dict):
            raise ValueError("hybrid target scope is unavailable")
        target = item["target"]
        key = target.get("binding_id")
        binding = registered.get(key)
        if (key in targets or key not in active_binding_ids or binding is None
                or target.get("account_id") != binding.get("account_id")
                or target.get("conversation_id") != binding.get("hub_conversation_id")
                or item.get("contact_id") != binding.get("contact_id")
                or target.get("identity_mode") != "persistent"
                or type(target.get("binding_revision")) is not int or target["binding_revision"] < 1
                or any(not isinstance(target.get(field), str) or not target[field]
                       for field in ("account_id", "conversation_id", "binding_id"))):
            raise ValueError("hybrid target disagrees with existing business scope")
        targets[key] = {field: target[field] for field in (
            "account_id", "conversation_id", "binding_id", "binding_revision")}
        targets[key]["contact_id"] = item["contact_id"]
    if set(targets) != set(active_binding_ids) or len({item["conversation_id"] for item in targets.values()}) != len(targets):
        raise ValueError("hybrid active target scope is incomplete")
    return settings_path, digest, tuple(targets[key] for key in active_binding_ids)


def _publish_event(*, run_id: str, event: str, count: int,
                   exc: BaseException | None = None,
                   exit_code: int | None = None) -> None:
    """Persist bounded supervisor diagnostics without exception text or paths."""

    payload: dict[str, object] = {
        "schema": "pmai-qq-runtime-supervisor-event-v1",
        "run_id": run_id,
        "component": "status_publish",
        "event": event,
        "recorded_at": datetime.now(UTC).isoformat(),
        "count": count,
    }
    if exc is not None:
        payload["exception_type"] = type(exc).__name__
        winerror = getattr(exc, "winerror", None)
        if isinstance(winerror, int) and not isinstance(winerror, bool):
            payload["winerror"] = winerror
    if exit_code is not None:
        payload["exit_code"] = exit_code
    try:
        SUPERVISOR_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SUPERVISOR_EVENT_LOG.open("a", encoding="utf-8") as log:
            log.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        # Keep a safe fallback for direct launches while never changing the
        # child lifecycle when either persistent sink is unavailable.
        try:
            sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
            sys.stderr.flush()
        except OSError:
            pass


class _PublishTracker:
    def __init__(self) -> None:
        self.failures = 0


def _publish_status(value: dict[str, object], tracker: _PublishTracker) -> bool:
    """Publish status with bounded Windows-share-lock retries.

    After a child is running, callers keep supervising it when this returns
    false.  A later loop iteration retries publication; this helper never
    restarts or terminates the child.
    """

    run_id = str(value.get("run_id", "unknown"))
    temporary = OUTPUT.with_suffix(".tmp.json")
    last_error: BaseException | None = None
    for attempt in range(len(_PUBLISH_RETRY_DELAYS) + 1):
        try:
            encoded = json.dumps(value, sort_keys=True)
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, OUTPUT)
            if tracker.failures:
                _publish_event(run_id=run_id, event="publish_recovered", count=tracker.failures)
                tracker.failures = 0
            return True
        except OSError as exc:
            last_error = exc
            transient = os.name == "nt" and getattr(exc, "winerror", None) in _TRANSIENT_WINDOWS_FILE_ERRORS
            if not transient or attempt == len(_PUBLISH_RETRY_DELAYS):
                break
            time.sleep(_PUBLISH_RETRY_DELAYS[attempt])
        except Exception as exc:
            last_error = exc
            break
    tracker.failures += 1
    # First failure and then every 30th failure are durable, bounded evidence.
    if tracker.failures == 1 or tracker.failures % 30 == 0:
        _publish_event(run_id=run_id, event="publish_failed", count=tracker.failures, exc=last_error)
    return False


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_observation(port: int) -> dict[str, object] | None:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        connection.request("GET", "/inbox")
        response = connection.getresponse()
        status = int(response.status)
        response.read()
        connection.close()
        if 200 <= status < 400:
            return {"path": "/inbox", "status_code": status,
                    "observed_at": datetime.now(UTC).isoformat()}
    except (OSError, http.client.HTTPException):
        return None
    return None


def _control_result(run_id: str) -> dict[str, object] | None:
    """Read the runtime's public acknowledgement only when it belongs to this run."""
    try:
        value = json.loads(CONTROL_RESULT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema") != "pmai-qq-runtime-control-result-v1":
        return None
    if value.get("target_run_id") != run_id:
        return None
    return value


def _worker_witness(run_id: str) -> tuple[dict[str, object] | None, list[str]]:
    """Accept only a fresh, same-run witness; it is metadata, never payload."""
    try:
        value = json.loads(WORKER_WITNESS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["WORKER_WITNESS_MISSING"]
    if not isinstance(value, dict) or value.get("schema") != WORKER_WITNESS_SCHEMA:
        return None, ["WORKER_WITNESS_SCHEMA_MISMATCH"]
    if value.get("run_id") != run_id:
        return None, ["WORKER_WITNESS_RUN_MISMATCH"]
    try:
        written_at = datetime.fromisoformat(str(value["written_at"]).replace("Z", "+00:00"))
        observe_freshness = float(value["observe_freshness_seconds"])
    except (KeyError, TypeError, ValueError):
        return None, ["WORKER_WITNESS_INVALID"]
    if (written_at.tzinfo is None or not MIN_OBSERVE_FRESHNESS_SECONDS <= observe_freshness
            <= MAX_OBSERVE_FRESHNESS_SECONDS):
        return None, ["WORKER_WITNESS_INVALID"]
    age = (datetime.now(UTC) - written_at.astimezone(UTC)).total_seconds()
    if age < 0 or age > MAX_WITNESS_WRITE_AGE_SECONDS:
        return None, ["WORKER_WITNESS_STALE"]
    return value, []


def _hybrid_witness(data: Path, run_id: str, *, started_at: str,
                    active_targets: tuple[dict, ...]) -> tuple[dict | None, list[str]]:
    try:
        path = data / "qq-hybrid-runtime-status.json"
        with path.open("rb") as stream:
            payload = stream.read(32769)
        if len(payload) > 32768:
            raise ValueError()
        value = json.loads(payload)
    except (OSError, ValueError, UnicodeDecodeError):
        return None, ["HYBRID_WITNESS_MISSING"]
    if not isinstance(value, dict) or value.get("schema") != HYBRID_WITNESS_SCHEMA:
        return None, ["HYBRID_WITNESS_SCHEMA_MISMATCH"]
    if value.get("run_id") != run_id:
        return None, ["HYBRID_WITNESS_RUN_MISMATCH"]
    try:
        written = datetime.fromisoformat(value["written_at"].replace("Z", "+00:00"))
        since = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        session = value["session"]
        active = value["active_conversations"]
        if (written.tzinfo is None or since.tzinfo is None or written < since
                or type(value["global_revision"]) is not int or value["global_revision"] < 1
                or type(value["paused"]) is not bool or not isinstance(session, dict)
                or session.get("schema") != "qq_hybrid_session_v2"
                or session.get("state") not in {"idle", "starting", "active", "closing", "cleanup_required", "closed"}
                or type(session.get("cleanup_required")) is not bool
                or not isinstance(active, list) or any(not isinstance(item, str) for item in active)
                or len(active) != len(set(active))
                or set(active) != {target["conversation_id"] for target in active_targets}):
            raise ValueError()
        if session["state"] in {"starting", "active", "closing", "cleanup_required"}:
            if (session.get("purpose") not in {"observe", "draft", "verify", "health"}
                    or uuid.UUID(session.get("worker_epoch")).int == 0):
                raise ValueError()
        if not 0 <= (now-written).total_seconds() <= MAX_WITNESS_WRITE_AGE_SECONDS:
            return None, ["HYBRID_WITNESS_STALE"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, ["HYBRID_WITNESS_INVALID"]
    return value, []


def _hybrid_driver_status(metrics, witness, witness_reasons) -> tuple[str, list[str]]:
    """Finite child idleness is valid; only fresh actual observations give ready."""
    if ((metrics is not None and metrics.get("global_paused") is True)
            or (witness is not None and witness.get("paused") is True)):
        return "paused", ["GLOBAL_PAUSED"]
    if witness_reasons:
        return "unknown", list(witness_reasons)
    if metrics is None or witness is None:
        return "unknown", ["HYBRID_READINESS_EVIDENCE_MISSING"]
    session = witness["session"]
    if session.get("cleanup_required") is True or session.get("state") == "cleanup_required":
        return "degraded", ["WORKER_CLEANUP_REQUIRED"]
    if session.get("state") == "closed":
        return "degraded", ["SESSION_CLOSED"]
    if (type(metrics.get("global_revision")) is not int
            or metrics["global_revision"] != witness["global_revision"]):
        return "unknown", ["GLOBAL_CONTROL_WITNESS_MISMATCH"]
    if metrics.get("active_identity_current") is not True:
        return "degraded", ["ACTIVE_BINDING_CHANGED"]
    pauses = metrics.get("active_conversation_pause_reason_counts")
    if (not isinstance(pauses, dict) or any(type(count) is not int or count < 0 for count in pauses.values())):
        return "unknown", ["ACTIVE_METRICS_INVALID"]
    if sum(count for reason, count in pauses.items() if reason != "none"):
        return "degraded", ["CONVERSATION_PAUSED"]
    expected = metrics.get("active_conversation_count")
    if (type(expected) is not int or expected < 1
            or type(metrics.get("this_run_active_unpaused_observed_count")) is not int
            or type(metrics.get("fresh_active_unpaused_observed_count")) is not int):
        return "unknown", ["ACTIVE_METRICS_INVALID"]
    if metrics["this_run_active_unpaused_observed_count"] != expected:
        return "unknown", ["CONTACT_OBSERVATION_INCOMPLETE"]
    if metrics["fresh_active_unpaused_observed_count"] != expected:
        return "unknown", ["SUCCESSFUL_OBSERVE_STALE"]
    return "available", []


def _recent_successful_observe(witness: dict[str, object]) -> tuple[bool, str]:
    """Require a bounded-age completed OBSERVE, not merely a fresh status file."""
    observe = witness.get("last_successful_observe")
    if not isinstance(observe, dict) or observe.get("kind") != "observe" or observe.get("status") != "ok":
        return False, "SUCCESSFUL_OBSERVE_MISSING"
    try:
        completed_at = datetime.fromisoformat(str(observe["completed_at"]).replace("Z", "+00:00"))
        freshness = float(witness["observe_freshness_seconds"])
    except (KeyError, TypeError, ValueError):
        return False, "SUCCESSFUL_OBSERVE_INVALID"
    if (completed_at.tzinfo is None or not MIN_OBSERVE_FRESHNESS_SECONDS <= freshness
            <= MAX_OBSERVE_FRESHNESS_SECONDS):
        return False, "SUCCESSFUL_OBSERVE_INVALID"
    age = (datetime.now(UTC) - completed_at.astimezone(UTC)).total_seconds()
    if age < 0 or age > freshness:
        return False, "SUCCESSFUL_OBSERVE_STALE"
    return True, ""


def _driver_status(
    metrics: dict[str, object] | None,
    witness: dict[str, object] | None = None,
    witness_reasons: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Classify explicit witness evidence and known degradation only."""
    reasons: list[str] = []
    if metrics is None:
        reasons.append("METRICS_UNAVAILABLE")
        pauses: object = None
        globally_paused = False
    else:
        pauses = metrics.get("conversation_pause_reason_counts")
        globally_paused = bool(metrics.get("global_paused"))
    if isinstance(pauses, dict) and int(pauses.get("driver_temporary:worker_not_alive", 0)) > 0:
        reasons.append("WORKER_NOT_ALIVE")
    if isinstance(pauses, dict) and int(pauses.get("driver_temporary:worker_action_failed", 0)) > 0:
        reasons.append("WORKER_ACTION_FAILED")
    if witness_reasons:
        reasons.extend(witness_reasons)
    if witness is not None:
        state = witness.get("state")
        worker_alive = witness.get("worker_alive") is True
        terminal_failure = witness.get("first_terminal_failure")
        if not worker_alive:
            reasons.append("WORKER_NOT_ALIVE")
        if state in {"degraded", "unavailable", "stopping"}:
            reasons.append("WORKER_" + str(state).upper())
        if terminal_failure not in (None, {}):
            reasons.append("WORKER_TERMINAL_FAILURE")
    if globally_paused:
        reasons.append("GLOBAL_PAUSED")
        return "paused", list(dict.fromkeys(reasons))
    if "WORKER_NOT_ALIVE" in reasons:
        return "degraded", list(dict.fromkeys(reasons))
    if witness is not None and witness.get("state") in {"degraded", "unavailable", "stopping"}:
        return "degraded", list(dict.fromkeys(reasons))
    if metrics is None:
        return "unknown", list(dict.fromkeys(reasons))
    observe_fresh, observe_reason = _recent_successful_observe(witness) if witness is not None else (False, "")
    if witness is not None and (
        witness.get("state") == "available"
        and witness.get("available") is True
        and witness.get("worker_alive") is True
        and isinstance(witness.get("worker_process_id"), int)
        and witness["worker_process_id"] > 0
        and witness.get("worker_exit_code") is None
        and witness.get("first_terminal_failure") in (None, {})
        and observe_fresh
    ):
        conversation_count = metrics.get("conversation_count")
        observed_count = metrics.get("this_run_unpaused_observed_count")
        if (
            isinstance(conversation_count, bool)
            or not isinstance(conversation_count, int)
            or conversation_count < 1
            or isinstance(observed_count, bool)
            or not isinstance(observed_count, int)
            or not isinstance(pauses, dict)
            or any(isinstance(count, bool) or not isinstance(count, int) or count < 0
                   for count in pauses.values())
        ):
            reasons.append("CONTACT_METRICS_INVALID")
        else:
            if observed_count != conversation_count:
                reasons.append("CONTACT_OBSERVATION_INCOMPLETE")
            if sum(count for reason, count in pauses.items() if reason != "none") > 0:
                reasons.append("CONVERSATION_PAUSED")
        if reasons:
            return "degraded", list(dict.fromkeys(reasons))
        return "available", []
    # Metrics with no pause do not establish health.  `available` requires the
    # fresh witness above, including a real successful OBSERVE.
    if observe_reason:
        reasons.append(observe_reason)
    return "unknown", list(dict.fromkeys(reasons or ["NO_WORKER_HEALTH_EVIDENCE"]))


def _code_list(value: object) -> list[str]:
    """Accept only the code-list shape from evaluation metadata columns."""
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return []
    return decoded


def _write_run_boundary(log, *, run_id: str, started_at: str, port: int) -> None:
    log.write(
        (f"\n=== PMAI_RUNTIME_RUN_START run_id={run_id} started_at_utc={started_at} "
         f"web_port={port} ===\n").encode("ascii")
    )


def _stop_fields(*, graceful_stop: bool, return_code: int, ever_web_reachable: bool) -> dict[str, object]:
    """Clear current reachability while retaining the separate historical witness."""
    return {
        "state": "graceful_stopped" if graceful_stop else "stopped",
        "succeeded": graceful_stop,
        "ready": False,
        "web_reachable": False,
        "driver_state": "unknown",
        "driver_state_reasons": ["RUNTIME_STOPPED"],
        "runtime_process_alive": False,
        "exit_code": return_code,
        "error_code": None if graceful_stop else (
            "RUNTIME_STOPPED" if ever_web_reachable else "RUNTIME_EXITED_BEFORE_READY"
        ),
        "stopped_at": datetime.now(UTC).isoformat(),
    }


def _metrics(data: Path, started_at: str) -> dict[str, object] | None:
    """Read aggregate counters only; none of these fields establishes ready."""
    try:
        with closing(sqlite3.connect(f"file:{data / 'runtime.sqlite3'}?mode=ro", uri=True)) as runtime_db, \
                closing(sqlite3.connect(f"file:{data / 'qq-vm-bridge.sqlite3'}?mode=ro", uri=True)) as bridge_db:
            conversations = runtime_db.execute("SELECT COUNT(*) FROM runtime_conversations").fetchone()[0]
            paused, pause_reason = runtime_db.execute(
                "SELECT paused,reason FROM runtime_global_control WHERE singleton=1").fetchone()
            observed_this_run = runtime_db.execute(
                "SELECT COUNT(*) FROM runtime_conversations WHERE paused=0 AND last_observed_at>=?", (started_at,)
            ).fetchone()[0]
            planning = dict(runtime_db.execute("SELECT status,COUNT(*) FROM runtime_planning_jobs GROUP BY status").fetchall())
            planning_errors = dict(runtime_db.execute(
                "SELECT COALESCE(error_code,'none'),COUNT(*) FROM runtime_planning_jobs GROUP BY error_code").fetchall())
            contact_pauses = dict(runtime_db.execute(
                "SELECT COALESCE(pause_reason,'none'),COUNT(*) FROM runtime_conversations GROUP BY pause_reason").fetchall())
            operations = dict(bridge_db.execute("SELECT status,COUNT(*) FROM qq_vm_ops GROUP BY status").fetchall())
            operation_errors = dict(bridge_db.execute(
                "SELECT COALESCE(error_code,'none'),COUNT(*) FROM qq_vm_ops GROUP BY error_code").fetchall())
            receipts = bridge_db.execute("SELECT COUNT(*) FROM qq_vm_receipts").fetchone()[0]
            try:
                decision_columns = ("request_id", "conversation_id", "action", "selection_reason",
                                    "model", "latency_ms", "created_at")
                decisions = [
                    dict(zip(decision_columns, row)) for row in runtime_db.execute(
                        """SELECT request_id,conversation_id,action,selection_reason,model,latency_ms,created_at
                           FROM runtime_planner_decisions ORDER BY created_at DESC LIMIT 5"""
                    ).fetchall()
                ]
            except sqlite3.Error:
                # A release may inspect an older durable database before this
                # diagnostics-only table exists.  Keep its other metrics usable.
                decisions = []
            try:
                evaluation_columns = (
                    "request_id", "conversation_id", "action", "outcome", "decision_code",
                    "policy_reason_codes", "policy_rule_ids", "policy_sensitive_categories",
                    "model", "latency_ms", "created_at",
                )
                evaluations = []
                for row in runtime_db.execute(
                    """SELECT request_id,conversation_id,action,outcome,decision_code,
                              policy_reason_codes_json,policy_rule_ids_json,policy_sensitive_categories_json,
                              model,latency_ms,created_at
                       FROM runtime_planner_evaluations ORDER BY created_at DESC LIMIT 5"""
                ).fetchall():
                    value = dict(zip(evaluation_columns, row))
                    value["policy_reason_codes"] = _code_list(value["policy_reason_codes"])
                    value["policy_rule_ids"] = _code_list(value["policy_rule_ids"])
                    value["policy_sensitive_categories"] = _code_list(value["policy_sensitive_categories"])
                    evaluations.append(value)
            except sqlite3.Error:
                # This is a new audit table.  Do not infer/backfill from the
                # older decisions table, whose semantics are not equivalent.
                evaluations = []
        return {
            "conversation_count": conversations,
            "global_paused": bool(paused),
            "global_pause_reason": pause_reason,
            "this_run_unpaused_observed_count": observed_this_run,
            "planning_status_counts": planning,
            "planning_error_code_counts": planning_errors,
            "conversation_pause_reason_counts": contact_pauses,
            "send_status_counts": operations,
            "send_error_code_counts": operation_errors,
            "receipt_count": receipts,
            "recent_planner_decisions": decisions,
            "recent_planner_evaluations": evaluations,
        }
    except (OSError, sqlite3.Error, TypeError):
        return None


def _hybrid_metrics(data: Path, started_at: str, targets: tuple[dict, ...]) -> dict | None:
    metrics = _metrics(data, started_at)
    if metrics is None:
        return None
    try:
        now, since = datetime.now(UTC), datetime.fromisoformat(started_at)
        placeholders = ",".join("?" for _ in targets)
        with closing(sqlite3.connect((data / "runtime.sqlite3").as_uri()+"?mode=ro", uri=True)) as db:
            db.execute("BEGIN")  # One current control/identity/observation read view.
            control = db.execute("SELECT revision,paused,reason FROM runtime_global_control WHERE singleton=1").fetchone()
            rows = db.execute("SELECT conversation_id,account_id,contact_id,binding_revision,conversation_type,"
                "paused,pause_reason,last_observed_at FROM runtime_conversations WHERE conversation_id IN ("
                + placeholders + ")", tuple(target["conversation_id"] for target in targets)).fetchall()
        if control is None or type(control[0]) is not int or control[0] < 1 or control[1] not in (0, 1):
            return None
        expected = {target["conversation_id"]: target for target in targets}
        current = len(rows) == len(targets)
        pauses, observed, fresh = {}, 0, 0
        for conversation_id, account, contact, revision, kind, paused, reason, completed in rows:
            target = expected[conversation_id]
            current = current and (account == target["account_id"] and contact == target["contact_id"]
                and revision == target["binding_revision"] and kind == "direct" and paused in (0, 1))
            code = reason or ("paused_without_reason" if paused else "none")
            pauses[code] = pauses.get(code, 0)+1
            if not paused and completed is not None:
                instant = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    return None
                if since <= instant <= now:
                    observed += 1
                    if (now-instant).total_seconds() <= HYBRID_OBSERVE_FRESHNESS_SECONDS:
                        fresh += 1
        return {**metrics, "global_revision": control[0], "global_paused": bool(control[1]),
            "global_pause_reason": control[2], "active_identity_current": current,
            "active_conversation_count": len(targets), "active_conversation_pause_reason_counts": pauses,
            "this_run_active_unpaused_observed_count": observed,
            "fresh_active_unpaused_observed_count": fresh,
            "hybrid_observe_freshness_seconds": HYBRID_OBSERVE_FRESHNESS_SECONDS}
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return None


def main(argv: list[str] | None = None) -> int:
    def positive_revision(value: str) -> int:
        revision = int(value)
        if revision < 1:
            raise argparse.ArgumentTypeError("session binding revision must be positive")
        return revision

    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-config-sha256")
    parser.add_argument("--expected-generation-id")
    parser.add_argument("--expected-session-binding-revision", type=positive_revision)
    parser.add_argument("--hybrid-settings")
    parser.add_argument("--expected-hybrid-settings-sha256")
    parser.add_argument("--active-binding", action="append", default=[])
    args = parser.parse_args([] if argv is None else argv)
    if not args.hybrid_settings and (args.active_binding or args.expected_hybrid_settings_sha256):
        parser.error("hybrid binding/settings digest requires --hybrid-settings")
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    port = _reserve_loopback_port()
    publish_tracker = _PublishTracker()
    report: dict[str, object] = {
        "schema": "pmai-qq-session-runtime-status-v2",
        "run_id": run_id,
        "started_at": started_at,
        "state": "starting",
        "succeeded": False,
        "ready": False,
        "runtime_started": False,
        "runtime_process_alive": False,
        "runtime_process_id": None,
        "generation_id": None,
        "session_binding_revision": None,
        "base_config_sha256": None,
        "web_port": port,
        "web_reachable": False,
        "this_run_web_observation": None,
        "driver_state": "unknown",
        "driver_state_reasons": ["NO_WORKER_HEALTH_EVIDENCE"],
        "metrics": None,
    }
    # Before Popen there is no child to supervise.  Do not create an unknown
    # runtime when its initial status cannot be made observable.
    if not _publish_status(report, publish_tracker):
        _publish_event(run_id=run_id, event="startup_publish_failed", count=publish_tracker.failures)
        return 2
    runtime = Path(__file__).resolve().parent / "run_vm_runtime.py"
    try:
        config, runtime_config, config_sha256 = _runtime_config_snapshot()
        _validate_requested_snapshot(
            config,
            config_sha256,
            expected_config_sha256=args.expected_config_sha256,
            expected_generation_id=args.expected_generation_id,
            expected_session_binding_revision=args.expected_session_binding_revision,
        )
        session_binding_revision = session_revision_number(config)
        data = Path(str(config["data_dir"]))
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        report.update({"state": "stopped", "error_code": "RUNTIME_CONFIG_UNREADABLE",
                       "stopped_at": datetime.now(UTC).isoformat()})
        _publish_status(report, publish_tracker)
        return 2
    hybrid_path = hybrid_digest = None
    hybrid_targets = ()
    if args.hybrid_settings:
        try:
            hybrid_path, hybrid_digest, hybrid_targets = _hybrid_settings_snapshot(args.hybrid_settings,
                expected_sha256=args.expected_hybrid_settings_sha256,
                active_binding_ids=args.active_binding, config=config)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            report.update({"state": "stopped", "error_code": "HYBRID_SETTINGS_INVALID",
                           "stopped_at": datetime.now(UTC).isoformat()})
            _publish_status(report, publish_tracker)
            return 2
        runtime = runtime.with_name("run_vm_runtime_v2.py")
        report.update({"runtime_mode": "hybrid_v2", "hybrid_settings_sha256": hybrid_digest,
                       "active_binding_ids": list(args.active_binding)})
    generation = config.get("runtime_generation")
    binding = config.get("session_binding")
    report.update({
        "runtime_config_sha256": config_sha256,
        "generation_id": generation.get("generation_id") if isinstance(generation, dict) else None,
        "session_binding_revision": session_binding_revision,
        "base_config_sha256": (
            str(binding["base_config_sha256"]).casefold() if isinstance(binding, dict) else config_sha256
        ),
    })
    command = [
        sys.executable,
        str(runtime),
        "--config",
        str(runtime_config),
        "--publication-config",
        str(CONFIG),
        "--expected-config-sha256",
        config_sha256,
        "--expected-session-binding-revision",
        str(session_binding_revision),
        "--run-id",
        run_id,
        "--web-host",
        "127.0.0.1",
        "--web-port",
        str(port),
    ]
    if hybrid_path is not None:
        command.extend(["--hybrid-settings", str(hybrid_path),
                        "--expected-hybrid-settings-sha256", hybrid_digest])
        for binding_id in args.active_binding:
            command.extend(["--active-binding", binding_id])
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_log = open(LOG_DIR / "qq-session-runtime.stdout.log", "ab", buffering=0)
    stderr_log = open(LOG_DIR / "qq-session-runtime.stderr.log", "ab", buffering=0)
    _write_run_boundary(stdout_log, run_id=run_id, started_at=started_at, port=port)
    _write_run_boundary(stderr_log, run_id=run_id, started_at=started_at, port=port)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout_log,
        stderr=stderr_log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stdout_log.close()
    stderr_log.close()
    report["runtime_process_id"] = process.pid
    report["runtime_started"] = True
    report["runtime_process_alive"] = True
    _publish_status(report, publish_tracker)
    ever_web_reachable = False
    while process.poll() is None:
        observation = _http_observation(port)
        if hybrid_path is None:
            metrics = _metrics(data, str(report["started_at"]))
            witness, witness_reasons = _worker_witness(run_id)
            driver_state, driver_reasons = _driver_status(metrics, witness, witness_reasons)
        else:
            metrics = _hybrid_metrics(data, str(report["started_at"]), hybrid_targets)
            witness, witness_reasons = _hybrid_witness(data, run_id, started_at=str(report["started_at"]),
                                                     active_targets=hybrid_targets)
            driver_state, driver_reasons = _hybrid_driver_status(metrics, witness, witness_reasons)
        control = _control_result(run_id)
        if observation is not None:
            ever_web_reachable = True
            report.update({
                "state": "paused" if driver_state == "paused" else "running",
                "succeeded": True,
                "ready": driver_state == "available",
                "runtime_process_alive": True,
                "web_reachable": True,
                "this_run_web_observation": observation,
                "driver_state": driver_state,
                "driver_state_reasons": driver_reasons,
                "metrics": metrics,
                "runtime_control": control,
                "updated_at": datetime.now(UTC).isoformat(),
            })
            _publish_status(report, publish_tracker)
        else:
            report.update({
                "state": "starting",
                "succeeded": False,
                "ready": False,
                "runtime_process_alive": True,
                "web_reachable": False,
                "driver_state": driver_state,
                "driver_state_reasons": driver_reasons,
                "metrics": metrics,
                "runtime_control": control,
                "updated_at": datetime.now(UTC).isoformat(),
            })
            _publish_status(report, publish_tracker)
        time.sleep(1)
    return_code = int(process.returncode or 0)
    control = _control_result(run_id)
    graceful_stop = bool(
        return_code == 0 and control and control.get("action") == "graceful_stop"
        and control.get("accepted") is True and control.get("state") == "stopping"
    )
    report.update(_stop_fields(
        graceful_stop=graceful_stop, return_code=return_code, ever_web_reachable=ever_web_reachable,
    ))
    report["runtime_control"] = control
    _publish_status(report, publish_tracker)
    _publish_event(run_id=run_id, event="process_exited", count=publish_tracker.failures,
                   exit_code=return_code)
    return 0 if graceful_stop else (return_code or 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
