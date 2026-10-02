from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import os
import time
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.vm_driver import hybrid_process as process_module
from messenger_ai.adapters.qq.vm_driver.hybrid_process import HybridProcessError, HybridWorkerProcess
from messenger_ai.adapters.qq.vm_driver.hybrid_session import HybridWorkerSession
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import HybridWorkerConfig
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerKind
from tests.adapters.qq.navigation.test_identity import case
from tests.adapters.qq.vm_driver.test_hybrid_process import (
    config, command, request, fake_rig, Desktop, echo_factory, hanging_factory,
)


class Factory:
    def __init__(self, config, *, reply=True, real_factory=None):
        self.config, self.reply, self.real_factory = config, reply, real_factory
        self.desktop = Desktop()
        self.rigs, self.calls, self.enter_tasks, self.exit_tasks = [], [], [], []

    @asynccontextmanager
    async def __call__(self, *, binding_id, purpose, worker_epoch, deadline_at):
        assert not self.desktop.lock.locked(), "new factory entered before old desktop cleanup"
        assert worker_epoch.int != 0
        self.calls.append((binding_id, purpose, worker_epoch, deadline_at))
        raw = self.config.model_dump()
        raw["navigation"]["expected_worker_epoch"] = str(worker_epoch)
        current = HybridWorkerConfig.model_validate(raw)
        if self.real_factory is None:
            rig = fake_rig(current, reply=self.reply)
            rig.desktop = self.desktop
        else:
            class Rig:
                pass
            rig = Rig()
            rig.process = HybridWorkerProcess(current, _factory=self.real_factory)
        self.rigs.append(rig)
        self.enter_tasks.append(asyncio.current_task())
        try:
            async with rig.process.protect_desktop_round(self.desktop.round(), deadline_at=deadline_at):
                yield rig.process
        finally:
            self.exit_tasks.append(asyncio.current_task())


def adopt_command(bundle, req):
    return command(WorkerKind.PREPARE, operation_id=uuid4(), text=req.body,
                   segment_ref=f"{req.pacing_plan_id}:{req.segment_index}")


