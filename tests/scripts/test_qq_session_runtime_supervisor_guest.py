from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
from messenger_ai.runtime import session_revision


SOURCE = Path(__file__).parents[2] / "scripts" / "deployment" / "qq_session_runtime_supervisor_guest.py"
SPEC = importlib.util.spec_from_file_location("runtime_supervisor", SOURCE)
assert SPEC and SPEC.loader
SUPERVISOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR)


def _session_revision_config(tmp_path: Path, monkeypatch) -> tuple[dict, Path, Path]:
    runtime_root = tmp_path / "runtime"
    generation_id = "d7c6d3a3-1a3f-45a9-93e0-e5d9e755ccee"
    data_dir = runtime_root / "recovery-generations" / generation_id / "qq-default-account"
    data_dir.mkdir(parents=True)
    proof = QQSessionObservedDirectIdentity(
        binding_id="session-contact-1", conversation_type="direct",
        type_evidence_source="operator_observed_direct", client_version="9.9.20",
        selector_pack_version="session-pack-v1", group_marker_probe_complete=True,
        group_marker_count=0, process_id=123, window_handle=456,
        process_started_at_100ns=789, vm_environment_fingerprint="a" * 64,
        selected_row_runtime_id_hash="b" * 64, header_digest="c" * 64,
    )
    binding = {
        "binding_id": proof.binding_id, "contact_id": "contact-1",
        "account_id": "qq-default-account", "hub_conversation_id": "conversation-1",
        "participant_signature": proof.participant_signature,
        "platform_conversation_id": "runtime:" + proof.selected_row_runtime_id_hash,
    }
    manifest = json.dumps({
        "schema": "pmai-isolated-runtime-generation-manifest-v1",
        "generation_id": generation_id,
    }, sort_keys=True).encode("utf-8")
    (data_dir / "generation-manifest.json").write_bytes(manifest)
    config = {
        "schema": "pmai-v5-runtime-1", "data_dir": str(data_dir),
        "identity_mode": "session_observed_direct", "start_globally_paused": True,
        "bindings": [binding],
        "contacts": [{"contact_id": "contact-1", "binding": copy.deepcopy(binding)}],
        "session_observed_evidence": [proof.model_dump(mode="json")],
        "selector_pack": {
            "environment_fingerprint": proof.vm_environment_fingerprint,
            "client_version": proof.client_version,
            "fixture_suite_version": proof.selector_pack_version,
        },
        "runtime_generation": {
            "schema": "pmai-isolated-runtime-generation-v1",
            "generation_id": generation_id, "mode": "isolated_identity_recovery",
            "enforce_global_pause": True,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        },
    }
    base_bytes = json.dumps(config, sort_keys=True).encode("utf-8")
    (data_dir / "runtime-config.json").write_bytes(base_bytes)
    base_digest = hashlib.sha256(base_bytes).hexdigest()
    config["session_observed_evidence"][0]["process_id"] = 999
    config["session_binding"] = {
        "schema": "pmai-qq-session-binding-v1", "revision": 2,
        "base_config_sha256": base_digest, "previous_config_sha256": base_digest,
    }
    payload = json.dumps(config, sort_keys=True).encode("utf-8")
    canonical = tmp_path / "runtime.json"
    canonical.write_bytes(payload)
    snapshot = data_dir / "runtime-config.session-2.json"
    snapshot.write_bytes(payload)
    monkeypatch.setattr(session_revision, "TRUSTED_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(SUPERVISOR, "CONFIG", canonical)
    return config, canonical, snapshot


def test_driver_status_never_infers_health_from_missing_pauses() -> None:
    assert SUPERVISOR._driver_status(None) == ("unknown", ["METRICS_UNAVAILABLE"])
    assert SUPERVISOR._driver_status({"global_paused": False, "conversation_pause_reason_counts": {}}) == (
        "unknown", ["NO_WORKER_HEALTH_EVIDENCE"]
    )
    assert SUPERVISOR._driver_status({
        "global_paused": False,
        "conversation_pause_reason_counts": {"driver_temporary:worker_not_alive": 1},
    }) == ("degraded", ["WORKER_NOT_ALIVE"])
    assert SUPERVISOR._driver_status({
        "global_paused": True,
        "conversation_pause_reason_counts": {"driver_temporary:worker_not_alive": 1},
    }) == ("paused", ["WORKER_NOT_ALIVE", "GLOBAL_PAUSED"])


def test_driver_status_requires_fresh_witness_successful_observe_for_available() -> None:
    metrics = {
        "global_paused": False,
        "conversation_count": 1,
        "this_run_unpaused_observed_count": 1,
        "conversation_pause_reason_counts": {"none": 1},
    }
    witness = {
        "state": "available", "available": True, "worker_alive": True,
        "worker_process_id": 123, "worker_exit_code": None, "first_terminal_failure": None,
        "observe_freshness_seconds": 30.0,
        "last_successful_observe": {
            "kind": "observe", "status": "ok", "completed_at": datetime.now(UTC).isoformat(),
        },
    }
    assert SUPERVISOR._driver_status(metrics, witness, []) == ("available", [])

    witness["last_successful_observe"] = None
    assert SUPERVISOR._driver_status(metrics, witness, []) == ("unknown", ["SUCCESSFUL_OBSERVE_MISSING"])
    assert SUPERVISOR._driver_status(metrics, None, ["WORKER_WITNESS_STALE"]) == (
        "unknown", ["WORKER_WITNESS_STALE"]
    )
    witness["last_successful_observe"] = {
        "kind": "observe", "status": "ok", "completed_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    }
    assert SUPERVISOR._driver_status(metrics, witness, []) == ("unknown", ["SUCCESSFUL_OBSERVE_STALE"])


def test_driver_status_rejects_healthy_worker_when_contacts_are_not_ready() -> None:
    metrics = {
        "global_paused": False,
        "conversation_count": 3,
        "this_run_unpaused_observed_count": 0,
        "conversation_pause_reason_counts": {
            "driver_temporary:worker_action_failed": 3,
        },
    }
    witness = {
        "state": "available", "available": True, "worker_alive": True,
        "worker_process_id": 123, "worker_exit_code": None,
        "first_terminal_failure": None, "observe_freshness_seconds": 30.0,
        "last_successful_observe": {
            "kind": "observe", "status": "ok",
            "completed_at": datetime.now(UTC).isoformat(),
        },
    }

    assert SUPERVISOR._driver_status(metrics, witness, []) == (
        "degraded",
        ["WORKER_ACTION_FAILED", "CONTACT_OBSERVATION_INCOMPLETE", "CONVERSATION_PAUSED"],
    )


def test_metrics_exports_evaluation_metadata_without_payload_columns(tmp_path: Path) -> None:
    runtime = sqlite3.connect(tmp_path / "runtime.sqlite3")
    runtime.executescript("""
        CREATE TABLE runtime_conversations(paused INTEGER,last_observed_at TEXT,pause_reason TEXT);
        INSERT INTO runtime_conversations VALUES(0,'2026-01-01T00:00:00+00:00',NULL);
        CREATE TABLE runtime_global_control(singleton INTEGER,paused INTEGER,reason TEXT);
        INSERT INTO runtime_global_control VALUES(1,0,NULL);
        CREATE TABLE runtime_planning_jobs(status TEXT,error_code TEXT);
        INSERT INTO runtime_planning_jobs VALUES('ignored',NULL);
        CREATE TABLE runtime_planner_decisions(request_id TEXT,conversation_id TEXT,action TEXT,selection_reason TEXT,model TEXT,latency_ms INTEGER,created_at TEXT);
        CREATE TABLE runtime_planner_evaluations(
          request_id TEXT,conversation_id TEXT,action TEXT,outcome TEXT,decision_code TEXT,
          policy_reason_codes_json TEXT,policy_rule_ids_json TEXT,policy_sensitive_categories_json TEXT,
          model TEXT,latency_ms INTEGER,created_at TEXT,provider_request_json TEXT,plan_json TEXT,usage_json TEXT
        );
        INSERT INTO runtime_planner_evaluations VALUES(
          'req-1','conversation-1','observe','review_required','review',
          '["manual_rule_hit"]','["rule-1"]','["sensitive"]','model-x',42,
          '2026-01-01T00:00:00+00:00','secret-request','secret-plan','secret-usage'
        );
    """)
    runtime.commit()
    runtime.close()
    bridge = sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3")
    bridge.executescript("CREATE TABLE qq_vm_ops(status TEXT,error_code TEXT); CREATE TABLE qq_vm_receipts(x INTEGER);")
    bridge.commit()
    bridge.close()

    metrics = SUPERVISOR._metrics(tmp_path, "2025-01-01T00:00:00+00:00")

    assert metrics is not None
    assert metrics["recent_planner_evaluations"] == [{
        "request_id": "req-1", "conversation_id": "conversation-1", "action": "observe",
        "outcome": "review_required", "decision_code": "review",
        "policy_reason_codes": ["manual_rule_hit"], "policy_rule_ids": ["rule-1"],
        "policy_sensitive_categories": ["sensitive"], "model": "model-x", "latency_ms": 42,
        "created_at": "2026-01-01T00:00:00+00:00",
    }]
    assert "secret-request" not in str(metrics)
    assert "secret-plan" not in str(metrics)
    assert "secret-usage" not in str(metrics)


def test_metrics_returns_empty_evaluations_when_legacy_db_lacks_table(tmp_path: Path) -> None:
    runtime = sqlite3.connect(tmp_path / "runtime.sqlite3")
    runtime.executescript("""
        CREATE TABLE runtime_conversations(paused INTEGER,last_observed_at TEXT,pause_reason TEXT);
        CREATE TABLE runtime_global_control(singleton INTEGER,paused INTEGER,reason TEXT);
        INSERT INTO runtime_global_control VALUES(1,0,NULL);
        CREATE TABLE runtime_planning_jobs(status TEXT,error_code TEXT);
        CREATE TABLE runtime_planner_decisions(request_id TEXT,conversation_id TEXT,action TEXT,selection_reason TEXT,model TEXT,latency_ms INTEGER,created_at TEXT);
    """)
    runtime.commit(); runtime.close()
    bridge = sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3")
    bridge.executescript("CREATE TABLE qq_vm_ops(status TEXT,error_code TEXT); CREATE TABLE qq_vm_receipts(x INTEGER);")
    bridge.commit(); bridge.close()

    metrics = SUPERVISOR._metrics(tmp_path, "2025-01-01T00:00:00+00:00")

    assert metrics is not None
    assert metrics["recent_planner_evaluations"] == []


def test_run_boundary_contains_only_run_identity_and_web_port() -> None:
    stream = BytesIO()
    SUPERVISOR._write_run_boundary(stream, run_id="run-1", started_at="2026-01-01T00:00:00+00:00", port=12345)
    assert stream.getvalue() == (
        b"\n=== PMAI_RUNTIME_RUN_START run_id=run-1 started_at_utc=2026-01-01T00:00:00+00:00 web_port=12345 ===\n"
    )


def test_stopped_status_clears_current_web_and_driver_health_but_keeps_web_history_for_cause() -> None:
    fields = SUPERVISOR._stop_fields(graceful_stop=False, return_code=7, ever_web_reachable=True)

    assert fields["state"] == "stopped"
    assert fields["web_reachable"] is False
    assert fields["ready"] is False
    assert fields["driver_state"] == "unknown"
    assert fields["driver_state_reasons"] == ["RUNTIME_STOPPED"]
    assert fields["error_code"] == "RUNTIME_STOPPED"


def test_status_publish_retries_only_transient_windows_replace_locks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(SUPERVISOR, "OUTPUT", tmp_path / "status.json")
    monkeypatch.setattr(SUPERVISOR.os, "name", "nt", raising=False)
    monkeypatch.setattr(SUPERVISOR.time, "sleep", lambda _: None)
    events = []
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **value: events.append(value))
    original = SUPERVISOR.os.replace
    attempts = 0

    def replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            error = OSError("sharing")
            error.winerror = 32
            raise error
        return original(source, target)

    monkeypatch.setattr(SUPERVISOR.os, "replace", replace)
    tracker = SUPERVISOR._PublishTracker()
    assert SUPERVISOR._publish_status({"run_id": "run-1"}, tracker) is True
    assert attempts == 4
    assert tracker.failures == 0
    assert events == []


def test_status_publish_does_not_retry_nontransient_error_and_recovers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(SUPERVISOR, "OUTPUT", tmp_path / "status.json")
    monkeypatch.setattr(SUPERVISOR.os, "name", "nt", raising=False)
    monkeypatch.setattr(SUPERVISOR.time, "sleep", lambda _: None)
    events = []
    original = SUPERVISOR.os.replace
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **value: events.append(value))
    attempts = 0

    def replace(source, target):
        nonlocal attempts
        attempts += 1
        error = OSError("denied")
        error.winerror = 87
        raise error

    monkeypatch.setattr(SUPERVISOR.os, "replace", replace)
    tracker = SUPERVISOR._PublishTracker()
    assert SUPERVISOR._publish_status({"run_id": "run-1"}, tracker) is False
    assert attempts == 1
    assert events[0]["event"] == "publish_failed"
    monkeypatch.setattr(SUPERVISOR.os, "replace", original)
    assert SUPERVISOR._publish_status({"run_id": "run-1"}, tracker) is True
    assert events[-1] == {"run_id": "run-1", "event": "publish_recovered", "count": 1}


