"""Explicit V2 guest runner over an existing, unchanged business generation."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from run_vm_runtime import (
    QQRuntimeInstanceOwner, load_config, validate_config, validate_active_rules,
    _capability, _watch_runtime_control, _write_worker_witness,
    CONTROL_REQUEST_PATH, CONTROL_RESULT_PATH,
)
from messenger_ai.runtime.config_publication import assert_publication_complete, assert_data_publication_complete
from messenger_ai.runtime.session_revision import session_revision_number
from messenger_ai.adapters.qq.navigation.contracts import NavigationStatus
from messenger_ai.adapters.qq.navigation.provider import ResponsesVisionNavigator
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerStatus
from messenger_ai.adapters.qq.vm_driver.hybrid_bridge import QQHybridDriverBridge
from messenger_ai.adapters.qq.vm_driver.hybrid_session import HybridWorkerSession
from messenger_ai.domain import Platform
from messenger_ai.llm.deepseek import DeepSeekResponsesProvider
from messenger_ai.observability import WindowsDPAPISecretStore
from messenger_ai.runtime.assembly import assemble_runtime
from messenger_ai.runtime.contracts import ObservationBatch
from messenger_ai.runtime.navigation_state import NavigationTaskStore
from messenger_ai.runtime.qq_hybrid_config import parse_hybrid_settings
from messenger_ai.runtime.qq_hybrid_navigation import QQHybridNavigationService
from messenger_ai.runtime.qq_hybrid_rounds import QQHybridRoundFactory
from messenger_ai.runtime.qq_hybrid_scope import QQHybridRuntimeScope
from messenger_ai.runtime.qq_observation_recovery import QQHybridObservationRecovery


class NavigatingHybridDriver(QQHybridDriverBridge):
    navigation = None
    app_lookup = None
    navigation_resources = ()
    observation_recovery = None

    def observation_recovery_context(self, conversation_id, **revisions):
        if self.observation_recovery is None:
            return nullcontext()
        return self.observation_recovery.context(conversation_id, **revisions)

    async def observe_conversation(self, conversation_id, *, binding_revision, conversation_revision):
        binding = self._bindings[conversation_id]
        if self.has_cleanup_obligation(binding.account_id) or not self._cursor.has_snapshot(conversation_id):
            return await super().observe_conversation(conversation_id,
                binding_revision=binding_revision, conversation_revision=conversation_revision)
        app = self.app_lookup()
        br, cr, blocked, _gr, gp = app.state.one_shot_observation_execution_state(conversation_id)
        if (blocked or gp or app._pause_requested.is_set()
                or (br, cr) != (binding_revision, conversation_revision)):
            row = app.state.connection.execute(
                "SELECT pause_reason FROM runtime_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            return ObservationBatch(account_id=binding.account_id, contact_id=binding.contact_id,
                conversation_id=conversation_id, binding_revision=binding_revision,
                conversation_revision=conversation_revision, complete=False, messages=(),
                gap_reason=row["pause_reason"] or "driver_quarantine:observation_scope_changed")
        result = await self.navigation.navigate(binding.binding_id,
            pending_input_key=f"observe:{conversation_id}:{conversation_revision}",
            cancel_event=self.app_lookup()._pause_requested)
        if (result.outcome.status is not NavigationStatus.CANDIDATE_OPENED
                or result.active_chat_lease is None):
            return ObservationBatch(account_id=binding.account_id, contact_id=binding.contact_id,
                conversation_id=conversation_id, binding_revision=binding_revision,
                conversation_revision=conversation_revision, complete=False, messages=(),
                gap_reason="ui_automation_unavailable:" + (result.outcome.error_code or "navigation_incomplete"))
        return await super().observe_conversation(conversation_id,
            binding_revision=binding_revision, conversation_revision=conversation_revision)

    async def aclose(self):
        if self.navigation is not None:
            if await self.navigation.aclose() is False:
                raise RuntimeError("hybrid_navigation_cleanup_required")
        await super().aclose()
        for resource in self.navigation_resources:
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result


def read_settings(path, *, expected_sha256=None):
    payload = Path(path).read_bytes()
    if len(payload) > 262144:
        raise ValueError("hybrid settings exceed size bound")
    if expected_sha256 is not None and not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256):
        raise ValueError("hybrid settings digest mismatch")
    raw = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("hybrid settings must be an object")
    return raw


def existing_binding_revisions(config, bindings):
    path = Path(config["data_dir"]) / "runtime.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        result = {}
        for binding in bindings:
            row = db.execute("SELECT account_id,contact_id,binding_revision,conversation_type "
                "FROM runtime_conversations WHERE conversation_id=?", (binding.hub_conversation_id,)).fetchone()
            if (row is None or (row[0], row[1], row[3]) !=
                    (binding.account_id, binding.contact_id, "direct")):
                raise ValueError("V2 requires an unchanged existing business binding")
            result[binding.binding_id] = int(row[2])
        return result


def select_bindings(all_bindings, active_ids):
    if not active_ids or len(active_ids) != len(set(active_ids)):
        raise ValueError("explicit unique active binding IDs are required")
    selected = tuple(binding for binding in all_bindings if binding.binding_id in active_ids)
    if len(selected) != len(active_ids):
        raise ValueError("active binding is not registered")
    return selected


def validate_existing_memory(app, bindings):
    # The migration attaches a separate profile anchor. It never changes the
    # original M6 identity, RulePack, contact or cursor to make a probe pass.
    for binding in bindings:
        app.rules.resolve(binding.contact_id)
        row = app.memory.store.connection.execute("SELECT contact_id,evidence_hash FROM memory_bindings "
            "WHERE platform=? AND account_id=? AND conversation_id=?",
            (Platform.QQ.value, binding.account_id, binding.hub_conversation_id)).fetchone()
        if (row is None or row["contact_id"] != binding.contact_id
                or row["evidence_hash"] != hashlib.sha256(binding.participant_signature.encode()).hexdigest()):
            raise ValueError("V2 requires unchanged existing memory identity evidence")


def validate_inactive_queues(data_dir, bindings):
    active = tuple(binding.hub_conversation_id for binding in bindings)
    placeholders = ",".join("?" for _ in active)
    queries = {
        "runtime.sqlite3": [
            f"SELECT 1 FROM runtime_planning_jobs WHERE conversation_id NOT IN ({placeholders}) AND status IN ('pending','dispatching','running') LIMIT 1",
            f"SELECT 1 FROM runtime_event_outbox WHERE aggregate_id NOT IN ({placeholders}) AND aggregate_id!='global' AND status IN ('pending','dispatching') LIMIT 1"],
        "pacing.sqlite3": [
            f"SELECT 1 FROM m10_plans WHERE conversation_id NOT IN ({placeholders}) AND status IN ('waiting','due_for_revalidation') LIMIT 1",
            f"SELECT 1 FROM m10_due_outbox o JOIN m10_plans p USING(pacing_plan_id) WHERE p.conversation_id NOT IN ({placeholders}) AND o.status IN ('pending','dispatching','dispatching_nonrecoverable') LIMIT 1"],
    }
    for name, statements in queries.items():
        with sqlite3.connect((Path(data_dir) / name).as_uri() + "?mode=ro", uri=True) as db:
            if any(db.execute(query, active).fetchone() is not None for query in statements):
                raise ValueError("inactive binding has pending work; existing queues preserved")


async def build_runtime_v2(config, raw_settings, *, api_key, authorization_signing_key,
                           run_id, active_binding_ids):
    pack, all_bindings, _ = validate_config(config, api_key=api_key)
    bindings = select_bindings(all_bindings, active_binding_ids)
    revisions = existing_binding_revisions(config, bindings)
    settings = parse_hybrid_settings(raw_settings, selector_pack=pack, bindings=bindings,
        binding_revisions=revisions, run_id=run_id)
    if settings.session_epoch != str(session_revision_number(config)):
        raise ValueError("hybrid session epoch disagrees with the validated session revision")
    if Path(settings.vault_path) != Path(config["secret_vault"]):
        raise ValueError("hybrid vault disagrees with the validated deployment")
    data_dir = Path(config["data_dir"])
    assert_data_publication_complete(data_dir)
    validate_inactive_queues(data_dir, bindings)
    app_box, bridge_box = {}, {}
    scope = QQHybridRuntimeScope(app_lookup=lambda: app_box.get("app"),
        bridge_lookup=lambda: bridge_box.get("bridge"), settings=settings, run_id=run_id)
    shared_lock = asyncio.Lock()
    rounds = QQHybridRoundFactory(settings, pack, bindings, shared_lock, scope.snapshot)
    session = HybridWorkerSession(rounds, health_binding_id=bindings[0].binding_id)
    model = settings.navigation_model
    navigator = ResponsesVisionNavigator(api_key=api_key, **model.model_dump())
    navigation_store = NavigationTaskStore(data_dir / "qq-v2-navigation.sqlite3")
    navigation = QQHybridNavigationService(settings, navigator, navigation_store, shared_lock, scope.snapshot)
    provider = DeepSeekResponsesProvider(api_key=api_key, model=config.get("model", "deepseek-v4-flash"),
        timeout_seconds=float(config.get("timeout_seconds", 30)))
    def text_provider(command):
        row = app_box["app"].hub.store.connection.execute("SELECT text FROM drafts WHERE draft_id=?",
            (str(command.draft_id),)).fetchone()
        return str(row["text"]) if row else ""
    bridge = NavigatingHybridDriver(worker=session, bindings=bindings, text_provider=text_provider,
        sqlite_path=data_dir / "qq-vm-bridge.sqlite3", timeout_seconds=45,
        scope_guard=scope.request_is_current,
        expected_profile_signatures={key: "qq-profile-hmac:" + value.expected_profile_hmac
                                     for key, value in settings.expectations.items()},
        verification_round=session.verification_round, guard_refresh=rounds.refresh_guard)
    bridge_box["bridge"] = bridge
    bridge.navigation, bridge.app_lookup = navigation, lambda: app_box["app"]
    bridge.navigation_resources = (navigator, navigation_store)
    app = None
    try:
        app = assemble_runtime(data_dir=data_dir, planner_provider=provider, driver=bridge,
            capability=_capability(config, pack), authorization_signing_key=authorization_signing_key,
            model_concurrency=int(config.get("model_concurrency", 2)),
            content_policy_checks_enabled=config.get("content_policy_checks_enabled", True),
            recover_persistent_state=False, initially_paused=True,
            initial_pause_reason="hybrid_migration", navigation_preflight=navigation.due_preflight(),
            staged_preparation=bridge)
        app_box["app"] = app
        bridge.observation_recovery = QQHybridObservationRecovery(app=app, bridge=bridge, navigation=navigation)
        validate_existing_memory(app, all_bindings)
        health = await bridge.probe_health_async()
        if health.status is not WorkerStatus.OK:
            raise RuntimeError("hybrid_startup_health_failed")
        return app
    except BaseException:
        if app is not None:
            await app.aclose()
        else:
            await bridge.aclose()
            await provider.aclose()
        raise


async def serve_v2(app, *, host, port, run_id):
    # The V1 witness assumes a permanent child process. V2 deliberately retires
    # each finite child; report its actual session state in a separate schema.
    import uvicorn
    from messenger_ai.runtime.webui_projection import RuntimeWebUIProjection
    from messenger_ai.webui import LiveHubFacade, create_app
    projection = RuntimeWebUIProjection(state=app.state, hub=app.hub, pacing=app.pacing, rules=app.rules, driver=app.driver)
    server = uvicorn.Server(uvicorn.Config(create_app(LiveHubFacade(projection)), host=host, port=port, log_level="info"))
    stop = asyncio.Event()
    async def witness():
        while not stop.is_set():
            revision, paused, _ = app.state.global_control()
            _write_worker_witness(Path(configured_status_path(app)), {
                "schema": "qq_hybrid_runtime_status_v2", "run_id": run_id,
                "written_at": datetime.now(UTC).isoformat(), "global_revision": revision,
                "paused": paused or app._pause_requested.is_set(),
                "session": app.driver.worker_status_snapshot(),
                "active_conversations": list(app.driver.observation_conversation_ids),
                "startup_health": app.driver.health().status.value,
            })
            await asyncio.sleep(.5)
    tasks = [asyncio.create_task(app.run_forever(stop_event=stop)), asyncio.create_task(server.serve()),
        asyncio.create_task(_watch_runtime_control(app, run_id=run_id, stop_event=stop,
            request_path=CONTROL_REQUEST_PATH, result_path=CONTROL_RESULT_PATH)), asyncio.create_task(witness())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.aclose()


def configured_status_path(app):
    return app.data_dir / "qq-hybrid-runtime-status.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Official QQ UI hybrid V2 guest runtime")
    parser.add_argument("--config", required=True)
    parser.add_argument("--publication-config", help="canonical configuration publication fence")
    parser.add_argument("--hybrid-settings", required=True)
    parser.add_argument("--active-binding", action="append", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-config-sha256")
    parser.add_argument("--expected-session-binding-revision", type=int,
                        help="require the immutable QQ session metadata revision (legacy=1)")
    parser.add_argument("--expected-hybrid-settings-sha256")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8765)
    args = parser.parse_args(argv)
    owner = None
    try:
        run_id = str(UUID(args.run_id))
        if not args.check:
            owner = QQRuntimeInstanceOwner()
            owner.acquire()
            assert_publication_complete(Path(args.config))
            if args.publication_config:
                assert_publication_complete(Path(args.publication_config))
        config = load_config(args.config, expected_sha256=args.expected_config_sha256)
        revision = session_revision_number(config)
        if args.expected_session_binding_revision is not None and (
            args.expected_session_binding_revision < 1
            or revision != args.expected_session_binding_revision
        ):
            raise ValueError("runtime session binding revision mismatch")
        raw = read_settings(args.hybrid_settings, expected_sha256=args.expected_hybrid_settings_sha256)
        secrets = WindowsDPAPISecretStore(Path(config["secret_vault"]))
        key = secrets.get_secret("deepseek.api_key").decode("utf-8")
        pack, all_bindings, _ = validate_config(config, api_key=key)
        bindings = select_bindings(all_bindings, args.active_binding)
        settings = parse_hybrid_settings(raw, selector_pack=pack, bindings=bindings,
            binding_revisions=existing_binding_revisions(config, bindings), run_id=run_id)
        if settings.session_epoch != str(revision):
            raise ValueError("hybrid session epoch mismatch")
        validate_active_rules(config)
        if args.check:
            print("V2 configuration valid; no QQ input, API request or worker startup")
            return 0
        async def run():
            app = await build_runtime_v2(config, raw, api_key=key,
                authorization_signing_key=secrets.get_or_create_hmac_key("runtime.authorization.signing"),
                run_id=run_id, active_binding_ids=args.active_binding)
            await serve_v2(app, host=args.web_host, port=args.web_port, run_id=run_id)
        asyncio.run(run())
        return 0
    except (ValueError, RuntimeError, OSError):
        # Validation/native exception messages may contain private source input.
        print("blocked: hybrid_configuration_or_runtime_failed")
        return 2
    finally:
        if owner is not None:
            owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
