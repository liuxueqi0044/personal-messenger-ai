from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import UUID, uuid4

import pytest

from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState, WindowsNavigationConfig
from messenger_ai.adapters.qq.vm_driver.hybrid_process import HybridProcessError
from messenger_ai.adapters.qq.vm_driver.hybrid_session import HybridWorkerSession
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import HybridWorkerConfig
from messenger_ai.runtime.qq_guard import QQGuardPublicationError
from messenger_ai.runtime.qq_hybrid_rounds import QQHybridRoundFactory, QQHybridRoundError
from tests.adapters.qq.navigation.test_identity import case
from tests.adapters.qq.navigation.test_profile_verifier import report
from tests.adapters.qq.vm_driver.test_hybrid_process import config, command, request, fake_rig
from tests.runtime.test_qq_hybrid_navigation import rig as navigation_rig


@pytest.fixture
def settings(config, tmp_path):
    def navigation_config(*, worker_epoch, guard_state_path):
        raw = config.navigation.model_dump()
        raw.update(expected_worker_epoch=str(worker_epoch), guard_state_path=guard_state_path)
        return WindowsNavigationConfig.model_validate(raw)
    return SimpleNamespace(guard_directory=tmp_path / "round-guards", helper_path=config.helper_path,
        vault_path=config.vault_path, session_epoch="session", surface_epoch="surface", max_seconds=45,
        prepare_write_reserve_seconds=config.prepare_write_reserve_seconds, run_id=config.navigation.expected_run_id,
        targets=MappingProxyType({x.binding_id:x for x in config.targets}),
        expectations=MappingProxyType({x.binding_id:x for x in config.expectations}), navigation_config=navigation_config)


class LiveSource:
    def __init__(self, config, lock, settings):
        self.config, self.lock, self.settings = config, lock, settings
        self.calls, self.changes = [], {}
        self.paused, self.has_owned_draft, self.has_commit_obligation = False, False, False
        self.revision = 3
    def __call__(self, **scope):
        assert self.lock.locked(), "runtime snapshot must be inside the shared desktop owner"
        self.calls.append(scope)
        nav = self.config.navigation
        value = dict(target=scope["target"], run_id=nav.expected_run_id,
            session_epoch=self.settings.session_epoch, surface_epoch=self.settings.surface_epoch,
            worker_epoch=str(scope["worker_epoch"]), observation_epoch=scope["observation_epoch"],
            desktop_lease_id=scope["desktop_lease_id"], lease_expires_at=scope["deadline_at"],
            process_id=nav.window.process_id, window_handle=nav.window.window_handle,
            process_started_at_100ns=nav.expected_process_started_at_100ns,
            control_revision=self.revision, paused=self.paused, has_owned_draft=self.has_owned_draft,
            has_commit_obligation=self.has_commit_obligation, published_at=datetime.now(UTC))
        value.update(self.changes)
        # model_construct also exercises the complete re-validation boundary.
        return NavigationGuardState.model_construct(**value)


class Publisher:
    def __init__(self, rig, path, initial, source):
        self.rig, self.path, self.initial, self.source = rig, path, initial, source
        self.failed_event = asyncio.Event()
        self.started, self.closed = False, False
        self.fail_start, self.fail_close = False, False
    async def start(self):
        assert self.rig.lock.locked() and all(worker.job.closed for worker in self.rig.workers)
        self.rig.order.append("publisher_start")
        if self.fail_start:
            raise QQGuardPublicationError("qq_guard_source_failed")
        self.started = True
    def raise_if_failed(self):
        if self.failed_event.is_set():
            raise QQGuardPublicationError("qq_guard_source_failed")
    async def aclose(self):
        assert self.rig.lock.locked()
        assert all(worker.job.empty() for worker in self.rig.workers)
        self.rig.order.append("publisher_close")
        if self.fail_close:
            raise QQGuardPublicationError("qq_guard_publication_failed")
        self.closed = True
    async def publish_now(self):
        self.raise_if_failed()
        self.initial = self.source()


