from __future__ import annotations

import asyncio
import ctypes
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.navigation.contracts import NavigationRect
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopError
from messenger_ai.adapters.qq.navigation.windows_backend import (
    NavigationGuardState, WindowsNavigationCommandHandler, WindowsNavigationConfig,
    mask_bgra, selected_runtime_token, foreground_belongs_to_exact_main,
    ProcessScopedDesktopOperator, _NativeWindowsSurface,
)
from messenger_ai.adapters.qq.vm_driver.transport import UIAUnavailable, WindowsUIAQQAccessibility
from messenger_ai.adapters.qq.navigation.worker_process import NavigationWorkerCommand

from .test_contracts import request


class Control:
    def __init__(self, rect, **values):
        self.BoundingRectangle = SimpleNamespace(**dict(zip(("left", "top", "right", "bottom"), rect)))
        self.IsOffscreen = False
        self.__dict__.update(values)
    def GetChildren(self):
        return getattr(self, "children", [])
    def GetRuntimeId(self):
        return [42, id(self) % 65536]


class Surface:
    blank = False
    events = None
    def __init__(self):
        self.events = []
    def snapshot(self, window):
        return (10, 20, 74, 60), (0, 0, 100, 100), 1.5, 123
    def capture(self, window, bounds):
        if self.blank:
            return b"\0\0\0\xff" * 64 * 40
        return b"".join(bytes((40 + x % 2 * 100, 80, 90, 255)) for _ in range(40) for x in range(64))
    def click(self, window, x, y):
        self.events.append(("click", x, y))


class Transport:
    def __init__(self):
        self.root = Control((10, 20, 74, 60))
        self.list = Control((10, 30, 42, 57))
        self.search = Control((10, 20, 42, 30))
        self.row = Control((10, 30, 42, 50))
        self.name = Control((12, 32, 38, 36))
        self.mapping = {
            (id(self.root), "list"): [self.list], (id(self.root), "search"): [self.search],
            (id(self.list), "row"): [self.row], (id(self.row), "name"): [self.name],
        }
        self.active = 0
    @contextmanager
    def read_phase(self, window):
        self.active += 1
        try:
            yield SimpleNamespace(root=self.root)
        finally:
            self.active -= 1
    def _select(self, root, selector):
        return self.mapping.get((id(root), selector.name), [])
    def _composer_focused(self, control, window):
        return True
    def _matches(self, item, selector, *args):
        return getattr(item, "kind", None) == selector.name
    def _descendants(self, root):
        return []


def setup(tmp_path):
    target = request().target
    now = datetime.now(UTC)
    state = NavigationGuardState(target=target, run_id="run", session_epoch="session", surface_epoch="surface",
        worker_epoch="worker", desktop_lease_id="lease", lease_expires_at=now + timedelta(seconds=45),
        control_revision=1, process_id=7, window_handle=8, process_started_at_100ns=123,
        paused=False, has_owned_draft=False, has_commit_obligation=False, published_at=now)
    path = tmp_path / "guard.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    selectors = {f"{name}_selector": {"name": name, "control_type": "Text"}
                 for name in ("list", "row", "name", "search", "header", "composer", "message")}
    config = WindowsNavigationConfig(guard_state_path=str(path), window={"process_id": 7, "window_handle": 8, "class_name": "QQ"},
        expected_process_started_at_100ns=123, expected_run_id="run", expected_worker_epoch="worker", **selectors)
    transport, surface, revoked = Transport(), Surface(), threading.Event()
    handler = WindowsNavigationCommandHandler(config, revoked, now + timedelta(seconds=45), transport=transport, surface=surface)
    return target, state, path, handler, transport, surface, revoked


def command(target, action, **values):
    return NavigationWorkerCommand(action=action, target=target, deadline_at=datetime.now(UTC) + timedelta(seconds=30), **values)


