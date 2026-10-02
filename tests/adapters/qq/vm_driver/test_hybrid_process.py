from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
import ctypes
from datetime import UTC, datetime, timedelta
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelector, QQSelectorPack
from messenger_ai.adapters.qq.navigation.windows_backend import WindowsNavigationConfig
from messenger_ai.adapters.qq.vm_driver import hybrid_process as module
from messenger_ai.adapters.qq.vm_driver.contracts import (
    PreparedTargetIdentity, PreparedVerificationEvidence, WorkerCommand, WorkerKind, WorkerResult,
)
from messenger_ai.adapters.qq.vm_driver.hybrid_process import HybridPreparedDraft, HybridProcessError, HybridWorkerProcess
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import HybridWorkerConfig
from messenger_ai.runtime.staged_preparation import DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket, source_keys_digest
from tests.adapters.qq.navigation.test_identity import case


@pytest.fixture
def config(case, tmp_path):
    target, _, expectation, _ = case
    selectors = {name: QQSelector(name=name, control_type="Text") for name in
                 ("list", "row", "name", "search", "header", "composer", "bubbles", "send")}
    nav = WindowsNavigationConfig(guard_state_path=str(tmp_path / "guard.json"),
        window=dict(process_id=123, window_handle=456, class_name="QQ"),
        expected_process_started_at_100ns=123456789, expected_run_id="run", expected_worker_epoch=str(uuid4()),
        **{f"{name}_selector": selectors["bubbles" if name == "message" else name] for name in
           ("list", "row", "name", "search", "header", "composer", "message")})
    binding = QQIdentityBinding(hub_conversation_id=target.conversation_id, contact_id="contact", account_id="account",
        platform_conversation_id="platform", participant_signature="unchanged", binding_id=target.binding_id, conversation_type="direct")
    pack = QQSelectorPack(client_version=expectation.client_version, environment_fingerprint=expectation.environment_fingerprint,
        selectors=tuple(selectors.values()), last_verified_at=datetime.now(UTC), fixture_suite_version=expectation.selector_pack_version)
    return HybridWorkerConfig(navigation=nav, selector_pack=pack, bindings=(binding,), targets=(target,),
        expectations=(expectation,), helper_path=str(tmp_path / "fixed.exe"), vault_path=str(tmp_path / "vault"))


def command(kind=WorkerKind.OBSERVE, *, seconds=20, **updates):
    values = dict(kind=kind, binding_id="binding", binding_revision=7, conversation_revision=2,
                  deadline=datetime.now(UTC) + timedelta(seconds=seconds))
    values.update(updates)
    return WorkerCommand(**values)


def request(seconds=20):
    now, tick = datetime.now(UTC), time.monotonic_ns()
    body = "synthetic reply"
    return DraftPreparationRequest(reservation_id=uuid4(), nonce=uuid4(), outbox_id=1, claim_token="claim", due_event_id=uuid4(),
        account_id="account", contact_id="contact", conversation_id="conversation", binding_id="binding", binding_revision=7,
        conversation_revision=2, global_revision=3, pacing_plan_id=uuid4(), segment_index=0, draft_id=uuid4(), body=body,
        body_hash=hashlib.sha256(body.encode()).hexdigest(), source_message_keys=("durable1",),
        source_keys_digest=source_keys_digest(("durable1",)), expected_last_message_key="durable1", original_snapshot_digest="e" * 64,
        requested_at=now, deadline_at=now + timedelta(seconds=seconds), requested_monotonic_ns=tick,
        deadline_monotonic_ns=tick + int(seconds * 1e9))


def portable(config, req):
    nav = config.navigation
    return PreparedVerificationEvidence(owner_binding_id=req.binding_id,
        target_identity=PreparedTargetIdentity(binding_id=req.binding_id, participant_signature="qq-profile-hmac:" + "a" * 64,
            conversation_type="direct", process_id=nav.window.process_id, window_handle=nav.window.window_handle),
        before_bubbles=(), text_hash=req.body_hash, segment_ref=f"{req.pacing_plan_id}:{req.segment_index}")


