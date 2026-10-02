"""Native-only scope keeps independent control and window fences."""
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.navigation.contracts import NavigationDecision, NavigationRequest
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopError
from messenger_ai.adapters.qq.navigation.windows_backend import (
    NavigationGuardState, ProcessScopedDesktopOperator, WindowsNavigationBackend,
)
from .test_windows_backend import setup, command


def forbid_uia_and_capture(transport, surface):
    def forbidden(*args, **kwargs):
        raise AssertionError("scope must not scan UI controls or render pixels")
    transport.read_phase = surface.capture = forbidden


def test_scope_returns_complete_scope_with_two_fresh_native_fences_and_zero_ui_reads(tmp_path):
    target, state, _, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    guards, snapshots = [], []
    original_guard, original_snapshot = handler._guard, surface.snapshot

    def guard(target):
        value = original_guard(target)
        guards.append(value)
        return value

    def snapshot(window):
        snapshots.append(window)
        return original_snapshot(window)

    handler._guard, surface.snapshot = guard, snapshot
    scope = handler(command(target, "scope")).scope
    assert len(guards) == 3  # command admission + the scope's two fresh guards
    assert len(snapshots) == 2
    expected = dict(account_id=target.account_id, conversation_id=target.conversation_id,
        binding_id=target.binding_id, binding_revision=target.binding_revision,
        run_id=state.run_id, session_epoch=state.session_epoch, surface_epoch=state.surface_epoch,
        worker_epoch=state.worker_epoch, desktop_lease_id=state.desktop_lease_id,
        lease_expires_at=state.lease_expires_at, control_revision=state.control_revision,
        process_id=state.process_id, window_handle=state.window_handle,
        screen_origin_x=0, screen_origin_y=0, screen_width=100, screen_height=100,
        crop_origin_x=10, crop_origin_y=20, crop_width=64, crop_height=40, dpi_scale=1.5,
        foreground=True, paused=False, has_owned_draft=False, has_commit_obligation=False)
    assert scope.model_dump(exclude={"observed_at"}) == expected
    assert state.published_at <= scope.observed_at <= datetime.now(UTC)
    assert not surface.events


@pytest.mark.parametrize("field,value", [
    ("run_id", "replacement-run"), ("session_epoch", "replacement-session"),
    ("surface_epoch", "replacement-surface"), ("worker_epoch", "replacement-worker"),
    ("observation_epoch", "replacement-observation"), ("desktop_lease_id", "replacement-lease"),
    ("control_revision", 2), ("process_id", 99), ("window_handle", 99),
    ("process_started_at_100ns", 124), ("paused", True),
    ("has_owned_draft", True), ("has_commit_obligation", True),
    ("lease_expires_at", "expired"), ("lease_expires_at", "shortened"),
    ("published_at", "stale"), ("published_at", "future"),
    ("target", "replacement-account"), ("target", "replacement-conversation"),
    ("target", "replacement-binding"), ("target", "replacement-revision"),
])
def test_scope_rejects_actual_guard_drift_between_native_boundaries_without_ui_reads(tmp_path, field, value):
    target, state, path, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    original_snapshot = surface.snapshot

    def snapshot(window):
        updates = value
        if field == "target":
            target_field = {"replacement-account": "account_id", "replacement-conversation": "conversation_id",
                            "replacement-binding": "binding_id", "replacement-revision": "binding_revision"}[value]
            updates = target.model_copy(update={target_field: 3 if target_field == "binding_revision" else "replacement"})
        elif field == "lease_expires_at":
            updates = datetime.now(UTC)-timedelta(seconds=1) if value == "expired" else state.lease_expires_at-timedelta(seconds=1)
        elif field == "published_at":
            updates = datetime.now(UTC)+timedelta(seconds=-6 if value == "stale" else 1)
        path.write_text(state.model_copy(update={field: updates}).model_dump_json(), encoding="utf-8")
        return original_snapshot(window)

    surface.snapshot = snapshot
    with pytest.raises(NavigationDesktopError):
        handler(command(target, "scope"))
    assert not surface.events


@pytest.mark.parametrize("replacement", [
    ((11, 20, 75, 60), (0, 0, 100, 100), 1.5, 123),
    ((10, 21, 74, 61), (0, 0, 100, 100), 1.5, 123),
    ((10, 20, 75, 60), (0, 0, 100, 100), 1.5, 123),
    ((10, 20, 74, 61), (0, 0, 100, 100), 1.5, 123),
    ((10, 20, 74, 60), (-1, 0, 100, 100), 1.5, 123),
    ((10, 20, 74, 60), (0, -1, 100, 100), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 101, 100), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 100, 101), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 100, 100), 2, 123),
    ((10, 20, 74, 60), (0, 0, 100, 100), 1.5, 124),
])
def test_scope_rejects_fresh_window_screen_dpi_or_process_drift(tmp_path, replacement):
    target, _, _, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    original_snapshot, snapshots = surface.snapshot, []

    def snapshot(window):
        snapshots.append(window)
        return original_snapshot(window) if len(snapshots) == 1 else replacement

    surface.snapshot = snapshot
    with pytest.raises(NavigationDesktopError, match="capture_scope_changed"):
        handler(command(target, "scope"))
    assert len(snapshots) == 2 and not surface.events


