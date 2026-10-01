from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
from messenger_ai.adapters.qq.vm_driver.bridge import QQVMDriverBridge
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore

SCRIPT = Path(__file__).parents[2] / "scripts" / "deployment" / "build_session_observed_runtime_guest.py"


def _load_module():
    name = "test_build_session_observed_runtime_guest_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _evidence(binding_id: str, row_digit: str) -> QQSessionObservedDirectIdentity:
    return QQSessionObservedDirectIdentity(
        binding_id=binding_id,
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version="9.9.20",
        selector_pack_version="session-pack-v1",
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=123,
        window_handle=456,
        process_started_at_100ns=789,
        vm_environment_fingerprint="a" * 64,
        selected_row_runtime_id_hash=row_digit * 64,
        header_digest=("c" if row_digit != "c" else "d") * 64,
    )


def _write_bootstrap(
    path: Path, evidence: QQSessionObservedDirectIdentity, *, latest_text: str | None = None
) -> None:
    value = {
        "schema": "pmai-qq-session-observed-bootstrap-v1",
        "run_id": str(uuid4()) if latest_text is not None else "audit-only",
        "started_at": "2026-09-11T00:00:00Z",
        "completed_at": "2026-09-11T00:00:01Z",
        "succeeded": True,
        "evidence": evidence.model_dump(mode="json"),
        "participant_signature": evidence.participant_signature,
    }
    if latest_text is not None:
        value["reader"] = {
            "succeeded": True,
            "bubble_count": 1,
            "bubbles": [{
                "ordinal": 0,
                "direction": "inbound",
                "text_length": len(latest_text),
                "text_sha256": hashlib.sha256(latest_text.encode()).hexdigest(),
            }],
        }
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_rules(path: Path) -> None:
    store = AtomicRulePackStore(str(path))
    draft = store.ingest(RuleSource(
        name="rules.yaml",
        content=(
            b"schema_version: 1\nrulepack_id: test\npersona: {}\n"
            b"required_behaviors: []\nprohibited_behaviors: []\n"
            b"escalation_rules: []\npacing: {}\ncontacts: {}\nexamples: {}\n"
        ),
    ))
    store.activate(
        draft.draft_id,
        HumanApproval(approver_id="test", reason="test"),
    )
    store.connection.close()


def _require_stable_signature(proof: QQSessionObservedDirectIdentity) -> None:
    if not hasattr(proof, "legacy_participant_signature"):
        pytest.skip("stable/legacy participant signature contract not installed yet")


class _Worker:
    def stop(self) -> None:
        pass


def test_bridge_cursor_is_not_consumed_until_conversation_row_exists(tmp_path, monkeypatch):
    module = _load_module()
    bridge = QQVMDriverBridge(
        worker=_Worker(), bindings=(), text_provider=lambda _command: "",
        sqlite_path=tmp_path / module.BRIDGE_DB.name,
    )
    try:
        cursor = Path(bridge._cursor.connection.execute("PRAGMA database_list").fetchone()[2])
        assert cursor.name == module.CURSOR_DB.name
        monkeypatch.setattr(module, "CURSOR_DB", cursor)
        assert module._bootstrap_consumed("qq-session-conversation-1") is False

        bridge._cursor.bootstrap_last_inbound_once(
            "qq-session-conversation-2", [{"direction": "inbound", "text": "other"}]
        )
        assert module._bootstrap_consumed("qq-session-conversation-1") is False

        bridge._cursor.bootstrap_last_inbound_once(
            "qq-session-conversation-1", [{"direction": "inbound", "text": "target"}]
        )
        assert module._bootstrap_consumed("qq-session-conversation-1") is True
    finally:
        bridge.close()


