from __future__ import annotations

import gc
import weakref
from collections import Counter

import pytest

from messenger_ai.adapters.qq import QQConversation, QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import (
    UIAUnavailable,
    WindowsUIAQQAccessibility,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE,
    RowBorderSample,
    ScreenRect,
)


class Pattern:
    def __init__(self, node):
        self.node = node

    def Invoke(self):
        self.node.ClassName = self.node.ClassName + " selected"


class LegacyPattern:
    DefaultAction = "按"

    def __init__(self, node):
        self.node = node

    def DoDefaultAction(self):
        self.node.ClassName = self.node.ClassName + " selected"


class Node:
    def __init__(self, *, control="Group", class_name="", automation_id="",
                 runtime_id=(1,), patterns=(), children=()):
        self._control = control
        self._class_name = class_name
        self._automation_id = automation_id
        self._runtime_id = runtime_id
        self._patterns = set(patterns)
        self._children = list(children)
        self.calls = Counter()
        self.ProcessId = 7
        self.Name = "contact"
        self.legacy_pattern = (
            LegacyPattern(self) if "legacy" in self._patterns else None
        )

    @property
    def ControlTypeName(self):
        self.calls["ControlTypeName"] += 1
        return self._control

    @property
    def ClassName(self):
        self.calls["ClassName"] += 1
        return self._class_name

    @ClassName.setter
    def ClassName(self, value):
        self._class_name = value

    @property
    def AutomationId(self):
        self.calls["AutomationId"] += 1
        return self._automation_id

    def GetChildren(self):
        self.calls["GetChildren"] += 1
        return list(self._children)

    def GetRuntimeId(self):
        self.calls["GetRuntimeId"] += 1
        return self._runtime_id

    def GetInvokePattern(self):
        self.calls["GetInvokePattern"] += 1
        return Pattern(self) if "invoke" in self._patterns else None

    def GetSelectionItemPattern(self):
        self.calls["GetSelectionItemPattern"] += 1
        return None

    def GetSelectionPattern(self):
        self.calls["GetSelectionPattern"] += 1
        return None

    def GetValuePattern(self):
        self.calls["GetValuePattern"] += 1
        return None

    def GetTextPattern(self):
        self.calls["GetTextPattern"] += 1
        return None

    def GetScrollPattern(self):
        self.calls["GetScrollPattern"] += 1
        return None

    def GetScrollItemPattern(self):
        self.calls["GetScrollItemPattern"] += 1
        return None

    def GetLegacyIAccessiblePattern(self):
        self.calls["GetLegacyIAccessiblePattern"] += 1
        return self.legacy_pattern


def _selector():
    return QQSelector(
        name="conversation_item",
        control_type="Group",
        class_name_tokens=("recent-contact-item",),
        required_patterns=("InvokePattern",),
        ancestor_control_types=("pane",),
        selected_class_name_token="selected",
    )


def _transport(root):
    access = object.__new__(WindowsUIAQQAccessibility)
    access._active_phase = None
    access._auto = type("Auto", (), {
        "ControlFromHandle": lambda _self, _handle: root,
    })()
    return access


def test_phase_materializes_tree_once_and_probes_only_requested_patterns():
    row = Node(class_name="recent-contact-item", patterns=("invoke",))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = _transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")

    with access.read_phase(window):
        assert len(access.list_conversations(window, _selector())) == 1
        assert len(access.list_conversations(window, _selector())) == 1
        first_digest = access.tree_digest(window)
        assert access.tree_digest(window) == first_digest

    assert root.calls["GetChildren"] == 1
    assert pane.calls["GetChildren"] == 1
    assert row.calls["GetChildren"] == 1
    assert row.calls["ControlTypeName"] == 1
    assert row.calls["AutomationId"] == 1
    assert row.calls["ClassName"] == 1
    assert row.calls["GetRuntimeId"] == 1
    assert row.calls["GetInvokePattern"] == 1
    assert row.calls["GetValuePattern"] == 0
    assert row.calls["GetTextPattern"] == 0
    assert row.calls["GetScrollPattern"] == 0
    assert row.calls["GetLegacyIAccessiblePattern"] == 0


def test_selection_mutation_invalidates_phase_and_requires_fresh_confirmation():
    row = Node(class_name="recent-contact-item", patterns=("invoke", "legacy"))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = _transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")

    with access.read_phase(window):
        conversation = access.list_conversations(window, _selector())[0]
        access.select_conversation(window, conversation, _selector())
        with pytest.raises(RuntimeError, match="no longer valid"):
            access.tree_digest(window)

    with access.read_phase(window):
        access.confirm_conversation_selected(window, conversation, _selector())

    assert root.calls["GetChildren"] == 2
    assert row.calls["GetLegacyIAccessiblePattern"] == 1


def test_visible_rows_cache_com_reads_only_within_each_fresh_enumeration():
    row = Node(class_name="recent-contact-item", patterns=("invoke",))
    row.IsOffscreen = False
    row.rect = ScreenRect(left=50, top=100, right=300, bottom=164)
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = _transport(root)
    access._row_screen_rect = lambda item: item.rect
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")

    first = access._visible_conversation_rows(window, _selector())
    assert access._active_phase is None
    for node in (root, pane, row):
        assert node.calls["GetChildren"] == 1
    # The walker, selector and row projection all read these properties, but
    # only one provider call per property belongs to this enumeration.
    assert row.calls["ControlTypeName"] == 1
    assert row.calls["AutomationId"] == 1
    assert row.calls["ClassName"] == 1
    assert row.calls["GetRuntimeId"] == 1
    assert row.calls["GetInvokePattern"] == 1
    assert row.calls["GetValuePattern"] == 0

    row._runtime_id = (2,)
    row.rect = ScreenRect(left=50, top=200, right=300, bottom=264)
    second = access._visible_conversation_rows(window, _selector())
    assert access._active_phase is None
    assert second[0].internal_id != first[0].internal_id
    assert second[0].rect != first[0].rect
    for node in (root, pane, row):
        assert node.calls["GetChildren"] == 2
    assert row.calls["ControlTypeName"] == 2
    assert row.calls["AutomationId"] == 2
    assert row.calls["GetRuntimeId"] == 2
    assert row.calls["GetInvokePattern"] == 2


