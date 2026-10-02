"""Opt-in V2 over original protected snapshots and actual readiness records."""
import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.scripts.test_qq_session_runtime_supervisor_guest import (
    SUPERVISOR, _session_revision_config,
)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    config, canonical, snapshot = _session_revision_config(tmp_path, monkeypatch)
    data = Path(config["data_dir"])
    target = {"account_id":"qq-default-account", "contact_id":"contact-1",
        "conversation_id":"conversation-1", "binding_id":"session-contact-1", "binding_revision":7}
    settings = tmp_path / "hybrid-settings.json"
    payload = {"schema_version":"qq_hybrid_runtime_v2", "enabled":True,
        "inputs":[{"contact_id":target["contact_id"], "target":{**target, "identity_mode":"persistent"}}]}
    settings.write_text(json.dumps(payload), encoding="utf-8")
    db = sqlite3.connect(data / "runtime.sqlite3")
    db.executescript("""
      CREATE TABLE runtime_global_control(singleton INTEGER,revision INTEGER,paused INTEGER,reason TEXT);
      INSERT INTO runtime_global_control VALUES(1,12,0,NULL);
      CREATE TABLE runtime_conversations(conversation_id TEXT,account_id TEXT,contact_id TEXT,
        binding_revision INTEGER,conversation_type TEXT,paused INTEGER,pause_reason TEXT,last_observed_at TEXT);
      INSERT INTO runtime_conversations VALUES('conversation-1','qq-default-account','contact-1',7,'direct',0,NULL,NULL);
      INSERT INTO runtime_conversations VALUES('inactive','qq-default-account','inactive',2,'direct',1,'driver_temporary:worker_not_alive',NULL);
      CREATE TABLE runtime_planning_jobs(status TEXT,error_code TEXT);
    """)
    db.commit(); db.close()
    bridge = sqlite3.connect(data / "qq-vm-bridge.sqlite3")
    bridge.executescript("CREATE TABLE qq_vm_ops(status TEXT,error_code TEXT); CREATE TABLE qq_vm_receipts(x INTEGER);")
    bridge.commit(); bridge.close()
    monkeypatch.setattr(SUPERVISOR, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(SUPERVISOR, "CONTROL_RESULT", tmp_path / "control.json")
    monkeypatch.setattr(SUPERVISOR, "WORKER_WITNESS", tmp_path / "old-v1-witness.json")
    monkeypatch.setattr(SUPERVISOR, "_reserve_loopback_port", lambda: 12345)
    monkeypatch.setattr(SUPERVISOR, "_http_observation", lambda _port: {"status_code":200})
    monkeypatch.setattr(SUPERVISOR.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(SUPERVISOR, "_publish_event", lambda **kwargs: None)
    state = SimpleNamespace(reports=[], commands=[], witness_change=None, observed=True,
        legacy=False, acknowledge=True, acknowledgement_run=None)
    monkeypatch.setattr(SUPERVISOR, "_publish_status",
        lambda value, _tracker: state.reports.append(copy.deepcopy(value)) or True)

    class Process:
        pid, returncode = 777, 0
        calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0

    def spawn(command, **kwargs):
        state.commands.append(command)
        run_id = command[command.index("--run-id")+1]
        if state.observed:
            with sqlite3.connect(data / "runtime.sqlite3") as current:
                current.execute("UPDATE runtime_conversations SET last_observed_at=? WHERE conversation_id=?",
                    (datetime.now(UTC).isoformat(), target["conversation_id"]))
        witness = {"schema":"qq_hybrid_runtime_status_v2", "run_id":run_id,
            "written_at":datetime.now(UTC).isoformat(), "global_revision":12, "paused":False,
            "session":{"schema":"qq_hybrid_session_v2", "state":"idle", "cleanup_required":False,
                       "purpose":None, "worker_epoch":None},
            "active_conversations":[target["conversation_id"]], "startup_health":"ok"}
        if state.witness_change:
            state.witness_change(witness)
        (data / "qq-hybrid-runtime-status.json").write_text(json.dumps(witness), encoding="utf-8")
        if state.acknowledge:
            SUPERVISOR.CONTROL_RESULT.write_text(json.dumps({"schema":"pmai-qq-runtime-control-result-v1",
                "target_run_id":state.acknowledgement_run or run_id, "action":"graceful_stop",
                "accepted":True, "state":"stopping"}), encoding="utf-8")
        return Process()

    monkeypatch.setattr(SUPERVISOR.subprocess, "Popen", spawn)
    return SimpleNamespace(config=config, canonical=canonical, snapshot=snapshot, target=target,
        payload=payload, settings=settings, data=data, state=state)


def args(rig):
    return ["--hybrid-settings", str(rig.settings), "--active-binding", "session-contact-1",
        "--expected-session-binding-revision", "2",
        "--expected-generation-id", rig.config["runtime_generation"]["generation_id"],
        "--expected-config-sha256", hashlib.sha256(rig.canonical.read_bytes()).hexdigest(),
        "--expected-hybrid-settings-sha256", hashlib.sha256(rig.settings.read_bytes()).hexdigest()]


def active_report(rig):
    return next(report for report in rig.state.reports if report.get("web_reachable") is True)


def test_v2_start_passes_both_exact_snapshots_and_keeps_original_generation_and_identity(rig):
    original_config = rig.canonical.read_bytes()
    original_settings = rig.settings.read_bytes()
    assert SUPERVISOR.main(args(rig)) == 0
    command = rig.state.commands[0]
    assert Path(command[1]).name == "run_vm_runtime_v2.py"
    assert command[command.index("--config")+1] == str(rig.snapshot)
    assert command[command.index("--publication-config")+1] == str(rig.canonical)
    assert command[command.index("--hybrid-settings")+1] == str(rig.settings)
    assert command[command.index("--expected-hybrid-settings-sha256")+1] == hashlib.sha256(original_settings).hexdigest()
    assert command[command.index("--expected-session-binding-revision")+1] == "2"
    assert command[command.index("--active-binding")+1] == "session-contact-1"
    report = active_report(rig)
    assert report["ready"] is True and report["driver_state"] == "available"
    assert report["metrics"]["active_conversation_count"] == 1
    assert report["metrics"]["conversation_count"] == 2  # Inactive pause is kept as diagnostics.
    assert "worker_alive" not in json.dumps(report)
    assert rig.canonical.read_bytes() == original_config and rig.settings.read_bytes() == original_settings
    assert rig.state.reports[-1]["state"] == "graceful_stopped"
    assert rig.state.reports[-1]["ready"] is False and rig.state.reports[-1]["web_reachable"] is False


@pytest.mark.parametrize("failure", ["digest", "relative", "duplicate", "missing_active", "unknown", "identity_drift"])
def test_bad_hybrid_snapshot_or_unapproved_scope_never_spawns(rig, failure):
    command = args(rig)
    if failure == "digest":
        command[-1] = "0"*64
    elif failure == "relative":
        command[1] = "hybrid-settings.json"
    elif failure == "duplicate":
        command.extend(["--active-binding", "session-contact-1"])
    elif failure == "missing_active":
        command = command[:2]+command[4:]
    elif failure == "unknown":
        command[3] = "unregistered"
    elif failure == "identity_drift":
        rig.payload["inputs"][0]["target"]["conversation_id"] = "foreign"
        rig.settings.write_text(json.dumps(rig.payload), encoding="utf-8")
        command[-1] = hashlib.sha256(rig.settings.read_bytes()).hexdigest()
    assert SUPERVISOR.main(command) == 2
    assert not rig.state.commands
    assert rig.state.reports[-1]["error_code"] == "HYBRID_SETTINGS_INVALID"
    assert not rig.state.reports[-1]["runtime_started"]


@pytest.mark.parametrize("flag", ["--active-binding", "--expected-hybrid-settings-sha256"])
def test_hybrid_flags_do_not_implicitly_change_default_runner(rig, flag):
    with pytest.raises(SystemExit):
        SUPERVISOR.main([flag, "value"])
    assert not rig.state.commands


def test_default_keeps_v1_runner_and_old_witness_path(rig, monkeypatch):
    called = []
    monkeypatch.setattr(SUPERVISOR, "_worker_witness", lambda run_id: called.append(run_id) or (None, []))
    assert SUPERVISOR.main() == 0
    command = rig.state.commands[0]
    assert Path(command[1]).name == "run_vm_runtime.py"
    assert "--hybrid-settings" not in command and called
    assert "runtime_mode" not in active_report(rig)


def test_cached_startup_health_and_web200_are_insufficient_before_this_run_observation(rig):
    rig.state.observed = False
    assert SUPERVISOR.main(args(rig)) == 0
    report = active_report(rig)
    assert report["ready"] is False
    assert report["driver_state_reasons"] == ["CONTACT_OBSERVATION_INCOMPLETE"]
    assert report["web_reachable"] is True


@pytest.mark.parametrize("mutation,reason", [
    (lambda value: value.update(run_id=str(uuid4())), "HYBRID_WITNESS_RUN_MISMATCH"),
    (lambda value: value.update(written_at=(datetime.now(UTC)-timedelta(seconds=30)).isoformat()), "HYBRID_WITNESS_INVALID"),
    (lambda value: value.update(written_at=(datetime.now(UTC)+timedelta(seconds=1)).isoformat()), "HYBRID_WITNESS_STALE"),
    (lambda value: value.update(active_conversations=["foreign"]), "HYBRID_WITNESS_INVALID"),
    (lambda value: value.update(global_revision=11), "GLOBAL_CONTROL_WITNESS_MISMATCH"),
    (lambda value: value.update(global_revision=True), "HYBRID_WITNESS_INVALID"),
    (lambda value: value["session"].update(state="cleanup_required", cleanup_required=True,
        purpose="observe", worker_epoch=str(uuid4())), "WORKER_CLEANUP_REQUIRED"),
])
def test_same_run_fresh_witness_and_actual_scope_are_required(rig, mutation, reason):
    rig.state.witness_change = mutation
    assert SUPERVISOR.main(args(rig)) == 0
    report = active_report(rig)
    assert report["ready"] is False
    assert reason in report["driver_state_reasons"]


def test_global_pause_and_intent_pause_never_report_ready(rig):
    rig.state.witness_change = lambda value: value.update(paused=True)
    assert SUPERVISOR.main(args(rig)) == 0
    report = active_report(rig)
    assert report["state"] == report["driver_state"] == "paused"
    assert report["ready"] is False


def test_stale_successful_observation_does_not_get_renewed_by_new_witness(rig):
    now = datetime.now(UTC)
    with sqlite3.connect(rig.data / "runtime.sqlite3") as db:
        db.execute("UPDATE runtime_conversations SET last_observed_at=? WHERE conversation_id='conversation-1'",
            ((now-timedelta(seconds=100)).isoformat(),))
    metrics = SUPERVISOR._hybrid_metrics(rig.data, (now-timedelta(seconds=110)).isoformat(), (rig.target,))
    assert metrics["this_run_active_unpaused_observed_count"] == 1
    assert metrics["fresh_active_unpaused_observed_count"] == 0
    witness = {"global_revision":12, "paused":False, "session":{"state":"idle", "cleanup_required":False}}
    assert SUPERVISOR._hybrid_driver_status(metrics, witness, []) == ("unknown", ["SUCCESSFUL_OBSERVE_STALE"])


def test_failed_incomplete_observation_timestamp_is_not_success(rig):
    with sqlite3.connect(rig.data / "runtime.sqlite3") as db:
        db.execute("UPDATE runtime_conversations SET last_observed_at=?,paused=1,pause_reason='history_gap' "
            "WHERE conversation_id='conversation-1'", (datetime.now(UTC).isoformat(),))
    rig.state.observed = False
    assert SUPERVISOR.main(args(rig)) == 0
    report = active_report(rig)
    assert report["ready"] is False and report["driver_state_reasons"] == ["CONVERSATION_PAUSED"]


def test_prior_run_stop_ack_cannot_mark_new_child_gracefully_stopped(rig):
    rig.state.acknowledgement_run = str(uuid4())
    assert SUPERVISOR.main(args(rig)) == 2
    final = rig.state.reports[-1]
    assert final["state"] == "stopped" and final["succeeded"] is False
    assert final["runtime_control"] is None


def test_observation_from_before_supervisor_start_cannot_make_new_run_ready(rig):
    rig.state.observed = False
    with sqlite3.connect(rig.data / "runtime.sqlite3") as db:
        db.execute("UPDATE runtime_conversations SET last_observed_at=? WHERE conversation_id='conversation-1'",
            ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
    assert SUPERVISOR.main(args(rig)) == 0
    assert active_report(rig)["ready"] is False
    assert active_report(rig)["metrics"]["this_run_active_unpaused_observed_count"] == 0


def test_fixed_settings_digest_is_forwarded_even_if_source_file_changes_after_capture(rig, monkeypatch):
    command = args(rig)
    digest = command[-1]
    real_snapshot = SUPERVISOR._hybrid_settings_snapshot
    def changed(*args, **kwargs):
        result = real_snapshot(*args, **kwargs)
        rig.settings.write_bytes(rig.settings.read_bytes()+b"\n")
        return result
    monkeypatch.setattr(SUPERVISOR, "_hybrid_settings_snapshot", changed)
    assert SUPERVISOR.main(command) == 0
    forwarded = rig.state.commands[0]
    assert forwarded[forwarded.index("--expected-hybrid-settings-sha256")+1] == digest
    assert hashlib.sha256(rig.settings.read_bytes()).hexdigest() != digest