def test_second_contact_requires_flag_and_preserves_stable_mapping(tmp_path, monkeypatch):
    module = _load_module()
    data_root = tmp_path / "durable-account"
    output = tmp_path / "runtime.json"
    bootstrap1 = tmp_path / "bootstrap-1.json"
    bootstrap2 = tmp_path / "bootstrap-2.json"
    registration1 = data_root / "registered-session-scope.json"
    registration2 = data_root / "registered-session-scope-2.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"

    proof1 = _evidence("session-contact-1", "1")
    proof2 = _evidence("session-contact-2", "2")
    _write_bootstrap(bootstrap1, proof1)
    _write_bootstrap(bootstrap2, proof2)
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)

    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    monkeypatch.setattr(module, "CURSOR_DB", data_root / "qq-vm-bridge.sqlite3")
    monkeypatch.setattr(module, "RUNTIME_DB", data_root / "runtime.sqlite3")
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    monkeypatch.setattr(module, "CONTACTS", (
        module.ContactRegistration(1, bootstrap1, registration1),
        module.ContactRegistration(2, bootstrap2, registration2),
    ))

    assert module.main([]) == 0
    default_config = json.loads(output.read_text(encoding="utf-8"))
    assert default_config["data_dir"] == str(data_root)
    assert default_config["worker_timeout_seconds"] == 90
    assert default_config["prepare_write_reserve_seconds"] == 20
    assert [item["contact_id"] for item in default_config["bindings"]] == ["session-contact-1"]
    assert registration1.is_file()
    assert not registration2.exists()

    first_registration = json.loads(registration1.read_text(encoding="utf-8"))
    first_registration["registered_at"] = "audit metadata must not trigger rebind"
    registration1.write_text(json.dumps(first_registration), encoding="utf-8")

    assert module.main(["--include-contact-2"]) == 0
    expanded = json.loads(output.read_text(encoding="utf-8"))
    assert expanded["data_dir"] == str(data_root)
    assert [(item["account_id"], item["contact_id"], item["hub_conversation_id"])
            for item in expanded["bindings"]] == [
        ("qq-default-account", "session-contact-1", "qq-session-conversation-1"),
        ("qq-default-account", "session-contact-2", "qq-session-conversation-2"),
    ]
    assert registration2.is_file()
    assert expanded["bootstrap_last_inbound_once"] == [
        "qq-session-conversation-1", "qq-session-conversation-2"
    ]

    assert module.main([
        "--include-contact-2",
        "--visual-label", "1=联系人甲",
        "--visual-label", "2=联系人乙",
    ]) == 0
    visual = json.loads(output.read_text(encoding="utf-8"))["visual_selection"]
    assert visual == {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {"session-contact-1": "联系人甲", "session-contact-2": "联系人乙"},
        "min_confidence": 0.98,
        "timeout_seconds": 8,
    }


def test_visual_labels_are_complete_unique_and_bounded():
    module = _load_module()
    assert module._parse_visual_labels(["1=联系人甲", "2=联系人乙"]) == {
        1: "联系人甲", 2: "联系人乙"
    }
    for values in (["bad"], ["1="], ["1=联系人甲", "1=联系人丙"], ["0=联系人甲"]):
        with pytest.raises(ValueError):
            module._parse_visual_labels(values)