class Rig:
    def __init__(self, config, settings, *, default_publisher=False, reply=True):
        self.config, self.settings, self.reply = config, settings, reply
        self.lock, self.order, self.workers, self.publishers = asyncio.Lock(), [], [], []
        self.source = LiveSource(config, self.lock, settings)
        self.fail_start, self.fail_close = False, False
        def publisher_factory(path, initial, source):
            pub = Publisher(self, path, initial, source)
            pub.fail_start, pub.fail_close = self.fail_start, self.fail_close
            self.publishers.append(pub)
            return pub
        def worker_factory(current, **options):
            assert self.lock.locked()
            if not default_publisher:
                assert self.publishers[-1].started
            self.order.append("worker_construct")
            worker = fake_rig(current, reply=self.reply, max_seconds=options["max_seconds"])
            self.workers.append(worker)
            return worker.process
        options = {"_worker_factory":worker_factory}
        if not default_publisher:
            options["publisher_factory"] = publisher_factory
        self.factory = QQHybridRoundFactory(settings, config.selector_pack, config.bindings,
            self.lock, self.source, **options)


def round_for(rig, *, epoch=None, purpose="observe", seconds=20):
    return rig.factory(binding_id="binding", purpose=purpose, worker_epoch=epoch or uuid4(),
                       deadline_at=datetime.now(UTC)+timedelta(seconds=seconds))


@pytest.mark.asyncio
async def test_complete_live_scope_published_before_worker_and_reap_before_stop_unlock(config, settings):
    rig, epoch = Rig(config, settings), uuid4()
    task = asyncio.current_task()
    async with round_for(rig, epoch=epoch) as worker:
        assert rig.order == ["publisher_start", "worker_construct"]
        nav = worker.config.navigation
        scope = rig.source.calls[0]
        assert scope["worker_epoch"] == epoch and nav.expected_worker_epoch == str(epoch)
        assert scope["purpose"] == "observe" and isinstance(UUID(scope["desktop_lease_id"]), UUID)
        assert isinstance(UUID(scope["observation_epoch"]), UUID)
        assert worker._round.get() is worker._owner_round and asyncio.current_task() is task
        assert Path(nav.guard_state_path).is_absolute()
        assert Path(nav.guard_state_path).parent == settings.guard_directory.resolve()
        assert (await worker.execute(command())).status == "ok"
        assert rig.lock.locked()
    assert rig.workers[0].job.closed and rig.publishers[0].closed and not rig.lock.locked()
    assert rig.order[-1] == "publisher_close" and worker._round.get() is None
    assert rig.factory._owners == {}


@pytest.mark.asyncio
async def test_real_guard_publisher_records_actual_dynamic_flags_without_defaults(config, settings):
    rig = Rig(config, settings, default_publisher=True)
    async with round_for(rig) as worker:
        path = Path(worker.config.navigation.guard_state_path)
        first = NavigationGuardState.model_validate_json(path.read_text())
        assert first.control_revision == 3 and not first.paused
        rig.source.paused, rig.source.has_owned_draft, rig.source.has_commit_obligation = True, True, True
        rig.source.revision = 4
        until = asyncio.get_running_loop().time()+1
        while asyncio.get_running_loop().time() < until:
            value = NavigationGuardState.model_validate_json(path.read_text())
            if value.control_revision == 4:
                break
            await asyncio.sleep(.01)
        assert value.paused and value.has_owned_draft and value.has_commit_obligation
        assert value.lease_expires_at == first.lease_expires_at
        assert value.worker_epoch == first.worker_epoch and value.observation_epoch == first.observation_epoch
        await worker.execute(command())  # Pure synthetic worker, no native action.
    assert not rig.lock.locked() and rig.workers[0].job.closed


