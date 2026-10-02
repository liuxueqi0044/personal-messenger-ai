import asyncio
from contextlib import asynccontextmanager
import ctypes
from contextvars import Context
from datetime import UTC, datetime, timedelta
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.navigation import supervised_profile as module
from messenger_ai.adapters.qq.navigation.profile_verifier import ProfileAcquisitionError
from messenger_ai.adapters.qq.navigation.supervised_profile import SupervisedProfileConfig, SupervisedProfileSource
from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState
from messenger_ai.adapters.qq.vm_driver.profile_identity import ProfileCaptureError, run_profile_helper
from .test_identity import case
from .test_profile_verifier import report, parse


class Desktop:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.released = 0

    @asynccontextmanager
    async def round(self):
        async with self.lock:
            try:
                yield
            finally:
                self.released += 1


class FakeProcess:
    def __init__(self, context, **kwargs):
        self.context, self.args = context, kwargs["args"]
        self.pid, self.alive = None, False
        self.events = []

    def start(self):
        self.pid, self.alive = 12345, True
        self.events.append("start")
        _, request, permit, revoked, ready, output, length = self.args
        assert not permit.is_set()
        if self.context.reply is not None:
            reply = self.context.reply(request)
            raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            output[:len(raw)] = raw
            length.value = len(raw)
            ready.set()

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        if self.context.killable:
            self.alive = False

    def kill(self):
        self.events.append("kill")
        if self.context.killable:
            self.alive = False

    def join(self, *, timeout):
        self.events.append(("join", timeout))

    def close(self):
        assert not self.alive
        self.events.append("close")


class FakeContext:
    def __init__(self, reply=None):
        self.reply = reply
        self.killable = True
        self.process = None

    def Event(self):
        return threading.Event()

    def RawArray(self, _, size):
        return bytearray(size)

    def RawValue(self, _, value):
        return SimpleNamespace(value=value)

    def Process(self, **kwargs):
        self.process = FakeProcess(self, **kwargs)
        return self.process


class FakeJob:
    def __init__(self, context):
        self.context, self.closed, self.assigned = context, False, False
        self.helper_alive = False
        self.fail_assign = False

    def assign(self, process):
        assert not process.args[2].is_set()
        if self.fail_assign:
            raise OSError("private native details")
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


@pytest.fixture
def setup(tmp_path, case, report):
    target, frame, expectation, _ = case
    now = datetime.now(UTC)
    frame = frame.model_copy(update={"captured_at": now})
    guard = NavigationGuardState(
        target=target, **{name: getattr(frame, name) for name in (
            "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
            "control_revision", "process_id", "window_handle")},
        observation_epoch="observation", process_started_at_100ns=123456789,
        lease_expires_at=now + timedelta(seconds=45), published_at=now,
        paused=False, has_owned_draft=False, has_commit_obligation=False,
    )
    guard_path = tmp_path / "guard.json"
    guard_path.write_text(guard.model_dump_json(), encoding="utf-8")
    config = SupervisedProfileConfig(
        helper_path=str(tmp_path / "trusted-helper.exe"), vault_path=str(tmp_path / "vault"),
        guard_state_path=str(guard_path),
        **{name: getattr(expectation, name) for name in (
            "environment_fingerprint", "selector_pack_version", "client_version", "hmac_key_id")},
        **{name: getattr(guard, name) for name in (
            "process_id", "window_handle", "process_started_at_100ns", "run_id", "worker_epoch")},
    )
    acquired = parse(report)
    context = FakeContext(lambda request: dict(request_id=request["request_id"], acquisition=acquired.model_dump(mode="json")))
    job = FakeJob(context)
    source = SupervisedProfileSource(config, _context=context, _job_factory=lambda: job)
    return SimpleNamespace(target=target, frame=frame, expectation=expectation, guard=guard,
                           guard_path=guard_path, config=config, acquired=acquired,
                           context=context, job=job, source=source, desktop=Desktop())