def test_explicit_additional_contact_adoption_is_scoped_and_idempotent(tmp_path, monkeypatch):
    module = _load_module()
    data_root = tmp_path / "durable-account"
    bootstrap_root = tmp_path / "bootstraps"
    bootstrap_root.mkdir()
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    monkeypatch.setattr(module, "BOOTSTRAP_ROOT", bootstrap_root)
    monkeypatch.setattr(module, "CURSOR_DB", data_root / "qq-vm-bridge.cursor.sqlite3")
    monkeypatch.setattr(module, "RUNTIME_DB", data_root / "runtime.sqlite3")
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    monkeypatch.setattr(module, "CONTACTS", tuple(
        module.ContactRegistration(
            index,
            bootstrap_root / f"qq-session-observed-bootstrap-{index}.json",
            data_root / ("registered-session-scope.json" if index == 1 else f"registered-session-scope-{index}.json"),
        )
        for index in (1, 2)
    ))
    for index in (1, 2, 3):
        _write_bootstrap(
            bootstrap_root / f"qq-session-observed-bootstrap-{index}.json",
            _evidence(f"session-contact-{index}", str(index)),
            latest_text="你好，你好" if index == 3 else None,
        )
    assert module.main(["--include-contact-2"]) == 0
    registration1 = data_root / "registered-session-scope.json"
    registration2 = data_root / "registered-session-scope-2.json"
    preserved = (registration1.read_bytes(), registration2.read_bytes())
    with sqlite3.connect(module.CURSOR_DB) as db:
        db.execute("CREATE TABLE cursor_state(conversation_id TEXT PRIMARY KEY,next_seq INTEGER,snapshot_json TEXT)")
        db.executemany("INSERT INTO cursor_state VALUES(?,1,'[]')", [
            ("qq-session-conversation-1",), ("qq-session-conversation-2",),
        ])

    arguments = [
        "--include-contact-2", "--additional-contact-index", "3",
        "--adopt-latest-inbound-index", "3",
    ]
    assert module.main(arguments) == 0
    first = json.loads(output.read_text(encoding="utf-8"))
    registration3 = data_root / "registered-session-scope-3.json"
    registration_bytes = registration3.read_bytes()

    assert [item["contact_id"] for item in first["bindings"]] == [
        "session-contact-1", "session-contact-2", "session-contact-3",
    ]
    assert first["bootstrap_last_inbound_once"] == ["qq-session-conversation-3"]
    adoption = first["bootstrap_last_inbound_provenance"]["qq-session-conversation-3"]
    assert adoption["binding_id"] == "session-contact-3"
    assert adoption["bubble_count"] == 1 and adoption["last_ordinal"] == 0
    assert adoption["text_sha256"] == hashlib.sha256("你好，你好".encode()).hexdigest()
    assert "你好，你好" not in json.dumps(first, ensure_ascii=False)
    assert (registration1.read_bytes(), registration2.read_bytes()) == preserved

    # A retry after registration was atomically written but before config was
    # consumed must accept the exact same scope and adoption proof.
    assert module.main(arguments) == 0
    assert registration3.read_bytes() == registration_bytes

    with sqlite3.connect(module.CURSOR_DB) as db:
        db.execute(
            "INSERT INTO cursor_state VALUES(?,2,'[]')",
            ("qq-session-conversation-3",),
        )
    assert module.main(arguments) == 0
    consumed = json.loads(output.read_text(encoding="utf-8"))
    assert consumed["bootstrap_last_inbound_once"] == []
    assert consumed["bootstrap_last_inbound_provenance"] == {}


