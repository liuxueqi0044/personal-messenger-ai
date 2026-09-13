"""Start one runtime and report Web reachability separately from QQ health.

`web_reachable` records the spawned PID's uniquely assigned local WebUI port.
`ready` additionally requires explicit driver health evidence, which this
supervisor does not currently possess; it must not be inferred from HTTP 200.
"""
from __future__ import annotations

import http.client
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


OUTPUT = Path(r"C:\PMAI\data\qq-session-runtime-status.json")
CONFIG = Path(r"C:\PMAI\data\runtime-session-1.json")
CONTROL_RESULT = Path(r"C:\PMAI\data\qq-session-runtime-control-result.json")
WORKER_WITNESS = Path(r"C:\PMAI\data\qq-session-runtime-worker-status.json")
WORKER_WITNESS_SCHEMA = "pmai-qq-runtime-worker-status-v1"
MAX_WITNESS_WRITE_AGE_SECONDS = 10
MIN_OBSERVE_FRESHNESS_SECONDS = 30.0
MAX_OBSERVE_FRESHNESS_SECONDS = 600.0
_TRANSIENT_WINDOWS_FILE_ERRORS = frozenset({5, 32, 33})
_PUBLISH_RETRY_DELAYS = (0.05, 0.10, 0.15)
SUPERVISOR_EVENT_LOG = Path(r"C:\PMAI\data\logs\qq-session-runtime-supervisor.stderr.log")
LOG_DIR = Path(r"C:\PMAI\data\logs")


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


def main() -> int:
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
        data = Path(json.loads(CONFIG.read_text(encoding="utf-8"))["data_dir"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        report.update({"state": "stopped", "error_code": "RUNTIME_CONFIG_UNREADABLE",
                       "stopped_at": datetime.now(UTC).isoformat()})
        _publish_status(report, publish_tracker)
        return 2
    command = [sys.executable, str(runtime), "--config", str(CONFIG), "--run-id", run_id,
               "--web-host", "127.0.0.1", "--web-port", str(port)]
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
        metrics = _metrics(data, str(report["started_at"]))
        witness, witness_reasons = _worker_witness(run_id)
        driver_state, driver_reasons = _driver_status(metrics, witness, witness_reasons)
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
    raise SystemExit(main())