def protected(rig):
    return rig.source.protect_desktop_round(
        rig.desktop.round(), target=rig.target, deadline_at=datetime.now(UTC) + timedelta(seconds=45))


async def capture(rig, **kwargs):
    return await rig.source.capture(
        rig.target, kwargs.pop("frame", rig.frame), kwargs.pop("expectation", rig.expectation),
        deadline_at=kwargs.pop("deadline", datetime.now(UTC) + timedelta(seconds=45)), **kwargs)


async def test_typed_capture_closes_job_and_worker_before_return_under_same_desktop_lock(setup):
    rig = setup
    async with protected(rig):
        result = await capture(rig)
        assert result == rig.acquired
        assert rig.desktop.lock.locked() and rig.desktop.released == 0
        assert rig.job.closed and not rig.context.process.alive
        assert rig.context.process.events[-1] == "close"
        assert rig.context.process.args[2].is_set() and rig.context.process.args[3].is_set()
    assert not rig.desktop.lock.locked() and rig.desktop.released == 1


async def test_capture_without_protected_desktop_context_is_forbidden(setup):
    with pytest.raises(ProfileAcquisitionError, match="round_required"):
        await capture(setup)
    assert setup.context.process is None


@pytest.mark.parametrize("field,value", [
    ("process_id", 124), ("window_handle", 457), ("run_id", "other"),
    ("session_epoch", "other"), ("surface_epoch", "other"), ("worker_epoch", "other"),
    ("desktop_lease_id", "other"), ("control_revision", 4),
    ("binding_id", "other"), ("binding_revision", 8),
])
async def test_frame_scope_mismatch_never_starts_worker(setup, field, value):
    async with protected(setup):
        with pytest.raises(ProfileAcquisitionError, match="scope_mismatch"):
            await capture(setup, frame=setup.frame.model_copy(update={field: value}))
    assert setup.context.process is None


@pytest.mark.parametrize("field,value", [
    ("account_id", "other"), ("conversation_id", "other"), ("binding_revision", 8),
    ("environment_fingerprint", "0" * 64), ("selector_pack_version", "other"),
    ("client_version", "other"), ("hmac_key_id", "other"),
])
async def test_expectation_cannot_select_other_business_scope_or_deployment(setup, field, value):
    async with protected(setup):
        with pytest.raises(ProfileAcquisitionError, match="scope_mismatch"):
            await capture(setup, expectation=setup.expectation.model_copy(update={field: value}))
    assert setup.context.process is None


@pytest.mark.parametrize("field,value", [
    ("paused", True), ("has_owned_draft", True), ("has_commit_obligation", True),
    ("process_started_at_100ns", 321), ("published_at", datetime(2020, 1, 1, tzinfo=UTC)),
    ("lease_expires_at", datetime(2020, 1, 1, tzinfo=UTC)),
])
async def test_live_guard_admission_prevents_profile_action(setup, field, value):
    setup.guard_path.write_text(setup.guard.model_copy(update={field: value}).model_dump_json(), encoding="utf-8")
    async with protected(setup):
        with pytest.raises(ProfileAcquisitionError, match="scope_mismatch"):
            await capture(setup)
    assert setup.context.process is None


async def test_expired_deadline_has_no_worker_or_secret_access(setup):
    async with protected(setup):
        with pytest.raises(ProfileAcquisitionError, match="deadline_exhausted"):
            await capture(setup, deadline=datetime.now(UTC))
    assert setup.context.process is None and not Path(setup.config.vault_path).exists()


@pytest.mark.parametrize("mode", ["cancel", "scope_change", "revoke"])
async def test_cancel_or_live_scope_change_reaps_before_context_release(setup, mode):
    rig = setup
    rig.context.reply = None
    async with protected(rig):
        task = asyncio.create_task(capture(rig))
        await asyncio.sleep(0.02)
        assert rig.source._active and rig.desktop.lock.locked()
        if mode == "cancel":
            task.cancel()
            error = asyncio.CancelledError
        elif mode == "scope_change":
            rig.guard_path.write_text(rig.guard.model_copy(update={"control_revision": 4}).model_dump_json(), encoding="utf-8")
            error = ProfileAcquisitionError
        else:
            rig.source.revoke()
            assert not rig.context.process.alive  # Termination is synchronous.
            error = ProfileAcquisitionError
        with pytest.raises(error):
            await task
        assert rig.job.closed and rig.source._active is None
        assert rig.desktop.lock.locked()
    assert rig.desktop.released == 1


