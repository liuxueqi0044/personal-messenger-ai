"""One short-lived, revoked and reaped process for a navigation round.

Only this parent's typed commands enter the private pipe. No model/provider or
arbitrary shell runs here. The worker is idle between short UI commands, and its
handler must not retain UI controls between commands.
"""

from __future__ import annotations

import asyncio
import ctypes
import multiprocessing as mp
import os
import time
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import ContactTarget, NavigationFrame, NavigationModel, NavigationRegion, _aware
from .desktop import NavigationDesktopError, NavigationDesktopScope
from .identity import CurrentChatWitness


class NavigationWorkerCommand(NavigationModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    action: Literal["capture", "scope", "local_witness", "digest", "search_focus", "click", "verified_click", "scroll", "set_query", "dismiss"]
    target: ContactTarget
    deadline_at: datetime
    _deadline_aware = field_validator("deadline_at")(_aware)
    frame: NavigationFrame | None = None
    region: NavigationRegion | None = None
    current: bool | None = None
    x: int | None = Field(default=None, strict=True)
    y: int | None = Field(default=None, strict=True)
    direction: Literal["up", "down"] | None = None
    amount: int | None = Field(default=None, ge=1, le=3, strict=True)
    query_alias_index: int | None = Field(default=None, ge=0, le=8, strict=True)

    @model_validator(mode="after")
    def _closed_arguments(self):
        required = {
            "capture": set(), "scope": set(), "local_witness": set(), "digest": {"frame", "region", "current"},
            "search_focus": {"frame", "region"}, "click": {"frame", "x", "y"},
            "verified_click": {"frame", "region", "x", "y"},
            "scroll": {"frame", "x", "y", "direction", "amount"},
            "set_query": {"frame", "region", "query_alias_index"},
            "dismiss": {"frame", "region", "x", "y"},
        }[self.action]
        present = {field for field in ("frame", "region", "current", "x", "y", "direction", "amount", "query_alias_index")
                   if getattr(self, field) is not None}
        if required != present:
            raise ValueError("worker navigation command has unrelated or missing arguments")
        if self.frame is not None and (
            self.frame.binding_id != self.target.binding_id or self.frame.binding_revision != self.target.binding_revision
        ):
            raise ValueError("worker command frame target mismatch")
        if self.query_alias_index is not None and self.query_alias_index >= len(self.target.trusted_queries):
            raise ValueError("worker query alias is not registered")
        return self


class NavigationWorkerResult(NavigationModel):
    request_id: str
    frame: NavigationFrame | None = None
    scope: NavigationDesktopScope | None = None
    witness: CurrentChatWitness | None = None
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    focused: bool | None = Field(default=None, strict=True)
    completed: bool = False
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _one_outcome(self):
        if sum((self.frame is not None, self.scope is not None, self.witness is not None, self.digest is not None,
                self.focused is not None, self.completed, self.error_code is not None)) != 1:
            raise ValueError("worker result requires one closed outcome")
        return self


def _owned_windows_job() -> Any:
    """Kill the worker's own helper descendants if the parent terminates it."""
    if os.name != "nt":
        return None
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
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise NavigationDesktopError("navigation_worker_job_unavailable")
    # The OS closes this worker-owned handle at process exit. Descendants were
    # never assigned the unrelated QQ process; only worker-created helpers join.
    return handle


def _worker_main(connection, revoked, factory, configuration, deadline_at, lifetime):
    started = time.monotonic()
    handler = None
    try:
        _job = _owned_windows_job()
        handler = factory(configuration, revoked, deadline_at)
        while not revoked.is_set() and datetime.now(UTC) < deadline_at and time.monotonic() - started < lifetime:
            if not connection.poll(0.02):
                continue
            raw = connection.recv()
            command = NavigationWorkerCommand.model_validate(raw)
            if revoked.is_set() or datetime.now(UTC) >= min(deadline_at, command.deadline_at) or time.monotonic() - started >= lifetime:
                break
            try:
                result = handler(command)
                result = NavigationWorkerResult.model_validate(result)
                if result.request_id != command.request_id:
                    raise ValueError("worker result request mismatch")
            except NavigationDesktopError as exc:
                result = NavigationWorkerResult(request_id=command.request_id, error_code=exc.code)
            except Exception:
                result = NavigationWorkerResult(request_id=command.request_id, error_code="navigation_worker_command_failed")
            connection.send(result.model_dump())
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            if handler is not None and callable(getattr(handler, "close", None)):
                handler.close()
        finally:
            connection.close()


class NavigationWorkerProcess:
    """45 seconds includes lock waits, pipe I/O, cancellation and termination.

    The final 0.5 seconds are reserved for terminate/reap. A failed reap is an
    explicit error; callers must keep desktop ownership until it is resolved.
    """

    def __init__(self, factory: Callable, configuration: Any, *, deadline_at: datetime,
                 max_seconds: float = 45, context: Any = None):
        if deadline_at.tzinfo is None or not 0.5 < max_seconds <= 45:
            raise ValueError("worker needs an aware deadline and a bounded lifetime")
        now = datetime.now(UTC)
        self.deadline_at = min(deadline_at, now + timedelta(seconds=max_seconds))
        self._stop_at = time.monotonic() + min(max_seconds, (self.deadline_at - now).total_seconds())
        self._lock = asyncio.Lock()
        self._closed = False
        context = context or mp.get_context("spawn")
        self.revoked = context.Event()
        self._parent, child = context.Pipe()
        self._process = context.Process(target=_worker_main, args=(
            child, self.revoked, factory, configuration, self.deadline_at, max_seconds,
        ), daemon=True)
        self._process.start()
        child.close()

    def revoke(self) -> None:
        self.revoked.set()

    def _remaining(self) -> float:
        return min(self._stop_at - time.monotonic(), (self.deadline_at - datetime.now(UTC)).total_seconds()) - 0.5

    async def request(self, command: NavigationWorkerCommand, *, cancel_event: asyncio.Event | None = None) -> NavigationWorkerResult:
        if self._closed or self.revoked.is_set():
            raise NavigationDesktopError("navigation_worker_revoked")
        try:
            async with asyncio.timeout(max(0.001, self._remaining())):
                async with self._lock:
                    if self._remaining() <= 0 or (cancel_event is not None and cancel_event.is_set()):
                        raise TimeoutError()
                    await asyncio.to_thread(self._parent.send, command.model_dump())
                    while self._remaining() > 0:
                        if cancel_event is not None and cancel_event.is_set():
                            self.revoke()
                            raise NavigationDesktopError("navigation_cancelled", status="cancelled")
                        if self._parent.poll():
                            raw = await asyncio.to_thread(self._parent.recv)
                            if self._remaining() <= 0 or self.revoked.is_set() or (cancel_event is not None and cancel_event.is_set()):
                                raise NavigationDesktopError("navigation_worker_revoked")
                            result = NavigationWorkerResult.model_validate(raw)
                            if result.request_id != command.request_id:
                                raise NavigationDesktopError("navigation_worker_response_mismatch")
                            if result.error_code is not None:
                                raise NavigationDesktopError(result.error_code)
                            return result
                        if not self._process.is_alive():
                            raise NavigationDesktopError("navigation_worker_exited")
                        await asyncio.sleep(0.005)
                    raise TimeoutError()
        except BaseException:
            self.revoke()
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        cleanup_deadline = time.monotonic() + 0.5
        self.revoke()
        self._parent.close()
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=min(0.25, max(0.0, cleanup_deadline - time.monotonic())))
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=min(0.25, max(0.0, cleanup_deadline - time.monotonic())))
        if self._process.is_alive():
            raise NavigationDesktopError("navigation_worker_reap_failed")
        self._closed = True