def test_isolated_recovery_generation_preserves_default_root(tmp_path, monkeypatch):
    module = _load_module()
    runtime_root = tmp_path / "runtime"
    default_root = runtime_root / module.ACCOUNT_ID
    default_root.mkdir(parents=True)
    marker = default_root / "existing-state.marker"
    marker.write_text("preserve", encoding="utf-8")
    bootstrap_root = tmp_path / "bootstraps"
    bootstrap_root.mkdir()
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(module, "BOOTSTRAP_ROOT", bootstrap_root)
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    for index in (1, 2, 3):
        _write_bootstrap(
            bootstrap_root / f"qq-session-observed-bootstrap-{index}.json",
            _evidence(f"session-contact-{index}", str(index)),
            latest_text=f"baseline-{index}",
        )

    generation_id = str(uuid4())
    build_args = [
        "--include-contact-2",
        "--additional-contact-index", "3",
        "--adopt-latest-inbound-index", "3",
        "--refresh-session-index", "1",
        "--refresh-session-index", "2",
        "--refresh-session-index", "3",
        "--isolated-recovery-generation", generation_id,
    ]
    assert module.main(build_args) == 0

    generation_root = (
        runtime_root / "recovery-generations" / generation_id / module.ACCOUNT_ID
    )
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config["data_dir"] == str(generation_root)
    generation = config["runtime_generation"]
    assert generation == {
        "schema": "pmai-isolated-runtime-generation-v1",
        "generation_id": generation_id,
        "mode": "isolated_identity_recovery",
        "enforce_global_pause": True,
        "manifest_sha256": generation["manifest_sha256"],
    }
    assert len(generation["manifest_sha256"]) == 64
    assert config["start_globally_paused"] is True
    assert config["bootstrap_last_inbound_once"] == [
        "qq-session-conversation-3",
    ]
    assert set(config["bootstrap_last_inbound_provenance"]) == {
        "qq-session-conversation-3",
    }
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert (generation_root / "registered-session-scope.json").is_file()
    assert (generation_root / "registered-session-scope-2.json").is_file()
    assert (generation_root / "registered-session-scope-3.json").is_file()
    manifest_path = generation_root / "generation-manifest.json"
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == generation[
        "manifest_sha256"
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["binding_id"] for item in manifest["contacts"]] == [
        "session-contact-1", "session-contact-2", "session-contact-3"
    ]
    by_binding = {item["binding_id"]: item for item in manifest["contacts"]}
    assert by_binding["session-contact-1"]["initial_adoption_sha256"] is None
    assert by_binding["session-contact-2"]["initial_adoption_sha256"] is None
    assert len(by_binding["session-contact-3"]["initial_adoption_sha256"]) == 64
    assert json.loads((generation_root / "runtime-config.json").read_text(
        encoding="utf-8"
    )) == config
    with sqlite3.connect(generation_root / "qq-vm-bridge.cursor.sqlite3") as db:
        db.execute(
            "CREATE TABLE cursor_state("
            "conversation_id TEXT PRIMARY KEY,last_sequence INTEGER,seen_keys_json TEXT)"
        )
        db.execute(
            "INSERT INTO cursor_state VALUES(?,2,'[]')",
            ("qq-session-conversation-3",),
        )
    assert module.main(build_args) == 0
    retried = json.loads(output.read_text(encoding="utf-8"))
    assert retried["bootstrap_last_inbound_once"] == [
        "qq-session-conversation-3"
    ]
    assert retried == config
    # A refresh rejected by the frozen manifest must not advance registrations.
    preserved = {path: path.read_bytes() for path in generation_root.glob("*.json")}
    preserved[output] = output.read_bytes()
    original_bootstraps = {path: path.read_bytes() for path in bootstrap_root.glob("*.json")}
    refreshed = _evidence("session-contact-1", "1").model_copy(update={
        "process_id": 999,
        "window_handle": 998,
        "process_started_at_100ns": 997,
    })
    for index in (1, 2, 3):
        proof = _evidence(f"session-contact-{index}", str(index)).model_copy(update={
            "process_id": refreshed.process_id,
            "window_handle": refreshed.window_handle,
            "process_started_at_100ns": refreshed.process_started_at_100ns,
        })
        _write_bootstrap(bootstrap_root / f"qq-session-observed-bootstrap-{index}.json", proof,
                         latest_text=f"baseline-{index}")
    with pytest.raises(RuntimeError, match="isolated generation manifest changed"):
        module.main(build_args)
    assert all(path.read_bytes() == before for path, before in preserved.items())
    for path, value in original_bootstraps.items():
        path.write_bytes(value)
    (generation_root / "rules.sqlite3").write_bytes(b"truncated")
    with pytest.raises(RuntimeError, match="RulePack database integrity"):
        module.main(build_args)