async def test_inherited_child_capture_is_invalid_after_parent_revokes_round(setup):
    rig = setup
    proceed = asyncio.Event()
    async with protected(rig):
        async def late():
            await proceed.wait()
            return await capture(rig)
        task = asyncio.create_task(late())
        rig.source.revoke()
        proceed.set()
        with pytest.raises(ProfileAcquisitionError, match="round_required"):
            await task
    assert rig.context.process is None


async def test_reap_failure_retains_underlying_desktop_lock_until_explicit_cleanup(setup):
    rig = setup
    rig.context.killable = False
    rig.job.helper_alive = True
    with pytest.raises(ProfileAcquisitionError, match="reap_failed"):
        async with protected(rig):
            await capture(rig)
    assert rig.desktop.lock.locked() and rig.desktop.released == 0
    assert rig.source._active and rig.source._failed_context
    with pytest.raises(ProfileAcquisitionError, match="cleanup_required"):
        async with protected(rig):
            pass
    rig.context.killable = True
    await rig.source.retry_cleanup()
    assert rig.source._active is None and rig.source._failed_context is None
    assert rig.job.closed and rig.desktop.released == 1 and not rig.desktop.lock.locked()


async def test_job_assignment_failure_never_permits_helper_and_is_redacted(setup):
    rig = setup
    rig.job.fail_assign = True
    async with protected(rig):
        with pytest.raises(ProfileAcquisitionError, match="capture_failed") as error:
            await capture(rig)
        assert "private native details" not in "".join(traceback.format_exception(error.value))
        assert not rig.context.process.args[2].is_set()
        assert not rig.context.process.alive and rig.job.closed


@pytest.mark.parametrize("reply", [
    b'{"request_id":"other","error_code":"identity_profile_capture_failed"}',
    b'{"request_id":"x","raw_profile_id":"private number"}',
    b'not json private native details',
])
async def test_invalid_or_cross_request_output_is_rejected_and_reaped(setup, reply):
    rig = setup
    rig.context.reply = lambda _: reply
    async with protected(rig):
        with pytest.raises(ProfileAcquisitionError) as error:
            await capture(rig)
        assert "private" not in str(error.value)
        assert rig.job.closed


async def test_worker_output_cannot_change_exact_main_or_process_lifetime(setup):
    rig = setup
    result = rig.acquired.model_dump(mode="json")
    result["acquisition"]["process_started_at_100ns"] = 987
    rig.context.reply = lambda request: dict(request_id=request["request_id"], acquisition=result)
    async with protected(rig):
        with pytest.raises(ProfileAcquisitionError, match="scope_mismatch"):
            await capture(rig)
    assert rig.job.closed


async def test_watchdog_revokes_even_when_asyncio_event_loop_is_blocked(setup):
    rig = setup
    rig.context.reply = None
    async with protected(rig):
        task = asyncio.create_task(capture(rig, deadline=datetime.now(UTC) + timedelta(seconds=1.15)))
        await asyncio.sleep(0.02)
        time.sleep(0.20)  # Deliberately prevent the async timeout/poll from running.
        assert rig.source._active.revoked.is_set()
        assert not rig.context.process.alive
        with pytest.raises(ProfileAcquisitionError):
            await task
    assert rig.job.closed


async def test_profile_capture_cannot_renew_original_round_monotonic_budget(setup):
    async with protected(setup):
        setup.source._owner_round.stop_at = time.monotonic() + 0.75
        with pytest.raises(ProfileAcquisitionError, match="deadline_exhausted"):
            await capture(setup)
    assert setup.context.process is None


