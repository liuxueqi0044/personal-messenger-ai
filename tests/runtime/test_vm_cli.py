from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore

SPEC = importlib.util.spec_from_file_location("vm_cli", Path(__file__).parents[2] / "scripts" / "run_vm_runtime.py")
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def test_load_config_binds_the_single_read_to_expected_digest(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    payload = json.dumps({"schema": "pmai-v5-runtime-1"}).encode("utf-8")
    path.write_bytes(payload)

    assert cli.load_config(
        path, expected_sha256=hashlib.sha256(payload).hexdigest()
    )["schema"] == "pmai-v5-runtime-1"
    path.write_bytes(payload + b"\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        cli.load_config(path, expected_sha256=hashlib.sha256(payload).hexdigest())


class FakeSecrets:
    key = b"deepseek-test-key"
    signing = b"s" * 32
    def __init__(self, _path): pass
    def get_secret(self, name):
        if name != "deepseek.api_key":
            raise KeyError(name)
        return self.key
    def get_or_create_hmac_key(self, _name): return self.signing


class FakeProvider:
    instances = []
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)
    async def aclose(self): self.closed = True


class FakeWorker:
    instances = []
    def __init__(self, _pack, _bindings, session_evidence=(), run_id=None, **kwargs): self.session_evidence = session_evidence; self.run_id = run_id; self.kwargs = kwargs; self.started = False; self.stopped = False; self.terminated = False; self._process = None; self.__class__.instances.append(self)
    def start(self): self.started = True
    def stop(self): self.stopped = True


class FakeBridge:
    instances = []
    def __init__(self, **kwargs):
        self._worker = kwargs["worker"]
        self.bootstrap_last_inbound = kwargs.get("bootstrap_last_inbound", ())
        self.bootstrap_last_inbound_provenance = kwargs.get(
            "bootstrap_last_inbound_provenance", {}
        )
        self._db = sqlite3.connect(":memory:")
        self.__class__.instances.append(self)
    async def observe_conversation(self, *args, **kwargs): raise AssertionError("not used in construction")
    def probe_health(self):
        return types.SimpleNamespace(status=cli.WorkerStatus.OK, error_code=None)

    def health(self):
        return types.SimpleNamespace(status=cli.WorkerStatus.OK, error_code=None)
    async def aclose(self):
        self._worker.stop()
        self._db.close()


def _config(tmp_path: Path) -> dict:
    fingerprint = "0" * 64
    names = ["main_window", "conversations", "conversation_item", "composer", "send", "bubbles"]
    return {
        "schema": "pmai-v5-runtime-1", "data_dir": str(tmp_path / "data"), "secret_vault": str(tmp_path / "vault"),
        "contacts": [{"contact_id": "contact-1", "rulepack_status": "active", "binding": {
            "hub_conversation_id": "hub-1", "contact_id": "contact-1", "account_id": "account-1",
            "platform_conversation_id": "qq-1", "participant_signature": "proof-1", "binding_id": "bind-1",
            "conversation_type": "direct", "friendship_verified": True, "authorization_scope": "all_direct_including_temporary"}}],
        "bindings": [{"hub_conversation_id": "hub-1", "contact_id": "contact-1", "account_id": "account-1", "platform_conversation_id": "qq-1", "participant_signature": "proof-1", "binding_id": "bind-1", "conversation_type": "direct", "friendship_verified": True, "authorization_scope": "all_direct_including_temporary"}],
        "selector_pack": {"client_version": "qq", "environment_fingerprint": fingerprint, "last_verified_at": "2026-01-01T00:00:00Z", "fixture_suite_version": "q1", "selectors": [{"name": n, "control_type": "Pane"} for n in names]},
        "capability": {"capability_version": "cap", "environment_fingerprint": fingerprint, "send_background": "unsupported", "verify_background": "unsupported", "send_guest_foreground": "supported", "verify_guest_foreground": "supported", "healthy": True, "client_version": "qq", "execution_mode": "guest_foreground", "binding_revision": 1}
    }


def _write_config(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_visual_selection_config_is_binding_complete_and_timeout_bounded(tmp_path):
    config = _config(tmp_path)
    config["visual_selection"] = {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {"bind-1": "联系人乙"},
        "min_confidence": 0.99,
        "timeout_seconds": 8,
    }
    _pack, bindings, _evidence = cli.validate_config(
        config, api_key="test-key"
    )
    visual = cli._validated_visual_selection(config, bindings)
    assert visual is not None
    assert visual.labels == {"bind-1": "联系人乙"}

    config["visual_selection"]["labels"] = {"wrong-binding": "联系人乙"}
    with pytest.raises(ValueError, match="match bindings one-to-one"):
        cli.validate_config(config, api_key="test-key")


def test_visual_selection_rejects_text_model_and_timeout_without_retirement_budget(tmp_path):
    config = _config(tmp_path)
    config["visual_selection"] = {
        "model": "deepseek-v4-flash",
        "labels": {"bind-1": "联系人乙"},
    }
    with pytest.raises(ValueError, match="visual_selection is invalid"):
        cli.validate_config(config, api_key="test-key")

    config["visual_selection"] = {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {"bind-1": "联系人乙"},
        "timeout_seconds": 12,
    }
    with pytest.raises(ValueError, match="leave four seconds"):
        cli.validate_config(config, api_key="test-key")

    config["visual_selection"]["timeout_seconds"] = 8
    config["model"] = "deepseek-v4-flash-vision-exp"
    with pytest.raises(ValueError, match="reserved for contact selection"):
        cli.validate_config(config, api_key="test-key")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("conversation_type", "unknown", "direct conversation type"),
        ("conversation_type", "group", "direct conversation type"),
        ("authorization_scope", "legacy_explicit_contacts", "all-direct authorization scope"),
        ("participant_signature", "uncertified:qq-1", "uncertified participant"),
    ],
)
def test_validate_config_rejects_non_production_binding_evidence(tmp_path, field, value, message):
    cfg = _config(tmp_path)
    cfg["bindings"][0][field] = value
    cfg["contacts"][0]["binding"][field] = value
    with pytest.raises(ValueError, match=message):
        cli.validate_config(cfg, api_key="key")


