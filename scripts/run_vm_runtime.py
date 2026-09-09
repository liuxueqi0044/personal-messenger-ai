"""Production guest entry point; ``--check`` has no worker/API side effects."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelectorPack
from messenger_ai.adapters.qq.vm_driver import QQVMDriverBridge, QQVMWorkerProcess
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
from messenger_ai.domain import Platform
from messenger_ai.llm.deepseek import DeepSeekResponsesProvider
from messenger_ai.memory import Contact, IdentityBinding
from messenger_ai.observability import WindowsDPAPISecretStore
from messenger_ai.policy import CapabilitySnapshot
from messenger_ai.runtime.assembly import assemble_runtime
from messenger_ai.runtime.webui_projection import RuntimeWebUIProjection
from messenger_ai.webui import LiveHubFacade, create_app


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read runtime config: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "pmai-v5-runtime-1":
        raise ValueError("unsupported or missing runtime config schema")
    return value


def validate_config(config: dict[str, Any], *, api_key: str | None) -> tuple[QQSelectorPack, tuple[QQIdentityBinding, ...]]:
    contacts = config.get("contacts")
    if not config.get("data_dir"):
        raise ValueError("data_dir is required")
    if not isinstance(contacts, list) or not contacts:
        raise ValueError("at least one explicitly configured contact is required")
    if any(item.get("rulepack_status") != "active" for item in contacts):
        raise ValueError("every configured contact must reference an already active RulePack")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required; configure it in the guest secret store")
    raw_pack = config.get("selector_pack")
    if not isinstance(raw_pack, dict):
        raise ValueError("selector_pack is required")
    pack = QQSelectorPack.model_validate(raw_pack)
    validate_guest_selector_pack(pack)
    raw_bindings = config.get("bindings") or [item.get("binding") for item in contacts]
    if not isinstance(raw_bindings, list) or len(raw_bindings) != len(contacts) or any(not isinstance(item, dict) for item in raw_bindings):
        raise ValueError("one explicit verified binding is required for every contact")
    bindings = tuple(QQIdentityBinding.model_validate(item) for item in raw_bindings)
    if len({item.hub_conversation_id for item in bindings}) != len(bindings):
        raise ValueError("binding conversation ids must be unique")
    if len({item.binding_id for item in bindings}) != len(bindings):
        raise ValueError("binding ids must be unique")
    configured_ids = {str(item.get("contact_id", "")) for item in contacts}
    if {item.contact_id for item in bindings} != configured_ids:
        raise ValueError("contacts and bindings must have the same contact ids")
    return pack, bindings


def validate_active_rules(config: dict[str, Any]) -> None:
    from messenger_ai.rules.service import AtomicRulePackStore
    path = Path(config["data_dir"]).expanduser() / "rules.sqlite3"
    store = AtomicRulePackStore(str(path))
    try:
        for item in config["contacts"]:
            try:
                store.resolve(str(item["contact_id"]))
            except Exception as exc:
                raise ValueError(f"no active M7 RulePack for {item['contact_id']}") from exc
    finally:
        store.connection.close()


def _capability(config: dict[str, Any], pack: QQSelectorPack) -> CapabilitySnapshot:
    raw = config.get("capability")
    if not isinstance(raw, dict):
        raise ValueError("capability evidence is required")
    capability = CapabilitySnapshot.model_validate(raw)
    if capability.environment_fingerprint != pack.environment_fingerprint:
        raise ValueError("capability evidence does not match selector pack environment")
    if capability.execution_mode.value != "guest_foreground":
        raise ValueError("production QQ runtime requires guest_foreground capability evidence")
    if capability.send_background.value != "unsupported" or capability.verify_background.value != "unsupported":
        raise ValueError("background send/verify must remain unsupported for guest runtime")
    if capability.send_guest_foreground.value != "supported" or capability.verify_guest_foreground.value != "supported":
        raise ValueError("guest send/verify evidence is missing")
    if not capability.healthy:
        raise ValueError("guest capability evidence is unhealthy")
    return capability


def build_runtime(config: dict[str, Any], *, api_key: str, authorization_signing_key: bytes):
    pack, bindings = validate_config(config, api_key=api_key)
    data_dir = Path(config["data_dir"]).expanduser(); data_dir.mkdir(parents=True, exist_ok=True)
    provider = DeepSeekResponsesProvider(api_key=api_key, model=config.get("model", "deepseek-v4-flash"), timeout_seconds=float(config.get("timeout_seconds", 30)))
    worker = QQVMWorkerProcess(pack, bindings)
    app_box: dict[str, Any] = {}
    def text_provider(command):
        app = app_box.get("app")
        row = app.hub.store.connection.execute("SELECT text FROM drafts WHERE draft_id=?", (str(command.draft_id),)).fetchone() if app else None
        return str(row["text"]) if row else ""
    bridge = QQVMDriverBridge(worker=worker, bindings=bindings, text_provider=text_provider, sqlite_path=data_dir / "qq-vm-bridge.sqlite3")
    try:
        capability = _capability(config, pack)
        app = assemble_runtime(data_dir=data_dir, planner_provider=provider, driver=bridge, capability=capability, authorization_signing_key=authorization_signing_key, model_concurrency=int(config.get("model_concurrency", 2)))
    except Exception:
        _close_unassembled(provider, worker, bridge)
        raise
    try:
        for item in bindings:
            try:
                resolved = app.rules.resolve(item.contact_id)
            except Exception as exc:
                raise ValueError(f"no active M7 RulePack for {item.contact_id}") from exc
            if not resolved.rulepack.version:
                raise ValueError(f"active M7 RulePack is incomplete for {item.contact_id}")
        for binding in bindings:
            state_row = app.state.connection.execute("SELECT account_id,contact_id,binding_revision FROM runtime_conversations WHERE conversation_id=?", (binding.hub_conversation_id,)).fetchone()
            if state_row is not None and (state_row["account_id"], state_row["contact_id"], int(state_row["binding_revision"])) != (binding.account_id, binding.contact_id, 1):
                raise ValueError(f"persisted binding changed for {binding.hub_conversation_id}; human rebind required")
            contact_row = app.memory.store.connection.execute("SELECT 1 FROM memory_contacts WHERE contact_id=?", (binding.contact_id,)).fetchone()
            if contact_row is None:
                app.memory.create_contact(Contact(contact_id=binding.contact_id, created_at=app.hub.now()))
            evidence = hashlib.sha256(binding.participant_signature.encode()).hexdigest()
            memory_row = app.memory.store.connection.execute("SELECT contact_id,evidence_hash FROM memory_bindings WHERE platform=? AND account_id=? AND conversation_id=?", (Platform.QQ.value, binding.account_id, binding.hub_conversation_id)).fetchone()
            if memory_row is not None and (memory_row["contact_id"], memory_row["evidence_hash"]) != (binding.contact_id, evidence):
                raise ValueError(f"persisted identity evidence changed for {binding.hub_conversation_id}; human rebind required")
            if memory_row is None:
                app.memory.bind_identity(IdentityBinding(contact_id=binding.contact_id, platform=Platform.QQ, account_id=binding.account_id, conversation_id=binding.hub_conversation_id, platform_evidence_hash=evidence, verified_by="configured-human-binding", verified_at=app.hub.now()))
            if state_row is None:
                app.state.register(account_id=binding.account_id, contact_id=binding.contact_id, conversation_id=binding.hub_conversation_id, binding_revision=1)
        app_box["app"] = app
        inherited_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            worker.start()
        finally:
            if inherited_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = inherited_key
        return app
    except Exception:
        _shutdown(app)
        raise


async def _serve(app, *, host: str, port: int) -> None:
    import uvicorn
    projection = RuntimeWebUIProjection(state=app.state, hub=app.hub, pacing=app.pacing, rules=app.rules, driver=app.driver)
    server = uvicorn.Server(uvicorn.Config(create_app(LiveHubFacade(projection)), host=host, port=port, log_level="info"))
    runtime_task = asyncio.create_task(app.run_forever())
    web_task = asyncio.create_task(server.serve())
    done, pending = await asyncio.wait({runtime_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
    try:
        for task in done:
            task.result()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in tuple(getattr(app, "_planning_tasks", ())):
            task.cancel()
        tasks = tuple(getattr(app, "_planning_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            getattr(app, "_planning_tasks", set()).clear()
        close_app = getattr(app, "aclose", None)
        if callable(close_app):
            await close_app()
        setattr(app, "_closed", True)


def _shutdown(app: Any) -> None:
    if getattr(app, "_closed", False):
        return
    close_app = getattr(app, "aclose", None)
    if callable(close_app):
        result = close_app()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        setattr(app, "_closed", True)
        return
    bridge = getattr(app, "driver", None)
    worker = getattr(bridge, "_worker", None)
    stop = getattr(worker, "stop", None)
    if callable(stop):
        stop()
    db = getattr(bridge, "_db", None)
    if db is not None:
        db.close()
    provider = getattr(app, "planner_provider", None)
    close_provider = getattr(provider, "aclose", None) or getattr(provider, "close", None)
    if callable(close_provider):
        result = close_provider()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    cursor = getattr(bridge, "_cursor", None)
    if cursor is not None and callable(getattr(cursor, "close", None)):
        cursor.close()
    auth_store = getattr(getattr(getattr(app, "due", None), "authorization", None), "_store", None)
    for obj in (auth_store, getattr(app, "state", None), getattr(getattr(app, "hub", None), "store", None), getattr(getattr(app, "memory", None), "store", None), getattr(app, "pacing", None), getattr(app, "rules", None)):
        close = getattr(obj, "close", None)
        if callable(close):
            close()
        elif obj is not None and hasattr(obj, "connection"):
            obj.connection.close()


def _close_unassembled(provider: Any, worker: Any, bridge: Any) -> None:
    """Release construction-time resources when assembly never returns an app."""
    stop = getattr(worker, "stop", None)
    if callable(stop):
        stop()
    close_provider = getattr(provider, "aclose", None) or getattr(provider, "close", None)
    if callable(close_provider):
        result = close_provider()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    close = getattr(bridge, "close", None)
    if callable(close):
        close()
    else:
        db = getattr(bridge, "_db", None)
        if db is not None:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Messenger AI V5 guest runtime")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true", help="validate without worker, API client, or WebUI")
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        vault = config.get("secret_vault")
        if not vault:
            raise ValueError("secret_vault is required; configure DPAPI secrets before startup")
        secrets = WindowsDPAPISecretStore(Path(vault))
        key = secrets.get_secret("deepseek.api_key").decode("utf-8")
        if args.check:
            pack, _ = validate_config(config, api_key=key)
            _capability(config, pack)
            validate_active_rules(config)
            print("configuration valid; no QQ login, send, or API call performed"); return 0
        app = build_runtime(config, api_key=key, authorization_signing_key=secrets.get_or_create_hmac_key("runtime.authorization.signing"))
        try:
            asyncio.run(_serve(app, host=args.web_host, port=args.web_port)); return 0
        finally:
            _shutdown(app)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"blocked: {exc}"); return 2


if __name__ == "__main__":
    raise SystemExit(main())