async def test_request_reserves_both_profile_and_outer_worker_cleanup(setup):
    deadline = datetime.now(UTC) + timedelta(seconds=8)
    async with protected(setup):
        await capture(setup, deadline=deadline)
        child_deadline = setup.context.process.args[1]["deadline_at"]
        assert child_deadline <= deadline - timedelta(seconds=1)


def test_worker_captures_only_fixed_config_and_emits_strict_projection(setup, monkeypatch):
    rig = setup
    monkeypatch.setattr(module, "_certified_guest", lambda: True)
    monkeypatch.setattr(module, "_certify_window_process", lambda config: None)
    calls = []
    def capture_fixed(helper, **kwargs):
        calls.append((helper, kwargs))
        return rig.acquired
    monkeypatch.setattr(module, "capture_profile_acquisition", capture_fixed)
    permit, revoked, ready = threading.Event(), threading.Event(), threading.Event()
    permit.set()
    output, length = bytearray(module._REPLY_LIMIT), SimpleNamespace(value=0)
    request = module._CaptureRequest(request_id="request", guard=rig.guard, deadline_at=datetime.now(UTC) + timedelta(seconds=30))
    module._profile_capture_main(rig.config.model_dump(), request.model_dump(), permit, revoked, ready, output, length)
    assert ready.is_set()
    value = json.loads(bytes(output[:length.value]))
    assert set(value) == {"request_id", "acquisition", "error_code"}
    assert value["acquisition"] == rig.acquired.model_dump(mode="json")
    assert calls[0][0] == rig.config.helper_path
    assert calls[0][1]["key_name"] == "qq.identity.hmac"
    assert calls[0][1]["pid"] == rig.frame.process_id and calls[0][1]["hwnd"] == rig.frame.window_handle
    assert "secret_store" not in calls[0][1] and "key" not in value


def test_child_cannot_launch_second_helper_after_revocation(setup, monkeypatch):
    rig = setup
    monkeypatch.setattr(module, "_certified_guest", lambda: True)
    monkeypatch.setattr(module, "_certify_window_process", lambda config: None)
    permit, revoked, ready = threading.Event(), threading.Event(), threading.Event()
    permit.set()
    launches = []
    def run(command, **kwargs):
        launches.append(command)
        revoked.set()
    def acquire(helper, **kwargs):
        kwargs["runner"]([helper, "header"])
        kwargs["runner"]([helper, "profile"])
        raise AssertionError("second helper must never execute")
    monkeypatch.setattr(module, "_bounded_run", run)
    monkeypatch.setattr(module, "capture_profile_acquisition", acquire)
    request = module._CaptureRequest(request_id="request", guard=rig.guard, deadline_at=datetime.now(UTC) + timedelta(seconds=30))
    module._profile_capture_main(rig.config.model_dump(), request.model_dump(), permit, revoked, ready,
                                 bytearray(module._REPLY_LIMIT), SimpleNamespace(value=0))
    assert len(launches) == 1 and not ready.is_set()


async def test_external_context_revocation_disables_inherited_capture_capability(setup):
    async with protected(setup):
        Context().run(setup.source.revoke)
        with pytest.raises(ProfileAcquisitionError, match="round_required"):
            await capture(setup)
    assert setup.context.process is None


async def test_old_capture_finally_cannot_clear_a_new_round_worker(setup):
    rig = setup
    rig.context.reply = None
    jobs = []
    def job_factory():
        job = FakeJob(rig.context)
        jobs.append(job)
        return job
    rig.source._job_factory = job_factory
    async with protected(rig):
        old = asyncio.create_task(capture(rig))
        await asyncio.sleep(0)
        assert rig.source._active
    async with protected(rig):
        new = asyncio.create_task(capture(rig))
        await asyncio.sleep(0)
        worker = rig.source._active
        assert worker is not None
        with pytest.raises(ProfileAcquisitionError):
            await old
        assert rig.source._active is worker
        new.cancel()
        with pytest.raises(asyncio.CancelledError):
            await new
    assert len(jobs) == 2 and all(job.closed for job in jobs)


