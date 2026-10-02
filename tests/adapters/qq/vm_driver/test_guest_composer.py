from __future__ import annotations

import sys

import pytest

from messenger_ai.adapters.qq.vm_driver import guest_composer
from messenger_ai.adapters.qq.vm_driver.guest_composer import (
    GuestComposerError, clear_with_local_selection, read_composer_text,
    write_with_text_pattern,
    write_with_value_pattern,
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


class ValueControl(Control):
    def __init__(self, text=""):
        super().__init__(text)
        self.value_calls = []
        self.IsReadOnly = False

    @property
    def Value(self):
        return self.text

    def GetValuePattern(self):
        return self

    def SetValue(self, value):
        self.value_calls.append(value)
        self.text = value


@pytest.mark.parametrize("kind", ["text", "value"])
def test_optional_input_diagnostic_marks_only_actual_input_boundary(kind):
    control = ValueControl() if kind == "value" else Control()
    events = []
    def mark():
        events.append(("attempt", control.text))
    if kind == "value":
        original = control.SetValue
        def set_value(text):
            assert events == [("attempt", "")]
            original(text)
        control.SetValue = set_value
        write_with_value_pattern(control, "reply", scope_guard=lambda: True,
            focus_guard=lambda c: c.focused, before_input=mark)
    else:
        def send(text):
            assert events[-1] == ("attempt", control.text)
            control.text += text
        write_with_text_pattern(control, "a" * 50, scope_guard=lambda: True,
            focus_guard=lambda c: c.focused, sender=send, before_input=mark)
        assert events == [("attempt", ""), ("attempt", "a" * 32)]
    assert control.text == ("reply" if kind == "value" else "a" * 50)


@pytest.mark.parametrize("kind", ["text", "value"])
def test_rejected_input_never_emits_attempt_and_bad_diagnostic_never_changes_writing(kind):
    control = ValueControl() if kind == "value" else Control()
    events = []
    writer = write_with_value_pattern if kind == "value" else write_with_text_pattern
    options = {} if kind == "value" else {"sender": lambda text: setattr(control, "text", text)}
    with pytest.raises(GuestComposerError, match="scope_rejected"):
        writer(control, "reply", scope_guard=lambda: False, focus_guard=lambda c: c.focused,
               before_input=lambda: events.append(True), **options)
    assert events == [] and control.text == ""
    def broken_diagnostic():
        raise RuntimeError("private diagnostic details")
    writer(control, "reply", scope_guard=lambda: True, focus_guard=lambda c: c.focused,
           before_input=broken_diagnostic, **options)
    assert control.text == "reply"


def test_value_pattern_writes_only_empty_focused_editor_and_reads_back():
    control = ValueControl()
    write_with_value_pattern(control, "你好\n🙂", scope_guard=lambda: True,
                             focus_guard=lambda item: item.focused)
    assert control.focused
    assert control.value_calls == ["你好\n🙂"]
    assert read_composer_text(control) == "你好\n🙂"


@pytest.mark.parametrize("mode", ["scope", "draft", "readonly", "missing"])
def test_value_pattern_rejects_invalid_preconditions_without_writing(mode):
    control = ValueControl("user draft" if mode == "draft" else "")
    control.IsReadOnly = mode == "readonly"
    if mode == "missing":
        control.GetValuePattern = lambda: None
    with pytest.raises(GuestComposerError):
        write_with_value_pattern(control, "bot reply", scope_guard=lambda: mode != "scope",
                                 focus_guard=lambda item: item.focused)
    assert control.value_calls == []
    assert not control.focused


def test_value_pattern_preserves_draft_that_arrives_during_focus():
    control = ValueControl()
    def focus():
        control.focused = True
        control.text = "new user draft"
    control.SetFocus = focus
    with pytest.raises(GuestComposerError, match="composer_not_empty"):
        write_with_value_pattern(control, "bot reply", scope_guard=lambda: True,
                                 focus_guard=lambda item: item.focused)
    assert control.value_calls == [] and control.text == "new user draft"


@pytest.mark.parametrize("drift", ["scope", "focus"])
def test_value_pattern_rechecks_scope_and_focus_after_final_empty_read(drift):
    live = {"scope": True, "focus": True}
    class DriftOnRead(ValueControl):
        reads = 0
        @property
        def Value(self):
            self.reads += 1
            if self.reads == 2:
                live[drift] = False
            return self.text
    control = DriftOnRead()
    with pytest.raises(GuestComposerError, match="composer_focus_or_scope_drift"):
        write_with_value_pattern(control, "bot reply", scope_guard=lambda: live["scope"],
                                 focus_guard=lambda _item: live["focus"])
    assert control.value_calls == []


def test_value_pattern_refuses_mismatched_readback(monkeypatch):
    control = ValueControl()
    control.SetValue = lambda value: control.value_calls.append(value)
    _virtual_focus_clock(monkeypatch)
    with pytest.raises(GuestComposerError, match="composer_readback_mismatch"):
        write_with_value_pattern(control, "bot reply", scope_guard=lambda: True,
                                 focus_guard=lambda item: item.focused)
    assert control.value_calls == ["bot reply"]


def test_transport_value_pattern_path_uses_guarded_write():
    from messenger_ai.adapters.qq.models import QQWindow
    from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility
    control = ValueControl("existing draft")
    access = object.__new__(WindowsUIAQQAccessibility)
    access._window = lambda _window: object()
    access._select = lambda _root, _selector: [control]
    access._guest_scope = lambda _window: True
    access._composer_focused = lambda target, _window: target.focused
    with pytest.raises(GuestComposerError, match="composer_not_empty"):
        access.write_composer(QQWindow(process_id=7, window_handle=9, class_name="QQ"),
                              "bot reply", object())
    assert control.value_calls == [] and control.text == "existing draft"


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


@pytest.mark.skipif(sys.platform != "win32", reason="Win64 SendInput ABI requires Windows")
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


def _virtual_focus_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(guest_composer.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guest_composer.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_delayed_uia_focus_is_confirmed_before_first_input(monkeypatch):
    now = _virtual_focus_clock(monkeypatch)
    control = Control()
    sent = []
    def sender(value):
        assert now[0] >= 0.1
        sent.append(value)
        control.text += value
    write_with_text_pattern(control, "hello", scope_guard=lambda: True,
                            focus_guard=lambda _control: now[0] >= 0.1, sender=sender)
    assert sent == ["hello"] and control.text == "hello"


def test_focus_never_arriving_times_out_without_keyboard_input(monkeypatch):
    now = _virtual_focus_clock(monkeypatch)
    sent = []
    control = Control()
    with pytest.raises(GuestComposerError, match="composer_focus_drift"):
        write_with_text_pattern(control, "hello", scope_guard=lambda: True,
                                focus_guard=lambda _control: False, sender=sent.append)
    assert 2.0 <= now[0] < 2.1
    assert sent == [] and control.text == ""


def test_focus_wait_stops_immediately_when_qq_scope_is_lost(monkeypatch):
    now = _virtual_focus_clock(monkeypatch)
    sent = []
    with pytest.raises(GuestComposerError, match="composer_focus_drift"):
        write_with_text_pattern(Control(), "hello", scope_guard=lambda: now[0] < 0.1,
                                focus_guard=lambda _control: False, sender=sent.append)
    assert now[0] == pytest.approx(0.1) and sent == []


@pytest.mark.parametrize("clearing", [False, True])
def test_draft_changed_during_focus_settle_is_preserved(monkeypatch, clearing):
    now = _virtual_focus_clock(monkeypatch)
    control = Control("original" if clearing else "")
    inputs = []
    def focused(_control):
        if now[0] < 0.1:
            return False
        control.text = "user draft"
        return True
    with pytest.raises(GuestComposerError, match="composer_clear_precondition_failed" if clearing else "composer_not_empty"):
        if clearing:
            clear_with_local_selection(control, expected_text="original", scope_guard=lambda: True,
                                       focus_guard=focused, clear_action=lambda: inputs.append("delete"))
        else:
            write_with_text_pattern(control, "hello", scope_guard=lambda: True,
                                    focus_guard=focused, sender=inputs.append)
    assert inputs == [] and control.text == "user draft"


def test_delayed_focus_also_precedes_clear(monkeypatch):
    now = _virtual_focus_clock(monkeypatch)
    control = Control("owned draft")
    def clear():
        assert now[0] >= 0.1
        control.text = ""
    clear_with_local_selection(control, expected_text="owned draft", scope_guard=lambda: True,
                               focus_guard=lambda _control: now[0] >= 0.1, clear_action=clear)
    assert control.text == ""


@pytest.mark.parametrize("clearing", [False, True])
def test_preserves_certified_existing_focus_without_redundant_setfocus(clearing):
    control = Control("owned draft" if clearing else "")
    control.focused = True

    def erroneous_electron_setfocus():
        raise AssertionError("SetFocus would activate a different renderer HWND")

    control.SetFocus = erroneous_electron_setfocus
    if clearing:
        clear_with_local_selection(control, expected_text="owned draft", scope_guard=lambda: True,
                                   focus_guard=lambda item: item.focused,
                                   clear_action=lambda: setattr(control, "text", ""))
        assert control.text == ""
    else:
        write_with_text_pattern(control, "hello", scope_guard=lambda: True,
                                focus_guard=lambda item: item.focused,
                                sender=lambda value: setattr(control, "text", value))
        assert control.text == "hello"


@pytest.mark.parametrize("drift", ["scope", "focus"])
def test_clear_rechecks_authority_after_final_readback(monkeypatch, drift):
    control = Control("owned draft")
    control.focused = True
    reads = []
    actions = []

    def read(_control):
        reads.append(1)
        return "owned draft"

    monkeypatch.setattr(guest_composer, "read_composer_text", read)
    with pytest.raises(GuestComposerError, match="composer_focus_or_scope_drift"):
        clear_with_local_selection(
            control, expected_text="owned draft", clear_action=lambda: actions.append("delete"),
            scope_guard=lambda: drift != "scope" or len(reads) < 2,
            focus_guard=lambda _: drift != "focus" or len(reads) < 2,
        )
    assert actions == [] and control.text == "owned draft"


@pytest.mark.parametrize("optimized", [False, True])
def test_v2_opt_in_composes_last_postproof_but_keeps_32_character_native_boundaries(optimized):
    control, trace, chunks = Control(), [], []
    control.focused = True
    body = "x" * 50
    def scope():
        trace.append("full-scope")
        return True
    def focus(_):
        trace.append("focus")
        return True
    def cheap():
        trace.append("cheap")
        return True
    def sender(chunk):
        assert trace[-2:] == ["full-scope", "focus"]
        trace.append("input")
        chunks.append(chunk)
        control.text += chunk
    options = {"focused_scope_guard": cheap, "defer_last_batch_postcheck": True} if optimized else {}
    write_with_text_pattern(control, body, scope_guard=scope, focus_guard=focus, sender=sender, **options)
    assert chunks == [body[:32], body[32:]] and control.text == body
    assert trace.count("full-scope") == (5 if optimized else 7)
    assert trace.count("cheap") == (2 if optimized else 0)
    for index, event in enumerate(trace):
        if event == "input":
            assert trace[index + 1:index + 3] == ["full-scope", "focus"]


def test_v2_deferred_last_postproof_stops_at_wait_entry_before_readback(monkeypatch):
    control, sent, reads = Control(), [], []
    control.focused = True
    original = guest_composer.read_composer_text
    def read(item):
        reads.append(1)
        return original(item)
    monkeypatch.setattr(guest_composer, "read_composer_text", read)
    def sender(chunk):
        sent.append(chunk)
        control.text += chunk
    with pytest.raises(GuestComposerError, match="composer_focus_or_scope_drift"):
        write_with_text_pattern(control, "x" * 50, scope_guard=lambda: len(sent) < 2,
            focus_guard=lambda _: True, focused_scope_guard=lambda: True,
            defer_last_batch_postcheck=True, sender=sender)
    assert [len(chunk) for chunk in sent] == [32, 18] and control.text == "x" * 50
    assert len(reads) == 2  # Only the two entry reads; cancelled terminal proof never reads body.


@pytest.mark.parametrize("clearing", [False, True])
def test_v2_existing_focus_fast_path_rechecks_cheap_scope_after_focus_read(clearing):
    control, scopes, inputs, cancelled = Control("owned" if clearing else ""), [], [], [False]
    control.focused = True
    def scope():
        scopes.append(1)
        return True
    def focus(_):
        cancelled[0] = True
        return True
    with pytest.raises(GuestComposerError, match="composer_focus_drift"):
        if clearing:
            clear_with_local_selection(control, expected_text="owned", clear_action=lambda: inputs.append("clear"),
                scope_guard=scope, focus_guard=focus, focused_scope_guard=lambda: not cancelled[0])
        else:
            write_with_text_pattern(control, "reply", scope_guard=scope, focus_guard=focus,
                focused_scope_guard=lambda: not cancelled[0], sender=inputs.append)
    assert scopes == [1] and inputs == [] and control.text == ("owned" if clearing else "")


@pytest.mark.parametrize("clearing", [False, True])
def test_v2_unfocused_editor_keeps_full_setfocus_and_settle_checks(monkeypatch, clearing):
    now = _virtual_focus_clock(monkeypatch)
    control, trace, inputs = Control("owned" if clearing else ""), [], []
    def scope():
        trace.append("full")
        return True
    def focus(_):
        trace.append("focus")
        return now[0] >= .1
    def set_focus():
        assert trace[-2:] == ["full", "focus"]
        trace.append("set-focus")
    control.SetFocus = set_focus
    def input_action(value=""):
        assert now[0] >= .1 and trace[-2:] == ["full", "focus"]
        inputs.append(value)
        control.text = value
    if clearing:
        clear_with_local_selection(control, expected_text="owned", clear_action=input_action,
            scope_guard=scope, focus_guard=focus, focused_scope_guard=lambda: True)
    else:
        write_with_text_pattern(control, "reply", scope_guard=scope, focus_guard=focus,
            focused_scope_guard=lambda: True, defer_last_batch_postcheck=True, sender=input_action)
    assert trace.count("set-focus") == 1 and trace.count("full") >= 6
    assert inputs == ([""] if clearing else ["reply"])


def test_v2_wait_still_polls_exact_body_until_unchanged_two_second_limit(monkeypatch):
    now = _virtual_focus_clock(monkeypatch)
    control, sent, guards = Control(), [], []
    control.focused = True
    def scope():
        guards.append(now[0])
        return True
    def partial(chunk):
        sent.append(chunk)
        control.text = chunk[:-1]
    with pytest.raises(GuestComposerError, match="composer_readback_mismatch"):
        write_with_text_pattern(control, "reply", scope_guard=scope, focus_guard=lambda _: True,
            focused_scope_guard=lambda: True, defer_last_batch_postcheck=True, sender=partial)
    assert 2 <= now[0] < 2.1 and len(guards) > 3
    assert sent == ["reply"] and control.text == "repl"
