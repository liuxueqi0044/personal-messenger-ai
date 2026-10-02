"""Real coordinator/desktop/N2/guard wiring, with value-only native sources."""
import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import time
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.models import QQSelector, QQWindow
from messenger_ai.adapters.qq.navigation.contracts import (
    NavigationDecision, NavigationProviderResult, NavigationRect, NavigationRegion,
)
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopScope
from messenger_ai.adapters.qq.navigation.supervised_profile import SupervisedProfileSource
from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState
from messenger_ai.runtime import qq_hybrid_navigation as module
from messenger_ai.runtime.navigation_state import NavigationTaskStore
from messenger_ai.runtime.qq_hybrid_config import QQHybridSettings, HybridNavigationModelSettings
from tests.adapters.qq.navigation.test_identity import case
from tests.adapters.qq.navigation.test_profile_verifier import report, parse


BOX = NavigationRect(left=2, top=2, right=40, bottom=20)


@pytest.fixture
def rig(tmp_path, monkeypatch, case, report):
    target, baseframe, expectation, evidence = case
    settings = QQHybridSettings(helper_path=str(tmp_path / "helper.exe"),
        vault_path=str(tmp_path / "vault"), guard_directory=str(tmp_path / "guards"),
        window=QQWindow(process_id=123, window_handle=456, class_name="QQ"),
        process_started_at_100ns=123456789, session_epoch="session", surface_epoch="surface",
        run_id="run", max_seconds=45, prepare_write_reserve_seconds=20,
        navigation_model=HybridNavigationModelSettings(),
        targets=MappingProxyType({target.binding_id: target}),
        expectations=MappingProxyType({target.binding_id: expectation}),
        contact_ids=MappingProxyType({target.binding_id: "contact"}),
        _composer_json=QQSelector(name="composer", control_type="EditControl").model_dump_json(),
        _messages_json=QQSelector(name="messages", control_type="ListControl").model_dump_json())
    lock = asyncio.Lock()
    store = NavigationTaskStore(tmp_path / "runtime.sqlite")
    state = SimpleNamespace(current=False, paused=False, owned=False, commit=False,
        revision=3, fail_source=False, fail_profile_reap=False, fail_nav_reap=False,
        events=[], backends=[], profiles=[], sources=[], requests=[], model_block=False,
        model_started=asyncio.Event(), profile_started=asyncio.Event(), block_profile=False)
    state.before_decision_return = None
    state.profile_hmac = None

    def snapshot(**kwargs):
        state.sources.append(kwargs)
        assert kwargs["purpose"] == "navigation"
        if state.fail_source:
            raise ValueError("private source contents")
        return NavigationGuardState(target=kwargs["target"], run_id="run", session_epoch="session",
            surface_epoch="surface", worker_epoch=str(kwargs["worker_epoch"]),
            observation_epoch=kwargs["observation_epoch"], desktop_lease_id=kwargs["desktop_lease_id"],
            process_id=123, window_handle=456, process_started_at_100ns=123456789,
            lease_expires_at=kwargs["deadline_at"], control_revision=state.revision,
            paused=state.paused, has_owned_draft=state.owned, has_commit_obligation=state.commit,
            published_at=datetime.now(UTC))

    class Backend:
        def __init__(self, config, current_target, *, deadline_at):
            assert lock.locked()
            assert Path(config.guard_state_path).is_file()
            assert store.connection.execute("SELECT 1 FROM runtime_nav_cleanup_obligations").fetchone()
            self.config, self.target, self.deadline = config, current_target, deadline_at
            self.closed, self.revoked, self.captures = False, False, 0
            self.frame = None
            state.events.append("navigation_spawn")
            state.backends.append(self)

        def guard(self):
            return NavigationGuardState.model_validate_json(Path(self.config.guard_state_path).read_bytes())

        def revoke(self):
            self.revoked = True
            state.events.append("navigation_revoke")

        def close(self):
            if self.closed:
                return
            assert lock.locked()
            if state.fail_nav_reap:
                raise ValueError("private process failure")
            self.closed = True
            state.events.append("navigation_reaped")

        async def bound_capture(self, current_target, *, deadline_at):
            assert not self.revoked and lock.locked()
            guard = self.guard()
            self.captures += 1
            self.frame = baseframe.model_copy(update={
                "frame_id": f"frame-{self.captures}", "captured_at": datetime.now(UTC),
                **{key: getattr(guard, key) for key in ("run_id", "session_epoch", "surface_epoch",
                    "worker_epoch", "desktop_lease_id", "control_revision")},
                "allowed_regions": (NavigationRegion(kind="candidate", bbox=BOX),
                    NavigationRegion(kind="list", bbox=BOX))})
            return self.frame

        async def current_scope(self, current_target):
            guard = self.guard()
            values = {key: getattr(self.frame, key) for key in (
                "binding_id", "binding_revision", "run_id", "session_epoch", "surface_epoch",
                "worker_epoch", "desktop_lease_id", "process_id", "window_handle", "screen_origin_x",
                "screen_origin_y", "screen_width", "screen_height", "crop_origin_x", "crop_origin_y",
                "crop_width", "crop_height", "dpi_scale")}
            return NavigationDesktopScope(**values, account_id=target.account_id,
                conversation_id=target.conversation_id, control_revision=guard.control_revision,
                lease_expires_at=guard.lease_expires_at, observed_at=datetime.now(UTC), foreground=True,
                paused=guard.paused, has_owned_draft=guard.has_owned_draft,
                has_commit_obligation=guard.has_commit_obligation)

        async def local_witness(self, current_target):
            guard = self.guard()
            return evidence.before.model_copy(update={
                "captured_at": datetime.now(UTC), "captured_monotonic_ns": time.monotonic_ns(),
                **{key: getattr(guard, key) for key in ("run_id", "session_epoch", "surface_epoch",
                    "worker_epoch", "desktop_lease_id", "control_revision", "observation_epoch")},
                "header_digest": sha256(target.display_name.encode()).hexdigest() if state.current else "f"*64,
                "active_chat_structure_digest": "e"*64})

        async def relevant_region_digest(self, *args, **kwargs):
            return "a"*64

        async def click(self, *args, **kwargs):
            assert not kwargs["cancel_event"].is_set()
            state.current = True
            state.events.append("click")

    class Profile(SupervisedProfileSource):
        def __init__(self, config):
            super().__init__(config)
            state.profiles.append(self)

        async def capture(self, current_target, frame, current_expectation, *, deadline_at):
            assert self._round.get().active and lock.locked()
            state.profile_started.set()
            state.events.append("profile_capture")
            class Tree:
                def revoke(self):
                    state.events.append("profile_revoke")
                def close(self):
                    assert lock.locked()
                    if state.fail_profile_reap:
                        raise ValueError("private profile failure")
                    state.events.append("profile_reaped")
            self._active = Tree()
            if state.block_profile:
                await asyncio.Event().wait()
            result = parse(report)
            if state.profile_hmac is not None:
                result = result.model_copy(update={"profile": result.profile.model_copy(
                    update={"profile_id_hmac":state.profile_hmac})})
            digest = sha256(target.display_name.encode()).hexdigest()
            before = result.acquisition.before.model_copy(update={"captured_at": datetime.now(UTC),
                "captured_monotonic_ns": time.monotonic_ns(), "header_digest": digest})
            captured, captured_ns = datetime.now(UTC), time.monotonic_ns()
            after = result.acquisition.after.model_copy(update={"captured_at": datetime.now(UTC),
                "captured_monotonic_ns": time.monotonic_ns(), "header_digest": digest})
            return result.model_copy(update={
                "profile": result.profile.model_copy(update={"active_header_digest": digest}),
                "acquisition": result.acquisition.model_copy(update={"before": before, "after": after,
                    "profile_captured_at": captured, "profile_captured_monotonic_ns": captured_ns})})

    class Navigator:
        async def decide(self, request, *, cancel_event=None):
            assert lock.locked()
            state.model_started.set()
            state.requests.append(request)
            if state.model_block:
                await asyncio.Event().wait()
            decision = NavigationDecision(action="candidate_opened", frame_id=request.frame.frame_id)
            if not state.current:
                decision = NavigationDecision(action="click_candidate", frame_id=request.frame.frame_id,
                    bbox=BOX, observed_label=target.display_name)
            if state.before_decision_return is not None:
                state.before_decision_return()
            return NavigationProviderResult(frame_id=request.frame.frame_id, model="synthetic",
                latency_ms=0, decision=decision)

    monkeypatch.setattr(module, "WindowsNavigationBackend", Backend)
    monkeypatch.setattr(module, "SupervisedProfileSource", Profile)
    service = module.QQHybridNavigationService(settings, Navigator(), store, lock, snapshot)
    return SimpleNamespace(service=service, state=state, lock=lock, store=store, target=target,
        settings=settings, snapshot=snapshot)