@pytest.mark.asyncio
async def test_different_caller_tasks_share_actor_context_and_same_owned_process(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    caller = asyncio.current_task()
    bundle = await asyncio.create_task(session.prepare_draft(req, expected_sequence_digest="a" * 64))
    rig = factory.rigs[0]
    # The ordinary caller never receives a ContextVar desktop capability.
    assert rig.process._round.get() is None and factory.desktop.lock.locked()
    adoption = adopt_command(bundle, req)
    result = await asyncio.create_task(session.adopt_prepared(bundle.ticket, adoption))
    assert str(result.worker_epoch) == bundle.ticket.worker_epoch
    committed = await asyncio.create_task(session.execute(adoption.model_copy(update={"kind": WorkerKind.COMMIT, "request_id": uuid4()})))
    assert committed.status == "ok" and len(factory.rigs) == 1
    assert all(call.worker_epoch == rig.process.worker_epoch for call in rig.context.calls)
    await asyncio.create_task(session.abort_draft(bundle.ticket, deadline_at=req.deadline_at))
    assert session.status_snapshot().state == "idle" and not factory.desktop.lock.locked()
    assert factory.enter_tasks == factory.exit_tasks and factory.enter_tasks[0] is not caller


@pytest.mark.asyncio
async def test_observe_is_one_shot_and_leaves_no_live_process_or_capability(config):
    factory, session = Factory(config), None
    session = HybridWorkerSession(factory)
    first = await session.execute(command())
    second = await asyncio.create_task(session.execute(command()))
    assert first.worker_epoch != second.worker_epoch
    assert [call[1] for call in factory.calls] == ["observe", "observe"]
    assert all(rig.job.closed and not rig.context.process.alive for rig in factory.rigs)
    assert factory.desktop.released == 2 and session.status_snapshot().state == "idle"
    await session.aclose()


@pytest.mark.asyncio
async def test_fresh_verification_reaps_old_draft_before_new_epoch_and_is_one_use(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    old = factory.rigs[0]
    verify = command(WorkerKind.VERIFY, operation_id=uuid4(), prepared_evidence=bundle.prepared_evidence)
    async with session.verification_round(verify) as endpoint:
        assert old.job.closed and not old.context.process.alive
        fresh = await asyncio.create_task(endpoint.execute(verify))
        assert str(fresh.worker_epoch) != bundle.ticket.worker_epoch
        with pytest.raises(HybridProcessError, match="verification_round_required"):
            await endpoint.execute(verify)
    assert [call[1] for call in factory.calls] == ["draft", "verify"]
    assert not factory.desktop.lock.locked() and session.status_snapshot().state == "idle"


@pytest.mark.asyncio
async def test_busy_draft_cannot_be_replaced_by_observation_or_second_preparation(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    for call in (session.execute(command()), session.prepare_draft(request(), expected_sequence_digest="a" * 64)):
        with pytest.raises(HybridProcessError, match="busy"):
            await call
    assert len(factory.calls) == 1 and factory.desktop.lock.locked()
    await session.abort_draft(bundle.ticket, deadline_at=req.deadline_at)


@pytest.mark.parametrize("mode", ["caller", "event", "deadline"])
@pytest.mark.asyncio
async def test_cancel_and_deadline_discard_late_reply_and_confirm_reap(config, mode):
    factory = Factory(config, reply=False)
    session, event = HybridWorkerSession(factory), asyncio.Event()
    task = asyncio.create_task(session.execute(command(seconds=1.25 if mode == "deadline" else 20), cancel_event=event))
    while not factory.rigs or not factory.rigs[0].context.calls:
        await asyncio.sleep(.005)
    rig = factory.rigs[0]
    if mode == "caller":
        task.cancel()
    elif mode == "event":
        event.set()
    with pytest.raises((HybridProcessError, asyncio.CancelledError, TimeoutError)):
        await task
    assert rig.job.closed and not rig.context.process.alive and not factory.desktop.lock.locked()
    assert session.status_snapshot().state == "idle"
    # A late shared-memory response cannot revive the old epoch.
    rig.context.reply = True
    rig.context.respond()
    assert len(rig.context.calls) == 2  # Only writes the fake old mailbox.
    assert session.status_snapshot().state == "idle"
    await session.aclose()


@pytest.mark.asyncio
async def test_ready_after_cancel_not_returned(config):
    event = asyncio.Event()
    def cancelled_ack(call):
        event.set()
        from tests.adapters.qq.vm_driver.test_hybrid_process import result
        return process_module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch,
                                     action=call.action, result=result(call))
    factory, session = Factory(config, reply=cancelled_ack), None
    session = HybridWorkerSession(factory)
    with pytest.raises(HybridProcessError, match="revoked"):
        await session.execute(command(), cancel_event=event)
    assert factory.rigs[0].job.closed and not factory.desktop.lock.locked()


@pytest.mark.asyncio
async def test_reap_failure_holds_lock_and_blocks_fresh_factory_until_retry(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    rig = factory.rigs[0]
    rig.context.killable = False
    verify = command(WorkerKind.VERIFY, operation_id=uuid4(), prepared_evidence=bundle.prepared_evidence)
    with pytest.raises(HybridProcessError, match="reap_failed"):
        async with session.verification_round(verify):
            pass
    assert factory.desktop.lock.locked() and session.status_snapshot().cleanup_required
    with pytest.raises(HybridProcessError, match="cleanup_required"):
        await session.execute(command())
    assert len(factory.calls) == 1
    with pytest.raises(HybridProcessError, match="reap_failed"):
        await session.retry_cleanup()
    assert factory.desktop.lock.locked()
    rig.context.killable = True
    await asyncio.create_task(session.retry_cleanup())
    assert not factory.desktop.lock.locked() and session.status_snapshot().state == "idle"
    # The old prepared ticket grants no new process draft ownership.
    with pytest.raises(HybridProcessError, match="owner_unavailable"):
        await session.adopt_prepared(bundle.ticket, adopt_command(bundle, req))
    await session.execute(command())
    assert len(factory.calls) == 2


@pytest.mark.asyncio
async def test_sync_close_revokes_without_cross_task_context_reset_then_aclose_confirms(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    rig = factory.rigs[0]
    from contextvars import Context
    Context().run(session.close)
    assert rig.process._owner_round.active is False
    assert session.status_snapshot().state == "closing"
    await asyncio.create_task(session.aclose())
    assert session.status_snapshot().state == "closed" and not factory.desktop.lock.locked()
    assert factory.enter_tasks == factory.exit_tasks
    with pytest.raises(HybridProcessError, match="closed"):
        await session.execute(command())


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.asyncio
async def test_revoke_error_does_not_skip_context_exit_or_forget_failed_owner(config, persistent):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    rig = factory.rigs[0]
    original, attempts = rig.job.terminate, []
    def failure():
        attempts.append(1)
        if persistent or len(attempts) == 1:
            raise OSError("synthetic termination unavailable")
        original()
    rig.job.terminate = failure
    if persistent:
        with pytest.raises(HybridProcessError, match="reap_failed"):
            await session.aclose()
        assert rig.process._owner_round is None
        assert rig.process._failed_context is not None and factory.desktop.lock.locked()
        rig.job.terminate = original
        await session.retry_cleanup()
    else:
        await session.aclose()
    assert not factory.desktop.lock.locked() and not rig.process.cleanup_pending
    assert session.status_snapshot().state == "closed" and len(attempts) >= 3


@pytest.mark.asyncio
async def test_deadline_never_renews_after_adopt_and_idle_draft_is_reaped(config):
    factory, req = Factory(config), request(seconds=1.3)
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    first_deadline = factory.calls[0][3]
    await session.adopt_prepared(bundle.ticket, adopt_command(bundle, req))
    await asyncio.sleep(1.05)
    assert factory.rigs[0].job.closed and not factory.desktop.lock.locked()
    assert factory.calls[0][3] == first_deadline
    with pytest.raises(HybridProcessError, match="owner_unavailable"):
        await session.execute(command(WorkerKind.COMMIT, operation_id=session._actor.operation_id))
    await session.aclose()


@pytest.mark.asyncio
async def test_valid_ack_business_failure_keeps_exact_request_for_owned_abort(config):
    def failure(call):
        return process_module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch,
            action=call.action, error_code="synthetic_owned_write_failed", cleanup_required=True)
    factory, req = Factory(config, reply=failure), request()
    session = HybridWorkerSession(factory)
    with pytest.raises(HybridProcessError, match="owned_write_failed"):
        await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    rig = factory.rigs[0]
    assert rig.context.process.alive and factory.desktop.lock.locked()
    rig.context.reply = True
    cleanup = await asyncio.create_task(session.abort_draft(req, deadline_at=req.deadline_at))
    assert cleanup.status == "cleaned" and rig.job.closed and not factory.desktop.lock.locked()


@pytest.mark.asyncio
async def test_invalid_target_epoch_and_deadline_fail_before_business_publication(config):
    factory = Factory(config)
    session = HybridWorkerSession(factory)
    with pytest.raises(HybridProcessError, match="binding_required"):
        await session.execute(command(binding_id=None))
    with pytest.raises(HybridProcessError, match="deadline_exhausted"):
        await session.prepare_draft(request(seconds=.1), expected_sequence_digest="a" * 64)
    with pytest.raises(HybridProcessError, match="command_invalid"):
        await session.prepare_draft(request(), expected_sequence_digest="z" * 64)
    assert factory.calls == []
    @asynccontextmanager
    async def wrong_epoch(**kwargs):
        rig = fake_rig(config)
        async with rig.process.protect_desktop_round(rig.desktop.round(), deadline_at=kwargs["deadline_at"]):
            yield rig.process
    bad = HybridWorkerSession(wrong_epoch)
    with pytest.raises(HybridProcessError, match="factory_scope_mismatch"):
        await bad.execute(command())
    assert bad.status_snapshot().state == "idle"


@pytest.mark.asyncio
async def test_health_binding_is_trusted_constructor_configuration(config):
    factory = Factory(config)
    missing = HybridWorkerSession(factory)
    with pytest.raises(HybridProcessError, match="binding_required"):
        await missing.execute(command(WorkerKind.HEALTH, binding_id=None))
    assert factory.calls == []
    configured = HybridWorkerSession(factory, health_binding_id="binding")
    await configured.execute(command(WorkerKind.HEALTH, binding_id=None))
    assert factory.calls[0][0:2] == ("binding", "health")
    assert not factory.desktop.lock.locked()


@pytest.mark.asyncio
async def test_queue_is_bounded_and_cancelling_waiter_revokes_whole_owned_round(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    adoption = adopt_command(bundle, req)
    await session.adopt_prepared(bundle.ticket, adoption)
    rig = factory.rigs[0]
    rig.context.reply = False
    cmd = adoption.model_copy(update={"kind": WorkerKind.COMMIT, "request_id": uuid4()})
    first = asyncio.create_task(session.execute(cmd))
    while len(rig.context.calls) < 3:
        await asyncio.sleep(.005)
    second = asyncio.create_task(session.execute(cmd.model_copy(update={"request_id": uuid4()})))
    await asyncio.sleep(.02)
    third = asyncio.create_task(session.execute(cmd.model_copy(update={"request_id": uuid4()})))
    await asyncio.sleep(.02)
    assert session._actor.queue.qsize() == 1 and len(rig.context.calls) == 3
    third.cancel()
    outcomes = await asyncio.gather(first, second, third, return_exceptions=True)
    assert all(isinstance(value, BaseException) for value in outcomes)
    assert len(rig.context.calls) == 3 and rig.job.closed and not factory.desktop.lock.locked()


@pytest.mark.asyncio
async def test_verification_endpoint_rejects_changed_original_command(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    bundle = await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    cmd = command(WorkerKind.VERIFY, operation_id=uuid4(), prepared_evidence=bundle.prepared_evidence)
    async with session.verification_round(cmd) as endpoint:
        with pytest.raises(HybridProcessError, match="verification_round_required"):
            await endpoint.execute(cmd.model_copy(update={"operation_id": uuid4()}))
        assert (await endpoint.execute(cmd)).status == "ok"
    assert len(factory.rigs[1].context.calls) == 1 and not factory.desktop.lock.locked()


@pytest.mark.asyncio
async def test_cancel_before_actor_starts_and_during_factory_lock_wait(config):
    factory = Factory(config)
    session = HybridWorkerSession(factory)
    event = asyncio.Event()
    event.set()
    with pytest.raises(HybridProcessError, match="revoked"):
        await session.execute(command(), cancel_event=event)
    assert factory.calls == [] and session.status_snapshot().state == "idle"
    entered, release = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def waiting(**kwargs):
        entered.set()
        await release.wait()
        async with factory(**kwargs) as endpoint:
            yield endpoint
    waiting_session = HybridWorkerSession(waiting)
    task = asyncio.create_task(waiting_session.execute(command()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiting_session.status_snapshot().state == "idle" and factory.calls == []


@pytest.mark.asyncio
async def test_late_cancel_suppression_child_cannot_submit_input_after_actor_exit(config):
    factory, req = Factory(config), request()
    session = HybridWorkerSession(factory)
    await session.prepare_draft(req, expected_sequence_digest="a" * 64)
    rig, entered, wake, denied = factory.rigs[0], asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = rig.process.execute
    async def late(cmd, **kwargs):
        entered.set()
        try:
            await wake.wait()
        except asyncio.CancelledError:
            await wake.wait()
        try:
            await original(cmd, **kwargs)
        except HybridProcessError:
            denied.set()
            raise
    rig.process.execute = late
    actor = session._actor
    actor.operation_id = uuid4()
    task = asyncio.create_task(session.execute(command(WorkerKind.COMMIT, operation_id=actor.operation_id)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    wake.set()
    await asyncio.wait_for(denied.wait(), .3)
    assert len(rig.context.calls) == 1 and not factory.desktop.lock.locked()
    await session.aclose()


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job supervision")
@pytest.mark.asyncio
async def test_real_spawn_cross_caller_tasks_use_same_process_and_epoch(config):
    factory, req = Factory(config, real_factory=echo_factory), request()
    session = HybridWorkerSession(factory)
    bundle = await asyncio.create_task(session.prepare_draft(req, expected_sequence_digest="a" * 64))
    process = factory.rigs[0].process
    pid = process._active._process.pid
    adopted = await asyncio.create_task(session.adopt_prepared(bundle.ticket, adopt_command(bundle, req)))
    assert str(adopted.worker_epoch) == bundle.ticket.worker_epoch and process._active._process.pid == pid
    await asyncio.create_task(session.abort_draft(bundle.ticket, deadline_at=req.deadline_at))
    assert process._active is None and not factory.desktop.lock.locked()
    assert factory.enter_tasks == factory.exit_tasks


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job supervision")
@pytest.mark.asyncio
async def test_real_actor_cancel_reaps_worker_and_helper_before_release(config):
    factory = Factory(config, real_factory=hanging_factory)
    session = HybridWorkerSession(factory)
    task = asyncio.create_task(session.execute(command()))
    marker = config.navigation.guard_state_path + ".test-child-pid"
    from pathlib import Path
    until = time.monotonic() + 8
    while not Path(marker).exists() and time.monotonic() < until:
        await asyncio.sleep(.01)
    assert Path(marker).exists()
    process = factory.rigs[0].process
    owned = process._active
    worker_pid = owned._process.pid
    helper_pid = int(Path(marker).read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    from tests.adapters.qq.vm_driver.test_hybrid_process import windows_process_gone
    assert windows_process_gone(worker_pid) and windows_process_gone(helper_pid)
    assert process._active is None and not factory.desktop.lock.locked()
    await session.aclose()
