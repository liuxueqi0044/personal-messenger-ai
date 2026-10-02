"""Synthetic ScrollPattern proof/action tests: no Windows calls or GUI input."""
from types import SimpleNamespace

import pytest

from messenger_ai.adapters.qq.models import QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import UIAUnavailable, WindowsUIAQQAccessibility

WINDOW = QQWindow(process_id=101, window_handle=1001, class_name="QQNT")
SELECTOR = QQSelector(name="bubbles", control_type="Group", class_name_tokens=("ml-root",))


def fixture(*, percent=100.0, scrollable=True, view_size=0.35933333333333334):
    calls = []
    pattern = SimpleNamespace(
        VerticallyScrollable=scrollable, VerticalScrollPercent=percent,
        VerticalViewSize=view_size, SetScrollPercent=lambda x, y: calls.append((x, y)),
    )
    region = SimpleNamespace(ClassName="q-scroll-view ml-container ml-root container",
                             ProcessId=101, IsOffscreen=False)
    access = WindowsUIAQQAccessibility.__new__(WindowsUIAQQAccessibility)
    access._active_phase = None
    access._window = lambda window: object()
    access._select = lambda root, selector: [region]
    access._pattern = lambda node, getter, pattern_id: pattern
    access._guest_scope = lambda window: True
    return access, pattern, region, calls


@pytest.mark.parametrize("percent,scrollable,view_size,latest", [
    (100.0, True, 0.35933333333333334, True),
    (20.0, True, 0.35933333333333334, False),
    (99.999, True, 1.0, False),
    (-1.0, False, 100.0, True),
])
def test_tail_readiness_requires_valid_latest_scroll_evidence(percent, scrollable, view_size, latest):
    access, _, _, calls = fixture(percent=percent, scrollable=scrollable, view_size=view_size)
    assert access.message_tail_is_latest(WINDOW, SELECTOR) is latest
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"VerticalScrollPercent": float("nan")}, {"VerticalScrollPercent": float("inf")},
    {"VerticalScrollPercent": -1.0}, {"VerticalScrollPercent": 101.0},
    {"VerticalScrollPercent": True}, {"VerticalScrollPercent": "100"},
    {"VerticalViewSize": 0}, {"VerticalViewSize": 101}, {"VerticalViewSize": None},
    {"VerticallyScrollable": 1}, {"VerticallyScrollable": None},
    {"VerticallyScrollable": False, "VerticalScrollPercent": 100.0},
    {"VerticallyScrollable": False, "VerticalScrollPercent": -1.0, "VerticalViewSize": 1},
])
def test_unknown_or_contradictory_scroll_state_never_moves_or_reports_latest(changes):
    access, pattern, _, calls = fixture()
    pattern.__dict__.update(changes)
    with pytest.raises(UIAUnavailable, match="message_tail_unproven"):
        access.message_tail_is_latest(WINDOW, SELECTOR)
    with pytest.raises(UIAUnavailable, match="message_tail_unproven"):
        access.scroll_message_tail_to_latest(WINDOW, SELECTOR, before_action=lambda: None)
    assert calls == []


@pytest.mark.parametrize("mode", ["missing", "ambiguous", "wrong_pid", "offscreen", "wrong_region", "no_pattern"])
def test_only_unique_in_scope_message_region_is_accepted(mode):
    access, _, region, calls = fixture()
    if mode == "missing": access._select = lambda *_: []
    if mode == "ambiguous": access._select = lambda *_: [region, region]
    if mode == "wrong_pid": region.ProcessId = 999
    if mode == "offscreen": region.IsOffscreen = True
    if mode == "wrong_region": region.ClassName = "unrelated"
    if mode == "no_pattern": access._pattern = lambda *_: None
    with pytest.raises(UIAUnavailable, match="message_tail_"):
        access.scroll_message_tail_to_latest(WINDOW, SELECTOR, before_action=lambda: None)
    assert calls == []


def test_semantic_scroll_calls_once_after_final_live_and_foreground_guards():
    access, _, _, calls = fixture(percent=20)
    access.scroll_message_tail_to_latest(
        WINDOW, SELECTOR, before_action=lambda: calls.append("live")
    )
    assert calls == ["live", (-1.0, 100.0)]


def test_already_latest_is_a_noop():
    access, _, _, calls = fixture()
    access.scroll_message_tail_to_latest(WINDOW, SELECTOR, before_action=lambda: calls.append("live"))
    assert calls == []


@pytest.mark.parametrize("mode", ["read_phase", "foreground", "deadline", "foreground_lost", "missing_action"])
def test_scroll_rejects_unsafe_action_context(mode):
    access, pattern, _, calls = fixture(percent=20)
    guard = lambda: None
    if mode == "read_phase": access._active_phase = object()
    if mode == "foreground": access._guest_scope = lambda _: False
    if mode == "deadline":
        def guard(): raise RuntimeError("deadline_expired")
    if mode == "foreground_lost":
        states = iter([True, False])
        access._guest_scope = lambda _: next(states)
    if mode == "missing_action": pattern.SetScrollPercent = None
    with pytest.raises((UIAUnavailable, RuntimeError)):
        access.scroll_message_tail_to_latest(WINDOW, SELECTOR, before_action=guard)
    assert calls == []