def test_capability_client_version_must_match_selector_pack(tmp_path):
    cfg = _config(tmp_path)
    pack, _, _ = cli.validate_config(cfg, api_key="key")
    cfg["capability"]["client_version"] = "QQ-other-build"
    with pytest.raises(ValueError, match="client version"):
        cli._capability(cfg, pack)


def _session_evidence(cfg):
    b = cfg["bindings"][0]
    return {"binding_id": b["binding_id"], "conversation_type": "direct", "type_evidence_source": "operator_observed_direct", "client_version": "qq", "selector_pack_version": "q1", "group_marker_probe_complete": True, "group_marker_count": 0, "process_id": 1, "window_handle": 2, "process_started_at_100ns": 3, "vm_environment_fingerprint": "0" * 64, "selected_row_runtime_id_hash": "1" * 64, "header_digest": "2" * 64}


def test_session_observed_identity_passes_to_worker(tmp_path, monkeypatch):
    _patch_external(monkeypatch); cfg = _config(tmp_path); cfg["identity_mode"] = "session_observed_direct"; proof = _session_evidence(cfg); cfg["session_observed_evidence"] = [proof]
    from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
    signature = QQSessionObservedDirectIdentity.model_validate(proof).participant_signature
    for row in (cfg["bindings"][0], cfg["contacts"][0]["binding"]):
        row["participant_signature"] = signature; row["platform_conversation_id"] = "runtime:" + proof["selected_row_runtime_id_hash"]
    _activate_rule(Path(cfg["data_dir"]))
    app = cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert len(FakeWorker.instances[-1].session_evidence) == 1
    assert FakeBridge.instances[-1].bootstrap_last_inbound == ()
    cli._shutdown(app)


def test_session_observed_identity_rejects_wrong_binding(tmp_path):
    cfg = _config(tmp_path); cfg["identity_mode"] = "session_observed_direct"; item = _session_evidence(cfg); item["binding_id"] = "other"; cfg["session_observed_evidence"] = [item]
    with pytest.raises(ValueError, match="one-to-one"):
        cli.validate_config(cfg, api_key="key")


def test_session_adoption_provenance_is_bound_to_evidence_and_bubble_shape(tmp_path):
    cfg = _config(tmp_path)
    cfg["identity_mode"] = "session_observed_direct"
    raw_proof = _session_evidence(cfg)
    from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
    proof = QQSessionObservedDirectIdentity.model_validate(raw_proof)
    for row in (cfg["bindings"][0], cfg["contacts"][0]["binding"]):
        row["participant_signature"] = proof.participant_signature
        row["platform_conversation_id"] = "runtime:" + proof.selected_row_runtime_id_hash
    cfg["session_observed_evidence"] = [raw_proof]
    cfg["bootstrap_last_inbound_once"] = ["hub-1"]
    provenance = {
        "schema": "pmai-qq-bootstrap-last-inbound-adoption-v1",
        "bootstrap_run_id": str(uuid4()),
        "captured_at": datetime.now(UTC).isoformat(),
        "text_sha256": "a" * 64,
        "bubble_count": 1,
        "last_ordinal": 0,
        "last_direction": "inbound",
        "binding_id": "bind-1",
        "participant_signature": proof.participant_signature,
        "process_id": proof.process_id,
        "window_handle": proof.window_handle,
        "process_started_at_100ns": proof.process_started_at_100ns,
        "selected_row_runtime_id_hash": proof.selected_row_runtime_id_hash,
        "client_version": proof.client_version,
        "selector_pack_version": proof.selector_pack_version,
    }
    cfg["bootstrap_last_inbound_provenance"] = {"hub-1": provenance}

    _, bindings, evidence = cli.validate_config(cfg, api_key="key")
    assert len(bindings) == len(evidence) == 1

    provenance["last_ordinal"] = 1
    with pytest.raises(ValueError, match="does not match session binding"):
        cli.validate_config(cfg, api_key="key")