@pytest.mark.parametrize("field,bad", [
    ("worker_epoch", str(uuid4())), ("desktop_lease_id", "different"), ("observation_epoch", "different"),
    ("run_id", "different"), ("session_epoch", "different"), ("surface_epoch", "different"),
    ("process_id", 999), ("window_handle", 999), ("process_started_at_100ns", 999),
    ("paused", "false"), ("has_owned_draft", 0), ("has_commit_obligation", None),
    ("published_at", datetime.now(UTC)-timedelta(seconds=10)),
    ("lease_expires_at", datetime.now(UTC)+timedelta(seconds=120)),
])
@pytest.mark.asyncio
async def test_unproven_live_snapshot_never_starts_publisher_or_worker(config, settings, field, bad):
    rig = Rig(config, settings)
    rig.source.changes[field] = bad
    with pytest.raises(QQHybridRoundError, match="live_snapshot_unproven"):
        async with round_for(rig):
            pass
    assert rig.publishers == [] and rig.workers == [] and not rig.lock.locked()


@pytest.mark.asyncio
async def test_snapshot_must_be_sync_complete_closed_value(config, settings):
    rig = Rig(config, settings)
    async def async_snapshot(**_):
        raise AssertionError("must not be awaited")
    rig.factory.live_snapshot = async_snapshot
    with pytest.raises(QQHybridRoundError, match="live_snapshot_unproven"):
        async with round_for(rig):
            pass
    assert not rig.lock.locked() and not rig.workers


@pytest.mark.parametrize("mode", ["deadline", "cancel"])
@pytest.mark.asyncio
async def test_lock_wait_in_original_deadline_never_releases_someone_elses_owner(config, settings, mode):
    rig = Rig(config, settings)
    await rig.lock.acquire()
    async def attempt():
        async with round_for(rig, seconds=1.15):
            pytest.fail("shared lock unexpectedly entered")
    task = asyncio.create_task(attempt())
    if mode == "cancel":
        await asyncio.sleep(.02)
        task.cancel()
    with pytest.raises((TimeoutError, asyncio.CancelledError)):
        await task
    assert rig.lock.locked() and not rig.source.calls and not rig.publishers and not rig.workers
    rig.lock.release()


@pytest.mark.asyncio
async def test_publisher_start_failure_self_cleans_before_factory_enter_raises(config, settings):
    rig = Rig(config, settings)
    rig.fail_start = True
    with pytest.raises(QQHybridRoundError, match="qq_guard_source_failed"):
        async with round_for(rig):
            pass
    assert rig.publishers[0].closed and not rig.workers and not rig.lock.locked()


@pytest.mark.asyncio
async def test_unique_guard_and_epoch_scope_never_mutate_trusted_base_config(config, settings):
    rig, paths, scopes = Rig(config, settings), [], []
    original = config.model_dump_json()
    for _ in range(2):
        async with round_for(rig) as worker:
            paths.append(worker.config.navigation.guard_state_path)
            scopes.append(rig.source.calls[-1])
            await worker.execute(command())
    assert len(set(paths)) == 2 and scopes[0]["desktop_lease_id"] != scopes[1]["desktop_lease_id"]
    assert scopes[0]["observation_epoch"] != scopes[1]["observation_epoch"]
    assert config.model_dump_json() == original and not rig.lock.locked()
    epoch = rig.workers[0].process.worker_epoch
    with pytest.raises(QQHybridRoundError, match="epoch_reused"):
        async with round_for(rig, epoch=epoch):
            pass
    assert len(rig.workers) == 2


@pytest.mark.parametrize("scope", [{"binding_id":"unknown"}, {"purpose":"arbitrary"},
                                  {"worker_epoch":UUID(int=0)}, {"worker_epoch":"model supplied"}])
@pytest.mark.asyncio
async def test_closed_factory_scope_before_lock_or_file(config, settings, scope):
    rig = Rig(config, settings)
    args = dict(binding_id="binding", purpose="observe", worker_epoch=uuid4(), deadline_at=datetime.now(UTC)+timedelta(seconds=20))
    args.update(scope)
    with pytest.raises(QQHybridRoundError, match="scope_invalid"):
        async with rig.factory(**args):
            pass
    assert not rig.lock.locked() and rig.source.calls == [] and not settings.guard_directory.exists()