def test_exact_window_capture_masks_all_except_name_and_search(tmp_path):
    target, _, _, handler, transport, _, _ = setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    assert frame.crop_origin_x == 10 and frame.crop_origin_y == 20
    assert frame.privacy_mask_applied and frame.png_bytes.startswith(b"\x89PNG")
    frozen_pixels = handler._frames[frame.frame_id][1]
    # Row preview (local x5,y22), whole chat/composer area remain masked.
    assert frozen_pixels[(22 * 64 + 5) * 4:(22 * 64 + 5) * 4 + 4] == b"\x20\x20\x20\xff"
    assert frozen_pixels[(12 * 64 + 3) * 4:(12 * 64 + 3) * 4 + 4] != b"\x20\x20\x20\xff"
    assert transport.active == 0


@pytest.mark.parametrize("field,value", [("paused", True), ("has_owned_draft", True), ("has_commit_obligation", True),
    ("control_revision", 2), ("run_id", "another"), ("worker_epoch", "another")])
def test_guard_changes_prevent_input(tmp_path, field, value):
    target, state, path, handler, _, surface, _ = setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    path.write_text(state.model_copy(update={field: value}).model_dump_json(), encoding="utf-8")
    with pytest.raises(NavigationDesktopError):
        handler(command(target, "click", frame=frame, x=15, y=33))
    assert not surface.events


def test_owned_frame_and_label_region_only_and_once(tmp_path):
    target, _, _, handler, _, surface, _ = setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    with pytest.raises(NavigationDesktopError):
        handler(command(target, "click", frame=frame, x=60, y=33))
    handler(command(target, "click", frame=frame, x=15, y=33))
    assert surface.events == [("click", 15, 33)]
    with pytest.raises(NavigationDesktopError, match="not_owned"):
        handler(command(target, "click", frame=frame, x=15, y=33))


def test_blank_capture_missing_or_ambiguous_label_and_revocation_fail_closed(tmp_path):
    target, _, _, handler, transport, surface, revoked = setup(tmp_path)
    surface.blank = True
    with pytest.raises(NavigationDesktopError, match="capture_blank"):
        handler(command(target, "capture"))
    surface.blank = False
    transport.mapping[(id(transport.row), "name")] = []
    with pytest.raises(NavigationDesktopError, match="label_unproven"):
        handler(command(target, "capture"))
    revoked.set()
    with pytest.raises(NavigationDesktopError, match="revoked"):
        handler(command(target, "capture"))


def test_worker_commands_have_no_arbitrary_text_keys_or_send(tmp_path):
    target = request().target
    for action in ("send", "shell", "press_key"):
        with pytest.raises(ValidationError):
            command(target, action)
    with pytest.raises(ValidationError):
        command(target, "capture", text="message")


def test_selected_witness_token_uses_shared_dot_hash():
    import hashlib
    assert selected_runtime_token([42, 13, 0]) == hashlib.sha256(b"42.13.0").hexdigest()
    with pytest.raises(NavigationDesktopError):
        selected_runtime_token([])


def test_mask_never_accepts_out_of_bounds_or_invalid_bitmap():
    with pytest.raises(NavigationDesktopError):
        mask_bgra(2, 2, b"bad", ())
    with pytest.raises(NavigationDesktopError):
        mask_bgra(2, 2, b"\0" * 16, (NavigationRect(left=0, top=0, right=3, bottom=2),))


@pytest.mark.parametrize("changes", [
    {"foreground": 0}, {"foreground_pid": 99}, {"main_pid": 99},
    {"foreground_root": 999}, {"visible": False}, {"iconic": True},
])
def test_native_foreground_accepts_only_renderer_under_exact_main(changes):
    from messenger_ai.adapters.qq.models import QQWindow
    data = dict(foreground=80, foreground_pid=7, foreground_root=8, main_pid=7,
                window=QQWindow(process_id=7, window_handle=8, class_name="QQ"), visible=True, iconic=False)
    assert foreground_belongs_to_exact_main(**data)
    data.update(changes)
    assert not foreground_belongs_to_exact_main(**data)


