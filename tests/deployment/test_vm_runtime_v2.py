"""Offline V2 runner integration: real journals, fixed fakes only at UI/API ports."""
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.navigation.contracts import NavigationOutcome, NavigationStatus
from messenger_ai.adapters.qq.navigation.identity import ActiveChatLease
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerKind, WorkerResult, WorkerStatus
from messenger_ai.adapters.qq.vm_driver.hybrid_bridge import QQHybridDriverBridge
from messenger_ai.adapters.qq.vm_driver.hybrid_session import HybridSessionStatus
from messenger_ai.runtime.contracts import ObservationBatch
from messenger_ai.runtime.navigation import RuntimeNavigationResult
from messenger_ai.runtime.state import RuntimeState
from tests.runtime.test_qq_hybrid_config import configuration
from tests.runtime.test_qq_hybrid_scope import scope_case


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("test_hybrid_runner", scripts / "run_vm_runtime_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_queue(case, kind, status, *, active=False, global_event=False):
    binding = case.bindings[0 if active else 1]
    now = datetime.now(UTC).isoformat()
    if kind == "planning":
        case.state.connection.execute("INSERT INTO runtime_planning_jobs"
            "(conversation_id,conversation_revision,status,updated_at,source_keys_json) VALUES(?,?,?,?,?)",
            (binding.hub_conversation_id,1,status,now,'["synthetic-original-key"]'))
    elif kind == "event":
        case.state.connection.execute("INSERT INTO runtime_event_outbox"
            "(dedupe_key,event_type,aggregate_id,payload_json,status,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid4()),"synthetic","global" if global_event else binding.hub_conversation_id,
             '{"sentinel":"unchanged"}',status,now))
    else:
        plan = str(uuid4())
        case.pacing.connection.execute("INSERT INTO m10_plans"
            "(pacing_plan_id,conversation_id,contact_id,status,earliest_send_at,expires_at,payload_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (plan,binding.hub_conversation_id,binding.contact_id,
            status if kind == "plan" else "completed",now,now,'{"sentinel":"unchanged"}',now,now))
        if kind == "due":
            case.pacing.connection.execute("INSERT INTO m10_due_outbox"
                "(pacing_plan_id,segment_index,payload_json,status,created_at,claim_token) VALUES(?,?,?,?,?,?)",
                (plan,0,'{"sentinel":"unchanged"}',status,now,"original-claim"))


@pytest.mark.parametrize("kind,status", [("planning","pending"),("planning","running"),
    ("planning","dispatching"),("event","pending"),("event","dispatching"),
    ("plan","waiting"),("plan","due_for_revalidation"),("due","pending"),
    ("due","dispatching"),("due","dispatching_nonrecoverable")])
def test_inactive_work_fails_closed_without_claiming_cancelling_or_rewriting_it(runner, scope_case, kind, status):
    c = scope_case
    seed_queue(c,kind,status)
    before = tuple(c.state.connection.iterdump()), tuple(c.pacing.connection.iterdump())
    with pytest.raises(ValueError, match="inactive binding has pending work"):
        runner.validate_inactive_queues(c.root, c.bindings[:1])
    assert before == (tuple(c.state.connection.iterdump()),tuple(c.pacing.connection.iterdump()))


@pytest.mark.parametrize("kind,status", [("planning","failed"),("planning","ignored"),
    ("event","delivered"),("plan","cancelled"),("plan","completed"),
    ("due","delivered"),("due","operation_recovery_hold"),("due","navigation_attention")])
def test_inactive_terminal_or_held_work_remains_untouched_and_is_not_replayed(runner, scope_case, kind, status):
    c = scope_case
    seed_queue(c,kind,status)
    before = tuple(c.state.connection.iterdump()), tuple(c.pacing.connection.iterdump())
    runner.validate_inactive_queues(c.root,c.bindings[:1])
    assert before == (tuple(c.state.connection.iterdump()),tuple(c.pacing.connection.iterdump()))


def test_active_pending_work_and_global_control_event_remain_eligible(runner, scope_case):
    c = scope_case
    seed_queue(c,"planning","pending",active=True)
    seed_queue(c,"due","pending",active=True)
    seed_queue(c,"event","pending",global_event=True)
    runner.validate_inactive_queues(c.root,c.bindings[:1])