def prepared(config, req, sequence, deadline):
    proof, now, tick = portable(config, req), datetime.now(UTC), time.monotonic_ns()
    seconds = min(10, (min(req.deadline_at, deadline) - now).total_seconds())
    delta_us = int(seconds * 1e6)
    nav = config.navigation
    ticket = PreparedDraftTicket(**req.model_dump(exclude={"body"}), run_id=nav.expected_run_id,
        session_epoch="session", surface_epoch="surface", worker_epoch=nav.expected_worker_epoch,
        process_id=nav.window.process_id, window_handle=nav.window.window_handle,
        process_started_at_100ns=nav.expected_process_started_at_100ns,
        issued_at=now, expires_at=now + timedelta(microseconds=delta_us), issued_monotonic_ns=tick,
        expires_monotonic_ns=tick + delta_us * 1000,
        evidence_digest=hashlib.sha256(proof.model_dump_json().encode()).hexdigest(), expected_sequence_digest=sequence)
    return HybridPreparedDraft(ticket=ticket, prepared_evidence=proof)


def result(call, **updates):
    cmd = call.command
    values = dict(request_id=cmd.request_id, kind=cmd.kind, status="ok", worker_epoch=call.worker_epoch,
        operation_id=cmd.operation_id, binding_id=cmd.binding_id, binding_revision=cmd.binding_revision,
        conversation_revision=cmd.conversation_revision)
    values.update(updates)
    return WorkerResult(**values)


class Desktop:
    def __init__(self):
        self.lock, self.released = asyncio.Lock(), 0
        self.before_release = lambda: None
    @asynccontextmanager
    async def round(self):
        async with self.lock:
            try:
                yield
            finally:
                self.before_release()
                self.released += 1


class Signal(threading.Event):
    def __init__(self, hook=None):
        super().__init__()
        self.hook = hook
    def set(self):
        super().set()
        if self.hook:
            self.hook()


class FakeProcess:
    def __init__(self, context, **values):
        self.context, self.args = context, values["args"]
        self.pid, self.alive, self.events = None, False, []
    def start(self):
        assert not self.args[4].is_set()
        self.pid, self.alive = 12345, True
        self.events.append("start")
    def is_alive(self):
        return self.alive
    def terminate(self):
        self.events.append("terminate")
        if self.context.killable:
            self.alive = False
    kill = terminate
    def join(self, *, timeout):
        self.events.append(("join", timeout))
    def close(self):
        assert not self.alive
        self.events.append("close")


class FakeContext:
    def __init__(self, config, reply=True):
        self.config, self.reply, self.event_index = config, reply, 0
        self.killable, self.process, self.calls = True, None, []
    def Event(self):
        index = self.event_index
        self.event_index += 1
        return Signal(self.respond if index == 2 else None)
    def RawArray(self, _, size):
        return bytearray(size)
    def RawValue(self, _, value):
        return SimpleNamespace(value=value)
    def Process(self, **values):
        self.process = FakeProcess(self, **values)
        return self.process
    def respond(self):
        args = self.process.args
        assert args[4].is_set()  # Admission follows Job assignment.
        call = module._Call.model_validate_json(bytes(args[8][:args[9].value]))
        self.calls.append(call)
        if self.reply is False:
            return
        if callable(self.reply):
            reply = self.reply(call)
        elif call.action == "prepare_draft":
            reply = module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
                prepared=prepared(self.config, call.request, call.expected_sequence_digest, call.deadline_at))
        elif call.action == "abort_draft":
            reply = module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
                cleanup=DraftCleanupResult(status="cleaned", reservation_id=call.owner.reservation_id, nonce=call.owner.nonce))
        else:
            reply = module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action, result=result(call))
        raw = reply if isinstance(reply, bytes) else reply.model_dump_json().encode()
        args[10][:len(raw)] = raw
        args[11].value = len(raw)
        args[7].set()


class FakeJob:
    def __init__(self, context):
        self.context, self.assigned, self.closed, self.helper_alive, self.fail_assign = context, False, False, False, False
    def assign(self, process):
        assert not process.args[4].is_set()
        if self.fail_assign:
            raise OSError("private native information")
        self.assigned = True
    def terminate(self):
        if self.context.killable:
            self.helper_alive = False
            if self.context.process:
                self.context.process.terminate()
    def empty(self):
        return not self.helper_alive and not (self.context.process and self.context.process.alive)
    def close(self):
        assert self.empty()
        self.closed = True


def fake_rig(config, *, reply=True, max_seconds=20):
    context, desktop = FakeContext(config, reply), Desktop()
    job = FakeJob(context)
    process = HybridWorkerProcess(config, max_seconds=max_seconds, _context=context, _job_factory=lambda: job)
    desktop.before_release = lambda: (pytest.fail("desktop released while owned job alive") if not job.empty() else None)
    return SimpleNamespace(process=process, context=context, desktop=desktop, job=job)