def test_child_native_process_lifetime_failure_cannot_invoke_helper(setup, monkeypatch):
    rig = setup
    monkeypatch.setattr(module, "_certified_guest", lambda: True)
    calls = []
    def wrong_lifetime(_):
        raise ProfileAcquisitionError("identity_profile_scope_mismatch")
    monkeypatch.setattr(module, "_certify_window_process", wrong_lifetime)
    monkeypatch.setattr(module, "capture_profile_acquisition", lambda *_args, **_kwargs: calls.append(True))
    permit, revoked, ready = threading.Event(), threading.Event(), threading.Event()
    permit.set()
    output, length = bytearray(module._REPLY_LIMIT), SimpleNamespace(value=0)
    request = module._CaptureRequest(request_id="request", guard=rig.guard, deadline_at=datetime.now(UTC) + timedelta(seconds=30))
    module._profile_capture_main(rig.config.model_dump(), request.model_dump(), permit, revoked, ready, output, length)
    assert not calls and ready.is_set()
    assert json.loads(bytes(output[:length.value]))["error_code"] == "identity_profile_scope_mismatch"


def test_uncertified_host_worker_cannot_read_identity_key_or_invoke_helper(setup, monkeypatch):
    rig = setup
    monkeypatch.setattr(module, "_certified_guest", lambda: False)
    calls = []
    monkeypatch.setattr(module, "capture_profile_acquisition", lambda *_args, **_kwargs: calls.append(True))
    permit, revoked, ready = threading.Event(), threading.Event(), threading.Event()
    permit.set()
    output, length = bytearray(module._REPLY_LIMIT), SimpleNamespace(value=0)
    request = module._CaptureRequest(request_id="request", guard=rig.guard, deadline_at=datetime.now(UTC) + timedelta(seconds=30))
    module._profile_capture_main(rig.config.model_dump(), request.model_dump(), permit, revoked, ready, output, length)
    assert not calls and ready.is_set()
    assert json.loads(bytes(output[:length.value]))["error_code"] == "identity_profile_capture_failed"
    assert not Path(rig.config.vault_path).exists()


def _diagnostic_child(rig, monkeypatch, acquire, *, native=None, deadline=None, revoked=None):
    monkeypatch.setattr(module, "_certified_guest", lambda: True)
    monkeypatch.setattr(module, "_certify_window_process", native or (lambda config: None))
    monkeypatch.setattr(module, "capture_profile_acquisition", acquire)
    permit, ready = threading.Event(), threading.Event()
    revoked = revoked or threading.Event()
    permit.set()
    output, length = bytearray(module._REPLY_LIMIT), SimpleNamespace(value=0)
    request = module._CaptureRequest(request_id=str(uuid4()), guard=rig.guard,
                                     deadline_at=deadline or datetime.now(UTC) + timedelta(seconds=30))
    module._profile_capture_main(rig.config.model_dump(), request.model_dump(), permit, revoked, ready, output, length)
    reply = json.loads(bytes(output[:length.value])) if ready.is_set() else None
    path = rig.guard_path.with_suffix(".profilediagnostic.json")
    diagnostic = json.loads(path.read_bytes()) if path.exists() else None
    return request, ready, reply, path, diagnostic