@pytest.mark.asyncio
async def test_build_refuses_inactive_work_before_any_model_session_or_navigation_factory(runner, scope_case, monkeypatch):
    c = scope_case
    raw, pack, bindings = configuration()
    raw["inputs"] = raw["inputs"][:1]
    config = {"data_dir":str(c.root),"secret_vault":raw["vault_path"]}
    seed_queue(c,"planning","pending")
    before = tuple(c.state.connection.iterdump()),tuple(c.pacing.connection.iterdump())
    monkeypatch.setattr(runner,"validate_config",lambda *_args,**_kwargs:(pack,bindings,()))
    monkeypatch.setattr(runner,"session_revision_number",lambda _:2)
    def forbidden(*args,**kwargs):
        pytest.fail("inactive queued work must block before opening external resources")
    for name in ("QQHybridRoundFactory","HybridWorkerSession","ResponsesVisionNavigator",
                 "NavigationTaskStore","QQHybridNavigationService","DeepSeekResponsesProvider","assemble_runtime"):
        monkeypatch.setattr(runner,name,forbidden)
    with pytest.raises(ValueError,match="inactive binding has pending work"):
        await runner.build_runtime_v2(config,raw,api_key="synthetic-key",authorization_signing_key=b"s"*32,
            run_id="synthetic-run",active_binding_ids=[bindings[0].binding_id])
    assert before == (tuple(c.state.connection.iterdump()),tuple(c.pacing.connection.iterdump()))


def test_existing_business_revision_read_is_readonly_and_distinct_from_session_revision(runner, scope_case):
    c = scope_case
    before = tuple(c.state.connection.iterdump())
    assert runner.existing_binding_revisions({"data_dir":str(c.root)},c.bindings) == {
        binding.binding_id:1 for binding in c.bindings}
    assert c.settings.session_epoch == "2"
    assert tuple(c.state.connection.iterdump()) == before


@pytest.mark.parametrize("active_ids", [[],["binding-1","binding-1"],["unknown-binding"]])
def test_active_bindings_must_be_explicit_unique_registered_members(runner, active_ids):
    with pytest.raises(ValueError):
        runner.select_bindings(configuration()[2],active_ids)


class Session:
    def __init__(self, events):
        self.events = events
        self.status = HybridSessionStatus(state="active",purpose="draft",worker_epoch=uuid4())
    def status_snapshot(self):
        return self.status
    async def aclose(self):
        self.events.append("session-reaped")
        self.status = HybridSessionStatus(state="closed")
    def close(self):
        self.events.append("session-close")


@pytest.fixture
def driver(runner,tmp_path):
    events = []
    binding = configuration()[2][0]
    worker = Session(events)
    bridge = runner.NavigatingHybridDriver(worker=worker,bindings=(binding,),text_provider=lambda _:"",
        sqlite_path=tmp_path/"v2-bridge.sqlite3",scope_guard=lambda _:True,
        expected_profile_signatures={binding.binding_id:"qq-profile-hmac:"+"a"*64})
    bridge._cursor.ingest_snapshot(binding.hub_conversation_id,[])
    bridge._last_health = WorkerResult(request_id=uuid4(),kind=WorkerKind.HEALTH,status=WorkerStatus.OK,
        worker_epoch=worker.status.worker_epoch)
    case = SimpleNamespace(bridge=bridge,worker=worker,binding=binding,events=events,pause=asyncio.Event())
    state = RuntimeState(tmp_path/"runtime.sqlite3")
    state.register(account_id=binding.account_id,contact_id=binding.contact_id,
        conversation_id=binding.hub_conversation_id,binding_revision=1,conversation_type="direct")
    state.connection.execute("UPDATE runtime_conversations SET conversation_revision=3")
    bridge.app_lookup = lambda:SimpleNamespace(_pause_requested=case.pause,state=state)
    yield case
    bridge._cursor.close()
    bridge._db.close()
    state.close()


def test_typed_active_uuid_witness_serializes_without_chat_or_secret_data(runner,driver,tmp_path):
    c = driver
    path = tmp_path/"status.json"
    snapshot = c.bridge.worker_status_snapshot()
    runner._write_worker_witness(path,{"schema":"qq_hybrid_runtime_status_v2","session":snapshot,
        "startup_health":c.bridge.health().status.value})
    saved = json.loads(path.read_text())
    assert saved["session"]["worker_epoch"] == str(c.worker.status.worker_epoch)
    assert saved["session"]["state"] == "active"
    assert set(saved["session"]) == {"schema","state","purpose","worker_epoch","cleanup_required"}
    assert saved["startup_health"] == "ok"
    c.worker.status = HybridSessionStatus(state="idle")
    assert c.bridge.health().status is WorkerStatus.OK  # Finite HEALTH child was correctly retired.
    c.worker.status = HybridSessionStatus(state="cleanup_required",cleanup_required=True)
    assert c.bridge.health().status is WorkerStatus.UNAVAILABLE