async def test_full_navigation_uses_real_masked_frame_coordinator_and_independent_n2(rig):
    result = await rig.service.navigate("binding", "qq-uia/conversation/1")
    assert result.outcome.status == "candidate_opened", result.outcome.error_code
    assert result.active_chat_lease is not None
    assert result.outcome.model_requests == 1 and result.outcome.desktop_actions == 1
    assert len(rig.state.requests) == 1
    assert rig.state.events.index("click") < rig.state.events.index("profile_capture")
    assert result.active_chat_lease.observation_epoch == rig.state.sources[0]["observation_epoch"]
    assert all(req.frame.privacy_mask_applied for req in rig.state.requests)
    assert rig.state.events.index("profile_reaped") < rig.state.events.index("navigation_reaped")
    assert not rig.lock.locked()
    assert not rig.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()
    assert all(source["deadline_at"] == rig.state.sources[0]["deadline_at"] for source in rig.state.sources)
    assert (rig.state.sources[0]["deadline_at"]-datetime.now(UTC)).total_seconds() < 45


async def test_already_current_still_profiles_but_uses_zero_model_calls_and_new_epoch(rig):
    rig.state.current = True
    first = await rig.service.navigate("binding", "qq-uia/conversation/1")
    second = await rig.service.navigate("binding", "qq-uia/conversation/2")
    assert first.outcome.status == second.outcome.status == "candidate_opened"
    assert first.active_chat_lease.worker_epoch != second.active_chat_lease.worker_epoch
    assert first.active_chat_lease.desktop_lease_id != second.active_chat_lease.desktop_lease_id
    assert first.active_chat_lease.observation_epoch != second.active_chat_lease.observation_epoch
    assert not rig.state.requests
    assert rig.state.events.count("profile_capture") == 2