@pytest.mark.parametrize("native_result", [
    ((74, 20, 10, 60), (0, 0, 100, 100), 1.5, 123),
    ((10, 60, 74, 20), (0, 0, 100, 100), 1.5, 123),
    ((110, 20, 174, 60), (0, 0, 100, 100), 1.5, 123),
    ((10, 120, 74, 160), (0, 0, 100, 100), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 0, 100), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 100, 0), 1.5, 123),
    ((10, 20, 74, 60), (0, 0, 100, 100), 1.5, 124),
])
def test_scope_still_rejects_invalid_crop_or_original_process_lifetime(tmp_path, native_result):
    target, _, _, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    surface.snapshot = lambda window: native_result
    with pytest.raises(NavigationDesktopError):
        handler(command(target, "scope"))


@pytest.mark.parametrize("code", ["navigation_qq_not_foreground", "navigation_qq_not_maximized", "navigation_dpi_unavailable"])
def test_scope_keeps_native_foreground_maximized_and_dpi_failure_closed(tmp_path, code):
    target, _, _, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    def unavailable(window):
        raise NavigationDesktopError(code)
    surface.snapshot = unavailable
    with pytest.raises(NavigationDesktopError, match=code):
        handler(command(target, "scope"))


def test_scope_timestamp_refresh_preserves_original_lease_and_total_deadline(tmp_path):
    target, state, path, handler, transport, surface, _ = setup(tmp_path)
    forbid_uia_and_capture(transport, surface)
    original_snapshot, original_deadline = surface.snapshot, handler.deadline_at

    def snapshot(window):
        path.write_text(state.model_copy(update={"published_at": datetime.now(UTC)}).model_dump_json(), encoding="utf-8")
        return original_snapshot(window)

    surface.snapshot = snapshot
    scope = handler(command(target, "scope").model_copy(update={"deadline_at": original_deadline+timedelta(seconds=100)})).scope
    assert scope.lease_expires_at == state.lease_expires_at and handler.deadline_at == original_deadline
    assert NavigationGuardState.model_validate_json(path.read_text(encoding="utf-8")).lease_expires_at == state.lease_expires_at


@pytest.mark.asyncio
async def test_click_pipeline_keeps_current_roi_input_and_postcapture_independent_ui_phases(tmp_path):
    target, _, _, handler, transport, surface, revoked = setup(tmp_path)
    counts, per_command = Counter(), []
    original_phase, original_capture = transport.read_phase, surface.capture
    def phase(window):
        counts["phase"] += 1
        return original_phase(window)
    def capture(window, bounds):
        counts["capture"] += 1
        return original_capture(window, bounds)
    transport.read_phase, surface.capture = phase, capture
    class Worker:
        async def request(self, request, **kwargs):
            before = counts.copy()
            result = handler(request)
            per_command.append((request.action, request.current, counts["phase"]-before["phase"], counts["capture"]-before["capture"]))
            return result
        def revoke(self): revoked.set()
        def close(self): handler.close()
    backend = WindowsNavigationBackend(handler.config, target, deadline_at=handler.deadline_at, worker=Worker())
    frame = await backend.bound_capture(target, deadline_at=handler.deadline_at)
    counts.clear()
    per_command.clear()
    region = next(region for region in frame.allowed_regions if region.kind == "candidate")
    decision = NavigationDecision(action="click_candidate", frame_id=frame.frame_id, bbox=region.bbox, observed_label=target.display_name)
    operator = ProcessScopedDesktopOperator(backend=backend)
    async with operator.round(target, deadline_at=handler.deadline_at):
        result = await operator.execute(NavigationRequest(target=target, frame=frame, deadline_at=handler.deadline_at), decision)
    assert result.status == "action_attempted" and result.error_code is None and result.next_frame.frame_id != frame.frame_id
    assert per_command == [
        ("scope", None, 0, 0), ("scope", None, 0, 0), ("verified_click", None, 2, 1),
        ("capture", None, 2, 1), ("scope", None, 0, 0),
    ]
    assert counts == {"phase": 4, "capture": 2}
    assert surface.events == [("click", 25, 34)] and revoked.is_set() and not operator.operation_lock.locked()