def test_candidate_rejection_rolls_back_new_generation(tmp_path, monkeypatch):
    module = _load_module()
    root = tmp_path / "runtime"
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({"environment_fingerprint": "a" * 64,
                                   "client_version": "9.9.20", "fixture_suite_version": "session-pack-v1"}))
    _write_rules(rules)
    for key, value in {"RUNTIME_ROOT": root, "BOOTSTRAP_ROOT": tmp_path, "OUTPUT": output,
                       "SOURCE_RULES": rules, "SELECTOR_PACK": selector}.items():
        monkeypatch.setattr(module, key, value)
    _write_bootstrap(tmp_path / "qq-session-observed-bootstrap-1.json", _evidence("session-contact-1", "1"),
                     latest_text="baseline-1")
    generation_id = str(uuid4())
    args = ["--isolated-recovery-generation", generation_id]
    def reject(*args):
        raise RuntimeError("candidate rejected")
    monkeypatch.setattr(module, "_validate_frozen_candidate", reject)
    with pytest.raises(RuntimeError, match="candidate rejected"):
        module.main(args)
    generation_root = root / "recovery-generations" / generation_id / module.ACCOUNT_ID
    assert list(generation_root.iterdir()) == []
    assert not output.exists()
    monkeypatch.setattr(module, "_validate_frozen_candidate", lambda *args: None)
    assert module.main(args) == 0


def test_isolated_recovery_generation_requires_uuid():
    module = _load_module()
    with pytest.raises(SystemExit):
        module.main(["--isolated-recovery-generation", "not-a-uuid"])