def test_semantic_container_direct_text_only_excludes_time_and_preview(tmp_path):
    target, _, _, handler, transport, _, _ = setup(tmp_path)
    info = Control((10, 30, 42, 50), children=[transport.name, Control((10, 40, 42, 48), kind="preview")])
    transport.name.kind = "name"
    search_container = Control((10, 20, 42, 30), children=[transport.search])
    transport.search.kind = "search"
    values = handler.config.model_dump()
    values.update({
        "name_container_selector": {"name": "info", "control_type": "Group"},
        "search_container_selector": {"name": "search_container", "control_type": "Group"},
    })
    handler.config = WindowsNavigationConfig.model_validate(values)
    transport.mapping[(id(transport.row), "info")] = [info]
    transport.mapping[(id(transport.root), "search_container")] = [search_container]
    frame = handler(command(target, "capture")).frame
    candidates = [region for region in frame.allowed_regions if region.kind == "candidate"]
    assert candidates[0].bbox == NavigationRect(left=2, top=12, right=28, bottom=16)
    info.children.append(Control((12, 36, 38, 39), kind="name"))
    with pytest.raises(NavigationDesktopError, match="label_unproven"):
        handler(command(target, "capture"))


def test_search_value_input_remains_registered_focus_only_without_enter(tmp_path):
    target, _, _, handler, transport, _, _ = setup(tmp_path)
    events = []
    class Value:
        IsReadOnly = False
        Value = ""
        def SetValue(self, text):
            events.append(text)
            self.Value = text
    value = Value()
    transport.search.GetValuePattern = lambda: value
    frame = handler(command(target, "capture")).frame
    region = next(region for region in frame.allowed_regions if region.kind == "search")
    handler(command(target, "set_query", frame=frame, region=region, query_alias_index=1))
    assert events == ["trusted-qq-id"]
    assert transport.active == 0


def witness_setup(tmp_path):
    values = setup(tmp_path)
    target, state, path, handler, transport, _, _ = values
    path.write_text(state.model_copy(update={"observation_epoch": "observation"}).model_dump_json(), encoding="utf-8")
    transport.mapping[(id(transport.root), "row")] = [transport.row]
    transport.row.GetSelectionItemPattern = lambda: SimpleNamespace(IsSelected=True)
    header = Control((44, 22, 60, 26), ClassName="chat-header__contact-name", Name="private contact")
    message = Control((44, 28, 72, 40), ClassName="ml-root")
    composer = Control((44, 41, 72, 58), ClassName="is-empty")
    composer.GetValuePattern = lambda: SimpleNamespace(Value="")
    for name, item in (("header", header), ("message", message), ("composer", composer)):
        transport.mapping[(id(transport.root), name)] = [item]
    transport.tail_pattern = SimpleNamespace(VerticallyScrollable=True, VerticalScrollPercent=100.0, VerticalViewSize=20.0)
    transport._message_scroll_pattern = lambda *args: transport.tail_pattern
    transport.message_tail_is_latest = lambda *args: WindowsUIAQQAccessibility.message_tail_is_latest(transport, *args)
    return values


@pytest.mark.parametrize("selected", [False, True])
def test_local_service_surface_requires_three_actual_zero_counts(tmp_path, selected):
    target, _, _, handler, transport, surface, _ = witness_setup(tmp_path)
    transport.row.GetSelectionItemPattern = lambda: SimpleNamespace(IsSelected=selected)
    for name in ("header", "message", "composer"):
        transport.mapping[(id(transport.root), name)] = []
    witness = handler(command(target, "local_witness")).witness
    assert witness.surface_kind == "non_chat"
    assert (witness.header_candidate_count, witness.message_candidate_count, witness.composer_candidate_count) == (0, 0, 0)
    assert witness.selected_row_candidate_count == int(selected)
    assert witness.conversation_type == "unknown" and witness.group_marker_probe_complete and witness.group_marker_count == 0
    assert witness.header_digest is witness.active_chat_structure_digest is witness.latest_tail is witness.composer_empty is None
    assert not surface.events and transport.active == 0


