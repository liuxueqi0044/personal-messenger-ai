"""One profile acquisition in a Windows Job, under the existing desktop round.

Integration must wrap the actual desktop context with protect_desktop_round.
That wrapper retains the context/lock if either worker or helper descendants
cannot be confirmed dead. Never release that retained context in an error
handler; retry_cleanup is the only release path after failed profile cleanup.
No COM work runs on an asyncio executor, and no raw helper report crosses IPC.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from messenger_ai.adapters.qq.vm_driver.profile_identity import _bounded_run
from .contracts import ContactTarget, NavigationFrame, NavigationModel, _aware
from .identity import Digest, ProfileIdentityExpectation, ScopeId
from .profile_verifier import ProfileAcquisitionError, ValidatedProfileAcquisition, capture_profile_acquisition
from .windows_backend import NavigationGuardState


_REPLY_LIMIT = 16384
_REAP_SECONDS = 0.5
_FRAME_FIELDS = ("run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
                 "control_revision", "binding_id", "binding_revision", "process_id", "window_handle")
_GUARD_FIELDS = tuple(x for x in _FRAME_FIELDS if not x.startswith("binding_")) + (
    "process_started_at_100ns", "observation_epoch",
)


class SupervisedProfileConfig(NavigationModel):
    """Fixed trusted deployment inputs; never build this from a model response."""
    helper_path: str
    vault_path: str
    guard_state_path: str
    environment_fingerprint: Digest
    selector_pack_version: ScopeId
    client_version: ScopeId
    hmac_key_id: ScopeId
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    process_started_at_100ns: int = Field(gt=0, strict=True)
    run_id: ScopeId
    worker_epoch: ScopeId
    max_seconds: float = Field(default=45, gt=_REAP_SECONDS, le=45)

    @field_validator("helper_path", "vault_path", "guard_state_path")
    @classmethod
    def _absolute_path(cls, value):
        if not value or "\x00" in value or not Path(value).is_absolute():
            raise ValueError("profile deployment paths must be absolute")
        return value


class _CaptureRequest(NavigationModel):
    request_id: str
    guard: NavigationGuardState
    deadline_at: datetime
    _aware_deadline = field_validator("deadline_at")(_aware)


class _CaptureReply(NavigationModel):
    request_id: str
    acquisition: ValidatedProfileAcquisition | None = None
    error_code: Literal["identity_profile_capture_failed", "identity_profile_scope_mismatch",
                        "identity_profile_capture_revoked"] | None = None

    @model_validator(mode="after")
    def _one_result(self):
        if (self.acquisition is None) == (self.error_code is None):
            raise ValueError("profile reply requires one result")
        return self


def _read_guard(config: SupervisedProfileConfig) -> NavigationGuardState:
    try:
        with open(config.guard_state_path, "rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError()
        return NavigationGuardState.model_validate_json(raw)
    except Exception:
        raise ProfileAcquisitionError("identity_profile_scope_mismatch") from None


def _admit(config: SupervisedProfileConfig, expected: NavigationGuardState) -> NavigationGuardState:
    guard = _read_guard(config)
    now = datetime.now(UTC)
    if (guard.target != expected.target or any(getattr(guard, key) != getattr(expected, key) for key in _GUARD_FIELDS)
            or any(getattr(guard, key) != getattr(config, key) for key in (
                "process_id", "window_handle", "process_started_at_100ns", "run_id", "worker_epoch"))
            or guard.paused or guard.has_owned_draft or guard.has_commit_obligation
            or not 0 <= (now - guard.published_at).total_seconds() <= 5
            or guard.lease_expires_at <= now):
        raise ProfileAcquisitionError("identity_profile_scope_mismatch")
    return guard


def _certify_window_process(config: SupervisedProfileConfig) -> None:
    """Fresh native ownership/lifetime check before either helper launch."""
    try:
        user, kernel = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD()
        if not user.GetWindowThreadProcessId(config.window_handle, ctypes.byref(pid)) or pid.value != config.process_id:
            raise ValueError()
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, config.process_id)
        if not handle:
            raise ValueError()
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise ValueError()
            started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if started != config.process_started_at_100ns:
                raise ValueError()
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        raise ProfileAcquisitionError("identity_profile_scope_mismatch") from None


def _certified_guest() -> bool:
    return os.name == "nt" and os.environ.get("PERSONAL_MESSENGER_VM_GUEST") == "1"


def _profile_capture_main(config_raw, request_raw, permitted, revoked, ready, output, length):
    """Fixed child entry point: only approved capture and strict projection."""
    request_id = "invalid"
    try:
        config = SupervisedProfileConfig.model_validate(config_raw)
        request = _CaptureRequest.model_validate(request_raw)
        request_id = request.request_id
        if not _certified_guest():
            raise RuntimeError()
        # The parent assigns this process to its kill-on-close Job before it
        # publishes permission. No helper can escape through a startup race.
        while not permitted.wait(0.01):
            if revoked.is_set() or datetime.now(UTC) >= request.deadline_at:
                raise ProfileAcquisitionError("identity_profile_capture_revoked")

        def check():
            if revoked.is_set() or datetime.now(UTC) >= request.deadline_at:
                raise ProfileAcquisitionError("identity_profile_capture_revoked")
            _admit(config, request.guard)
            _certify_window_process(config)

        def guarded_runner(command, **kwargs):
            check()  # Admission before BOTH header and profile helper launches.
            return _bounded_run(command, **kwargs)

        check()
        result = capture_profile_acquisition(
            config.helper_path, pid=config.process_id, hwnd=config.window_handle,
            vault=Path(config.vault_path), key_name="qq.identity.hmac",
            environment_fingerprint=config.environment_fingerprint,
            selector_pack_version=config.selector_pack_version,
            deadline=request.deadline_at, runner=guarded_runner,
        )
        check()
        result = ValidatedProfileAcquisition.model_validate(result)
        reply = _CaptureReply(request_id=request_id, acquisition=result)
    except ProfileAcquisitionError as exc:
        code = exc.code if exc.code in {"identity_profile_scope_mismatch", "identity_profile_capture_revoked"} else "identity_profile_capture_failed"
        reply = _CaptureReply(request_id=request_id, error_code=code)
    except BaseException:
        # Never serialize a native exception, traceback, key or raw report.
        reply = _CaptureReply(request_id=request_id, error_code="identity_profile_capture_failed")
    raw = reply.model_dump_json().encode("utf-8")
    if len(raw) > _REPLY_LIMIT:
        raw = _CaptureReply(request_id=request_id, error_code="identity_profile_capture_failed").model_dump_json().encode()
    if not revoked.is_set():
        output[:len(raw)] = raw
        length.value = len(raw)
        ready.set()  # Publish only after the complete bounded payload is copied.


class _WindowsProfileJob:
    """Parent-owned Job; admission is withheld until assignment succeeds."""
    def __init__(self):
        if os.name != "nt":
            raise ProfileAcquisitionError("identity_profile_supervision_unavailable")
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]
        self._accounting = Accounting
        self._lock = threading.Lock()
        self._kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", (ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
            ("SetInformationJobObject", (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD), wintypes.BOOL),
            ("QueryInformationJobObject", (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p), wintypes.BOOL),
            ("AssignProcessToJobObject", (wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
            ("TerminateJobObject", (wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
            ("CloseHandle", (wintypes.HANDLE,), wintypes.BOOL),
        ):
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        self._handle = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
        if not self._handle or not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            if self._handle:
                kernel.CloseHandle(self._handle)
            self._handle = None
            raise ProfileAcquisitionError("identity_profile_supervision_unavailable")

    def assign(self, process):
        with self._lock:
            if not self._handle or not self._kernel.AssignProcessToJobObject(self._handle, process.sentinel):
                raise ProfileAcquisitionError("identity_profile_supervision_unavailable")

    def terminate(self):
        with self._lock:
            if self._handle and not self._kernel.TerminateJobObject(self._handle, 1):
                raise ProfileAcquisitionError("identity_profile_reap_failed")

    def empty(self):
        with self._lock:
            if not self._handle:
                return True
            info = self._accounting()
            if not self._kernel.QueryInformationJobObject(self._handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ProfileAcquisitionError("identity_profile_reap_failed")
            return info.active == 0

    def close(self):
        with self._lock:
            if self._handle:
                if not self._kernel.CloseHandle(self._handle):
                    raise ProfileAcquisitionError("identity_profile_reap_failed")
                self._handle = None


class _CaptureProcess:
    def __init__(self, config, request, *, stop_at, context, job_factory):
        self.request, self.stop_at = request, stop_at
        self.revoked, self.ready, self.permitted = context.Event(), context.Event(), context.Event()
        self.output, self.length = context.RawArray("B", _REPLY_LIMIT), context.RawValue("i", 0)
        self._job = job_factory()
        self._process = None
        self._closed = False
        self._watch_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        try:
            self._process = context.Process(target=_profile_capture_main, args=(
                config.model_dump(), request.model_dump(), self.permitted, self.revoked,
                self.ready, self.output, self.length,
            ), daemon=True)
        except BaseException:
            self._job.close()  # Nothing has started and no helper can exist.
            raise ProfileAcquisitionError("identity_profile_supervision_unavailable") from None
        self._watchdog.start()

    def start(self):
        self._process.start()
        self._job.assign(self._process)
        if self.revoked.is_set() or time.monotonic() >= self.stop_at - _REAP_SECONDS:
            raise ProfileAcquisitionError("identity_deadline_exhausted")
        self.permitted.set()

    def _watch(self):
        if not self._watch_stop.wait(max(0, self.stop_at - _REAP_SECONDS - time.monotonic())):
            try:
                self.revoke()
            except Exception:
                pass  # close still must prove empty; no failure grants release.

    def revoke(self):
        self.revoked.set()
        self._job.terminate()

    def read(self):
        size = self.length.value
        if not 0 < size <= _REPLY_LIMIT:
            raise ProfileAcquisitionError("identity_profile_worker_output_invalid")
        try:
            reply = _CaptureReply.model_validate_json(bytes(self.output[:size]))
        except (ValidationError, ValueError):
            raise ProfileAcquisitionError("identity_profile_worker_output_invalid") from None
        if reply.request_id != self.request.request_id:
            raise ProfileAcquisitionError("identity_profile_worker_response_mismatch")
        if reply.error_code:
            raise ProfileAcquisitionError(reply.error_code)
        return reply.acquisition

    def close(self):
        if self._closed:
            return
        self._watch_stop.set()
        self.revoked.set()
        # Retry also has a bounded reap attempt; never abandon ownership on failure.
        until = time.monotonic() + _REAP_SECONDS
        try:
            self._job.terminate()
            process = self._process
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=max(0, min(0.2, until - time.monotonic())))
                if process.is_alive():
                    process.kill()
                    process.join(timeout=max(0, until - time.monotonic()))
            while not self._job.empty() and time.monotonic() < until:
                time.sleep(0.005)
            if (process is not None and process.pid is not None and process.is_alive()) or not self._job.empty():
                raise ProfileAcquisitionError("identity_profile_reap_failed")
            self._job.close()
            if process is not None:
                process.close()
            self._closed = True
        except Exception:
            raise ProfileAcquisitionError("identity_profile_reap_failed") from None


@dataclass
class _Round:
    target: ContactTarget
    deadline_at: datetime
    stop_at: float
    active: bool = True


class SupervisedProfileSource:
    """Async profile source; protect the SAME existing desktop round, not a new lock.

    revoke() immediately disables inherited captures and terminates the Job.
    close() confirms worker + all Job members are gone, or raises while keeping
    resources. protect_desktop_round retains the underlying lock on that error;
    retry_cleanup must succeed before the original context can exit.
    """
    def __init__(self, config: SupervisedProfileConfig, *, _context=None, _job_factory=None):
        self.config = SupervisedProfileConfig.model_validate(config)
        self._context = _context or mp.get_context("spawn")
        self._job_factory = _job_factory or _WindowsProfileJob
        self._round = ContextVar(f"profile_round_{id(self)}", default=None)
        self._owner_round: _Round | None = None
        self._active: _CaptureProcess | None = None
        self._failed_context = None
        self._cleanup_lock = asyncio.Lock()

    @asynccontextmanager
    async def protect_desktop_round(self, desktop_context, *, target: ContactTarget, deadline_at: datetime):
        _aware(deadline_at)
        if self._round.get() is not None or self._failed_context is not None or self._active is not None:
            raise ProfileAcquisitionError("identity_profile_cleanup_required")
        now, started = datetime.now(UTC), time.monotonic()
        duration = min(self.config.max_seconds, (deadline_at - now).total_seconds())
        if duration <= 2 * _REAP_SECONDS:
            raise ProfileAcquisitionError("identity_deadline_exhausted")
        await desktop_context.__aenter__()
        capability = _Round(target, min(deadline_at, now + timedelta(seconds=duration)), started + duration)
        self._owner_round = capability
        token = self._round.set(capability)
        try:
            yield
        finally:
            capability.active = False
            if self._owner_round is capability:
                self._owner_round = None
            try:
                self._round.reset(token)
            except ValueError:
                # The coordinator enters in its parent context and bounds
                # cleanup in a child task. Revocation above invalidates the
                # original capability; clear only this child's inherited one.
                self._round.set(None)
            try:
                self.close()
                await desktop_context.__aexit__(None, None, None)
            except BaseException:
                self._failed_context = desktop_context
                raise

    def revoke(self):
        # An external supervisor need not inherit the caller's ContextVar.
        capability = self._owner_round
        if capability is not None:
            capability.active = False
        if self._active is not None:
            self._active.revoke()

    def close(self):
        self.revoke()
        if self._active is not None:
            self._active.close()
            self._active = None

    async def retry_cleanup(self):
        if self._round.get() is not None:
            raise ProfileAcquisitionError("identity_profile_cleanup_context_active")
        async with self._cleanup_lock:
            self.close()
            context = self._failed_context
            if context is not None:
                self._failed_context = None
                await context.__aexit__(None, None, None)

    async def capture(self, target: ContactTarget, frame: NavigationFrame,
                      expectation: ProfileIdentityExpectation, *, deadline_at: datetime) -> ValidatedProfileAcquisition:
        _aware(deadline_at)
        now, started = datetime.now(UTC), time.monotonic()
        capability = self._round.get()
        if capability is None or not capability.active or capability.target != target:
            raise ProfileAcquisitionError("identity_profile_round_required")
        if self._active is not None:
            raise ProfileAcquisitionError("identity_profile_capture_busy")
        if (any(getattr(expectation, key) != getattr(target, key) for key in (
                "account_id", "conversation_id", "binding_id", "binding_revision"))
                or any(getattr(expectation, key) != getattr(self.config, key) for key in (
                    "environment_fingerprint", "selector_pack_version", "client_version", "hmac_key_id"))):
            raise ProfileAcquisitionError("identity_profile_scope_mismatch")
        guard = _read_guard(self.config)
        if (guard.target != target or any(
                getattr(frame, key) != (getattr(guard.target, key) if key.startswith("binding_") else getattr(guard, key))
                for key in _FRAME_FIELDS) or frame.captured_at > now):
            raise ProfileAcquisitionError("identity_profile_scope_mismatch")
        _admit(self.config, guard)
        # Reserve one reap interval for the surrounding navigation worker, and
        # another inside this process for its own Job. Lock waits/model waits
        # have already consumed the original round's monotonic lifetime.
        duration = min(
            (min(deadline_at, capability.deadline_at, guard.lease_expires_at) - now).total_seconds(),
            self.config.max_seconds, capability.stop_at - started,
        ) - _REAP_SECONDS
        if duration <= _REAP_SECONDS:
            raise ProfileAcquisitionError("identity_deadline_exhausted")
        deadline = now + timedelta(seconds=duration)
        stop_at = started + duration
        request = _CaptureRequest(request_id=str(uuid4()), guard=guard,
                                  deadline_at=deadline - timedelta(seconds=_REAP_SECONDS))
        worker = _CaptureProcess(self.config, request, stop_at=stop_at,
                                 context=self._context, job_factory=self._job_factory)
        self._active = worker  # Retain even if start/assignment/reaping fails.
        try:
            worker.start()
            while True:
                if (not capability.active or worker.revoked.is_set()
                        or datetime.now(UTC) >= request.deadline_at
                        or time.monotonic() >= stop_at - _REAP_SECONDS):
                    raise ProfileAcquisitionError("identity_profile_capture_revoked")
                _admit(self.config, guard)
                if worker.ready.is_set():
                    result = worker.read()
                    if (result.profile.process_id != frame.process_id or result.profile.window_handle != frame.window_handle
                            or result.acquisition.process_started_at_100ns != self.config.process_started_at_100ns
                            or result.profile.environment_fingerprint != self.config.environment_fingerprint
                            or result.profile.selector_pack_version != self.config.selector_pack_version):
                        raise ProfileAcquisitionError("identity_profile_scope_mismatch")
                    break
                if not worker._process.is_alive():
                    raise ProfileAcquisitionError("identity_profile_worker_exited")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except ProfileAcquisitionError:
            raise
        except Exception:
            raise ProfileAcquisitionError("identity_profile_capture_failed") from None
        finally:
            # Synchronous cleanup cannot be interrupted by another task.cancel().
            worker.close()
            if self._active is worker:
                self._active = None
        # Result parsing/cleanup cannot turn a late response into usable proof.
        if not capability.active or datetime.now(UTC) >= deadline or time.monotonic() >= stop_at:
            raise ProfileAcquisitionError("identity_profile_capture_revoked")
        _admit(self.config, guard)
        return result


__all__ = ["SupervisedProfileConfig", "SupervisedProfileSource"]
