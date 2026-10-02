"""Read-only capture rebuilds complete real UIA phases, never input."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.navigation.contracts import NavigationDecision, NavigationRequest
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopError
from messenger_ai.adapters.qq.navigation.windows_backend import ProcessScopedDesktopOperator, WindowsNavigationBackend
from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility
from tests.adapters.qq.vm_driver.test_phase_index import Node
from .test_windows_backend import setup, command


class ElementUnavailable(RuntimeError):
    def __init__(self, hresult=-2147220991):
        super().__init__("synthetic property unavailable")
        self.hresult = hresult


class ControlNode(Node):
    def __init__(self, name, rect, *, children=(), error=None):
        super().__init__(control="Text", automation_id=name, children=children)
        self.BoundingRectangle = type("Rectangle", (), dict(zip(("left", "top", "right", "bottom"), rect)))()
        self.IsOffscreen, self.error = False, error

    @property
    def AutomationId(self):
        if self.error is not None:
            raise self.error
        return super().AutomationId


class PhaseTransport(WindowsUIAQQAccessibility):
    def __init__(self, *, faults=None):
        self._active_phase = None
        self.faults, self.phases, self.roots, self.errors = faults or {}, [], [], []
        self.after_phase = None

    def _window_uncached(self, window):
        number = len(self.roots)+1
        name = ControlNode("name", (12, 32, 38, 36))
        row = ControlNode("row", (10, 30, 42, 50), children=(name,))
        rows = ControlNode("list", (10, 30, 42, 57), children=(row,))
        search = ControlNode("search", (10, 20, 42, 30))
        error = self.faults.get(number)
        if error is not None:
            self.errors.append(error)
        transient = ControlNode("ignored", (44, 30, 50, 40), error=error)
        root = ControlNode("root", (10, 20, 74, 60), children=(rows, search, transient))
        self.roots.append(root)
        return root

    @contextmanager
    def read_phase(self, window):
        try:
            with super().read_phase(window) as phase:
                self.phases.append(phase)
                yield phase
        finally:
            assert self._active_phase is None
            if self.after_phase is not None:
                self.after_phase(len(self.phases))


def capture_case(tmp_path, *, faults=None):
    target, state, path, handler, _, surface, revoked = setup(tmp_path)
    transport = PhaseTransport(faults=faults)
    selectors = {f"{name}_selector": getattr(handler.config, f"{name}_selector").model_copy(update={"automation_id": name})
                 for name in ("list", "row", "name", "search", "header", "composer", "message")}
    handler.config = handler.config.model_copy(update=selectors)
    handler.transport = transport
    raw_capture, captures = surface.capture, []
    def capture(window, bounds):
        raw = bytearray(raw_capture(window, bounds))
        raw[0] = len(captures)+1  # A visible search pixel identifies each render.
        captures.append(bytes(raw))
        return bytes(raw)
    surface.capture = capture
    return target, state, path, handler, transport, surface, revoked, captures


def assert_discarded_phases(transport):
    assert transport._active_phase is None
    assert all(not phase.active and phase.root is None and not phase._properties
               and not phase._cached_controls and not phase._children for phase in transport.phases)
    assert len({id(root) for root in transport.roots}) == len(transport.roots)


@pytest.mark.parametrize("fault_phase,phases,captures", [(1, 3, 1), (2, 4, 2)])
def test_capture_rebuilds_whole_surface_after_real_phase_property_or_final_sweep_failure(tmp_path, fault_phase, phases, captures):
    target, _, _, handler, transport, _, _, rendered = capture_case(tmp_path, faults={fault_phase: ElementUnavailable()})
    frame = handler(command(target, "capture")).frame
    assert len(transport.phases) == phases and len(rendered) == captures
    assert list(handler._frames) == [frame.frame_id]
    assert handler._frames[frame.frame_id][1][0] == captures  # Only the final render is published.
    assert_discarded_phases(transport)
    assert any(error.hresult == -2147220991 for error in transport.errors)


@pytest.mark.parametrize("faults,phase_count", [
    ({1: ElementUnavailable(-2147024891)}, 1),
    ({1: ElementUnavailable(), 2: ElementUnavailable()}, 2),
    ({2: ElementUnavailable(), 4: ElementUnavailable()}, 4),
])
def test_other_hresult_or_second_failed_attempt_remains_original_error_without_frame(tmp_path, faults, phase_count):
    target, _, _, handler, transport, surface, _, _ = capture_case(tmp_path, faults=faults)
    with pytest.raises(ElementUnavailable) as failure:
        handler(command(target, "capture"))
    assert failure.value is faults[max(faults)]
    assert len(transport.phases) == phase_count and handler._frames == {} and not surface.events
    assert_discarded_phases(transport)


@pytest.mark.parametrize("when", ["before_retry", "after_retry"])
@pytest.mark.parametrize("drift", ["control", "epoch", "lease", "pause", "expiry", "stale", "deadline", "revoked", "window", "process", "dpi"])
def test_capture_rebuild_never_crosses_entry_control_window_lifetime_or_original_deadline(tmp_path, when, drift):
    target, state, path, handler, transport, surface, revoked, _ = capture_case(tmp_path, faults={1: ElementUnavailable()})
    original_snapshot = surface.snapshot
    native = [original_snapshot(handler.config.window)]
    surface.snapshot = lambda window: native[0]
    def changed(phase_number):
        if phase_number != (1 if when == "before_retry" else 3):
            return
        if drift == "deadline":
            handler.deadline_at = datetime.now(UTC)-timedelta(seconds=1)
        elif drift == "revoked":
            revoked.set()
        elif drift in {"window", "process", "dpi"}:
            bounds, screen, dpi, start = native[0]
            native[0] = ((11, 20, 75, 60) if drift == "window" else bounds, screen,
                         2 if drift == "dpi" else dpi, 124 if drift == "process" else start)
        else:
            field, value = {
                "control": ("control_revision", 2), "epoch": ("session_epoch", "replacement-session"),
                "lease": ("desktop_lease_id", "replacement-lease"), "pause": ("paused", True),
                "expiry": ("lease_expires_at", datetime.now(UTC)-timedelta(seconds=1)),
                "stale": ("published_at", datetime.now(UTC)-timedelta(seconds=6)),
            }[drift]
            path.write_text(state.model_copy(update={field: value}).model_dump_json(), encoding="utf-8")
    transport.after_phase = changed
    with pytest.raises(NavigationDesktopError):
        handler(command(target, "capture"))
    assert len(transport.phases) == (1 if when == "before_retry" else 3)
    assert handler._frames == {} and not surface.events
    assert_discarded_phases(transport)


def test_capture_retry_allows_fresh_timestamp_heartbeat_without_lease_or_deadline_extension(tmp_path):
    target, state, path, handler, transport, _, _, _ = capture_case(tmp_path, faults={1: ElementUnavailable()})
    original_deadline = handler.deadline_at
    def heartbeat(number):
        path.write_text(state.model_copy(update={"published_at": datetime.now(UTC)}).model_dump_json(), encoding="utf-8")
    transport.after_phase = heartbeat
    frame = handler(command(target, "capture").model_copy(update={"deadline_at": original_deadline+timedelta(seconds=100)})).frame
    assert frame.control_revision == state.control_revision
    assert handler.deadline_at == original_deadline
    assert handler._guard(target).lease_expires_at == state.lease_expires_at
    assert_discarded_phases(transport)


@pytest.mark.parametrize("action", ["current_digest", "click", "verified_click"])
def test_input_and_current_digest_do_not_gain_capture_retry(tmp_path, action):
    target, _, _, handler, transport, surface, _, _ = capture_case(tmp_path, faults={3: ElementUnavailable()})
    frame = handler(command(target, "capture")).frame
    candidate = next(region for region in frame.allowed_regions if region.kind == "candidate")
    operation = (command(target, "digest", frame=frame, region=candidate, current=True) if action == "current_digest"
                 else command(target, action, frame=frame, x=25, y=34,
                              **({"region": candidate} if action == "verified_click" else {})))
    with pytest.raises(ElementUnavailable):
        handler(operation)
    assert len(transport.phases) == 3 and not surface.events
    assert_discarded_phases(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_phase,phase_count,capture_count", [(5, 7, 3), (6, 8, 4)])
async def test_real_click_pipeline_rebuilds_only_postcapture_without_replaying_native_input(tmp_path, fault_phase, phase_count, capture_count):
    target, _, _, handler, transport, surface, revoked, captures = capture_case(tmp_path, faults={fault_phase: ElementUnavailable()})
    class Worker:
        async def request(self, operation, **kwargs): return handler(operation)
        def revoke(self): revoked.set()
        def close(self): handler.close()
    backend = WindowsNavigationBackend(handler.config, target, deadline_at=handler.deadline_at, worker=Worker())
    frame = await backend.bound_capture(target, deadline_at=handler.deadline_at)
    region = next(region for region in frame.allowed_regions if region.kind == "candidate")
    operator = ProcessScopedDesktopOperator(backend=backend)
    async with operator.round(target, deadline_at=handler.deadline_at):
        result = await operator.execute(NavigationRequest(target=target, frame=frame, deadline_at=handler.deadline_at),
            NavigationDecision(action="click_candidate", frame_id=frame.frame_id, bbox=region.bbox, observed_label=target.display_name))
        assert list(handler._frames) == [result.next_frame.frame_id]
        assert handler._frames[result.next_frame.frame_id][1][0] == capture_count
    assert result.status == "action_attempted" and result.error_code is None
    assert len(transport.phases) == phase_count and len(captures) == capture_count
    assert surface.events == [("click", 25, 34)] and not operator.operation_lock.locked()
    assert_discarded_phases(transport)