@pytest.mark.parametrize("flag", ["paused", "owned", "commit"])
async def test_actual_control_blocks_before_process_spawn_or_model(rig, flag):
    setattr(rig.state, flag, True)
    result = await rig.service.navigate("binding", "input")
    assert result.outcome.status == "needs_attention"
    assert not rig.state.backends and not rig.state.requests
    assert not rig.lock.locked()


async def test_initial_source_failure_cannot_spawn_or_leak_lock(rig):
    rig.state.fail_source = True
    result = await rig.service.navigate("binding", "input")
    assert result.outcome.status == "needs_attention"
    assert not rig.state.backends and not rig.lock.locked()
    assert "private" not in str(result)


async def test_same_name_wrong_profile_anchor_is_never_reinterpreted_as_navigation(rig):
    rig.state.current = True
    rig.state.profile_hmac = "0"*64
    result = await rig.service.navigate("binding", "input")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "identity_profile_mismatch"
    assert result.active_chat_lease is None and not rig.state.requests
    assert "click" not in rig.state.events and not rig.lock.locked()


@pytest.mark.parametrize("signal", ["pause", "external_cancel"])
async def test_control_change_at_model_return_is_checked_before_click_without_heartbeat_wait(rig, signal):
    cancelled = asyncio.Event()
    rig.state.before_decision_return = (lambda: setattr(rig.state, "paused", True)) if signal == "pause" else cancelled.set
    result = await rig.service.navigate("binding", "input", cancelled)
    assert result.active_chat_lease is None
    assert "click" not in rig.state.events
    assert rig.state.backends[0].closed and not rig.lock.locked()
    if signal == "pause":
        saved = NavigationGuardState.model_validate_json(next(Path(rig.settings.guard_directory).glob("*.json")).read_bytes())
        assert saved.paused


@pytest.mark.parametrize("failure", ["fail_profile_reap", "fail_nav_reap"])
async def test_failed_reap_retains_shared_lock_and_durable_hold_then_owned_retry(rig, failure):
    rig.state.current = True
    setattr(rig.state, failure, True)
    result = await rig.service.navigate("binding", "input")
    assert result.outcome.error_code == "qq_navigation_cleanup_required"
    assert result.active_chat_lease is None and rig.lock.locked()
    assert rig.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()
    blocked = await rig.service.navigate("binding", "new-input")
    assert blocked.outcome.error_code == "qq_navigation_cleanup_required"
    assert len(rig.state.backends) == 1
    setattr(rig.state, failure, False)
    assert await rig.service.retry_cleanup()
    assert not rig.lock.locked()
    assert not rig.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()


async def test_restart_does_not_clear_or_replay_orphaned_cleanup_obligation(rig):
    rig.store.connection.execute("INSERT INTO runtime_nav_cleanup_obligations VALUES(?,?,?,?,?)",
        (str(uuid4()), rig.target.account_id, "prior-task", "prior-guard.json", datetime.now(UTC).isoformat()))
    other = module.QQHybridNavigationService(rig.settings, rig.service.navigator, rig.store,
        rig.lock, rig.snapshot)
    assert not await other.retry_cleanup()
    result = await other.navigate("binding", "input")
    assert result.outcome.error_code == "qq_navigation_cleanup_required"
    assert not rig.state.backends


async def test_cancel_during_profile_revokes_and_reaps_both_before_unlock(rig):
    rig.state.current = True
    rig.state.block_profile = True
    cancelled = asyncio.Event()
    task = asyncio.create_task(rig.service.navigate("binding", "input", cancelled))
    await rig.state.profile_started.wait()
    cancelled.set()
    result = await asyncio.wait_for(task, 2)
    assert result.outcome.status == "cancelled", result.outcome.error_code
    assert result.active_chat_lease is None
    assert not rig.lock.locked()
    assert rig.state.events.index("profile_reaped") < rig.state.events.index("navigation_reaped")
    assert not rig.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()