@pytest.mark.parametrize("counts", [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1),
    (2, 0, 0), (0, 2, 0), (0, 0, 2), (2, 1, 1), (1, 2, 1), (1, 1, 2), (1, 1, 1)])
def test_local_partial_or_ambiguous_chat_never_becomes_non_chat(tmp_path, counts):
    target, _, _, handler, transport, surface, _ = witness_setup(tmp_path)
    for name, count in zip(("header", "message", "composer"), counts):
        transport.mapping[(id(transport.root), name)] *= count
    witness = handler(command(target, "local_witness")).witness
    assert witness.surface_kind == ("chat" if counts == (1, 1, 1) else "unknown")
    assert (witness.header_candidate_count, witness.message_candidate_count, witness.composer_candidate_count) == counts
    assert not surface.events and transport.active == 0


@pytest.mark.parametrize("kind", ["multiple_selected", "group", "bad_group_metadata", "incomplete_group_probe"])
def test_service_surface_ambiguity_or_group_probe_failure_is_not_navigation_permission(tmp_path, kind):
    target, _, _, handler, transport, surface, _ = witness_setup(tmp_path)
    for name in ("header", "message", "composer"):
        transport.mapping[(id(transport.root), name)] = []
    if kind == "multiple_selected":
        second = Control((10, 50, 42, 57), GetSelectionItemPattern=lambda: SimpleNamespace(IsSelected=True))
        transport.mapping[(id(transport.root), "row")].append(second)
    elif kind == "group":
        transport._descendants = lambda root: [Control((44, 28, 72, 40), ClassName="group-member-list")]
    elif kind == "bad_group_metadata":
        transport._descendants = lambda root: [Control((44, 28, 72, 40), ClassName=None)]
    else:
        def incomplete(root):
            yield Control((44, 28, 72, 40), ClassName="normal-control")
            raise UIAUnavailable("incomplete tree")
        transport._descendants = incomplete
    if kind in {"multiple_selected", "group"}:
        assert handler(command(target, "local_witness")).witness.surface_kind == "unknown"
    else:
        with pytest.raises((NavigationDesktopError, UIAUnavailable)):
            handler(command(target, "local_witness"))
    assert not surface.events and transport.active == 0


@pytest.mark.parametrize("scrollable,percent,view_size,expected", [
    (True, 100, 20, True), (True, 99.9, 20, False), (True, 0, 20, False),
    (False, -1, 100, True), (False, 0, 100, None), (False, -1, 99, None),
    (True, float("nan"), 20, None), (True, 100, float("inf"), None),
    (True, "100", 20, None), ("False", -1, 100, None), (0, -1, 100, None),
    (True, True, 20, None), (True, 100, False, None), (True, 100, 0, None),
])
def test_local_witness_reuses_strict_tail_proof_without_coercion(tmp_path, scrollable, percent, view_size, expected):
    target, _, _, handler, transport, _, _ = witness_setup(tmp_path)
    transport.tail_pattern = SimpleNamespace(VerticallyScrollable=scrollable, VerticalScrollPercent=percent, VerticalViewSize=view_size)
    witness = handler(command(target, "local_witness")).witness
    assert witness.latest_tail is expected
    assert witness.composer_empty is True
    assert witness.selected_row_runtime_id_hash == selected_runtime_token(transport.row.GetRuntimeId())
    assert transport.active == 0
    assert "private contact" not in witness.model_dump_json()