def test_unavailable_row_releases_failed_snapshot_before_rebuilding_from_hwnd():
    access = object.__new__(WindowsUIAQQAccessibility)
    access._active_phase = None
    references = []
    handles = []

    def fresh_root(handle):
        gc.collect()
        assert all(reference() is None for reference in references)
        assert access._active_phase is None
        handles.append(handle)
        rows = []
        for index in range(2):
            row = Node(
                class_name="recent-contact-item",
                automation_id=f"generation-{len(handles)}-row-{index}",
                patterns=("invoke",),
            )
            row.IsOffscreen = False
            rows.append(row)
        pane = Node(control="Pane", children=rows)
        root = Node(control="Window", children=(pane,))
        references.extend(weakref.ref(node) for node in (root, pane, *rows))
        return root

    def row_rect(item):
        if item._automation_id == "generation-1-row-1":
            error = RuntimeError("provider row vanished after partial projection")
            error.hresult = -2147220991
            raise error
        return ScreenRect(left=50, top=100, right=300, bottom=164)

    access._auto = type("Auto", (), {
        "ControlFromHandle": lambda _self, handle: fresh_root(handle),
    })()
    access._row_screen_rect = row_rect
    rows = access._visible_conversation_rows(
        QQWindow(process_id=7, window_handle=9, class_name="QQ"), _selector(),
    )
    assert handles == [9, 9]
    assert [row.internal_id for row in rows] == [
        "generation-2-row-0", "generation-2-row-1",
    ]
    gc.collect()
    assert all(reference() is None for reference in references)
    assert access._active_phase is None


@pytest.mark.parametrize("last_sample_drift", [False, True])
def test_visual_samples_release_controls_and_reenumerate_before_each_capture(
    last_sample_drift,
):
    access = object.__new__(WindowsUIAQQAccessibility)
    access._active_phase = None
    handles = []
    references = []
    node_calls = []
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    profile = QQ_VM_ROW_PALETTE_PROFILE.model_copy(update={
        "hover_settle_seconds": 0,
        "poll_interval_seconds": 0,
    })

    def assert_controls_released():
        assert access._active_phase is None
        gc.collect()
        assert all(reference() is None for reference in references)

    def fresh_root(handle):
        assert_controls_released()
        handles.append(handle)
        rows = []
        for index in range(3):
            identity = f"row-{index}"
            if last_sample_drift and len(handles) == 3 and index == 1:
                identity = "replacement-target"
            row = Node(
                class_name="recent-contact-item",
                automation_id=identity,
                patterns=("invoke",),
            )
            row.IsOffscreen = False
            row.rect = ScreenRect(
                left=50, top=100 * (index + 1),
                right=300, bottom=100 * (index + 1) + 64,
            )
            rows.append(row)
        pane = Node(control="Pane", children=rows)
        root = Node(control="Window", children=(pane,))
        nodes = (root, pane, *rows)
        references.extend(weakref.ref(node) for node in nodes)
        node_calls.extend(node.calls for node in nodes)
        return root

    access._auto = type("Auto", (), {
        "ControlFromHandle": lambda _self, handle: fresh_root(handle),
    })()
    access._row_screen_rect = lambda item: item.rect
    access.ensure_guest_foreground = lambda _window, **_kwargs: assert_controls_released()
    access._window_screen_bounds = lambda _window: (0, 0, 1600, 1000)
    access._point_belongs_to_window = lambda _window, _x, _y: True
    access._neutral_hover_point = lambda _window, _rows, _bounds: (1500, 900)
    moves = []
    samples = []

    def move(*point):
        assert_controls_released()
        moves.append(point)

    def sample(_window, ref, _profile):
        assert_controls_released()
        samples.append(ref.internal_id)
        state = profile.selected if ref.internal_id == "row-1" else profile.unselected
        return RowBorderSample(
            pixel_count=1744,
            dominant_rgb=state.dominant_rgb,
            dominant_count=1744,
            ratio=1,
            unique_count=1,
        )

    access._send_guest_mouse_move = move
    access._sample_row_border = sample
    conversation = QQConversation(
        internal_id="row-1", participant_signature="fixture", tree_digest="a" * 64,
    )
    if last_sample_drift:
        with pytest.raises(UIAUnavailable, match="row set changed"):
            access.certify_conversation_selected_visual(
                window, conversation, _selector(), profile,
            )
        assert len(samples) == 3  # No pixels from the changed second sample.
    else:
        proof = access.certify_conversation_selected_visual(
            window, conversation, _selector(), profile,
        )
        assert proof.stable_sample_count == 2
        assert len(samples) == 6
    assert handles == [window.window_handle] * 3  # Baseline and two fresh samples.
    assert len(moves) == 2
    assert all(calls["GetChildren"] == 1 for calls in node_calls)
    assert_controls_released()