@pytest.mark.parametrize("helper_stage", ["header", "profile"])
def test_child_preserves_finite_real_helper_failure_diagnostic_without_changing_reply(setup, monkeypatch, helper_stage):
    rig = setup
    launches, native_calls, capture_deadlines = [], [], []
    flag = "--inspect-guest-current-header" if helper_stage == "header" else "--capture-current-identity-guest-foreground"
    private_output = b'{"succeeded":false,"status":"RESTORATION_FAILED","private":"body/key/HMAC"}'
    def bounded(command, **kwargs):
        launches.append((command, kwargs))
        return subprocess.CompletedProcess(command, 2, private_output, b"private stderr/body/key/HMAC")
    def acquire(helper, **kwargs):
        capture_deadlines.append(kwargs["deadline"])
        return run_profile_helper([helper, flag], stage=helper_stage, timeout=3, runner=kwargs["runner"])
    monkeypatch.setattr(module, "_bounded_run", bounded)
    request, ready, reply, path, diagnostic = _diagnostic_child(
        rig, monkeypatch, acquire, native=lambda config: native_calls.append(config))
    assert ready.is_set() and reply == dict(request_id=request.request_id, acquisition=None,
                                           error_code="identity_profile_capture_failed")
    assert diagnostic["schema"] == "qq_profile_failure_diagnostic_v1"
    assert diagnostic["run_id"] == rig.config.run_id and diagnostic["worker_epoch"] == rig.config.worker_epoch
    assert diagnostic["request_id"] == request.request_id
    assert datetime.fromisoformat(diagnostic["deadline_at"]) == request.deadline_at == capture_deadlines[0]
    assert diagnostic["stage"] == "parse" and diagnostic["code"] == "RESTORATION_FAILED"
    assert diagnostic["exception_category"] == "profile_capture_error"
    helper = diagnostic["helper_diagnostic"]
    assert helper["stage"] == helper_stage and helper["exit_code"] == 2
    assert helper["stdout_bytes"] == len(private_output) and helper["stderr_bytes"] > 0
    assert len(helper["stdout_sha256"]) == len(helper["stderr_sha256"]) == 64
    assert [entry["stage"] for entry in diagnostic["stage_timings"]] == ["admission", helper_stage, "parse"]
    assert diagnostic["started_monotonic_ns"] <= diagnostic["stage_started_monotonic_ns"] <= diagnostic["failed_monotonic_ns"]
    assert all(type(entry["elapsed_ns"]) is int and entry["elapsed_ns"] >= 0 for entry in diagnostic["stage_timings"])
    assert len(launches) == 1 and len(native_calls) == 2  # Exactly the pre-existing admission checks.
    assert len(path.read_bytes()) <= module._DIAGNOSTIC_LIMIT
    assert b"body/key/HMAC" not in path.read_bytes() and b"private stderr" not in path.read_bytes()
    assert set(reply) == {"request_id", "acquisition", "error_code"} and not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("helper_stage", ["header", "profile"])
def test_child_timeout_tracks_actual_helper_stage_and_keeps_original_deadline(setup, monkeypatch, helper_stage):
    rig = setup
    seen = []
    flag = "--inspect-guest-current-header" if helper_stage == "header" else "--capture-current-identity-guest-foreground"
    def bounded(command, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"private body", stderr=b"private key")
    def acquire(helper, **kwargs):
        return run_profile_helper([helper, flag], stage=helper_stage, timeout=1.25, runner=kwargs["runner"])
    monkeypatch.setattr(module, "_bounded_run", bounded)
    request, ready, reply, _, diagnostic = _diagnostic_child(rig, monkeypatch, acquire)
    assert ready.is_set() and reply["error_code"] == "identity_profile_capture_failed"
    assert seen == [1.25] and diagnostic["stage"] == helper_stage
    assert diagnostic["helper_diagnostic"] == {"stage": helper_stage}
    assert diagnostic["code"] == "HELPER_TIMEOUT"
    assert diagnostic["deadline_at"] == request.deadline_at.isoformat()


def test_child_scope_change_before_second_helper_has_no_extra_native_reads_or_launch(setup, monkeypatch):
    rig = setup
    launches, native_calls = [], []
    def bounded(command, **kwargs):
        launches.append(command)
        rig.guard_path.write_text(rig.guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"{}", b"")
    def acquire(helper, **kwargs):
        kwargs["runner"]([helper, "--inspect-guest-current-header"])
        kwargs["runner"]([helper, "--capture-current-identity-guest-foreground"])
        pytest.fail("must not launch after scope change")
    monkeypatch.setattr(module, "_bounded_run", bounded)
    _, ready, reply, _, diagnostic = _diagnostic_child(rig, monkeypatch, acquire, native=lambda config: native_calls.append(config))
    assert ready.is_set() and reply["error_code"] == "identity_profile_scope_mismatch"
    assert len(launches) == 1 and len(native_calls) == 2
    assert diagnostic["stage"] == "admission" and diagnostic["code"] == "identity_profile_scope_mismatch"