def _session_config_with_migration(tmp_path):
    cfg = _config(tmp_path)
    cfg["identity_mode"] = "session_observed_direct"
    raw_proof = _session_evidence(cfg)
    from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
    proof = QQSessionObservedDirectIdentity.model_validate(raw_proof)
    for row in (cfg["bindings"][0], cfg["contacts"][0]["binding"]):
        row["participant_signature"] = proof.participant_signature
        row["platform_conversation_id"] = (
            "runtime:" + proof.selected_row_runtime_id_hash
        )
    cfg["session_observed_evidence"] = [raw_proof]
    previous_hash = hashlib.sha256(
        proof.legacy_participant_signature.encode("utf-8")
    ).hexdigest()
    current_hash = hashlib.sha256(
        proof.participant_signature.encode("utf-8")
    ).hexdigest()
    cfg["session_identity_migrations"] = [{
        "schema": cli.SESSION_IDENTITY_MIGRATION_SCHEMA,
        "binding_id": "bind-1",
        "contact_id": "contact-1",
        "account_id": "account-1",
        "conversation_id": "hub-1",
        "previous_evidence_hash": previous_hash,
        "current_evidence_hash": current_hash,
    }]
    return cfg, proof, previous_hash, current_hash


def test_session_identity_migration_is_narrow_and_validated(tmp_path):
    cfg, _proof, _previous_hash, _current_hash = (
        _session_config_with_migration(tmp_path)
    )
    cli.validate_config(cfg, api_key="key")

    cfg["session_identity_migrations"][0]["contact_id"] = "other"
    with pytest.raises(ValueError, match="cannot change durable identity"):
        cli.validate_config(cfg, api_key="key")