def test_local_group_probe_records_markers_and_never_certifies_partial_probe(tmp_path):
    target, _, _, handler, transport, _, _ = witness_setup(tmp_path)
    transport._descendants = lambda root: [Control((44, 28, 72, 40), ClassName="group-member-list")]
    witness = handler(command(target, "local_witness")).witness
    assert witness.conversation_type == "group" and witness.group_marker_count == 1
    assert witness.group_marker_probe_complete
    transport._descendants = lambda root: [Control((44, 28, 72, 40), ClassName=None)]
    with pytest.raises(NavigationDesktopError, match="group_metadata_invalid"):
        handler(command(target, "local_witness"))
    def broken_probe(root):
        yield Control((44, 28, 72, 40), ClassName="normal-control")
        raise UIAUnavailable("read failed")
    transport._descendants = broken_probe
    with pytest.raises(UIAUnavailable):
        handler(command(target, "local_witness"))
    assert transport.active == 0


@pytest.mark.parametrize("invalid", ["True", 1, 0])
def test_selected_pattern_does_not_coerce_bad_metadata(tmp_path, invalid):
    target, _, _, handler, transport, _, _ = witness_setup(tmp_path)
    transport.row.GetSelectionItemPattern = lambda: SimpleNamespace(IsSelected=invalid)
    with pytest.raises(NavigationDesktopError, match="selection_metadata_invalid"):
        handler(command(target, "local_witness"))


def test_native_snapshot_requires_maximized_main_before_capture_or_process_read(tmp_path):
    _, _, _, handler, _, _, _ = setup(tmp_path)
    surface = _NativeWindowsSurface.__new__(_NativeWindowsSurface)
    def pid(hwnd, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_ulong)).contents.value = 7
    surface.user = SimpleNamespace(GetForegroundWindow=lambda: 80,
        GetWindowThreadProcessId=pid, GetAncestor=lambda *args: 8,
        IsWindowVisible=lambda *args: True, IsIconic=lambda *args: False,
        IsZoomed=lambda *args: False)
    with pytest.raises(NavigationDesktopError, match="not_maximized"):
        surface.snapshot(handler.config.window)


class OwnedBackend:
    def __init__(self, lock):
        self.lock, self.events, self.fail = lock, [], False
    def revoke(self):
        self.events.append("revoke")
    def close(self):
        assert self.lock.locked()  # Never release desktop before owned reap.
        self.events.append("close")
        if self.fail:
            raise NavigationDesktopError("navigation_worker_reap_failed")


@pytest.mark.asyncio
async def test_process_round_cross_task_cleanup_reaps_before_shared_lock_release():
    req, lock = request(), asyncio.Lock()
    backend = OwnedBackend(lock)
    operator = ProcessScopedDesktopOperator(backend=backend, operation_lock=lock)
    context = operator.round(req.target, deadline_at=req.deadline_at)
    await context.__aenter__()
    async def inherited_step():
        async with operator.round(req.target, deadline_at=req.deadline_at):
            assert lock.locked()
    await asyncio.wait_for(asyncio.create_task(inherited_step()), timeout=0.5)
    assert not backend.events  # Nested steps cannot close the whole worker.
    capability = operator._round_capability.get()
    operator.revoke_round()
    assert operator._round_capability.get() is None and not capability.active
    await asyncio.wait_for(asyncio.create_task(context.__aexit__(None, None, None)), timeout=0.5)
    assert not lock.locked() and backend.events == ["revoke", "revoke", "close"]


@pytest.mark.asyncio
async def test_failed_reap_keeps_own_lock_and_retry_releases_only_after_worker_gone():
    req, lock = request(), asyncio.Lock()
    backend = OwnedBackend(lock)
    backend.fail = True
    operator = ProcessScopedDesktopOperator(backend=backend, operation_lock=lock)
    with pytest.raises(NavigationDesktopError, match="reap_failed"):
        async with operator.round(req.target, deadline_at=req.deadline_at):
            pass
    assert lock.locked() and operator._round_capability.get() is None
    with pytest.raises(NavigationDesktopError, match="cleanup_required"):
        async with operator.round(req.target, deadline_at=req.deadline_at):
            pytest.fail("still-owned desktop must not admit a new round")
    with pytest.raises(NavigationDesktopError, match="reap_failed"):
        await operator.retry_cleanup()
    assert lock.locked()
    backend.fail = False
    await asyncio.create_task(operator.retry_cleanup())
    assert not lock.locked() and operator._failed_cleanup is None
    await lock.acquire()  # An unrelated owner now owns the same shared lock.
    await operator.retry_cleanup()
    assert lock.locked()
    lock.release()


