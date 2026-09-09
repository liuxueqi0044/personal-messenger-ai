from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import types
from pathlib import Path

import pytest

from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore


SPEC = importlib.util.spec_from_file_location("vm_cli", Path(__file__).parents[2] / "scripts" / "run_vm_runtime.py")
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


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
    def __init__(self, _pack, _bindings): self.started = False; self.stopped = False; self.terminated = False; self._process = None; self.__class__.instances.append(self)
    def start(self): self.started = True
    def stop(self): self.stopped = True


class FakeBridge:
    instances = []
    def __init__(self, **kwargs):
        self._worker = kwargs["worker"]
        self._db = sqlite3.connect(":memory:")
        self.__class__.instances.append(self)
    async def observe_conversation(self, *args, **kwargs): raise AssertionError("not used in construction")
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
            "platform_conversation_id": "qq-1", "participant_signature": "proof-1", "binding_id": "bind-1"}}],
        "bindings": [{"hub_conversation_id": "hub-1", "contact_id": "contact-1", "account_id": "account-1", "platform_conversation_id": "qq-1", "participant_signature": "proof-1", "binding_id": "bind-1"}],
        "selector_pack": {"client_version": "qq", "environment_fingerprint": fingerprint, "last_verified_at": "2026-01-01T00:00:00Z", "fixture_suite_version": "q1", "selectors": [{"name": n, "control_type": "Pane"} for n in names]},
        "capability": {"capability_version": "cap", "environment_fingerprint": fingerprint, "send_background": "unsupported", "verify_background": "unsupported", "send_guest_foreground": "supported", "verify_guest_foreground": "supported", "healthy": True, "client_version": "qq", "execution_mode": "guest_foreground", "binding_revision": 1}
    }


def _write_config(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


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
    assert app.rules.resolve("contact-1").rulepack.version
    worker = FakeWorker.instances[-1]; assert worker.started
    cli._shutdown(app)
    assert worker.stopped
    assert FakeProvider.instances[-1].closed
    for connection in (app.state.connection, app.hub.store.connection, app.memory.store.connection, app.pacing.connection, app.rules.connection):
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


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