def test_initial_publish_failure_prevents_spawn(monkeypatch) -> None:
    monkeypatch.setattr(SUPERVISOR, "_publish_status", lambda *_: False)
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **_: None)
    monkeypatch.setattr(SUPERVISOR.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")))
    assert SUPERVISOR.main() == 2


def test_isolated_supervisor_uses_exact_generation_config_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "generation"
    data_dir.mkdir()
    canonical = tmp_path / "runtime.json"
    value = {"data_dir": str(data_dir), "runtime_generation": {"generation_id": "x"}}
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    canonical.write_bytes(payload)
    generation_config = data_dir / "runtime-config.json"
    generation_config.write_bytes(payload)
    monkeypatch.setattr(SUPERVISOR, "CONFIG", canonical)

    loaded, selected, digest = SUPERVISOR._runtime_config_snapshot()

    assert loaded == value
    assert selected == generation_config
    assert len(digest) == 64
    generation_config.write_bytes(payload + b"\n")
    with pytest.raises(ValueError, match="does not match"):
        SUPERVISOR._runtime_config_snapshot()


def test_supervisor_selects_exact_session_revision_snapshot(tmp_path: Path, monkeypatch) -> None:
    config, canonical, snapshot = _session_revision_config(tmp_path, monkeypatch)

    loaded, selected, digest = SUPERVISOR._runtime_config_snapshot()

    assert loaded == config
    assert selected == snapshot
    assert digest == hashlib.sha256(canonical.read_bytes()).hexdigest()
    # Equal JSON values are insufficient: bind the launch to exact snapshot bytes.
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="does not match"):
        SUPERVISOR._runtime_config_snapshot()