def protected(rig, seconds=20):
    return rig.process.protect_desktop_round(rig.desktop.round(), deadline_at=datetime.now(UTC) + timedelta(seconds=seconds))


@pytest.mark.asyncio
async def test_one_owned_worker_epoch_cold_prepare_adopt_commit_and_abort(config):
    rig = fake_rig(config)
    async with protected(rig):
        req = request()
        bundle = await rig.process.prepare_draft(req, expected_sequence_digest="a" * 64)
        adopt = command(WorkerKind.PREPARE, operation_id=uuid4(), text=req.body, segment_ref=f"{req.pacing_plan_id}:0")
        assert (await rig.process.adopt_prepared(bundle.ticket, adopt)).status == "ok"
        commit = adopt.model_copy(update={"request_id": uuid4(), "kind": WorkerKind.COMMIT})
        assert (await rig.process.execute(commit)).status == "ok"
        assert (await rig.process.abort_draft(bundle.ticket, deadline_at=req.deadline_at)).status == "cleaned"
        assert rig.desktop.lock.locked() and rig.context.process.alive
        assert all(call.worker_epoch == rig.process.worker_epoch for call in rig.context.calls)
        assert rig.job.assigned and rig.process.mutation_may_have_occurred
    assert rig.job.closed and not rig.context.process.alive and rig.desktop.released == 1
    with pytest.raises(HybridProcessError, match="lifetime_used"):
        async with protected(rig):
            pass


@pytest.mark.asyncio
async def test_round_required_child_inheritance_and_late_child_revoked(config):
    rig = fake_rig(config)
    with pytest.raises(HybridProcessError, match="round_required"):
        await rig.process.execute(command())
    assert rig.context.process is None
    wake = asyncio.Event()
    async def late():
        await wake.wait()
        return await rig.process.execute(command())
    async with protected(rig):
        assert (await asyncio.create_task(rig.process.execute(command()))).status == "ok"
        task = asyncio.create_task(late())
    wake.set()
    with pytest.raises(HybridProcessError, match="round_required"):
        await task
    assert len(rig.context.calls) == 1


@pytest.mark.parametrize("field,value", [("request_id", uuid4()), ("worker_epoch", uuid4()),
    ("operation_id", uuid4()), ("binding_revision", 8), ("kind", WorkerKind.VERIFY)])
@pytest.mark.asyncio
async def test_wrong_correlated_result_rejected_and_job_reaped(config, field, value):
    def reply(call):
        return module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
                             result=result(call, **{field: value}))
    rig = fake_rig(config, reply=reply)
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="response_mismatch"):
            await rig.process.execute(command())
        assert rig.job.closed and not rig.context.process.alive and rig.desktop.lock.locked()


@pytest.mark.asyncio
async def test_cancel_and_fixed_deadline_revoke_before_return(config):
    for mode in ("cancel", "deadline"):
        rig = fake_rig(config, reply=False)
        async with protected(rig):
            cancel = asyncio.Event()
            running = asyncio.create_task(rig.process.execute(command(seconds=.6 if mode == "deadline" else 20), cancel_event=cancel))
            await asyncio.sleep(.01)
            if mode == "cancel":
                cancel.set()
                with pytest.raises(HybridProcessError, match="revoked"):
                    await running
            else:
                with pytest.raises((TimeoutError, HybridProcessError)):
                    await running
            assert not rig.context.process.alive and rig.job.closed and rig.desktop.lock.locked()


@pytest.mark.asyncio
async def test_expired_or_invalid_request_never_published(config):
    rig = fake_rig(config)
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="deadline_exhausted"):
            await rig.process.execute(command(seconds=.1))
        with pytest.raises(HybridProcessError, match="command_invalid"):
            await rig.process.execute(command().model_copy(update={"deadline": None}))
        with pytest.raises(ValidationError):
            await rig.process.prepare_draft(request(), expected_sequence_digest="model supplied prose")
        assert rig.context.process is None