@pytest.mark.asyncio
async def test_reaped_round_late_child_cannot_execute_after_parent_revocation():
    req, lock = request(), asyncio.Lock()
    backend = OwnedBackend(lock)
    operator = ProcessScopedDesktopOperator(backend=backend, operation_lock=lock)
    wake = asyncio.Event()
    async def late_child():
        await wake.wait()
        async with operator.round(req.target, deadline_at=req.deadline_at):
            pytest.fail("late inherited round must be revoked")
    async with operator.round(req.target, deadline_at=req.deadline_at):
        task = asyncio.create_task(late_child())
    wake.set()
    with pytest.raises(NavigationDesktopError, match="round_released"):
        await task
    assert not lock.locked()


def test_parent_timestamp_only_heartbeat_does_not_invalidate_capture_or_renew_deadline(tmp_path):
    target, state, path, handler, _, surface, _ = setup(tmp_path)
    initial_deadline = handler.deadline_at
    original_capture = surface.capture
    def refreshed_capture(window, bounds):
        path.write_text(state.model_copy(update={"published_at": datetime.now(UTC)}).model_dump_json(), encoding="utf-8")
        return original_capture(window, bounds)
    surface.capture = refreshed_capture
    operation = command(target, "capture").model_copy(update={"deadline_at": initial_deadline + timedelta(seconds=100)})
    frame = handler(operation).frame
    assert frame.control_revision == state.control_revision
    assert handler.deadline_at == initial_deadline
    assert NavigationGuardState.model_validate_json(path.read_text(encoding="utf-8")).lease_expires_at == state.lease_expires_at


def test_revocation_arriving_at_last_native_boundary_prevents_input(tmp_path):
    target, _, _, handler, _, surface, revoked = setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    original_snapshot = surface.snapshot
    calls = 0
    def snapshot(window):
        nonlocal calls
        calls += 1
        # Fresh masked capture + independent fresh-region read use four native
        # snapshots; the fifth is the final input boundary.
        if calls == 5:
            revoked.set()
        return original_snapshot(window)
    surface.snapshot = snapshot
    with pytest.raises(NavigationDesktopError, match="revoked"):
        handler(command(target, "click", frame=frame, x=15, y=33))
    assert calls == 5 and not surface.events


@pytest.mark.parametrize("readonly", ["False", 0, None, True])
def test_search_input_does_not_coerce_writable_pattern_metadata(tmp_path, readonly):
    target, _, _, handler, transport, _, _ = setup(tmp_path)
    events = []
    value = SimpleNamespace(IsReadOnly=readonly, Value="", SetValue=lambda text: events.append(text))
    transport.search.GetValuePattern = lambda: value
    frame = handler(command(target, "capture")).frame
    region = next(region for region in frame.allowed_regions if region.kind == "search")
    with pytest.raises(NavigationDesktopError, match="search_value_unavailable"):
        handler(command(target, "set_query", frame=frame, region=region, query_alias_index=0))
    assert not events


