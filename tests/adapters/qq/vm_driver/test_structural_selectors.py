from __future__ import annotations

import pytest

from messenger_ai.adapters.qq import QQConversation, QQIdentityBinding, QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.transport import UIAUnavailable, WindowsUIAQQAccessibility


class Pattern:
    def __init__(self, node=None):
        self.node = node

    def Invoke(self):
        self.node.ClassName += " selected"


class LegacyPattern:
    def __init__(self, node, *, default_action="按", error=None, callable_action=True):
        self.node = node
        self.DefaultAction = default_action
        self.calls = 0
        self.error = error
        if not callable_action:
            self.DoDefaultAction = None

    def DoDefaultAction(self):
        self.calls += 1
        self.node.ClassName += " selected"
        if self.error is not None:
            raise self.error


class Node:
    def __init__(self, *, control="Group", class_name="recent-contact-item", name="Alice",
                 automation_id="", runtime_id=(1, 2), patterns=(), children=(),
                 legacy_default_action="按", legacy_error=None,
                 legacy_callable=True):
        self.ControlTypeName = control
        self.ClassName = class_name
        self.Name = name
        self.AutomationId = automation_id
        self.runtime_id = runtime_id
        self.ProcessId = 7
        self._patterns = set(patterns)
        self._children = list(children)
        self.legacy_pattern = (
            LegacyPattern(
                self,
                default_action=legacy_default_action,
                error=legacy_error,
                callable_action=legacy_callable,
            )
            if "legacy" in self._patterns else None
        )

    def GetChildren(self): return list(self._children)
    def GetRuntimeId(self): return self.runtime_id
    def GetInvokePattern(self): return Pattern(self) if "invoke" in self._patterns else None
    def GetSelectionItemPattern(self): return None
    def GetSelectionPattern(self): return None
    def GetValuePattern(self): return None
    def GetTextPattern(self): return None
    def GetScrollPattern(self): return None
    def GetScrollItemPattern(self): return None
    def GetLegacyIAccessiblePattern(self): return self.legacy_pattern


def transport(root):
    value = object.__new__(WindowsUIAQQAccessibility)
    value._window = lambda _window: root
    value.tree_digest = lambda _window: "digest"
    return value


def selector(**updates):
    values = dict(name="conversation_item", control_type="Group",
                  class_name_tokens=("recent-contact-item",),
                  required_patterns=("InvokePattern",),
                  ancestor_control_types=("pane",),
                  selected_class_name_token="selected")
    values.update(updates)
    return QQSelector(**values)


def test_structural_selector_accepts_empty_automation_id_and_class_token_order():
    row = Node(class_name="unread recent-contact-item", patterns=("invoke",))
    pane = Node(control="Pane", class_name="recent-contact-list", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    found = access.list_conversations(QQWindow(process_id=7, window_handle=9, class_name="QQ"), selector())
    assert len(found) == 1
    assert found[0].internal_id.startswith("runtime:")
    assert found[0].participant_signature.startswith("uncertified:")
    binding = QQIdentityBinding(hub_conversation_id="hub", contact_id="contact",
                                account_id="account", platform_conversation_id=found[0].internal_id,
                                participant_signature=found[0].participant_signature, binding_id="binding")
    assert binding.matches(found[0]) is False


def test_real_uia_control_type_suffixes_are_normalized():
    row = Node(control="GroupControl", patterns=("invoke",))
    pane = Node(control="PaneControl", children=(row,))
    root = Node(control="WindowControl", children=(pane,))
    assert len(transport(root)._select(root, selector())) == 1


def test_group_control_generic_get_pattern_matches_real_library_shape():
    class GenericGroup(Node):
        GetInvokePattern = None
        def GetPattern(self, pattern_id):
            return Pattern(self) if pattern_id == 10000 else None
    row = GenericGroup(control="GroupControl")
    pane = Node(control="PaneControl", children=(row,))
    root = Node(control="WindowControl", children=(pane,))
    assert len(transport(root)._select(root, selector())) == 1


@pytest.mark.parametrize("change", ["pattern", "ancestor", "token"])
def test_structural_selector_rejects_missing_evidence(change):
    row = Node(patterns=(() if change == "pattern" else ("invoke",)),
               class_name=("other" if change == "token" else "recent-contact-item"))
    pane = Node(control=("Window" if change == "ancestor" else "Pane"), children=(row,))
    root = Node(control="Window", children=(pane,))
    assert transport(root)._select(root, selector()) == []


def test_duplicate_structural_rows_are_not_silently_selected():
    rows = (Node(runtime_id=(1,), patterns=("invoke",)), Node(runtime_id=(2,), patterns=("invoke",)))
    pane = Node(control="Pane", children=rows)
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(rows[0]), participant_signature="certified", tree_digest="digest")
    # A duplicated runtime locator is an explicit ambiguity, never first-match.
    rows[1].runtime_id = rows[0].runtime_id
    with pytest.raises(UIAUnavailable, match="absent or ambiguous"):
        access.select_conversation(window, candidate, selector())