@pytest.mark.asyncio
async def test_worker_reap_failure_keeps_publisher_and_shared_lock_until_exact_retry(config, settings):
    rig = Rig(config, settings)
    with pytest.raises(HybridProcessError, match="reap_failed"):
        async with round_for(rig) as worker:
            await worker.execute(command())
            rig.workers[0].context.killable = False
    assert rig.lock.locked() and not rig.publishers[0].closed and worker.cleanup_pending
    with pytest.raises(HybridProcessError, match="reap_failed"):
        await worker.retry_cleanup()
    assert not rig.publishers[0].closed and rig.lock.locked()
    rig.workers[0].context.killable = True
    await worker.retry_cleanup()
    assert rig.publishers[0].closed and not rig.lock.locked() and not worker.cleanup_pending


@pytest.mark.asyncio
async def test_publisher_close_failure_after_reap_keeps_original_context_for_retry(config, settings):
    rig = Rig(config, settings)
    rig.fail_close = True
    with pytest.raises(QQHybridRoundError, match="qq_guard_publication_failed"):
        async with round_for(rig) as worker:
            await worker.execute(command())
    assert rig.workers[0].job.closed and rig.lock.locked() and worker.cleanup_pending
    rig.publishers[0].fail_close = False
    await worker.retry_cleanup()
    assert rig.publishers[0].closed and not rig.lock.locked() and not worker.cleanup_pending


@pytest.mark.asyncio
async def test_guard_failure_revokes_and_cleans_actor_on_its_original_context(config, settings):
    rig = Rig(config, settings)
    session = HybridWorkerSession(rig.factory)
    await asyncio.create_task(session.prepare_draft(request(), expected_sequence_digest="a" * 64))
    actor = session._actor
    rig.publishers[0].failed_event.set()
    await asyncio.wait_for(asyncio.shield(actor.finished), 1)
    assert rig.workers[0].job.closed and rig.publishers[0].closed and not rig.lock.locked()
    assert not actor.cleanup_required and actor.endpoint._round.get() is None
    await session.aclose()


@pytest.mark.asyncio
async def test_cancelled_factory_body_reaps_then_stops_publisher_before_unlock(config, settings):
    rig, entered = Rig(config, settings), asyncio.Event()
    async def use():
        async with round_for(rig) as worker:
            await worker.execute(command())
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(use())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.workers[0].job.closed and rig.publishers[0].closed and not rig.lock.locked()


@pytest.mark.asyncio
async def test_total_deadline_clamps_to_45_and_snapshot_cannot_renew_it(config, settings):
    rig = Rig(config, settings)
    before = datetime.now(UTC)
    async with round_for(rig, seconds=120) as worker:
        scope = rig.source.calls[0]
        assert 44 < (scope["deadline_at"]-before).total_seconds() <= 45.01
        rig.source.changes["lease_expires_at"] = scope["deadline_at"]+timedelta(seconds=1)
        with pytest.raises(QQHybridRoundError, match="live_snapshot_unproven"):
            rig.publishers[0].source()
        rig.source.changes.clear()
        await worker.execute(command())
    assert not rig.lock.locked()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="production Windows settings path syntax")
async def test_actual_frozen_settings_feed_one_target_factory_with_certified_selectors(tmp_path):
    from tests.runtime.test_qq_hybrid_config import configuration
    from messenger_ai.runtime.qq_hybrid_config import parse_hybrid_settings
    raw, pack, bindings = configuration()
    raw.update(guard_directory=str(tmp_path / "certified-guards"), helper_path=str(tmp_path / "fixed-helper.exe"),
               vault_path=str(tmp_path / "vault.json"))
    parsed = parse_hybrid_settings(raw, selector_pack=pack, bindings=bindings,
        binding_revisions={b.binding_id:1 for b in bindings}, run_id="trusted-run")
    template = HybridWorkerConfig(navigation=parsed.navigation_config(worker_epoch=uuid4(),
        guard_state_path=str(Path(parsed.guard_directory) / "template.json")), selector_pack=pack,
        bindings=bindings, targets=tuple(parsed.targets.values()), expectations=tuple(parsed.expectations.values()),
        helper_path=parsed.helper_path, vault_path=parsed.vault_path)
    rig, epoch = Rig(template, parsed), uuid4()
    async with rig.factory(binding_id=bindings[1].binding_id, purpose="observe", worker_epoch=epoch,
                           deadline_at=datetime.now(UTC)+timedelta(seconds=20)) as worker:
        assert worker.config.bindings == (bindings[1],)
        assert worker.config.targets == (parsed.targets[bindings[1].binding_id],)
        assert worker.config.navigation.row_selector.class_name_tokens == ("recent-contact-item",)
        assert worker.config.navigation.name_container_selector.class_name_tokens == ("item__info",)
        assert worker.config.navigation.expected_run_id == parsed.run_id
        assert (await worker.execute(command(binding_id=bindings[1].binding_id, binding_revision=1))).status == "ok"
    assert not rig.lock.locked() and rig.workers[0].job.closed