@pytest.mark.parametrize("failure", [
    "malformed", "out_of_order", "digest", "root", "missing_snapshot",
])
def test_supervisor_rejects_invalid_session_revision_before_spawn(
    failure: str, tmp_path: Path, monkeypatch,
) -> None:
    config, canonical, snapshot = _session_revision_config(tmp_path, monkeypatch)
    if failure == "malformed":
        config["session_binding"]["revision"] = True
    elif failure == "out_of_order":
        config["session_binding"]["revision"] = 3
    elif failure == "digest":
        config["session_binding"]["base_config_sha256"] = "0" * 64
    elif failure == "root":
        config["data_dir"] = str(tmp_path / "untrusted-root")
    payload = json.dumps(config, sort_keys=True).encode("utf-8")
    canonical.write_bytes(payload)
    snapshot.write_bytes(payload)
    if failure == "missing_snapshot":
        snapshot.unlink()
    monkeypatch.setattr(SUPERVISOR, "_reserve_loopback_port", lambda: 12345)
    reports = []
    monkeypatch.setattr(
        SUPERVISOR, "_publish_status",
        lambda report, _tracker: reports.append(dict(report)) or True,
    )
    monkeypatch.setattr(
        SUPERVISOR.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("runtime spawned for invalid revision"),
    )

    assert SUPERVISOR.main() == 2
    assert reports[-1]["error_code"] == "RUNTIME_CONFIG_UNREADABLE"
    assert reports[-1]["runtime_started"] is False