def test_selection_rejects_a_non_conversation_action_selector():
    row = Node(patterns=("invoke", "legacy"))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    with pytest.raises(UIAUnavailable, match="selector is not certified"):
        access.select_conversation(
            window, candidate, selector(name="conversations")
        )

    assert row.legacy_pattern.calls == 0


def test_legacy_default_action_normal_return_reports_selection_attempted():
    row = Node(patterns=("invoke", "legacy"))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")
    changed = access.select_conversation(window, candidate, selector())
    assert changed is True
    assert row.legacy_pattern.calls == 1
    assert "selected" in row.ClassName.split()


def test_selection_action_does_not_run_an_inline_confirmation():
    row = Node(patterns=("invoke", "legacy"))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    access.confirm_conversation_selected = lambda *_args: (_ for _ in ()).throw(
        AssertionError("confirmation must use a caller-owned fresh phase")
    )
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(
        internal_id=access._conversation_id(row),
        participant_signature="certified",
        tree_digest="digest",
    )

    changed = access.select_conversation(window, candidate, selector())

    assert changed is True
    assert "selected" in row.ClassName.split()


def test_invoke_pattern_is_only_structural_and_legacy_is_the_selection_action():
    actions = []

    class DualPatternRow(Node):
        def GetSelectionItemPattern(self):
            class SelectionPattern:
                IsSelected = False

                def Select(self):
                    actions.append("select")

            return SelectionPattern()

        def GetInvokePattern(self):
            class InvokePattern:
                def Invoke(self):
                    actions.append("invoke")

            return InvokePattern()

        def GetLegacyIAccessiblePattern(self):
            class DefaultActionPattern:
                DefaultAction = "按"

                def DoDefaultAction(self):
                    actions.append("legacy")
                    row.ClassName += " selected"

            return DefaultActionPattern()

    row = DualPatternRow(patterns=("invoke",))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    changed = access.select_conversation(
        window, candidate, selector(required_patterns=("InvokePattern",))
    )

    assert changed is True
    assert actions == ["legacy"]
    assert "selected" in row.ClassName.split()


def test_missing_legacy_pattern_fails_before_any_selection_action():
    row = Node(patterns=("invoke",))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    with pytest.raises(UIAUnavailable, match="no certified legacy"):
        access.select_conversation(window, candidate, selector())


def test_wrong_legacy_default_action_fails_before_invocation():
    row = Node(
        patterns=("invoke", "legacy"), legacy_default_action="打开",
    )
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    with pytest.raises(UIAUnavailable, match="default action is not certified"):
        access.select_conversation(window, candidate, selector())

    assert row.legacy_pattern.calls == 0


def test_uncallable_legacy_default_action_fails_before_invocation():
    row = Node(patterns=("invoke", "legacy"), legacy_callable=False)
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    with pytest.raises(UIAUnavailable, match="no callable legacy"):
        access.select_conversation(window, candidate, selector())

    assert row.legacy_pattern.calls == 0


def test_legacy_provider_exception_after_action_started_reports_attempted():
    row = Node(
        patterns=("invoke", "legacy"), legacy_error=RuntimeError("provider stale"),
    )
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    changed = access.select_conversation(window, candidate, selector())

    assert changed is True
    assert row.legacy_pattern.calls == 1
    assert "selected" in row.ClassName.split()


def test_legacy_selection_does_not_swallow_base_exception():
    class FatalSelectionSignal(BaseException):
        pass

    row = Node(
        patterns=("invoke", "legacy"), legacy_error=FatalSelectionSignal(),
    )
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    with pytest.raises(FatalSelectionSignal):
        access.select_conversation(window, candidate, selector())

    assert row.legacy_pattern.calls == 1


def test_preselected_row_does_not_require_or_call_legacy_action():
    actions = []

    class Preselected(Node):
        def GetInvokePattern(self):
            class ExplodingPattern:
                def Invoke(self):
                    actions.append("invoke")
                    raise AssertionError("preselected row must not be invoked")
            return ExplodingPattern()

    row = Preselected(class_name="recent-contact-item selected", patterns=("invoke",))
    pane = Node(control="Pane", children=(row,))
    root = Node(control="Window", children=(pane,))
    access = transport(root)
    window = QQWindow(process_id=7, window_handle=9, class_name="QQ")
    candidate = QQConversation(internal_id=access._conversation_id(row), participant_signature="certified", tree_digest="digest")

    changed = access.select_conversation(window, candidate, selector())

    assert changed is False
    assert actions == []


def test_focus_rejects_same_automation_id_with_different_runtime_id():
    target = Node(automation_id="composer", runtime_id=(1,))
    focused = Node(automation_id="composer", runtime_id=(2,))
    target.ProcessId = focused.ProcessId = 7
    focused.GetParentControl = lambda: None
    access = object.__new__(WindowsUIAQQAccessibility)
    access._auto = type("Auto", (), {"GetFocusedControl": lambda self: focused})()
    assert access._composer_focused(
        target, QQWindow(process_id=7, window_handle=9, class_name="QQ")
    ) is False