async def test_guard_source_failure_during_model_wait_revokes_worker(rig):
    rig.state.model_block = True
    task = asyncio.create_task(rig.service.navigate("binding", "input"))
    await rig.state.model_started.wait()
    rig.state.fail_source = True
    result = await asyncio.wait_for(task, 2)
    assert result.outcome.status in {"cancelled", "needs_attention"}
    assert result.active_chat_lease is None
    assert rig.state.backends[0].revoked and rig.state.backends[0].closed
    assert not rig.lock.locked() and "private" not in str(result)


async def test_caller_task_cancellation_does_not_leave_a_reusable_round_or_lock(rig):
    rig.state.model_block = True
    task = asyncio.create_task(rig.service.navigate("binding", "input"))
    await rig.state.model_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert rig.state.backends[0].closed and not rig.lock.locked()
    assert not rig.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()
    assert await rig.service.aclose()


async def test_shared_desktop_lock_wait_cancellation_never_spawns(rig):
    await rig.lock.acquire()
    cancelled = asyncio.Event()
    task = asyncio.create_task(rig.service.navigate("binding", "input", cancelled))
    await asyncio.sleep(.01)
    cancelled.set()
    result = await asyncio.wait_for(task, 2)
    assert result.active_chat_lease is None and not rig.state.backends
    assert rig.lock.locked()  # The unrelated owner is never released.
    rig.lock.release()


async def test_live_navigation_is_temporary_busy_without_starting_a_second_episode(rig):
    rig.state.model_block = True
    cancelled = asyncio.Event()
    first = asyncio.create_task(rig.service.navigate("binding", "input", cancelled))
    await rig.state.model_started.wait()
    second = await rig.service.navigate("binding", "second-input")
    assert second.outcome.status == "retry_wait"
    assert second.outcome.error_code == "qq_navigation_episode_in_progress"
    assert second.retry_at >= datetime.now(UTC)+timedelta(seconds=9)
    assert len(rig.state.backends) == 1
    assert rig.store.connection.execute("SELECT COUNT(*) FROM runtime_nav_episodes").fetchone()[0] == 1
    cancelled.set()
    await first
    assert not rig.lock.locked()


async def test_due_preflight_rechecks_actual_control_without_caching_send_lease(rig):
    rig.state.current = True
    due = SimpleNamespace(event_id=uuid4(), contact_id="contact", conversation_id="conversation")
    gate = rig.service.due_preflight()
    result = await gate.check(due, binding_revision=7, conversation_revision=1, global_revision=3)
    assert result.status == "ready"
    rig.state.revision = 4
    result = await gate.check(due, binding_revision=7, conversation_revision=1, global_revision=3)
    assert result.status == "needs_attention" and result.error_code == "qq_navigation_due_scope_changed"
    assert rig.state.events.count("profile_capture") == 2
    result = await gate.check(due, binding_revision=8, conversation_revision=1, global_revision=4)
    assert result.error_code == "qq_navigation_due_target_unproven"
    assert rig.state.events.count("profile_capture") == 2


async def test_profile_context_cross_task_exit_closes_tree_then_underlying_desktop(rig):
    # The actual production sequence enters in parent, then revokes and exits
    # in a bounded child. It must not stop at a cross-context token reset.
    service = rig.service
    target = rig.target
    task = rig.store.ensure_task(target, "input", now=datetime.now(UTC))
    episode = module._NavigationRound(service, target, task.task_id, asyncio.Event())
    context = episode.round(target, deadline_at=datetime.now(UTC)+timedelta(seconds=40))
    await context.__aenter__()
    parent_cap = episode.profile._round.get()
    episode.revoke_round()
    await asyncio.create_task(context.__aexit__(None, None, None))
    assert not parent_cap.active
    assert episode.cleaned and episode.owner.released and not rig.lock.locked()
    with pytest.raises(Exception, match="identity_profile_round_required"):
        await SupervisedProfileSource.capture(episode.profile, target, None, None,
            deadline_at=datetime.now(UTC)+timedelta(seconds=1))


async def test_aclose_blocks_new_round_without_claiming_orphan_cleanup(rig):
    rig.store.connection.execute("INSERT INTO runtime_nav_cleanup_obligations VALUES(?,?,?,?,?)",
        (str(uuid4()), rig.target.account_id, "prior-task", "prior-guard.json", datetime.now(UTC).isoformat()))
    assert not await rig.service.aclose()
    assert (await rig.service.navigate("binding", "input")).outcome.error_code == "qq_navigation_cleanup_required"
    assert not rig.state.backends