@pytest.mark.asyncio
async def test_default_publisher_heartbeat_failure_reaps_actor_before_lock_release(config, settings):
    rig = Rig(config, settings, default_publisher=True)
    session = HybridWorkerSession(rig.factory)
    await session.prepare_draft(request(), expected_sequence_digest="a" * 64)
    actor = session._actor
    rig.source.changes["window_handle"] = 999
    await asyncio.wait_for(asyncio.shield(actor.finished), 1.5)
    assert rig.workers[0].job.closed and not rig.lock.locked() and not actor.cleanup_required
    await session.aclose()


@pytest.mark.asyncio
async def test_guard_failure_and_failed_reap_keep_publisher_until_session_retry(config, settings):
    rig = Rig(config, settings)
    session = HybridWorkerSession(rig.factory)
    await session.prepare_draft(request(), expected_sequence_digest="a" * 64)
    actor = session._actor
    rig.workers[0].context.killable = False
    rig.publishers[0].failed_event.set()
    await asyncio.wait_for(asyncio.shield(actor.finished), 1.5)
    assert rig.lock.locked() and actor.cleanup_required and not rig.publishers[0].closed
    assert actor.endpoint.cleanup_pending
    assert rig.factory._owners[actor.epoch].closing
    with pytest.raises(QQHybridRoundError, match="refresh_owner_unavailable"):
        await rig.factory.refresh_guard(actor.epoch)
    rig.workers[0].context.killable = True
    await session.retry_cleanup()
    assert rig.publishers[0].closed and not rig.lock.locked() and session.status_snapshot().state == "idle"
    assert rig.factory._owners == {}


@pytest.mark.asyncio
async def test_explicit_guard_refresh_cross_caller_fences_owned_and_commit_flags_before_ipc(config, settings):
    rig = Rig(config, settings, default_publisher=True)
    session = HybridWorkerSession(rig.factory)
    req = request()
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    epoch = UUID(bundle.ticket.worker_epoch)
    worker = rig.workers[0].process
    assert worker._round.get() is None  # The caller owns no desktop capability.
    path = Path(worker.config.navigation.guard_state_path)
    before = NavigationGuardState.model_validate_json(path.read_text())
    rig.source.has_owned_draft, rig.source.has_commit_obligation = True, True
    rig.source.revision = 4
    await asyncio.create_task(rig.factory.refresh_guard(epoch))
    after = NavigationGuardState.model_validate_json(path.read_text())
    assert after.has_owned_draft and after.has_commit_obligation and after.control_revision == 4
    assert after.lease_expires_at == before.lease_expires_at
    assert after.desktop_lease_id == before.desktop_lease_id and after.observation_epoch == before.observation_epoch
    assert len(rig.workers[0].context.calls) == 1  # Refresh publishes no worker IPC.
    from tests.adapters.qq.vm_driver.test_hybrid_session import adopt_command
    adoption = adopt_command(bundle, req)
    await session.adopt_prepared(bundle.ticket, adoption)
    from messenger_ai.adapters.qq.vm_driver.contracts import WorkerKind
    assert (await session.execute(adoption.model_copy(update={"kind":WorkerKind.COMMIT,"request_id":uuid4()}))).status == "ok"
    await session.aclose()
    assert rig.factory._owners == {} and not rig.lock.locked()