@pytest.mark.asyncio
async def test_failed_worker_or_helper_reap_holds_shared_lock_until_retry_succeeds(config):
    rig = fake_rig(config)
    with pytest.raises(HybridProcessError, match="reap_failed"):
        async with protected(rig):
            await rig.process.execute(command())
            rig.context.killable, rig.job.helper_alive = False, True
    assert rig.desktop.lock.locked() and rig.process._failed_context is not None
    with pytest.raises(HybridProcessError, match="reap_failed"):
        await rig.process.retry_cleanup()
    assert rig.desktop.lock.locked()
    rig.context.killable = True
    await asyncio.create_task(rig.process.retry_cleanup())
    assert not rig.desktop.lock.locked() and rig.job.closed and rig.desktop.released == 1
    await rig.desktop.lock.acquire()
    await rig.process.retry_cleanup()
    assert rig.desktop.lock.locked()  # A later unrelated owner is untouched.
    rig.desktop.lock.release()


@pytest.mark.asyncio
async def test_assignment_failure_never_grants_helper_permission(config):
    rig = fake_rig(config)
    rig.job.fail_assign = True
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="supervision_unavailable") as failure:
            await rig.process.execute(command())
        assert "private" not in str(failure.value)
        assert not rig.context.process.args[4].is_set()
        assert rig.job.closed and not rig.context.process.alive


@pytest.mark.asyncio
async def test_external_empty_context_revocation_and_cross_task_exit(config):
    rig = fake_rig(config)
    context = protected(rig)
    await context.__aenter__()
    await rig.process.execute(command())
    Context().run(rig.process.revoke)
    assert rig.process._owner_round.active is False and not rig.context.process.alive
    with pytest.raises(HybridProcessError):
        await rig.process.execute(command())
    await asyncio.create_task(context.__aexit__(None, None, None))
    assert not rig.desktop.lock.locked() and rig.desktop.released == 1
    # Explicit revoke clears the parent ContextVar; inherited cancellation does
    # not automatically clear a different context's binding.
    rig.process._round.set(None)


def test_closed_wire_has_no_model_code_path_or_unrelated_parameters(config):
    base = dict(worker_epoch=uuid4(), action="execute", deadline_at=datetime.now(UTC) + timedelta(seconds=5),
                stop_at_monotonic_ns=time.monotonic_ns() + 5_000_000_000, command=command())
    for updates in ({"script": "arbitrary"}, {"action": "shell"}, {"ticket": {}}, {"owner": request()}):
        with pytest.raises(ValidationError):
            module._Call(**{**base, **updates})
    with pytest.raises(AttributeError):
        HybridWorkerProcess(config).worker_epoch = uuid4()


@pytest.mark.asyncio
async def test_live_correlated_prepare_failure_retains_exact_worker_for_owned_abort(config):
    def failure(call):
        return module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
            error_code="synthetic_owned_write_failed", cleanup_required=True)
    rig = fake_rig(config, reply=failure)
    req = request()
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="owned_write_failed") as error:
            await rig.process.prepare_draft(req, expected_sequence_digest="a" * 64)
        assert error.value.cleanup_required and rig.context.process.alive and not rig.job.closed
        worker_pid = rig.context.process.pid
        rig.context.reply = True
        cleanup = await rig.process.abort_draft(req, deadline_at=req.deadline_at)
        assert cleanup.status == "cleaned" and cleanup.reservation_id == req.reservation_id
        assert rig.context.process.pid == worker_pid


@pytest.mark.asyncio
async def test_single_inflight_mailbox_serializes_inherited_business_requests(config):
    rig = fake_rig(config, reply=False)
    async with protected(rig):
        first = asyncio.create_task(rig.process.execute(command()))
        await asyncio.sleep(.02)
        second = asyncio.create_task(rig.process.execute(command()))
        await asyncio.sleep(.02)
        assert len(rig.context.calls) == 1
        call = rig.context.calls[0]
        raw = module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
            result=result(call)).model_dump_json().encode()
        args = rig.context.process.args
        args[10][:len(raw)], args[11].value = raw, len(raw)
        args[7].set()
        rig.context.reply = True
        assert (await first).status == "ok" and (await second).status == "ok"
        assert len(rig.context.calls) == 2


@pytest.mark.asyncio
async def test_ready_response_after_cancel_is_discarded_and_tree_reaped(config):
    cancellation = asyncio.Event()
    def ready_after_cancel(call):
        cancellation.set()
        return module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action, result=result(call))
    rig = fake_rig(config, reply=ready_after_cancel)
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="revoked"):
            await rig.process.execute(command(), cancel_event=cancellation)
        assert rig.job.closed and not rig.context.process.alive