def test_session_revision_status_preserves_paused_not_ready(tmp_path: Path, monkeypatch) -> None:
    config, canonical, snapshot = _session_revision_config(tmp_path, monkeypatch)
    monkeypatch.setattr(SUPERVISOR, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(SUPERVISOR, "_reserve_loopback_port", lambda: 12345)
    monkeypatch.setattr(SUPERVISOR, "_write_run_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(SUPERVISOR, "_http_observation", lambda _port: {"status_code": 200})
    monkeypatch.setattr(SUPERVISOR, "_metrics", lambda *_args: {
        "global_paused": True, "conversation_pause_reason_counts": {},
    })
    monkeypatch.setattr(SUPERVISOR, "_worker_witness", lambda _run: (None, []))
    monkeypatch.setattr(SUPERVISOR, "_control_result", lambda _run: None)
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **_kwargs: None)
    monkeypatch.setattr(SUPERVISOR.time, "sleep", lambda _seconds: None)
    reports = []
    monkeypatch.setattr(
        SUPERVISOR, "_publish_status",
        lambda report, _tracker: reports.append(dict(report)) or True,
    )

    class Process:
        pid = 999
        returncode = 0

        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0

    commands = []
    monkeypatch.setattr(
        SUPERVISOR.subprocess, "Popen",
        lambda command, **_kwargs: commands.append(command) or Process(),
    )

    assert SUPERVISOR.main(["--expected-session-binding-revision", "2"]) == 2

    paused = next(report for report in reports if report["state"] == "paused")
    assert paused["schema"] == "pmai-qq-session-runtime-status-v2"
    assert paused["web_reachable"] is True
    assert paused["driver_state"] == "paused"
    assert paused["ready"] is False
    assert paused["generation_id"] == config["runtime_generation"]["generation_id"]
    assert paused["session_binding_revision"] == 2
    assert paused["base_config_sha256"] == config["session_binding"]["base_config_sha256"]
    assert paused["runtime_config_sha256"] == hashlib.sha256(canonical.read_bytes()).hexdigest()
    assert commands[0][commands[0].index("--config") + 1] == str(snapshot)
    assert commands[0][commands[0].index("--expected-session-binding-revision") + 1] == "2"
    assert reports[-1]["session_binding_revision"] == 2


def test_supervisor_binds_start_to_requested_generation_and_digest() -> None:
    value = {"runtime_generation": {"generation_id": "generation-a"}}
    SUPERVISOR._validate_requested_snapshot(
        value,
        "a" * 64,
        expected_config_sha256="A" * 64,
        expected_generation_id="generation-a",
    )
    with pytest.raises(ValueError, match="requested build"):
        SUPERVISOR._validate_requested_snapshot(
            value,
            "a" * 64,
            expected_config_sha256="b" * 64,
            expected_generation_id="generation-a",
        )
    with pytest.raises(ValueError, match="requested build"):
        SUPERVISOR._validate_requested_snapshot(
            value,
            "a" * 64,
            expected_config_sha256="a" * 64,
            expected_generation_id="generation-b",
        )


def test_supervisor_binds_revision_independently_of_generation_and_digest() -> None:
    config = {"session_binding": {
        "schema": "pmai-qq-session-binding-v1",
        "revision": 2,
        "base_config_sha256": "a" * 64,
        "previous_config_sha256": "a" * 64,
    }}
    SUPERVISOR._validate_requested_snapshot(
        config, "b" * 64,
        expected_config_sha256=None,
        expected_generation_id=None,
        expected_session_binding_revision=2,
    )
    with pytest.raises(ValueError, match="session binding revision"):
        SUPERVISOR._validate_requested_snapshot(
            config, "b" * 64,
            expected_config_sha256=None,
            expected_generation_id=None,
            expected_session_binding_revision=3,
        )
    SUPERVISOR._validate_requested_snapshot(
        {}, "b" * 64,
        expected_config_sha256=None,
        expected_generation_id=None,
        expected_session_binding_revision=1,
    )


@pytest.mark.parametrize("revision", ["0", "-1", "abc", "1.5"])
def test_supervisor_cli_requires_positive_revision(revision: str, monkeypatch) -> None:
    monkeypatch.setattr(
        SUPERVISOR.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("runtime spawned for invalid revision"),
    )
    with pytest.raises(SystemExit) as stopped:
        SUPERVISOR.main(["--expected-session-binding-revision", revision])
    assert stopped.value.code == 2


def test_supervisor_requested_revision_mismatch_prevents_spawn(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(SUPERVISOR, "CONFIG", config)
    monkeypatch.setattr(SUPERVISOR, "_reserve_loopback_port", lambda: 12345)
    reports = []
    monkeypatch.setattr(
        SUPERVISOR, "_publish_status",
        lambda report, _tracker: reports.append(dict(report)) or True,
    )
    monkeypatch.setattr(
        SUPERVISOR.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("runtime spawned for mismatched revision"),
    )

    assert SUPERVISOR.main(["--expected-session-binding-revision", "2"]) == 2

    assert reports[-1]["state"] == "stopped"
    assert reports[-1]["error_code"] == "RUNTIME_CONFIG_UNREADABLE"
    assert reports[-1]["runtime_started"] is False


def test_postspawn_publish_failure_does_not_abandon_polling(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(SUPERVISOR, "CONFIG", config)
    monkeypatch.setattr(SUPERVISOR, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(SUPERVISOR, "_reserve_loopback_port", lambda: 12345)
    monkeypatch.setattr(SUPERVISOR, "_write_run_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(SUPERVISOR, "_http_observation", lambda _port: None)
    monkeypatch.setattr(SUPERVISOR, "_metrics", lambda *_args: None)
    monkeypatch.setattr(SUPERVISOR, "_worker_witness", lambda _run: (None, []))
    monkeypatch.setattr(SUPERVISOR, "_driver_status", lambda *_args: ("unknown", []))
    monkeypatch.setattr(SUPERVISOR, "_control_result", lambda _run: None)
    monkeypatch.setattr(SUPERVISOR.time, "sleep", lambda _seconds: None)
    events = []
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **value: events.append(value))
    published = []
    def publish(_report, _tracker):
        published.append(dict(_report))
        return len(published) != 3
    monkeypatch.setattr(SUPERVISOR, "_publish_status", publish)
    class Process:
        pid = 999
        returncode = 0
        def __init__(self): self.calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0
    process = Process()
    commands = []
    monkeypatch.setattr(
        SUPERVISOR.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or process,
    )
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: BytesIO())
    assert SUPERVISOR.main() == 2
    assert process.calls == 2
    assert len(published) == 4
    assert events[-1]["event"] == "process_exited"
    assert events[-1]["exit_code"] == 0
    digest_index = commands[0].index("--expected-config-sha256")
    expected = hashlib.sha256(config.read_bytes()).hexdigest()
    assert commands[0][digest_index + 1] == expected
    revision_index = commands[0].index("--expected-session-binding-revision")
    assert commands[0][revision_index + 1] == "1"
    assert published[-1]["generation_id"] is None
    assert published[-1]["session_binding_revision"] == 1
    assert published[-1]["base_config_sha256"] == expected


def test_publish_event_never_serializes_exception_message_or_path(tmp_path: Path, monkeypatch) -> None:
    event_log = tmp_path / "supervisor-events.log"
    monkeypatch.setattr(SUPERVISOR, "SUPERVISOR_EVENT_LOG", event_log)
    error = OSError("SENTINEL_SECRET_TEXT C:\\sensitive\\path")
    error.winerror = 32
    SUPERVISOR._publish_event(run_id="run-1", event="publish_failed", count=1, exc=error)
    written = event_log.read_text(encoding="utf-8")
    assert "SENTINEL_SECRET_TEXT" not in written
    assert "sensitive" not in written
    assert '"exception_type": "OSError"' in written
    assert '"winerror": 32' in written