@pytest.mark.parametrize("value", [uuid4(), "model supplied", UUID(int=0)])
@pytest.mark.asyncio
async def test_refresh_requires_exact_current_actual_owner_and_never_revokes_other_epoch(config, settings, value):
    rig = Rig(config, settings)
    async with round_for(rig) as worker:
        await worker.execute(command())
        before = len(rig.source.calls)
        with pytest.raises(QQHybridRoundError, match="refresh_owner_unavailable"):
            await rig.factory.refresh_guard(value)
        assert worker._owner_round.active and rig.lock.locked() and len(rig.source.calls) == before
    with pytest.raises(QQHybridRoundError, match="refresh_owner_unavailable"):
        await rig.factory.refresh_guard(worker.worker_epoch)
    assert rig.factory._owners == {} and not rig.lock.locked()


@pytest.mark.asyncio
async def test_refresh_failure_revokes_actor_and_denies_any_following_commit(config, settings):
    rig = Rig(config, settings, default_publisher=True)
    session = HybridWorkerSession(rig.factory)
    bundle = await session.prepare_draft(request(), expected_sequence_digest="a" * 64)
    actor = session._actor
    rig.source.changes["process_started_at_100ns"] = 999
    with pytest.raises(QQHybridRoundError):
        await rig.factory.refresh_guard(actor.epoch)
    await asyncio.wait_for(asyncio.shield(actor.finished), 1)
    assert rig.workers[0].job.closed and not rig.lock.locked()
    assert len(rig.workers[0].context.calls) == 1 and rig.factory._owners == {}
    from tests.adapters.qq.vm_driver.test_hybrid_session import adopt_command
    with pytest.raises(HybridProcessError, match="owner_unavailable"):
        await session.adopt_prepared(bundle.ticket, adopt_command(bundle, request()))
    await session.aclose()


@pytest.mark.asyncio
async def test_startup_consumes_original_monotonic_budget_even_if_utc_rolls_back(config, settings, monkeypatch):
    from messenger_ai.runtime import qq_hybrid_rounds as rounds_module
    from messenger_ai.adapters.qq.vm_driver import hybrid_process as process_module
    rig, offset = Rig(config, settings), [0]
    class Clock:
        @classmethod
        def now(cls, timezone):
            return datetime.now(timezone)+timedelta(seconds=offset[0])
    original = rig.factory._publisher_factory
    def delayed(path, initial, source):
        pub = original(path, initial, source)
        start = pub.start
        async def start_then_rollback():
            await start()
            await asyncio.sleep(.1)
            offset[0] = -10
        pub.start = start_then_rollback
        return pub
    rig.factory._publisher_factory = delayed
    monkeypatch.setattr(rounds_module, "datetime", Clock)
    monkeypatch.setattr(process_module, "datetime", Clock)
    before = time.monotonic()
    async with round_for(rig, seconds=2) as worker:
        assert worker.max_seconds < 1.95
        assert worker._owner_round.stop_at <= before+2.02
        assert (await worker.execute(command())).status == "ok"
    assert not rig.lock.locked() and rig.workers[0].job.closed


