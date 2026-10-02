"""Actual operator/backend/process/handler; only IPC and native UI are fake."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.navigation.contracts import NavigationDecision, NavigationRequest
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopError
from messenger_ai.adapters.qq.navigation.windows_backend import (
    ProcessScopedDesktopOperator, WindowsNavigationBackend, _NativeWindowsSurface,
)
from messenger_ai.adapters.qq.navigation.worker_process import (
    NavigationWorkerCommand, NavigationWorkerProcess, NavigationWorkerResult,
)

from .test_windows_backend import setup
from .test_worker_process import Process


class HandlerPipe:
    def __init__(self, handler):
        self.handler, self.inbox, self.sent, self.closed = handler, [], [], False
        self.before_command = lambda command: None

    def send(self, raw):
        self.sent.append(raw)
        self.before_command(raw)
        try:
            result = self.handler(NavigationWorkerCommand.model_validate(raw))
        except NavigationDesktopError as exc:
            result = NavigationWorkerResult(request_id=raw["request_id"], error_code=exc.code)
        except Exception:
            result = NavigationWorkerResult(request_id=raw["request_id"], error_code="navigation_worker_command_failed")
        self.inbox.append(result.model_dump())

    def poll(self):
        return bool(self.inbox)

    def recv(self):
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


class HandlerContext:
    def __init__(self, handler, revoked):
        self.pipe, self.revoked = HandlerPipe(handler), revoked

    def Event(self):
        return self.revoked

    def Pipe(self):
        return self.pipe, SimpleNamespace(close=lambda: None)

    def Process(self, **values):
        self.process = Process(**values)
        return self.process


class GenericBackend:
    """An existing generic producer with no verified-click capability."""

    def __init__(self, backend):
        self.backend = backend

    def __getattr__(self, name):
        if name == "verified_click":
            raise AttributeError(name)
        return getattr(self.backend, name)


class Harness:
    def __init__(self, tmp_path, *, generic=False):
        (self.target, self.state, self.path, self.handler, self.transport,
         self.surface, self.revoked) = setup(tmp_path)
        self.deadline = self.state.lease_expires_at
        self.counts = {"fresh": 0, "capture": 0, "uia": 0, "point": 0}
        self.roi_changed = False
        self.point_owned = True
        self.input_count = 0
        self.on_input = lambda: None

        fresh = self.handler._fresh_frame
        def counted_fresh(command):
            self.counts["fresh"] += 1
            return fresh(command)
        self.handler._fresh_frame = counted_fresh

        capture = self.surface.capture
        def counted_capture(window, bounds):
            self.counts["capture"] += 1
            pixels = capture(window, bounds)
            if self.roi_changed:
                pixels = bytes(value ^ 1 if index % 4 == 0 else value for index, value in enumerate(pixels))
            return pixels
        self.surface.capture = counted_capture

        read_phase = self.transport.read_phase
        @contextmanager
        def counted_phase(window):
            self.counts["uia"] += 1
            with read_phase(window) as phase:
                yield phase
        self.transport.read_phase = counted_phase

        def point_belongs(window, x, y):
            self.counts["point"] += 1
            return self.point_owned
        def native_click(x, y):
            self.input_count += 1
            self.surface.events.append(("click", x, y))
            self.on_input()
            return 3
        self.transport._point_belongs_to_window = point_belongs
        self.transport._send_guest_click = native_click
        self.surface.transport = self.transport
        self.surface.click = lambda window, x, y: _NativeWindowsSurface.click(self.surface, window, x, y)

        self.context = HandlerContext(self.handler, self.revoked)
        self.worker = NavigationWorkerProcess(None, {}, context=self.context, deadline_at=self.deadline)
        self.backend = WindowsNavigationBackend(self.handler.config, self.target, worker=self.worker, deadline_at=self.deadline)
        self.operator = ProcessScopedDesktopOperator(backend=GenericBackend(self.backend) if generic else self.backend)

    async def capture(self):
        frame = await self.operator.capture(self.target, deadline_at=self.deadline)
        self.reset_counts()
        return frame

    def reset_counts(self):
        for name in self.counts:
            self.counts[name] = 0
        self.context.pipe.sent.clear()

    def request_and_decision(self, frame, action="click_candidate"):
        region = next(region for region in frame.allowed_regions
                      if region.kind == ("candidate" if action == "click_candidate" else "search"))
        req = NavigationRequest(target=self.target, frame=frame, deadline_at=self.deadline)
        decision = NavigationDecision(frame_id=frame.frame_id, action=action, bbox=region.bbox,
                                      **({"observed_label": self.target.display_name} if action == "click_candidate" else {}))
        return req, decision

    def change_guard(self, **changes):
        self.path.write_text(self.state.model_copy(update=changes).model_dump_json(), encoding="utf-8")

    def assert_retired(self):
        assert self.worker._closed and self.worker.revoked.is_set()
        assert self.context.pipe.closed and not self.context.process.alive
        assert not self.operator.operation_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["click_candidate", "open_search"])
async def test_verified_click_full_chain_has_one_fresh_proof_and_one_postcapture(tmp_path, action):
    h = Harness(tmp_path)
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        req, decision = h.request_and_decision(frame, action)
        result = await h.operator.execute(req, decision)
        assert result.status == "action_attempted" and result.next_frame is not None
        assert result.next_frame.frame_id != frame.frame_id
        assert h.counts == {"fresh": 1, "capture": 2, "uia": 4, "point": 1}
        assert h.input_count == 1
        assert [command["action"] for command in h.context.pipe.sent] == [
            "scope", "scope", "verified_click", "capture", "scope",
        ]
        sent = h.context.pipe.sent[2]
        assert sent["region"]["kind"] == ("candidate" if action == "click_candidate" else "search")
        assert sent["deadline_at"] <= h.deadline
        assert frame.frame_id not in h.handler._frames
        assert result.next_frame.frame_id in h.handler._frames
    h.assert_retired()


@pytest.mark.asyncio
async def test_generic_click_keeps_two_independent_fresh_proofs_and_postcapture(tmp_path):
    h = Harness(tmp_path, generic=True)
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        result = await h.operator.execute(*h.request_and_decision(frame))
        assert result.status == "action_attempted"
        assert h.counts == {"fresh": 2, "capture": 3, "uia": 6, "point": 1}
        assert [command["action"] for command in h.context.pipe.sent] == [
            "scope", "digest", "digest", "scope", "click", "capture", "scope",
        ]
        assert h.input_count == 1
    h.assert_retired()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("roi", "navigation_region_changed"),
    ("unknown_region", "navigation_region_not_owned"),
    ("wrong_kind", "navigation_region_not_owned"),
    ("outside_point", "navigation_input_region_unproven"),
    ("native_point", "navigation_click_window_mismatch"),
    ("pause", "navigation_guard_input_disabled"),
    ("owned_draft", "navigation_guard_input_disabled"),
    ("commit", "navigation_guard_input_disabled"),
    ("revision", "navigation_frame_scope_changed"),
    ("geometry", "navigation_frame_scope_changed"),
    ("native_lifetime", "navigation_process_lifetime_changed"),
    ("expired_deadline", "navigation_worker_revoked"),
])
async def test_verified_click_child_rejects_changes_after_parent_scopes_without_input(tmp_path, case, code):
    h = Harness(tmp_path)
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        req, decision = h.request_and_decision(frame)
        def inject(raw):
            if raw["action"] != "verified_click":
                return
            if case == "roi":
                h.roi_changed = True
            elif case == "unknown_region":
                raw["region"]["bbox"]["right"] -= 1
            elif case == "wrong_kind":
                raw["region"] = next(region.model_dump() for region in frame.allowed_regions if region.kind == "list")
            elif case == "outside_point":
                raw["x"] = frame.crop_origin_x + frame.crop_width - 1
            elif case == "native_point":
                h.point_owned = False
            elif case in {"pause", "owned_draft", "commit"}:
                h.change_guard(**{{"pause": "paused", "owned_draft": "has_owned_draft", "commit": "has_commit_obligation"}[case]: True})
            elif case == "revision":
                h.change_guard(control_revision=2)
            elif case == "geometry":
                h.surface.snapshot = lambda window: ((10, 20, 73, 60), (0, 0, 100, 100), 1.5, 123)
            elif case == "native_lifetime":
                h.surface.snapshot = lambda window: ((10, 20, 74, 60), (0, 0, 100, 100), 1.5, 124)
            elif case == "expired_deadline":
                raw["deadline_at"] = datetime.now(UTC) - timedelta(seconds=1)
        h.context.pipe.before_command = inject
        result = await h.operator.execute(req, decision)
        assert result.status == "rejected" and result.error_code == code
        assert h.input_count == 0 and not h.surface.events
        assert not any(command["action"] == "capture" for command in h.context.pipe.sent)
        assert h.worker._closed and h.worker.revoked.is_set()
    h.assert_retired()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("pause", "navigation_guard_input_disabled"),
    ("revision", "navigation_last_boundary_changed"),
    ("geometry", "navigation_last_boundary_changed"),
    ("native_lifetime", "navigation_last_boundary_changed"),
    ("revoked", "navigation_worker_revoked"),
    ("deadline", "navigation_worker_revoked"),
])
async def test_verified_click_keeps_last_boundary_after_fresh_roi(tmp_path, case, code):
    h = Harness(tmp_path)
    boundary = h.handler._last_boundary
    def changed_boundary(target, frame):
        if case == "pause":
            h.change_guard(paused=True)
        elif case == "revision":
            h.change_guard(control_revision=2)
        elif case == "geometry":
            h.surface.snapshot = lambda window: ((10, 20, 73, 60), (0, 0, 100, 100), 1.5, 123)
        elif case == "native_lifetime":
            h.surface.snapshot = lambda window: ((10, 20, 74, 60), (0, 0, 100, 100), 1.5, 124)
        elif case == "revoked":
            h.revoked.set()
        elif case == "deadline":
            h.handler.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        return boundary(target, frame)
    h.handler._last_boundary = changed_boundary
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        result = await h.operator.execute(*h.request_and_decision(frame))
        assert result.status == "rejected" and result.error_code == code
        assert h.counts["fresh"] == h.counts["capture"] == 1 and h.counts["uia"] == 2
        assert h.input_count == h.counts["point"] == 0
    h.assert_retired()


@pytest.mark.asyncio
async def test_cancelled_step_never_sends_verified_click(tmp_path):
    h, cancel = Harness(tmp_path), asyncio.Event()
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        cancel.set()
        result = await h.operator.execute(*h.request_and_decision(frame), cancel_event=cancel)
        assert result.status == "cancelled" and result.error_code == "navigation_cancelled"
        assert not h.context.pipe.sent and h.input_count == 0
    h.assert_retired()


@pytest.mark.asyncio
async def test_afterclick_capture_failure_retires_without_replaying_input(tmp_path):
    h = Harness(tmp_path)
    h.on_input = lambda: setattr(h.surface, "blank", True)
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        req, decision = h.request_and_decision(frame)
        result = await h.operator.execute(req, decision)
        assert result.error_code == "navigation_capture_blank" and result.next_frame is None
        assert h.input_count == 1 and frame.frame_id not in h.handler._frames
        assert h.worker._closed and h.worker.revoked.is_set()
        before = len(h.context.pipe.sent)
        replay = await h.operator.execute(req, decision)
        assert replay.error_code == "navigation_worker_revoked"
        assert len(h.context.pipe.sent) == before and h.input_count == 1
    h.assert_retired()


@pytest.mark.asyncio
async def test_search_query_keeps_generic_digests_and_fresh_search_focus_proof(tmp_path):
    h, writes = Harness(tmp_path), []
    class Value:
        IsReadOnly, Value = False, ""
        def SetValue(self, text):
            self.Value = text
            writes.append(text)
    value = Value()
    h.transport.search.GetValuePattern = lambda: value
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        req = NavigationRequest(target=h.target, frame=frame, deadline_at=h.deadline)
        decision = NavigationDecision(frame_id=frame.frame_id, action="set_target_query", query_alias_index=1)
        result = await h.operator.execute(req, decision)
        assert result.status == "action_attempted" and writes == [h.target.trusted_queries[1]]
        assert [command["action"] for command in h.context.pipe.sent] == [
            "scope", "digest", "digest", "search_focus", "scope", "set_query", "capture", "scope",
        ]
        assert h.counts == {"fresh": 3, "capture": 4, "uia": 10, "point": 0}
        assert h.input_count == 0
    h.assert_retired()


@pytest.mark.asyncio
async def test_boolean_capability_flag_cannot_skip_generic_proof_or_input(tmp_path):
    h = Harness(tmp_path, generic=True)
    h.operator.backend.verified_click = True
    async with h.operator.round(h.target, deadline_at=h.deadline):
        frame = await h.capture()
        result = await h.operator.execute(*h.request_and_decision(frame))
        assert result.status == "rejected" and result.error_code == "navigation_verified_click_unavailable"
        assert h.counts["fresh"] == h.input_count == 0
        assert [command["action"] for command in h.context.pipe.sent] == ["scope"]
    h.assert_retired()


def test_verified_click_wire_command_requires_closed_frame_region_and_coordinates():
    from .test_contracts import request
    req = request()
    values = dict(action="verified_click", target=req.target, frame=req.frame,
                  region=req.frame.allowed_regions[0], x=100, y=150, deadline_at=req.deadline_at)
    assert NavigationWorkerCommand(**values).action == "verified_click"
    for name in ("frame", "region", "x", "y"):
        with pytest.raises(ValidationError):
            NavigationWorkerCommand(**{key: value for key, value in values.items() if key != name})
    for name, value in (("current", False), ("verified", True), ("skip_digest", True), ("x", True)):
        with pytest.raises(ValidationError):
            NavigationWorkerCommand(**dict(values, **{name: value}))
