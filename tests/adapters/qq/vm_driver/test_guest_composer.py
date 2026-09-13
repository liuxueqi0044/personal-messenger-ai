from __future__ import annotations

import pytest

from messenger_ai.adapters.qq.vm_driver import guest_composer
from messenger_ai.adapters.qq.vm_driver.guest_composer import (
    GuestComposerError, clear_with_local_selection, read_composer_text,
    write_with_text_pattern,
)


class Range:
    def __init__(self, owner): self.owner = owner
    def GetText(self, length):
        assert length == -1
        return self.owner.text + self.owner.terminator


class TextPattern:
    def __init__(self, owner): self.DocumentRange = Range(owner)


class Control:
    def __init__(self, text="", terminator=""):
        self.text, self.terminator, self.focused = text, terminator, False
        self.ClassName = "ProseMirror ExEditor-qq-msg-editor" + (" is-empty" if text == "" else "")
    def GetValuePattern(self): return None
    def GetTextPattern(self): return TextPattern(self)
    def SetFocus(self): self.focused = True


def test_nonempty_text_is_preserved_exactly_including_newlines():
    assert read_composer_text(Control("  中文🙂\n保留空格  ")) == "  中文🙂\n保留空格  "
    assert read_composer_text(Control("末尾换行\n")) == "末尾换行\n"


def test_only_observed_empty_placeholder_with_empty_token_maps_to_empty():
    control = Control()
    control.text = guest_composer.EMPTY_PLACEHOLDER
    control.terminator = "\n"
    assert read_composer_text(control) == ""
    control.ClassName = "ProseMirror ExEditor-qq-msg-editor"
    assert read_composer_text(control) == guest_composer.EMPTY_PLACEHOLDER + "\n"


def test_only_observed_single_lf_with_empty_token_maps_to_empty():
    control = Control()
    control.terminator = "\n"
    assert read_composer_text(control) == ""


@pytest.mark.parametrize("class_name, result", [
    ("ProseMirror ExEditor-qq-msg-editor", "\n"),
    ("ProseMirror ExEditor-qq-msg-editor is-emptyish", "\n"),
    ("ProseMirror ExEditor-qq-msg-editor is-empty", "\n\n"),
    ("ProseMirror ExEditor-qq-msg-editor is-empty", " \n"),
    ("ProseMirror ExEditor-qq-msg-editor is-empty", "draft\n"),
])
def test_only_exact_single_lf_empty_shape_is_normalized(class_name, result):
    control = Control()
    control.text = result
    control.ClassName = class_name
    assert read_composer_text(control) == result


def test_real_group_control_shape_uses_generic_get_pattern() -> None:
    class GroupControlShape:
        text = "真实形状"
        terminator = ""
        ClassName = "ProseMirror ExEditor-qq-msg-editor"
        def GetPattern(self, pattern_id):
            return TextPattern(self) if pattern_id == 10014 else None
    assert read_composer_text(GroupControlShape()) == "真实形状"


def test_unicode_batches_round_trip_without_enter_action():
    control = Control()
    sent = []
    def sender(value): sent.append(value); control.text += value
    control.ClassName = "ProseMirror ExEditor-qq-msg-editor"
    text = "你好🙂\n第二行"
    write_with_text_pattern(control, text, scope_guard=lambda: True,
                            focus_guard=lambda item: item.focused, sender=sender,
                            batch_units=3)
    assert control.text == text
    assert sent and "\n" in "".join(sent)


def test_focus_drift_stops_before_next_batch():
    control = Control(); checks = iter([True, True, True, False])
    control.ClassName = "ProseMirror ExEditor-qq-msg-editor"
    with pytest.raises(GuestComposerError, match="drift"):
        write_with_text_pattern(control, "abcdef", scope_guard=lambda: True,
            focus_guard=lambda _item: next(checks), sender=lambda value: setattr(control, "text", control.text + value), batch_units=3)
    assert control.text == "abc"


def test_scope_rejection_does_not_focus_or_type():
    control = Control(); calls = []
    with pytest.raises(GuestComposerError, match="scope"):
        write_with_text_pattern(control, "不可写", scope_guard=lambda: False,
            focus_guard=lambda _item: True, sender=calls.append)
    assert not control.focused and calls == []


def test_clear_requires_exact_expected_text_and_verified_local_focus():
    control = Control("draft")
    with pytest.raises(GuestComposerError, match="precondition"):
        clear_with_local_selection(control, clear_action=lambda: None,
            expected_text="other", scope_guard=lambda: True, focus_guard=lambda _: True)
    clear_with_local_selection(control, clear_action=lambda: setattr(control, "text", ""),
        expected_text="draft", scope_guard=lambda: True, focus_guard=lambda item: item.focused)
    assert control.text == ""


def test_win64_input_abi_includes_full_union():
    import ctypes
    assert ctypes.sizeof(guest_composer._INPUT) == 40


def test_partial_clear_always_attempts_modifier_release(monkeypatch):
    calls = []
    def fail_then_release(events):
        calls.append([(item.u.ki.wVk, item.u.ki.dwFlags) for item in events])
        if len(calls) == 1:
            raise GuestComposerError("partial")
    monkeypatch.setattr(guest_composer, "_send_inputs", fail_then_release)
    with pytest.raises(GuestComposerError, match="partial"):
        guest_composer._select_all_delete()
    assert calls[-1] == [(0x41, 0x0002), (0x11, 0x0002)]


def test_bounded_readback_poll_accepts_delayed_value(monkeypatch):
    control = Control()
    values = iter(["", "", "done"])
    monkeypatch.setattr(guest_composer, "read_composer_text", lambda _control: next(values))
    monkeypatch.setattr(guest_composer.time, "sleep", lambda _seconds: None)
    assert guest_composer._wait_for_exact_text(
        control, "done", scope_guard=lambda: True,
        focus_guard=lambda _: True, timeout_seconds=2.0,
    ) is True


def test_readback_poll_rechecks_scope_before_every_read(monkeypatch):
    control = Control()
    reads = []
    monkeypatch.setattr(guest_composer, "read_composer_text", lambda _control: reads.append(1) or "")
    scopes = iter([True, False])
    monkeypatch.setattr(guest_composer.time, "sleep", lambda _seconds: None)
    with pytest.raises(GuestComposerError, match="drift"):
        guest_composer._wait_for_exact_text(
            control, "done", scope_guard=lambda: next(scopes),
            focus_guard=lambda _: True, timeout_seconds=2.0,
        )
    assert len(reads) == 1