def test_isolated_recovery_requires_previous_generation_pause(tmp_path, monkeypatch):
    module = _load_module()
    runtime_root = tmp_path / "runtime"
    default_root = runtime_root / module.ACCOUNT_ID
    default_root.mkdir(parents=True)
    with sqlite3.connect(default_root / "runtime.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE runtime_global_control(
              singleton INTEGER PRIMARY KEY, revision INTEGER, paused INTEGER, reason TEXT
            );
            INSERT INTO runtime_global_control VALUES(1,1,0,NULL);
        """)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime_root)

    with pytest.raises(RuntimeError, match="must be globally paused"):
        module.main(["--isolated-recovery-generation", str(uuid4())])


def test_isolated_recovery_checks_published_predecessor_generation(
    tmp_path, monkeypatch
) -> None:
    module = _load_module()
    runtime_root = tmp_path / "runtime"
    prior_id = str(uuid4())
    prior_root = (
        runtime_root / "recovery-generations" / prior_id / module.ACCOUNT_ID
    )
    prior_root.mkdir(parents=True)
    with sqlite3.connect(prior_root / "runtime.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE runtime_global_control(
              singleton INTEGER PRIMARY KEY, revision INTEGER, paused INTEGER, reason TEXT
            );
            INSERT INTO runtime_global_control VALUES(1,1,0,NULL);
        """)
    output = tmp_path / "runtime.json"
    output.write_text(json.dumps({
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(prior_root),
        "runtime_generation": {"generation_id": prior_id},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(module, "OUTPUT", output)

    with pytest.raises(RuntimeError, match="must be globally paused"):
        module.main(["--isolated-recovery-generation", str(uuid4())])


def test_isolated_generation_rejects_preexisting_hardlinked_file(
    tmp_path, monkeypatch
) -> None:
    module = _load_module()
    runtime_root = tmp_path / "runtime"
    generation_id = str(uuid4())
    generation_root = (
        runtime_root / "recovery-generations" / generation_id / module.ACCOUNT_ID
    )
    generation_root.mkdir(parents=True)
    source = tmp_path / "outside.sqlite3"
    source.write_bytes(b"outside")
    (generation_root / "runtime.sqlite3").hardlink_to(source)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime_root)

    with pytest.raises(RuntimeError, match="must not be hard linked"):
        module.main(["--isolated-recovery-generation", generation_id])


def test_frozen_candidate_check_uses_exact_digest(tmp_path, monkeypatch) -> None:
    module = _load_module()
    runner = tmp_path / "run_vm_runtime.py"
    runner.write_text("# frozen runner", encoding="utf-8")
    config = tmp_path / "runtime-config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(tmp_path / "builder.py"))
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or Result(),
    )
    module._validate_frozen_candidate(config, "a" * 64)

    assert calls[0][0][-2:] == ["--expected-config-sha256", "a" * 64]


def test_refresh_replaces_only_session_evidence_and_preserves_adoption(tmp_path, monkeypatch):
    module = _load_module()
    data_root = tmp_path / "durable-account"
    bootstrap = tmp_path / "bootstrap-1.json"
    registration = data_root / "registered-session-scope.json"
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    monkeypatch.setattr(module, "BOOTSTRAP_ROOT", tmp_path)
    monkeypatch.setattr(module, "CURSOR_DB", data_root / "qq-vm-bridge.cursor.sqlite3")
    monkeypatch.setattr(module, "RUNTIME_DB", data_root / "runtime.sqlite3")
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    monkeypatch.setattr(module, "CONTACTS", (module.ContactRegistration(1, bootstrap, registration),))

    first = _evidence("session-contact-1", "1")
    _require_stable_signature(first)
    _write_bootstrap(bootstrap, first, latest_text="inbound")
    # Contact 1 uses the compatibility path; establish the initial registration.
    assert module.main([]) == 0
    original = json.loads(registration.read_text(encoding="utf-8"))
    old_adoption = original.get("initial_adoption")

    refreshed = first.model_copy(update={
        "process_id": 999,
        "window_handle": 998,
        "process_started_at_100ns": 997,
        "selected_row_runtime_id_hash": "2" * 64,
    })
    _write_bootstrap(bootstrap, refreshed)
    with pytest.raises(RuntimeError, match="explicit refresh required"):
        module.main([])
    assert module.main(["--refresh-session-index", "1"]) == 0
    updated = json.loads(registration.read_text(encoding="utf-8"))
    assert updated.get("initial_adoption") == old_adoption
    assert updated["session_evidence"]["process_id"] == 999
    config = json.loads(output.read_text(encoding="utf-8"))
    assert "session_identity_migrations" not in config


def test_refresh_rejects_stable_identity_change(tmp_path, monkeypatch):
    module = _load_module()
    data_root = tmp_path / "durable-account"
    bootstrap = tmp_path / "bootstrap-1.json"
    registration = data_root / "registered-session-scope.json"
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    monkeypatch.setattr(module, "BOOTSTRAP_ROOT", tmp_path)
    monkeypatch.setattr(module, "CURSOR_DB", data_root / "qq-vm-bridge.cursor.sqlite3")
    monkeypatch.setattr(module, "RUNTIME_DB", data_root / "runtime.sqlite3")
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    monkeypatch.setattr(module, "CONTACTS", (module.ContactRegistration(1, bootstrap, registration),))
    first = _evidence("session-contact-1", "1")
    _require_stable_signature(first)
    _write_bootstrap(bootstrap, first)
    assert module.main([]) == 0
    changed = first.model_copy(update={"header_digest": "e" * 64})
    _write_bootstrap(bootstrap, changed)
    with pytest.raises(RuntimeError, match="stable participant identity changed|explicit rebind"):
        module.main(["--refresh-session-index", "1"])
    with pytest.raises(RuntimeError, match="stable participant identity changed|explicit rebind"):
        module.main([
            "--refresh-session-index", "1",
            "--migrate-header-digest-index", "1",
        ])

def test_refresh_migrates_legacy_signature_and_retry_keeps_envelope(
        tmp_path, monkeypatch):
    module = _load_module()
    data_root = tmp_path / "durable-account"
    bootstrap = tmp_path / "bootstrap-1.json"
    registration = data_root / "registered-session-scope.json"
    output = tmp_path / "runtime.json"
    selector = tmp_path / "selector.json"
    rules = tmp_path / "rules.sqlite3"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    _write_rules(rules)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    monkeypatch.setattr(module, "BOOTSTRAP_ROOT", tmp_path)
    monkeypatch.setattr(module, "CURSOR_DB", data_root / "qq-vm-bridge.cursor.sqlite3")
    monkeypatch.setattr(module, "RUNTIME_DB", data_root / "runtime.sqlite3")
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "SOURCE_RULES", rules)
    monkeypatch.setattr(module, "SELECTOR_PACK", selector)
    monkeypatch.setattr(
        module, "CONTACTS", (module.ContactRegistration(1, bootstrap, registration),)
    )

    old_proof = _evidence("session-contact-1", "1")
    _write_bootstrap(bootstrap, old_proof)
    assert module.main([]) == 0

    old_registration = json.loads(registration.read_text(encoding="utf-8"))
    old_registration["participant_signature"] = old_proof.legacy_participant_signature
    old_registration.pop("stable_participant_signature", None)
    registration.write_text(json.dumps(old_registration), encoding="utf-8")

    restarted = old_proof.model_copy(update={
        "process_id": 901,
        "window_handle": 902,
        "process_started_at_100ns": 903,
        "selected_row_runtime_id_hash": "9" * 64,
        "header_digest": "e" * 64,
    })
    _write_bootstrap(bootstrap, restarted)
    with pytest.raises(RuntimeError, match="stable participant identity changed"):
        module.main(["--refresh-session-index", "1"])
    migration_args = [
        "--refresh-session-index", "1",
        "--migrate-header-digest-index", "1",
    ]
    assert module.main(migration_args) == 0

    migrated = json.loads(registration.read_text(encoding="utf-8"))
    envelope = migrated["session_identity_migration"]
    assert migrated["participant_signature"] == restarted.participant_signature
    assert envelope["previous_evidence_hash"] == hashlib.sha256(
        old_proof.legacy_participant_signature.encode("utf-8")
    ).hexdigest()
    assert envelope["current_evidence_hash"] == hashlib.sha256(
        restarted.participant_signature.encode("utf-8")
    ).hexdigest()
    assert json.loads(output.read_text(encoding="utf-8"))[
        "session_identity_migrations"
    ] == [envelope]

    assert module.main(migration_args) == 0
    assert json.loads(output.read_text(encoding="utf-8"))[
        "session_identity_migrations"
    ] == [envelope]