@pytest.mark.parametrize("bad", ["nonce", "proof"])
@pytest.mark.asyncio
async def test_wrong_prepared_scope_or_portable_proof_never_becomes_authority(config, bad):
    def corrupted(call):
        bundle = prepared(config, call.request, call.expected_sequence_digest, call.deadline_at)
        raw = module._Reply(request_id=call.request_id, worker_epoch=call.worker_epoch, action=call.action,
            prepared=bundle).model_dump(mode="json")
        if bad == "nonce":
            raw["prepared"]["ticket"]["nonce"] = str(uuid4())
        else:
            raw["prepared"]["prepared_evidence"]["text_hash"] = "9" * 64
        return json.dumps(raw).encode()
    rig = fake_rig(config, reply=corrupted)
    async with protected(rig):
        with pytest.raises(HybridProcessError, match="response_(mismatch|invalid)"):
            await rig.process.prepare_draft(request(), expected_sequence_digest="a" * 64)
        assert rig.job.closed and not rig.context.process.alive and rig.process.mutation_may_have_occurred


def test_worker_final_guard_preserves_original_monotonic_call_deadline():
    event = threading.Event()
    guard = module._ChildRevocation(event, datetime.now(UTC) + timedelta(seconds=45), time.monotonic() + 45)
    guard.call = module._Call(worker_epoch=uuid4(), action="execute", command=command(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=20), stop_at_monotonic_ns=time.monotonic_ns() - 1)
    assert guard.is_set()  # Even if UTC is still before its apparent deadline.


@pytest.mark.asyncio
async def test_empty_external_context_cannot_retry_cleanup_of_active_round(config):
    rig = fake_rig(config)
    async with protected(rig):
        await rig.process.execute(command())
        task = Context().run(asyncio.create_task, rig.process.retry_cleanup())
        with pytest.raises(HybridProcessError, match="cleanup_context_active"):
            await task
        assert rig.context.process.alive and rig.desktop.lock.locked()


@pytest.mark.asyncio
async def test_cancelled_desktop_exit_retains_context_until_successful_retry(config):
    rig = fake_rig(config)
    class ExitFence:
        fail = True
        attempts = 0
        async def __aenter__(self):
            await rig.desktop.lock.acquire()
        async def __aexit__(self, *_):
            self.attempts += 1
            if self.fail:
                raise asyncio.CancelledError()
            rig.desktop.lock.release()
            rig.desktop.released += 1
    fence = ExitFence()
    with pytest.raises(asyncio.CancelledError):
        async with rig.process.protect_desktop_round(fence, deadline_at=datetime.now(UTC) + timedelta(seconds=10)):
            await rig.process.execute(command())
    assert rig.job.closed and rig.desktop.lock.locked() and rig.process.cleanup_pending
    with pytest.raises(asyncio.CancelledError):
        await rig.process.retry_cleanup()
    assert rig.process._failed_context is fence and rig.desktop.lock.locked()
    fence.fail = False
    await rig.process.retry_cleanup()
    assert not rig.desktop.lock.locked() and not rig.process.cleanup_pending
    assert rig.desktop.released == 1 and fence.attempts == 3


@pytest.mark.asyncio
async def test_repeated_ordinary_reap_attempt_cannot_extend_original_deadline(config):
    rig = fake_rig(config)
    async with protected(rig):
        await rig.process.execute(command())
        rig.context.killable = False
        rig.process._active.stop_at = time.monotonic() - 1
        started = time.monotonic()
        with pytest.raises(HybridProcessError, match="reap_failed"):
            rig.process.close()
        assert time.monotonic() - started < .1
        rig.context.killable = True
        rig.process.close()


class EchoWorker:
    def __init__(self, config, revoked, *, deadline_at, stop_at):
        self.config, self.revoked, self.deadline_at = config, revoked, deadline_at
        self.bundle = None
    def execute(self, cmd):
        if self.revoked.is_set():
            raise RuntimeError("revoked")
        return WorkerResult(request_id=cmd.request_id, kind=cmd.kind, status="ok", worker_epoch=self.config.navigation.expected_worker_epoch,
            operation_id=cmd.operation_id, binding_id=cmd.binding_id, binding_revision=cmd.binding_revision,
            conversation_revision=cmd.conversation_revision, evidence={"synthetic": True})
    def prepare_draft(self, req, *, expected_sequence_digest):
        self.bundle = prepared(self.config, req, expected_sequence_digest, self.deadline_at)
        return self.bundle.ticket
    def prepared_evidence(self, ticket):
        assert ticket == self.bundle.ticket
        return self.bundle.prepared_evidence
    def adopt_prepared(self, ticket, cmd):
        assert ticket == self.bundle.ticket
        return self.execute(cmd)
    def abort_draft(self, owner, *, deadline_at):
        return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=owner.nonce)
    def close(self):
        pass