@pytest.mark.parametrize("failure_stage", ["admission", "postcapture", "parse"])
def test_child_admission_postcapture_and_validation_failures_remain_distinct(setup, monkeypatch, failure_stage):
    rig = setup
    calls = []
    def native(config):
        if failure_stage == "admission":
            raise ProfileAcquisitionError("identity_profile_scope_mismatch")
    def acquire(helper, **kwargs):
        calls.append(kwargs["deadline"])
        if failure_stage == "postcapture":
            rig.guard_path.write_text(rig.guard.model_copy(update={"paused": True}).model_dump_json(), encoding="utf-8")
        return {"private": "body/key/HMAC"} if failure_stage == "parse" else rig.acquired
    _, ready, reply, path, diagnostic = _diagnostic_child(rig, monkeypatch, acquire, native=native)
    assert ready.is_set() and diagnostic["stage"] == failure_stage
    assert len(calls) == (0 if failure_stage == "admission" else 1)
    expected = "identity_profile_capture_failed" if failure_stage == "parse" else "identity_profile_scope_mismatch"
    assert reply["error_code"] == expected and b"body/key/HMAC" not in path.read_bytes()
    assert diagnostic["exception_category"] == ("validation_error" if failure_stage == "parse" else "profile_acquisition_error")


def test_child_unknown_exception_is_not_formatted_or_inspected(setup, monkeypatch):
    touched = []
    class PrivateNativeError(Exception):
        @property
        def code(self):
            touched.append("code")
            raise AssertionError()
        @property
        def diagnostic(self):
            touched.append("diagnostic")
            raise AssertionError()
        def __str__(self):
            touched.append("str")
            raise AssertionError()
    def acquire(*args, **kwargs):
        raise PrivateNativeError("private body/key/HMAC")
    _, ready, reply, path, diagnostic = _diagnostic_child(setup, monkeypatch, acquire)
    assert ready.is_set() and reply["error_code"] == "identity_profile_capture_failed"
    assert diagnostic["code"] == diagnostic["exception_category"] == "unknown" and not touched
    assert b"PrivateNativeError" not in path.read_bytes() and b"body/key/HMAC" not in path.read_bytes()


def test_child_rejects_untyped_malicious_diagnostic_values_and_unknown_code(setup, monkeypatch):
    class PrivateValue:
        def __str__(self):
            pytest.fail("must not format diagnostic values")
    private = PrivateValue()
    def acquire(*args, **kwargs):
        raise ProfileCaptureError("private body/key/HMAC", {
            "stage": "profile", "exit_code": True, "stdout_bytes": -1, "stderr_bytes": private,
            "stdout_sha256": "0" * 64, "stderr_sha256": "private body/key/HMAC",
            "output_limit_exceeded": True, "stdout": private, "stderr": private, "key": private,
        })
    _, ready, reply, path, diagnostic = _diagnostic_child(setup, monkeypatch, acquire)
    assert ready.is_set() and reply["error_code"] == "identity_profile_capture_failed"
    assert diagnostic["code"] == "unknown"
    assert diagnostic["helper_diagnostic"] == {"stage": "profile", "stdout_sha256": "0" * 64, "output_limit_exceeded": True}
    assert b"body/key/HMAC" not in path.read_bytes()


