from __future__ import annotations

from collections import Counter

import pytest

from messenger_ai.adapters.qq import QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility


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
