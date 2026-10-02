from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopError
from messenger_ai.adapters.qq.navigation.worker_process import (
    NavigationWorkerCommand, NavigationWorkerProcess, NavigationWorkerResult,
)
from messenger_ai.adapters.qq.navigation import worker_process as module

from .test_contracts import request


class Pipe:
    def __init__(self, *, reply=True, mismatch=False):
        self.reply, self.mismatch, self.inbox, self.sent, self.closed = reply, mismatch, [], [], False
    def send(self, command):
        self.sent.append(command)
        if self.reply:
            self.inbox.append(NavigationWorkerResult(request_id="wrong" if self.mismatch else command["request_id"], completed=True).model_dump())
    def poll(self, *args):
        return bool(self.inbox)
    def recv(self):
        return self.inbox.pop(0)
    def close(self):
        self.closed = True


class Process:
    alive = True
    def __init__(self, **values):
        self.events = []
    def start(self):
        self.events.append("start")
    def is_alive(self):
        return self.alive
    def terminate(self):
        self.events.append("terminate")
        self.alive = False
    def kill(self):
        self.events.append("kill")
        self.alive = False
    def join(self, *, timeout):
        self.events.append(("join", timeout))


class Context:
    def __init__(self, **options):
        self.pipe = Pipe(**options)
    def Event(self):
        return threading.Event()
    def Pipe(self):
        return self.pipe, SimpleNamespace(close=lambda: None)
    def Process(self, **values):
        self.process = Process(**values)
        return self.process


def command():
    req = request()
    return NavigationWorkerCommand(action="click", target=req.target, frame=req.frame, x=1, y=1, deadline_at=req.deadline_at)


@pytest.mark.asyncio
async def test_private_typed_request_response_and_termination():
    context = Context()
    worker = NavigationWorkerProcess(None, {}, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    result = await worker.request(command())
    assert result.completed
    worker.close()
    worker.close()
    assert worker.revoked.is_set() and context.pipe.closed and not context.process.alive
    assert context.process.events.count("terminate") == 1


@pytest.mark.asyncio
async def test_cancel_or_timeout_revokes_and_reaps_before_return():
    context = Context(reply=False)
    worker = NavigationWorkerProcess(None, {}, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    cancellation = asyncio.Event()
    running = asyncio.create_task(worker.request(command(), cancel_event=cancellation))
    await asyncio.sleep(0.01)
    cancellation.set()
    with pytest.raises(NavigationDesktopError, match="cancelled"):
        await running
    assert worker.revoked.is_set() and not context.process.alive
    context = Context(reply=False)
    worker = NavigationWorkerProcess(None, {}, max_seconds=0.52, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    with pytest.raises(TimeoutError):
        await worker.request(command())
    assert worker.revoked.is_set() and not context.process.alive


@pytest.mark.asyncio
async def test_wrong_response_and_external_task_cancel_close_worker():
    context = Context(mismatch=True)
    worker = NavigationWorkerProcess(None, {}, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    with pytest.raises(NavigationDesktopError, match="mismatch"):
        await worker.request(command())
    assert not context.process.alive
    context = Context(reply=False)
    worker = NavigationWorkerProcess(None, {}, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    running = asyncio.create_task(worker.request(command()))
    await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not context.process.alive


def test_unreaped_process_is_explicit_failure():
    context = Context()
    worker = NavigationWorkerProcess(None, {}, deadline_at=datetime.now(UTC) + timedelta(seconds=45), context=context)
    context.process.terminate = lambda: None
    context.process.kill = lambda: None
    with pytest.raises(NavigationDesktopError, match="reap_failed"):
        worker.close()
    assert worker.revoked.is_set() and not worker._closed


def test_child_handler_cleanup_and_expired_command_never_dispatch(monkeypatch):
    monkeypatch.setattr(module, "_owned_windows_job", lambda: None)
    revoked = threading.Event()
    events = []
    class Connection:
        def poll(self, *args):
            return True
        def recv(self):
            return command().model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)}).model_dump()
        def send(self, response):
            events.append("send")
            revoked.set()
        def close(self):
            events.append("connection_closed")
    class Handler:
        def __call__(self, value):
            events.append("dispatch")
            return NavigationWorkerResult(request_id=value.request_id, completed=True)
        def close(self):
            events.append("handler_closed")
    module._worker_main(Connection(), revoked, lambda *args: Handler(), {}, datetime.now(UTC) + timedelta(seconds=45), 45)
    assert events == ["handler_closed", "connection_closed"]