def lease(binding):
    now = datetime.now(UTC)
    return ActiveChatLease(lease_id="b"*64, account_id=binding.account_id,
        conversation_id=binding.hub_conversation_id,binding_id=binding.binding_id,binding_revision=1,
        run_id="test-run",session_epoch="2",surface_epoch="surface",worker_epoch=str(uuid4()),
        observation_epoch="observation",desktop_lease_id="desktop",control_revision=1,
        process_id=123,window_handle=456,process_started_at_100ns=789,frame_id="frame",evidence_ref="proof",
        evidence_digest="c"*64,verification_method="profile_from_current_header_with_selected_row_fence",
        issued_at=now,expires_at=now+timedelta(seconds=10),issued_monotonic_ns=1,expires_monotonic_ns=10_000_000_001)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,with_lease,should_observe", [
    (NavigationStatus.CANDIDATE_OPENED,True,True),(NavigationStatus.CANDIDATE_OPENED,False,False),
    (NavigationStatus.NEEDS_ATTENTION,False,False),(NavigationStatus.RETRY_WAIT,False,False),
    (NavigationStatus.CANCELLED,False,False),(NavigationStatus.NEEDS_ATTENTION,True,False)])
async def test_only_navigation_success_with_a_lease_reaches_independent_worker_observe(
        driver,monkeypatch,status,with_lease,should_observe):
    c = driver
    calls = []
    async def navigate(binding_id,*,pending_input_key,cancel_event):
        calls.append("navigate")
        assert binding_id == c.binding.binding_id and cancel_event is c.pause
        assert pending_input_key == f"observe:{c.binding.hub_conversation_id}:3"
        return RuntimeNavigationResult(outcome=NavigationOutcome(status=status,binding_id=binding_id,
            binding_revision=1),task_id="task",active_chat_lease=lease(c.binding) if with_lease else None)
    c.bridge.navigation = SimpleNamespace(navigate=navigate)
    async def observe(self,conversation_id,*,binding_revision,conversation_revision):
        calls.append("worker-observe")
        return ObservationBatch(account_id=c.binding.account_id,contact_id=c.binding.contact_id,
            conversation_id=conversation_id,binding_revision=binding_revision,
            conversation_revision=conversation_revision,complete=True,messages=())
    monkeypatch.setattr(QQHybridDriverBridge,"observe_conversation",observe)
    result = await c.bridge.observe_conversation(c.binding.hub_conversation_id,binding_revision=1,conversation_revision=3)
    assert result.complete is should_observe
    assert calls == (["navigate","worker-observe"] if should_observe else ["navigate"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["manual_pause", "identity_mismatch",
    "ui_automation_unavailable:identity_profile_capture_failed"])
async def test_hard_contact_pause_without_a_recovery_scope_never_starts_navigation(driver, reason):
    c = driver
    state = c.bridge.app_lookup().state
    state.connection.execute("UPDATE runtime_conversations SET paused=1,pause_reason=?", (reason,))
    async def forbidden(*args, **kwargs):
        pytest.fail("hard pause must not start another UI episode")
    c.bridge.navigation = SimpleNamespace(navigate=forbidden)
    before = tuple(c.bridge._cursor.connection.iterdump())
    result = await c.bridge.observe_conversation(c.binding.hub_conversation_id,
        binding_revision=1,conversation_revision=3)
    assert not result.complete and result.gap_reason == reason and not result.messages
    assert tuple(c.bridge._cursor.connection.iterdump()) == before
    assert state.execution_state(c.binding.hub_conversation_id)[2]


@pytest.mark.asyncio
async def test_navigation_cleanup_false_retains_driver_resources_until_proven_retry(driver):
    c = driver
    cleanup = [False]
    async def nav_close():
        c.events.append("navigation-close")
        return cleanup[0]
    async def provider_close():
        c.events.append("navigator-close")
    c.bridge.navigation = SimpleNamespace(aclose=nav_close)
    c.bridge.navigation_resources = (SimpleNamespace(aclose=provider_close),
        SimpleNamespace(close=lambda:c.events.append("navigation-store-close")))
    with pytest.raises(RuntimeError,match="hybrid_navigation_cleanup_required"):
        await c.bridge.aclose()
    assert c.events == ["navigation-close"]
    assert c.bridge._db.execute("SELECT COUNT(*) FROM qq_vm_ops").fetchone()[0] == 0
    cleanup[0] = True
    await c.bridge.aclose()
    assert c.events == ["navigation-close","navigation-close","session-reaped","navigator-close","navigation-store-close"]


def test_external_settings_digest_validation_never_edits_canonical_or_original_bytes(runner,tmp_path):
    canonical = tmp_path/"runtime-config.json"
    canonical.write_bytes(b'{"generation":"original"}')
    external = tmp_path/"v2-settings.json"
    payload = b'{"schema_version":"qq_hybrid_runtime_v2","enabled":true}'
    external.write_bytes(payload)
    assert runner.read_settings(external,expected_sha256=hashlib.sha256(payload).hexdigest())["enabled"] is True
    with pytest.raises(ValueError,match="digest mismatch"):
        runner.read_settings(external,expected_sha256="0"*64)
    assert external.read_bytes() == payload
    assert canonical.read_bytes() == b'{"generation":"original"}'


@pytest.fixture
def cli(runner, monkeypatch, tmp_path):
    """Real file/digest/revision parsing; replace only business validation and external ports."""
    raw, pack, bindings = configuration()
    config = {"schema":"pmai-v5-runtime-1","secret_vault":str(tmp_path/"synthetic-vault"),
        "session_binding":{"schema":"pmai-qq-session-binding-v1","revision":2,
            "base_config_sha256":"a"*64,"previous_config_sha256":"b"*64}}
    snapshot = tmp_path/"runtime-config.session-2.json"
    canonical = tmp_path/"runtime-config.json"
    settings = tmp_path/"hybrid-settings.json"
    snapshot.write_text(json.dumps(config), encoding="utf-8")
    canonical.write_bytes(b'{"canonical":"must only be checked as a publication fence"}')
    settings.write_text(json.dumps(raw), encoding="utf-8")
    events, publications, builds = [], [], []
    run_id = str(uuid4())

    class Owner:
        def __init__(self):
            events.append("owner-created")
        def acquire(self):
            events.append("owner-acquired")
        def close(self):
            events.append("owner-closed")

    class Secrets:
        def __init__(self, path):
            assert path == Path(config["secret_vault"])
            events.append("secrets-opened")
        def get_secret(self, name):
            assert name == "deepseek.api_key"
            events.append("synthetic-secret-read")
            return b"offline-synthetic-key"
        def get_or_create_hmac_key(self, name):
            assert name == "runtime.authorization.signing"
            events.append("signing-key-requested")
            return b"s"*32

    publication_check = runner.assert_publication_complete
    def check_publication(path):
        events.append("publication-checked")
        publications.append(path)
        publication_check(path)

    def validate(value, *, api_key):
        assert api_key == "offline-synthetic-key"
        events.append("business-validated")
        return pack, bindings, ()

    async def build(value, raw_settings, **kwargs):
        events.append("runtime-built")
        builds.append((value, raw_settings, kwargs))
        return SimpleNamespace()

    async def serve(app, **kwargs):
        assert kwargs["run_id"] == run_id
        events.append("runtime-served")

    monkeypatch.setattr(runner, "QQRuntimeInstanceOwner", Owner)
    monkeypatch.setattr(runner, "WindowsDPAPISecretStore", Secrets)
    monkeypatch.setattr(runner, "assert_publication_complete", check_publication)
    monkeypatch.setattr(runner, "validate_config", validate)
    monkeypatch.setattr(runner, "validate_active_rules", lambda _:events.append("rules-validated"))
    monkeypatch.setattr(runner, "existing_binding_revisions",
        lambda _, selected:{binding.binding_id:1 for binding in selected})
    monkeypatch.setattr(runner, "build_runtime_v2", build)
    monkeypatch.setattr(runner, "serve_v2", serve)

    def args(*extra):
        return ["--config",str(snapshot),"--hybrid-settings",str(settings),
            "--active-binding",bindings[0].binding_id,"--active-binding",bindings[1].binding_id,
            "--run-id",run_id,"--expected-config-sha256",hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "--expected-hybrid-settings-sha256",hashlib.sha256(settings.read_bytes()).hexdigest(),*extra]

    return SimpleNamespace(runner=runner, args=args, snapshot=snapshot, canonical=canonical,
        settings=settings, events=events, publications=publications, builds=builds, config=config, raw=raw)


def test_cli_supervisor_fences_canonical_and_loads_only_frozen_snapshot(cli):
    before = tuple(path.read_bytes() for path in (cli.snapshot,cli.canonical,cli.settings))
    assert cli.runner.main(cli.args("--publication-config",str(cli.canonical),
        "--expected-session-binding-revision","2")) == 0
    assert cli.publications == [cli.snapshot,cli.canonical]
    assert cli.builds[0][0] == cli.config
    assert cli.builds[0][1] == cli.raw
    assert cli.events[:4] == ["owner-created","owner-acquired","publication-checked","publication-checked"]
    assert cli.events[-3:] == ["runtime-built","runtime-served","owner-closed"]
    assert before == tuple(path.read_bytes() for path in (cli.snapshot,cli.canonical,cli.settings))


@pytest.mark.parametrize("fenced_path", ["snapshot","canonical"])
def test_cli_incomplete_publication_blocks_before_config_secrets_or_runtime(cli, fenced_path, capsys):
    path = getattr(cli, fenced_path)
    marker = path.with_name(f".{path.name}.publication.json")
    marker.write_bytes(b'{"private-sentinel":"must not be printed"}')
    before = tuple(item.read_bytes() for item in (cli.snapshot,cli.canonical,cli.settings,marker))
    assert cli.runner.main(cli.args("--publication-config",str(cli.canonical),
        "--expected-session-binding-revision","2")) == 2
    assert cli.events == ["owner-created","owner-acquired",*(["publication-checked"] *
        (1 if fenced_path == "snapshot" else 2)),"owner-closed"]
    assert capsys.readouterr().out.strip() == "blocked: hybrid_configuration_or_runtime_failed"
    assert before == tuple(item.read_bytes() for item in (cli.snapshot,cli.canonical,cli.settings,marker))


@pytest.mark.parametrize("check", [False,True])
@pytest.mark.parametrize("revision", ["0","-1","1","3"])
def test_cli_expected_session_revision_fails_closed_before_secrets(cli, revision, check):
    assert cli.runner.main(cli.args("--expected-session-binding-revision",revision,
        *(["--check"] if check else []))) == 2
    assert cli.events == ([] if check else
        ["owner-created","owner-acquired","publication-checked","owner-closed"])
    assert not cli.builds


def test_cli_check_preserves_v1_fence_semantics_without_owner_api_or_worker(cli):
    for path in (cli.snapshot,cli.canonical):
        path.with_name(f".{path.name}.publication.json").write_bytes(b"{}")
    assert cli.runner.main(cli.args("--publication-config",str(cli.canonical),
        "--expected-session-binding-revision","2","--check")) == 0
    assert cli.events == ["secrets-opened","synthetic-secret-read","business-validated","rules-validated"]
    assert not cli.publications and not cli.builds


@pytest.mark.parametrize("expected", [None,"1"])
def test_cli_legacy_session_revision_defaults_to_one(cli, expected):
    cli.config.pop("session_binding")
    cli.snapshot.write_text(json.dumps(cli.config), encoding="utf-8")
    cli.raw["session_epoch"] = "1"
    cli.settings.write_text(json.dumps(cli.raw), encoding="utf-8")
    extra = [] if expected is None else ["--expected-session-binding-revision",expected]
    assert cli.runner.main(cli.args("--check",*extra)) == 0
    assert not cli.builds


def test_cli_malformed_session_metadata_is_rejected_even_without_expected_revision(cli):
    cli.config["session_binding"]["revision"] = True
    cli.snapshot.write_text(json.dumps(cli.config), encoding="utf-8")
    assert cli.runner.main(cli.args("--check")) == 2
    assert not cli.events


def test_cli_noninteger_expected_revision_is_rejected_by_parser_without_external_effects(cli):
    with pytest.raises(SystemExit) as error:
        cli.runner.main(cli.args("--expected-session-binding-revision","2.0"))
    assert error.value.code == 2
    assert not cli.events