@pytest.mark.parametrize("separate_caller", [False, True])
@pytest.mark.asyncio
async def test_real_navigation_cleanup_then_hybrid_actor_same_shared_lock_and_close(
        config, navigation_rig, separate_caller):
    """Actual N1/N2 coordinator + protected owners + N4 actor, native values fake."""
    nav = navigation_rig
    workers, sources = [], []
    def snapshot(**scope):
        assert nav.lock.locked()
        sources.append(scope)
        settings, state = nav.settings, nav.state
        return NavigationGuardState(target=scope["target"], run_id=settings.run_id,
            session_epoch=settings.session_epoch, surface_epoch=settings.surface_epoch,
            worker_epoch=str(scope["worker_epoch"]), observation_epoch=scope["observation_epoch"],
            desktop_lease_id=scope["desktop_lease_id"], lease_expires_at=scope["deadline_at"],
            process_id=settings.window.process_id, window_handle=settings.window.window_handle,
            process_started_at_100ns=settings.process_started_at_100ns, control_revision=state.revision,
            paused=state.paused, has_owned_draft=state.owned, has_commit_obligation=state.commit,
            published_at=datetime.now(UTC))
    nav.service.live_snapshot = snapshot
    def worker_factory(current, **options):
        assert nav.lock.locked()
        assert nav.state.backends[-1].closed
        assert not nav.store.connection.execute("SELECT * FROM runtime_nav_cleanup_obligations").fetchall()
        rig = fake_rig(current, max_seconds=options["max_seconds"])
        workers.append(rig)
        return rig.process
    rounds = QQHybridRoundFactory(nav.settings, config.selector_pack, config.bindings,
        nav.lock, snapshot, _worker_factory=worker_factory)
    session = HybridWorkerSession(rounds)
    async def caller(awaitable):
        return await asyncio.create_task(awaitable) if separate_caller else await awaitable
    # Repeat the same handoff. In particular, N1's inactive parent ContextVar
    # is not authority for N4 and cannot make its fresh owner reentrant.
    for turn in range(2):
        opened = await asyncio.wait_for(caller(nav.service.navigate("binding", f"handoff/{turn}")), 1)
        assert opened.outcome.status == "candidate_opened"
        assert opened.active_chat_lease is not None and not nav.lock.locked()
        profile_capability = nav.state.profiles[-1]._round.get()
        assert profile_capability is None or not profile_capability.active
        observed = await asyncio.wait_for(caller(session.execute(command())), 1)
        assert observed.status == "ok"
        assert str(observed.worker_epoch) != opened.active_chat_lease.worker_epoch
        assert session.status_snapshot().state == "idle"
        assert workers[-1].job.closed and not workers[-1].context.process.alive
        assert rounds._owners == {} and not nav.lock.locked()
        assert workers[-1].process._round.get() is None
    assert {value["purpose"] for value in sources} == {"navigation", "observe"}
    assert await nav.service.aclose()
    await asyncio.wait_for(caller(session.aclose()), 1)
    assert session.status_snapshot().state == "closed" and not nav.lock.locked()


@pytest.mark.asyncio
async def test_navigation_handoff_n4_guard_entry_failure_has_bounded_actor_finish_and_close(config, navigation_rig):
    nav = navigation_rig
    opened = await nav.service.navigate("binding", "handoff/entry-failure")
    assert opened.outcome.status == "candidate_opened" and not nav.lock.locked()
    def unavailable(**_):
        assert nav.lock.locked()
        raise ValueError("synthetic runtime source unavailable")
    rounds = QQHybridRoundFactory(nav.settings, config.selector_pack, config.bindings, nav.lock, unavailable)
    session = HybridWorkerSession(rounds)
    with pytest.raises(QQHybridRoundError, match="live_snapshot_unproven"):
        await asyncio.wait_for(session.execute(command()), 1)
    assert session.status_snapshot().state == "idle" and not nav.lock.locked()
    assert rounds._owners == {}
    await asyncio.wait_for(session.aclose(), 1)
    assert await nav.service.aclose()


@pytest.mark.asyncio
async def test_navigation_handoff_n4_unanswered_ipc_closes_in_original_budget(config, navigation_rig):
    nav, workers = navigation_rig, []
    opened = await nav.service.navigate("binding", "handoff/timeout")
    assert opened.outcome.status == "candidate_opened" and not nav.lock.locked()
    source = LiveSource(config, nav.lock, nav.settings)
    def worker_factory(current, **options):
        rig = fake_rig(current, max_seconds=options["max_seconds"], reply=False)
        workers.append(rig)
        return rig.process
    rounds = QQHybridRoundFactory(nav.settings, config.selector_pack, config.bindings,
        nav.lock, source, _worker_factory=worker_factory)
    session = HybridWorkerSession(rounds)
    started = time.monotonic()
    with pytest.raises((HybridProcessError, TimeoutError)):
        await asyncio.wait_for(session.execute(command(seconds=1.25)), 1.7)
    assert time.monotonic()-started < 1.7
    assert workers[0].job.closed and not workers[0].context.process.alive
    assert rounds._owners == {} and not nav.lock.locked()
    await asyncio.wait_for(session.aclose(), 1)
    assert await nav.service.aclose()