def test_build_runtime_migrates_legacy_evidence_once_and_is_idempotent(
        tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg, proof, previous_hash, current_hash = _session_config_with_migration(tmp_path)
    _activate_rule(Path(cfg["data_dir"]))

    # Seed the exact legacy evidence that a pre-migration runtime persisted.
    from messenger_ai.memory import Contact, IdentityBinding
    from messenger_ai.memory.service import MemoryService
    from messenger_ai.memory.store import SQLiteMemoryStore
    from messenger_ai.domain import Platform

    store = SQLiteMemoryStore(Path(cfg["data_dir"]) / "memory.sqlite3")
    memory = MemoryService(store)
    now = datetime.now(UTC)
    memory.create_contact(Contact(contact_id="contact-1", created_at=now))
    memory.bind_identity(IdentityBinding(
        contact_id="contact-1",
        platform=Platform.QQ,
        account_id="account-1",
        conversation_id="hub-1",
        platform_evidence_hash=previous_hash,
        verified_by="pre-migration-bootstrap",
        verified_at=now,
    ))
    store.connection.close()

    app = cli.build_runtime(
        cfg, api_key="key", authorization_signing_key=b"a" * 32
    )
    row = app.memory.store.connection.execute(
        "SELECT contact_id,evidence_hash FROM memory_bindings "
        "WHERE platform=? AND account_id=? AND conversation_id=?",
        ("qq", "account-1", "hub-1"),
    ).fetchone()
    assert (row["contact_id"], row["evidence_hash"]) == (
        "contact-1", current_hash
    )
    assert app.memory.store.connection.execute(
        "SELECT COUNT(*) FROM memory_audit "
        "WHERE action='identity.evidence_changed'"
    ).fetchone()[0] == 1
    cli._shutdown(app)

    # Keeping the migration envelope is safe after the evidence is current.
    second = cli.build_runtime(
        cfg, api_key="key", authorization_signing_key=b"a" * 32
    )
    assert second.memory.store.connection.execute(
        "SELECT evidence_hash FROM memory_bindings WHERE conversation_id='hub-1'"
    ).fetchone()[0] == current_hash
    assert second.memory.store.connection.execute(
        "SELECT COUNT(*) FROM memory_audit "
        "WHERE action='identity.evidence_changed'"
    ).fetchone()[0] == 1
    assert proof.participant_signature != proof.legacy_participant_signature
    cli._shutdown(second)


def test_build_runtime_rejects_evidence_change_without_matching_migration(
        tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg, _proof, previous_hash, _current_hash = (
        _session_config_with_migration(tmp_path)
    )
    cfg.pop("session_identity_migrations")
    _activate_rule(Path(cfg["data_dir"]))

    from messenger_ai.memory import Contact, IdentityBinding
    from messenger_ai.memory.service import MemoryService
    from messenger_ai.memory.store import SQLiteMemoryStore
    from messenger_ai.domain import Platform

    store = SQLiteMemoryStore(Path(cfg["data_dir"]) / "memory.sqlite3")
    memory = MemoryService(store)
    now = datetime.now(UTC)
    memory.create_contact(Contact(contact_id="contact-1", created_at=now))
    memory.bind_identity(IdentityBinding(
        contact_id="contact-1",
        platform=Platform.QQ,
        account_id="account-1",
        conversation_id="hub-1",
        platform_evidence_hash=previous_hash,
        verified_by="pre-migration-bootstrap",
        verified_at=now,
    ))
    store.connection.close()

    with pytest.raises(ValueError, match="human rebind required"):
        cli.build_runtime(
            cfg, api_key="key", authorization_signing_key=b"a" * 32
        )


def test_legacy_mode_remains_compatible(tmp_path):
    pack, bindings, evidence = cli.validate_config(_config(tmp_path), api_key="key")
    assert pack.client_version == "qq" and len(bindings) == 1 and evidence == ()


def test_content_policy_check_config_requires_a_boolean(tmp_path):
    cfg = _config(tmp_path)
    cfg["content_policy_checks_enabled"] = "false"

    with pytest.raises(ValueError, match="must be a boolean"):
        cli.validate_config(cfg, api_key="key")


def _isolated_generation_config(tmp_path: Path) -> dict:
    cli.TRUSTED_RUNTIME_ROOT = tmp_path
    cfg = _config(tmp_path)
    cfg["identity_mode"] = "session_observed_direct"
    raw_proof = _session_evidence(cfg)
    from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity

    proof = QQSessionObservedDirectIdentity.model_validate(raw_proof)
    for row in (cfg["bindings"][0], cfg["contacts"][0]["binding"]):
        row["participant_signature"] = proof.participant_signature
        row["platform_conversation_id"] = (
            "runtime:" + proof.selected_row_runtime_id_hash
        )
    cfg["session_observed_evidence"] = [raw_proof]
    generation_id = str(uuid4())
    cfg["data_dir"] = str(
        tmp_path / "recovery-generations" / generation_id / "qq-default-account"
    )
    manifest = {
        "schema": "pmai-isolated-runtime-generation-manifest-v1",
        "generation_id": generation_id,
        "mode": "isolated_identity_recovery",
        "account_id": "qq-default-account",
        "contacts": [{
            "binding_id": proof.binding_id,
            "account_id": cfg["bindings"][0]["account_id"],
            "contact_id": cfg["bindings"][0]["contact_id"],
            "conversation_id": cfg["bindings"][0]["hub_conversation_id"],
            "platform_conversation_id": cfg["bindings"][0]["platform_conversation_id"],
            "bootstrap_run_id": str(uuid4()),
            "evidence_sha256": hashlib.sha256(json.dumps(
                raw_proof, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "participant_evidence_sha256": hashlib.sha256(
                proof.participant_signature.encode("utf-8")
            ).hexdigest(),
            "initial_adoption_sha256": None,
        }],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    data_dir = Path(cfg["data_dir"])
    data_dir.mkdir(parents=True)
    (data_dir / "generation-manifest.json").write_bytes(manifest_bytes)
    cfg["runtime_generation"] = {
        "schema": "pmai-isolated-runtime-generation-v1",
        "generation_id": generation_id,
        "mode": "isolated_identity_recovery",
        "enforce_global_pause": True,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    cfg["start_globally_paused"] = True
    return cfg


def test_isolated_runtime_generation_is_path_bound_and_pause_required(tmp_path):
    cfg = _isolated_generation_config(tmp_path)
    generation_id = cfg["runtime_generation"]["generation_id"]
    cli.validate_config(cfg, api_key="key")

    cfg["start_globally_paused"] = False
    with pytest.raises(ValueError, match="isolated runtime generation is invalid"):
        cli.validate_config(cfg, api_key="key")
    cfg["start_globally_paused"] = True
    cfg["data_dir"] = str(tmp_path / "wrong" / generation_id / "qq-default-account")
    with pytest.raises(ValueError, match="data directory"):
        cli.validate_config(cfg, api_key="key")


def test_isolated_runtime_generation_binds_exact_adoption_scope(tmp_path):
    cfg = _isolated_generation_config(tmp_path)
    manifest_path = Path(cfg["data_dir"]) / "generation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contacts"][0]["initial_adoption_sha256"] = "a" * 64
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    cfg["runtime_generation"]["manifest_sha256"] = hashlib.sha256(
        manifest_bytes
    ).hexdigest()

    with pytest.raises(ValueError, match="adoption scope"):
        cli.validate_config(cfg, api_key="key")


def test_isolated_runtime_generation_rejects_reparse_components(
    tmp_path, monkeypatch
):
    cfg = _isolated_generation_config(tmp_path)
    monkeypatch.setattr(cli, "_has_reparse_component", lambda _path: True)
    with pytest.raises(ValueError, match="reparse point"):
        cli.validate_config(cfg, api_key="key")


def test_isolated_runtime_generation_rejects_linked_mutable_state(
    tmp_path, monkeypatch
) -> None:
    cfg = _isolated_generation_config(tmp_path)
    state = Path(cfg["data_dir"]) / "runtime.sqlite3"
    state.write_bytes(b"state")
    original = cli._has_reparse_component
    monkeypatch.setattr(
        cli,
        "_has_reparse_component",
        lambda path: path == state or original(path),
    )

    with pytest.raises(ValueError, match="state cannot use a reparse point"):
        cli.validate_config(cfg, api_key="key")


def _activate_rule(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    store = AtomicRulePackStore(str(data_dir / "rules.sqlite3"))
    draft = store.ingest(RuleSource(name="rules.yaml", content=b"schema_version: 1\nrulepack_id: test\npersona: {}\nrequired_behaviors: []\nprohibited_behaviors: []\nescalation_rules: []\npacing: {}\ncontacts: {}\nexamples: {}\n"))
    store.activate(draft.draft_id, HumanApproval(approver_id="test", reason="test"))
    store.connection.close()


def _patch_external(monkeypatch):
    FakeWorker.instances.clear()
    FakeBridge.instances.clear()
    FakeProvider.instances.clear()
    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", FakeSecrets)
    monkeypatch.setattr(cli, "DeepSeekResponsesProvider", FakeProvider)
    monkeypatch.setattr(cli, "QQVMWorkerProcess", FakeWorker)
    monkeypatch.setattr(cli, "QQVMDriverBridge", FakeBridge)


def test_build_runtime_uses_real_shared_sqlite_and_cleans_external_worker(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"])); app = cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert app.state.connection.execute("SELECT COUNT(*) FROM runtime_conversations").fetchone()[0] == 1
    assert app.state.connection.execute("SELECT conversation_type FROM runtime_conversations").fetchone()[0] == "direct"
    assert app.rules.resolve("contact-1").rulepack.version
    worker = FakeWorker.instances[-1]; assert worker.started
    cli._shutdown(app)
    assert worker.stopped
    assert FakeProvider.instances[-1].closed
    for connection in (app.state.connection, app.hub.store.connection, app.memory.store.connection, app.pacing.connection, app.rules.connection):
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_isolated_generation_starts_globally_paused(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _isolated_generation_config(tmp_path)
    _activate_rule(Path(cfg["data_dir"]))

    app = cli.build_runtime(
        cfg, api_key="key", authorization_signing_key=b"a" * 32
    )
    revision, paused, reason = app.state.global_control()
    assert revision == 1
    assert paused is True
    assert reason == "isolated_identity_recovery"
    cli._shutdown(app)


def test_isolated_pause_exists_before_bridge_recovery(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _isolated_generation_config(tmp_path)
    data_dir = Path(cfg["data_dir"])
    _activate_rule(data_dir)
    base = cli.QQVMDriverBridge

    class PauseWitnessBridge(base):
        def __init__(self, **kwargs):
            with sqlite3.connect(data_dir / "runtime.sqlite3") as connection:
                row = connection.execute(
                    "SELECT paused FROM runtime_global_control WHERE singleton=1"
                ).fetchone()
            assert row == (1,)
            super().__init__(**kwargs)

    monkeypatch.setattr(cli, "QQVMDriverBridge", PauseWitnessBridge)
    app = cli.build_runtime(
        cfg, api_key="key", authorization_signing_key=b"a" * 32
    )
    cli._shutdown(app)


def test_build_runtime_keeps_visual_selection_separate_from_reply_provider(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path)
    cfg["visual_selection"] = {
        "model": "deepseek-v4-flash-vision-exp",
        "labels": {"bind-1": "联系人乙"},
        "timeout_seconds": 8,
    }
    _activate_rule(Path(cfg["data_dir"]))

    app = cli.build_runtime(
        cfg,
        api_key="key",
        authorization_signing_key=b"a" * 32,
    )
    worker = FakeWorker.instances[-1]
    assert FakeProvider.instances[-1].kwargs["model"] == "deepseek-v4-flash"
    assert worker.kwargs["visual_selection"].model == "deepseek-v4-flash-vision-exp"
    assert worker.kwargs["visual_api_key"] == "key"
    cli._shutdown(app)


def test_build_runtime_rejects_existing_unknown_conversation_type(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path)
    _activate_rule(Path(cfg["data_dir"]))
    first = cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    cli._shutdown(first)
    state_db = Path(cfg["data_dir"]) / "runtime.sqlite3"
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE runtime_conversations SET conversation_type='unknown'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="re-authentication required"):
        cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)


def test_check_has_no_worker_or_provider_side_effects(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"])); path = tmp_path / "runtime.json"; _write_config(path, cfg)
    assert cli.main(["--config", str(path), "--check"]) == 0
    assert not FakeWorker.instances


def test_build_failure_closes_bridge_before_worker_start(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"]))
    monkeypatch.setattr(cli, "_capability", lambda *_: (_ for _ in ()).throw(ValueError("bad evidence")))
    with pytest.raises(ValueError, match="bad evidence"):
        cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert FakeBridge.instances
    with pytest.raises(sqlite3.ProgrammingError):
        FakeBridge.instances[-1]._db.execute("SELECT 1")
    assert FakeWorker.instances[-1].stopped
    assert FakeProvider.instances[-1].closed
    assert not FakeWorker.instances[-1].started


def test_post_assembly_rule_failure_cleans_all_resources(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path)
    # Assembly succeeds, then the real rules store is checked and rejects the contact.
    Path(cfg["data_dir"]).mkdir(parents=True)
    with pytest.raises(ValueError, match="RulePack"):
        cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert FakeWorker.instances[-1].stopped
    assert FakeProvider.instances[-1].closed
    with pytest.raises(sqlite3.ProgrammingError):
        FakeBridge.instances[-1]._db.execute("SELECT 1")


def test_worker_start_failure_cleans_assembled_application(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"]))
    def fail_start(self): raise RuntimeError("worker start failed")
    monkeypatch.setattr(FakeWorker, "start", fail_start)
    with pytest.raises(RuntimeError, match="worker start failed"):
        cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert FakeWorker.instances[-1].stopped
    assert FakeProvider.instances[-1].closed
    with pytest.raises(sqlite3.ProgrammingError):
        FakeBridge.instances[-1]._db.execute("SELECT 1")


def test_worker_health_failure_blocks_startup_and_cleans_resources(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"]))
    monkeypatch.setattr(
        FakeBridge,
        "probe_health",
        lambda _self: types.SimpleNamespace(
            status=cli.WorkerStatus.UNAVAILABLE,
            error_code="guest_machine_identity_unavailable",
        ),
    )
    with pytest.raises(RuntimeError, match="guest_machine_identity_unavailable"):
        cli.build_runtime(cfg, api_key="key", authorization_signing_key=b"a" * 32)
    assert FakeWorker.instances[-1].started
    assert FakeWorker.instances[-1].stopped
    assert FakeProvider.instances[-1].closed


def test_duplicate_runtime_owner_blocks_before_worker_construction(tmp_path, monkeypatch):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path); _activate_rule(Path(cfg["data_dir"])); path = tmp_path / "runtime.json"
    _write_config(path, cfg)

    class DuplicateOwner:
        closed = False
        def acquire(self): raise RuntimeError("QQ runtime is already running for this Windows user")
        def close(self): self.closed = True

    owner = DuplicateOwner()
    monkeypatch.setattr(cli, "QQRuntimeInstanceOwner", lambda: owner)
    assert cli.main(["--config", str(path), "--run-id", str(uuid4())]) == 2
    assert owner.closed
    assert not FakeWorker.instances


def test_runtime_stopped_assertion_uses_owner_without_config_or_secrets(monkeypatch):
    class Owner:
        acquired = False
        closed = False

        def acquire(self):
            self.acquired = True

        def close(self):
            self.closed = True

    owner = Owner()
    monkeypatch.setattr(cli, "QQRuntimeInstanceOwner", lambda: owner)
    assert cli.main(["--assert-runtime-stopped"]) == 0
    assert owner.acquired is True
    assert owner.closed is True


@pytest.mark.skipif(os.name != "nt", reason="production owner is a Windows named mutex")
def test_windows_runtime_owner_is_exclusive_and_released():
    scope = f"pytest-{uuid4()}"
    first = cli.QQRuntimeInstanceOwner(user_scope=scope)
    second = cli.QQRuntimeInstanceOwner(user_scope=scope)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


def test_serve_web_exit_cancels_runtime_and_closes_app(monkeypatch):
    class App:
        _planning_tasks = set()
        cancelled = False
        closed = False
        state = hub = pacing = rules = driver = object()
        async def run_forever(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        async def aclose(self): self.closed = True

    class Server:
        async def serve(self): return

    fake_uvicorn = types.SimpleNamespace(Config=lambda *a, **k: object(), Server=lambda *a, **k: Server())
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "RuntimeWebUIProjection", lambda **kwargs: object())
    monkeypatch.setattr(cli, "LiveHubFacade", lambda projection: object())
    monkeypatch.setattr(cli, "create_app", lambda facade: object())
    app = App()
    asyncio.run(cli._serve(app, host="127.0.0.1", port=0))
    assert app.cancelled and app.closed


def test_file_control_rejects_wrong_run_then_acknowledges_graceful_stop(tmp_path):
    run_id = str(uuid4())
    request_path = tmp_path / "control-request.json"
    result_path = tmp_path / "control-result.json"

    class App:
        calls = []

        async def set_global_pause_from_control(self, *, paused, reason):
            self.calls.append((paused, reason))

    async def wait_for_result(request_id):
        for _ in range(100):
            if result_path.exists():
                value = json.loads(result_path.read_text(encoding="utf-8"))
                if value["request_id"] == request_id:
                    return value
            await asyncio.sleep(.01)
        raise AssertionError("control result was not written")

    async def run():
        stop_event = asyncio.Event()
        app = App()
        task = asyncio.create_task(cli._watch_runtime_control(
            app,
            run_id=run_id,
            stop_event=stop_event,
            request_path=request_path,
            result_path=result_path,
            poll_interval=.01,
        ))
        wrong_request_id = str(uuid4())
        request_path.write_text(json.dumps({
            "schema": cli.CONTROL_REQUEST_SCHEMA,
            "request_id": wrong_request_id,
            "target_run_id": str(uuid4()),
            "action": "pause",
            "requested_at": datetime.now(UTC).isoformat(),
        }), encoding="utf-8")
        rejected = await wait_for_result(wrong_request_id)
        assert rejected["accepted"] is False
        assert rejected["state"] == "rejected"
        assert rejected["error_code"] == "TARGET_RUN_MISMATCH"
        assert not app.calls and not task.done()

        stop_request_id = str(uuid4())
        request_path.write_text(json.dumps({
            "schema": cli.CONTROL_REQUEST_SCHEMA,
            "request_id": stop_request_id,
            "target_run_id": run_id,
            "action": "graceful_stop",
            "requested_at": datetime.now(UTC).isoformat(),
        }), encoding="utf-8")
        assert await task == "graceful_stop"
        accepted = await wait_for_result(stop_request_id)
        assert accepted["accepted"] is True and accepted["state"] == "stopping"
        assert stop_event.is_set()
        assert app.calls and app.calls[-1][0] is True

    asyncio.run(run())


def test_worker_witness_requires_live_fresh_observation():
    now = datetime.now(UTC)

    class Driver:
        observation_freshness_seconds = 60.0
        snapshot = {
            "run_id": "run-1",
            "worker_process_id": 42,
            "worker_alive": True,
            "worker_exit_code": None,
            "parent_terminate_reason": None,
            "startup_health": {
                "kind": "health", "status": "ok", "completed_at": now.isoformat(),
            },
            "last_request": None,
            "last_successful_observe": {
                "request_id": "observe-1", "kind": "observe", "binding_id": "binding-1",
                "status": "ok", "completed_at": now.isoformat(),
            },
            "first_terminal_failure": None,
            "worker_generation": 2,
            "historical_terminal_failures": [{
                "request_id": "old-observe", "kind": "observe",
                "binding_id": "binding-old", "status": "uncertain",
                "error_code": "worker_timeout_isolated", "failed_generation": 1,
                "failed_run_id": "run-old",
                "completed_at": now.isoformat(), "bubbles": ["old secret"],
            }],
            "last_read_only_recovery": {
                "run_id": "run-old", "failed_generation": 1, "successor_generation": 2,
                "conversation_id": "conversation-old",
                "binding_id": "binding-old", "binding_revision": 3,
                "request_id": "old-observe", "status": "successor_active",
                "health_request_id": "health-2", "health_error_code": None,
                "created_at": now.isoformat(), "completed_at": now.isoformat(),
                "provider_request_json": "secret prompt",
            },
            "observation_quarantine_count": 1,
        }

        def worker_status_snapshot(self):
            return self.snapshot

    app = types.SimpleNamespace(driver=Driver())
    available = cli._worker_witness_payload(app, run_id="run-1")
    assert available["state"] == "available" and available["available"] is True
    assert available["observe_freshness_seconds"] == 60.0
    assert available["worker_generation"] == 2
    assert available["observation_quarantine_count"] == 1
    assert available["historical_terminal_failures"][0]["failed_generation"] == 1
    assert available["historical_terminal_failures"][0]["failed_run_id"] == "run-old"
    assert available["last_read_only_recovery"]["status"] == "successor_active"
    assert available["last_read_only_recovery"]["run_id"] == "run-old"
    assert "old secret" not in json.dumps(available)
    assert "secret prompt" not in json.dumps(available)

    app.driver.snapshot["last_request"] = {
        "request_id": "observe-2", "kind": "observe", "binding_id": "binding-1",
        "status": "failed_safe", "error_code": "worker_action_failed",
        "completed_at": now.isoformat(),
        "com_hresult": -2147418111,
        "project_frames": [{
            "function": "list_conversations",
            "file": "messenger_ai/adapters/qq/vm_driver/transport.py",
            "line": 700,
        }],
        "bubbles": ["sensitive text"],
    }
    failed = cli._worker_witness_payload(app, run_id="run-1")
    assert failed["state"] == "degraded"
    assert failed["reason_codes"] == ["WORKER_ACTION_FAILED"]
    assert failed["last_request"]["com_hresult"] == -2147418111
    assert "sensitive text" not in json.dumps(failed)
    app.driver.snapshot["last_request"] = None

    app.driver.snapshot["last_successful_observe"]["completed_at"] = "2000-01-01T00:00:00+00:00"
    stale = cli._worker_witness_payload(app, run_id="run-1")
    assert stale["state"] == "degraded" and stale["reason_codes"] == ["OBSERVE_STALE"]

    app.driver.snapshot["last_successful_observe"]["completed_at"] = (
        now + timedelta(seconds=1)
    ).isoformat()
    future = cli._worker_witness_payload(app, run_id="run-1")
    assert future["state"] == "degraded" and future["reason_codes"] == ["OBSERVE_STALE"]

    app.driver.snapshot["worker_alive"] = False
    app.driver.snapshot["worker_exit_code"] = -15
    app.driver.snapshot["first_terminal_failure"] = {
        "error_code": "worker_timeout_isolated", "kind": "observe",
        "completed_at": now.isoformat(),
    }
    dead = cli._worker_witness_payload(app, run_id="run-1")
    assert dead["state"] == "unavailable" and dead["available"] is False
    assert dead["reason_codes"] == ["WORKER_TIMEOUT_ISOLATED"]


def test_worker_witness_failure_is_reported_and_stops_serve(
        tmp_path, monkeypatch, capsys):
    run_id = str(uuid4())

    class Driver:
        observation_freshness_seconds = 60.0

        def worker_status_snapshot(self):
            return {
                "run_id": run_id,
                "worker_alive": True,
                "startup_health": None,
                "last_request": None,
                "last_successful_observe": None,
                "first_terminal_failure": None,
            }

    class App:
        _planning_tasks = set()
        state = hub = pacing = rules = object()
        driver = Driver()
        cancelled = False
        closed = False

        async def run_forever(self, *, stop_event):
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def set_global_pause_from_control(self, **_kwargs):
            return 1

        async def aclose(self):
            self.closed = True

    class Server:
        async def serve(self):
            await asyncio.Event().wait()

    fake_uvicorn = types.SimpleNamespace(
        Config=lambda *a, **k: object(), Server=lambda *a, **k: Server()
    )
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "RuntimeWebUIProjection", lambda **kwargs: object())
    monkeypatch.setattr(cli, "LiveHubFacade", lambda projection: object())
    monkeypatch.setattr(cli, "create_app", lambda facade: object())

    def fail_write(_path, _payload):
        raise OSError(5, "sensitive filesystem message")

    monkeypatch.setattr(cli, "_write_worker_witness", fail_write)
    app = App()
    with pytest.raises(OSError):
        asyncio.run(cli._serve(
            app,
            host="127.0.0.1",
            port=0,
            run_id=run_id,
            control_request_path=tmp_path / "control-request.json",
            control_result_path=tmp_path / "control-result.json",
            worker_status_path=tmp_path / "worker-status.json",
        ))

    assert app.cancelled and app.closed
    output = capsys.readouterr().out
    diagnostic = json.loads(output.strip())
    assert diagnostic["schema"] == "pmai-runtime-background-task-failure-v1"
    assert diagnostic["run_id"] == run_id
    assert diagnostic["component"] == "worker_status_publisher"
    assert diagnostic["exception_type"] == "OSError"
    assert diagnostic["errno"] == 5
    assert any(
        frame["file"] == "scripts/run_vm_runtime.py"
        for frame in diagnostic["project_frames"]
    )
    assert "sensitive filesystem message" not in output


def test_graceful_stop_keeps_worker_witness_clean(tmp_path, monkeypatch, capsys):
    run_id = str(uuid4())
    request_path = tmp_path / "control-request.json"
    result_path = tmp_path / "control-result.json"
    witness_path = tmp_path / "worker-status.json"
    request_path.write_text(json.dumps({
        "schema": cli.CONTROL_REQUEST_SCHEMA,
        "request_id": str(uuid4()),
        "target_run_id": run_id,
        "action": "graceful_stop",
        "requested_at": datetime.now(UTC).isoformat(),
    }), encoding="utf-8")

    class Driver:
        observation_freshness_seconds = 60.0

        def worker_status_snapshot(self):
            return {
                "run_id": run_id,
                "worker_alive": True,
                "startup_health": {
                    "kind": "health", "status": "ok",
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                "last_request": None,
                "last_successful_observe": None,
                "first_terminal_failure": None,
            }

    class App:
        _planning_tasks = set()
        state = hub = pacing = rules = object()
        driver = Driver()
        closed = False

        async def run_forever(self, *, stop_event):
            await stop_event.wait()

        async def set_global_pause_from_control(self, **_kwargs):
            return 2

        async def aclose(self):
            self.closed = True

    class Server:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(.001)

    server = Server()
    fake_uvicorn = types.SimpleNamespace(
        Config=lambda *a, **k: object(), Server=lambda *a, **k: server
    )
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "RuntimeWebUIProjection", lambda **kwargs: object())
    monkeypatch.setattr(cli, "LiveHubFacade", lambda projection: object())
    monkeypatch.setattr(cli, "create_app", lambda facade: object())

    app = App()
    asyncio.run(cli._serve(
        app,
        host="127.0.0.1",
        port=0,
        run_id=run_id,
        control_request_path=request_path,
        control_result_path=result_path,
        worker_status_path=witness_path,
    ))

    assert app.closed
    assert json.loads(witness_path.read_text(encoding="utf-8"))["state"] == "stopping"
    assert "pmai-runtime-background-task-failure-v1" not in capsys.readouterr().out


def test_graceful_stop_timeout_cancels_tasks_and_preserves_timeout(
        tmp_path, monkeypatch):
    run_id = str(uuid4())
    request_path = tmp_path / "control-request.json"
    result_path = tmp_path / "control-result.json"
    request_path.write_text(json.dumps({
        "schema": cli.CONTROL_REQUEST_SCHEMA,
        "request_id": str(uuid4()),
        "target_run_id": run_id,
        "action": "graceful_stop",
        "requested_at": datetime.now(UTC).isoformat(),
    }), encoding="utf-8")

    class App:
        _planning_tasks = set()
        state = hub = pacing = rules = driver = object()
        closed = False

        async def run_forever(self, *, stop_event):
            await stop_event.wait()

        async def set_global_pause_from_control(self, **_kwargs):
            return 2

        async def aclose(self):
            self.closed = True

    class StuckServer:
        should_exit = False

        async def serve(self):
            await asyncio.Event().wait()

    server = StuckServer()
    fake_uvicorn = types.SimpleNamespace(
        Config=lambda *a, **k: object(), Server=lambda *a, **k: server
    )
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "RuntimeWebUIProjection", lambda **kwargs: object())
    monkeypatch.setattr(cli, "LiveHubFacade", lambda projection: object())
    monkeypatch.setattr(cli, "create_app", lambda facade: object())
    monkeypatch.setattr(cli, "GRACEFUL_STOP_TIMEOUT_SECONDS", .01)

    app = App()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(cli._serve(
            app,
            host="127.0.0.1",
            port=0,
            run_id=run_id,
            control_request_path=request_path,
            control_result_path=result_path,
            worker_status_path=tmp_path / "worker-status.json",
        ))

    assert app.closed


@pytest.mark.parametrize("change,needle", [("key", "secret"), ("capability", "capability"), ("active", "RulePack")])
def test_check_blocks_missing_key_capability_or_active_rule(tmp_path, monkeypatch, change, needle):
    _patch_external(monkeypatch)
    cfg = _config(tmp_path)
    if change == "key":
        class Missing(FakeSecrets):
            def get_secret(self, _): raise ValueError("secret missing")
        monkeypatch.setattr(cli, "WindowsDPAPISecretStore", Missing)
    elif change == "capability":
        cfg.pop("capability")
    else:
        Path(cfg["data_dir"]).mkdir(parents=True)
    _activate_rule(Path(cfg["data_dir"])) if change != "active" else None
    path = tmp_path / f"{change}.json"; _write_config(path, cfg)
    assert cli.main(["--config", str(path), "--check"]) == 2
    assert not FakeWorker.instances