def test_frozen_digest_uses_owned_pixels_and_current_digest_independent_capture(tmp_path):
    target, _, _, handler, _, surface, _ = setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    region = next(region for region in frame.allowed_regions if region.kind == "candidate")
    captures = 0
    original_capture = surface.capture
    def capture(window, bounds):
        nonlocal captures
        captures += 1
        return original_capture(window, bounds)
    surface.capture = capture
    frozen = handler(command(target, "digest", frame=frame, region=region, current=False)).digest
    assert captures == 0
    current = handler(command(target, "digest", frame=frame, region=region, current=True)).digest
    assert frozen == current and captures == 1
    unowned = frame.model_copy(update={"frame_id": "never-captured"})
    with pytest.raises(NavigationDesktopError, match="not_owned"):
        handler(command(target, "digest", frame=unowned, region=region, current=False))


def fragmented_name_setup(tmp_path):
    values = setup(tmp_path)
    _, _, _, handler, transport, _, _ = values
    class Fragment(Control):
        @property
        def Name(self):
            raise AssertionError("nickname fragment geometry does not read text")
    fragments = [Fragment(rect, kind="name") for rect in
                 ((12, 32, 20, 36), (20, 32, 26, 36), (25, 32, 38, 36))]
    # Nested preview/time Text controls are never part of the direct nickname.
    preview = Control((12, 40, 38, 46), kind="group", children=[Fragment((12, 40, 38, 44), kind="name")])
    info = Control((10, 30, 42, 50), children=[*fragments, preview])
    data = handler.config.model_dump()
    data["name_container_selector"] = {"name": "info", "control_type": "Group"}
    handler.config = WindowsNavigationConfig.model_validate(data)
    transport.mapping[(id(transport.row), "info")] = [info]
    return values, info, fragments


def test_direct_single_line_nickname_fragments_have_union_candidate_and_fragment_masks(tmp_path):
    (target, _, _, handler, _, _, _), _, _ = fragmented_name_setup(tmp_path)
    frame = handler(command(target, "capture")).frame
    candidate = next(region for region in frame.allowed_regions if region.kind == "candidate")
    assert candidate.bbox == NavigationRect(left=2, top=12, right=28, bottom=16)
    pixels = handler._frames[frame.frame_id][1]
    assert pixels[(22 * 64 + 5) * 4:(22 * 64 + 5) * 4 + 4] == b"\x20\x20\x20\xff"


@pytest.mark.parametrize("rect", [(20, 40, 26, 44), (29, 32, 35, 36), (41, 32, 43, 36), (12, 32, 18, 36)])
def test_nickname_fragment_wrong_baseline_gap_bounds_or_overlap_is_rejected(tmp_path, rect):
    (target, _, _, handler, _, _, _), _, fragments = fragmented_name_setup(tmp_path)
    fragments[1].BoundingRectangle = SimpleNamespace(**dict(zip(("left", "top", "right", "bottom"), rect)))
    with pytest.raises(NavigationDesktopError, match="label_unproven"):
        handler(command(target, "capture"))


def test_fragment_geometry_change_even_with_same_union_rejects_capture(tmp_path):
    (target, _, _, handler, _, surface, _), _, fragments = fragmented_name_setup(tmp_path)
    original_capture = surface.capture
    def capture(window, bounds):
        fragments[1].BoundingRectangle.left = 21  # Outer union remains identical.
        return original_capture(window, bounds)
    surface.capture = capture
    with pytest.raises(NavigationDesktopError, match="capture_regions_changed"):
        handler(command(target, "capture"))


def test_partial_offscreen_false_row_is_skipped_and_its_name_remains_masked(tmp_path):
    target, _, _, handler, transport, _, _ = setup(tmp_path)
    transport.row.BoundingRectangle.bottom = 70
    frame = handler(command(target, "capture")).frame
    assert not any(region.kind == "candidate" for region in frame.allowed_regions)
    pixels = handler._frames[frame.frame_id][1]
    assert pixels[(12 * 64 + 3) * 4:(12 * 64 + 3) * 4 + 4] == b"\x20\x20\x20\xff"
    transport.row.BoundingRectangle.bottom = float("nan")
    with pytest.raises(NavigationDesktopError, match="rectangle_invalid"):
        handler(command(target, "capture"))