@pytest.mark.parametrize("failure", ["open", "replace"])
def test_child_diagnostic_io_failure_preserves_original_reply_and_existing_file(setup, monkeypatch, failure):
    rig = setup
    path = rig.guard_path.with_suffix(".profilediagnostic.json")
    path.write_bytes(b'{"previous":true}')
    original_open = Path.open
    if failure == "open":
        def denied(path, *args, **kwargs):
            if path.name.endswith(".tmp"):
                raise PermissionError("private path/body")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", denied)
    else:
        def denied(*args):
            raise PermissionError("private path/body")
        monkeypatch.setattr(module.os, "replace", denied)
    def acquire(*args, **kwargs):
        raise ProfileCaptureError("HEADER_INVOKE_FAILED")
    _, ready, reply, _, _ = _diagnostic_child(rig, monkeypatch, acquire)
    assert ready.is_set() and reply["error_code"] == "identity_profile_capture_failed"
    assert path.read_bytes() == b'{"previous":true}' and not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("mode", ["expired", "revoked"])
def test_child_diagnostic_does_not_revive_expired_or_revoked_capture(setup, monkeypatch, mode):
    calls = []
    revoked = threading.Event()
    if mode == "revoked":
        revoked.set()
    deadline = datetime.now(UTC) - timedelta(seconds=1) if mode == "expired" else None
    request, ready, reply, _, diagnostic = _diagnostic_child(
        setup, monkeypatch, lambda *args, **kwargs: calls.append(True), deadline=deadline, revoked=revoked)
    assert not calls and diagnostic["code"] == "identity_profile_capture_revoked"
    assert diagnostic["deadline_at"] == request.deadline_at.isoformat() and diagnostic["stage"] == "admission"
    assert ready.is_set() is (mode == "expired")
    assert (reply["error_code"] if reply else None) == ("identity_profile_capture_revoked" if mode == "expired" else None)


def test_successful_child_emits_no_failure_diagnostic(setup, monkeypatch):
    _, ready, reply, path, diagnostic = _diagnostic_child(setup, monkeypatch, lambda *args, **kwargs: setup.acquired)
    assert ready.is_set() and reply["acquisition"] == setup.acquired.model_dump(mode="json")
    assert diagnostic is None and not path.exists()


@pytest.mark.parametrize("field,value", [("helper_path", "relative.exe"), ("vault_path", "relative"),
                                         ("guard_state_path", "relative"), ("max_seconds", 46)])
def test_fixed_configuration_rejects_relative_paths_and_unbounded_budget(setup, field, value):
    values = setup.config.model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        SupervisedProfileConfig.model_validate(values)


def _hanging_test_child(config, request, permitted, revoked, ready, output, length):
    # Real process-tree test; it launches ONLY a sleeping Python process, no UI.
    if not permitted.wait(5) or revoked.is_set():
        return
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    length.value = child.pid  # Separate test signal; ready is deliberately never published.
    time.sleep(30)


class SleepingSpawnContext:
    def __init__(self):
        self.context = mp.get_context("spawn")
    def __getattr__(self, name):
        return getattr(self.context, name)
    def Process(self, **kwargs):
        kwargs["target"] = _hanging_test_child
        return self.context.Process(**kwargs)


def _windows_process_gone(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="real Windows Job isolation")
@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_native_job_reaps_sleeping_worker_and_helper_before_desktop_release(setup, mode):
    rig = setup
    rig.source = SupervisedProfileSource(rig.config, _context=SleepingSpawnContext())
    async with protected(rig):
        started = time.monotonic()
        budget = 8 if mode == "cancel" else 3
        task = asyncio.create_task(capture(rig, deadline=datetime.now(UTC) + timedelta(seconds=budget)))
        until = time.monotonic() + 5
        while time.monotonic() < until:
            worker = rig.source._active
            if worker is not None and worker.length.value:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert worker and worker.length.value, "sleeping helper did not start"
        helper_pid, worker_pid = worker.length.value, worker._process.pid
        assert not _windows_process_gone(helper_pid)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProfileAcquisitionError):
                await task
            assert time.monotonic() - started < budget
        assert _windows_process_gone(helper_pid) and _windows_process_gone(worker_pid)
        assert rig.desktop.lock.locked() and rig.source._active is None
    assert rig.desktop.released == 1