def echo_factory(config, revoked, *, deadline_at, stop_at):
    return EchoWorker(config, revoked, deadline_at=deadline_at, stop_at=stop_at)


class HangingWorker(EchoWorker):
    def execute(self, cmd):
        # No UI, keys, networking or model call: only a sleeping Python helper.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        Path(self.config.navigation.guard_state_path + ".test-child-pid").write_text(str(child.pid), encoding="utf-8")
        time.sleep(30)  # Intentionally ignores cancellation to prove Job kill.
        return super().execute(cmd)


def hanging_factory(config, revoked, *, deadline_at, stop_at):
    return HangingWorker(config, revoked, deadline_at=deadline_at, stop_at=stop_at)


def windows_process_gone(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32), ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = (ctypes.c_void_p, ctypes.c_uint32), ctypes.c_uint32
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job supervision")
@pytest.mark.asyncio
async def test_real_spawn_keeps_same_epoch_and_mailbox_for_multiple_closed_business_calls(config):
    desktop = Desktop()
    process = HybridWorkerProcess(config, max_seconds=10, _factory=echo_factory)
    async with process.protect_desktop_round(desktop.round(), deadline_at=datetime.now(UTC) + timedelta(seconds=10)):
        one = await process.execute(command(seconds=10))
        pid = process._active._process.pid
        bundle = await process.prepare_draft(request(seconds=10), expected_sequence_digest="a" * 64)
        assert bundle.ticket.worker_epoch == str(one.worker_epoch)
        assert (await process.execute(command(seconds=10))).worker_epoch == one.worker_epoch
        assert process._active._process.pid == pid and desktop.lock.locked()
    assert windows_process_gone(pid) and desktop.released == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job supervision")
@pytest.mark.parametrize("mode", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_real_spawn_cancel_and_deadline_reap_worker_and_helper_before_desktop_release(config, mode):
    desktop = Desktop()
    process = HybridWorkerProcess(config, max_seconds=8, _factory=hanging_factory)
    signal = Path(config.navigation.guard_state_path + ".test-child-pid")
    budget = 8 if mode == "cancel" else 4
    started = time.monotonic()
    async with process.protect_desktop_round(desktop.round(), deadline_at=datetime.now(UTC) + timedelta(seconds=budget)):
        running = asyncio.create_task(process.execute(command(seconds=budget)))
        until = time.monotonic() + 5
        while not signal.exists() and time.monotonic() < until:
            if running.done():
                await running
            await asyncio.sleep(.01)
        assert signal.exists(), "synthetic sleeping helper did not start"
        helper_pid = int(signal.read_text(encoding="utf-8"))
        worker_pid = process._active._process.pid
        assert not windows_process_gone(helper_pid)
        if mode == "cancel":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            with pytest.raises((HybridProcessError, TimeoutError)):
                await running
            assert time.monotonic() - started < budget
        assert windows_process_gone(helper_pid) and windows_process_gone(worker_pid)
        assert desktop.lock.locked() and process._active is None
    assert desktop.released == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job supervision")
@pytest.mark.asyncio
async def test_native_watchdog_revokes_even_while_parent_event_loop_is_blocked(config):
    desktop = Desktop()
    process = HybridWorkerProcess(config, max_seconds=4, _factory=hanging_factory)
    signal = Path(config.navigation.guard_state_path + ".test-child-pid")
    async with process.protect_desktop_round(desktop.round(), deadline_at=datetime.now(UTC) + timedelta(seconds=4)):
        task = asyncio.create_task(process.execute(command(seconds=4)))
        until = time.monotonic() + 2
        while not signal.exists() and time.monotonic() < until:
            await asyncio.sleep(.01)
        assert signal.exists()
        helper_pid, worker_pid = int(signal.read_text()), process._active._process.pid
        # No GUI or network: deliberately starve only this test's asyncio loop.
        time.sleep(4)
        assert windows_process_gone(worker_pid) and windows_process_gone(helper_pid)
        with pytest.raises((HybridProcessError, TimeoutError)):
            await task
        assert desktop.lock.locked()
    assert not desktop.lock.locked()