def test_process_session_refresh_requires_every_selected_contact(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "_existing_registration_indices", lambda: {1, 2})
    monkeypatch.setattr(module, "_existing_runtime_indices", lambda: set())
    with pytest.raises(SystemExit):
        module.main(["--refresh-session-index", "1"])


def test_refresh_accepts_adoption_signature_only_through_exact_migration_chain(
    tmp_path,
):
    module = _load_module()
    item = module.ContactRegistration(
        3,
        tmp_path / "bootstrap.json",
        tmp_path / "registration.json",
    )
    adoption_signature = "qq-profile-hmac:" + "1" * 64
    stored_signature = "qq-profile-hmac:" + "2" * 64
    current_signature = "qq-profile-hmac:" + "3" * 64
    existing = {
        "participant_signature": stored_signature,
        "initial_adoption": {"participant_signature": adoption_signature},
        "session_identity_migration": {
            "schema": "pmai-qq-session-identity-migration-v1",
            "binding_id": item.binding_id,
            "contact_id": item.contact_id,
            "account_id": module.ACCOUNT_ID,
            "conversation_id": item.conversation_id,
            "previous_evidence_hash": module._memory_evidence_hash(
                adoption_signature
            ),
            "current_evidence_hash": module._memory_evidence_hash(
                stored_signature
            ),
        },
    }

    allowed = module._refresh_allowed_adoption_signatures(
        item, existing, current_signature
    )
    assert set(allowed) == {
        adoption_signature,
        stored_signature,
        current_signature,
    }

    for field, value in (
        ("binding_id", "session-contact-9"),
        ("previous_evidence_hash", "a" * 64),
        ("current_evidence_hash", "b" * 64),
    ):
        tampered = json.loads(json.dumps(existing))
        tampered["session_identity_migration"][field] = value
        rejected = module._refresh_allowed_adoption_signatures(
            item, tampered, current_signature
        )
        assert adoption_signature not in rejected
        assert set(rejected) == {stored_signature, current_signature}
