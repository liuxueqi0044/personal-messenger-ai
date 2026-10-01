"""Certified-guest composer text I/O. This module never invokes Send."""
from __future__ import annotations

import ctypes
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any


class GuestComposerError(RuntimeError):
    pass


EMPTY_PLACEHOLDER = "按住 Win + Alt，使用语音输入文字"


def get_uia_pattern(control: Any, method_name: str, pattern_id: int) -> Any:
    """Use a typed convenience method when present, else Control.GetPattern."""
    typed = getattr(control, method_name, None)
    if callable(typed):
        return typed()
    generic = getattr(control, "GetPattern", None)
    if not callable(generic):
        return None
    return generic(pattern_id)


def read_composer_text(control: Any) -> str:
    value = get_uia_pattern(control, "GetValuePattern", 10002)
    if value is not None:
        return str(value.Value)
    text = get_uia_pattern(control, "GetTextPattern", 10014)
    if text is None:
        raise GuestComposerError("composer_has_no_readable_pattern")
    result = str(text.DocumentRange.GetText(-1))
    class_tokens = set(str(getattr(control, "ClassName", "")).split())
    # QQ has two observed empty TextPattern shapes.  Preserve every other
    # result verbatim: a newline can be a real draft unless this exact empty
    # editor marker is also present.
    if "is-empty" in class_tokens and result in (EMPTY_PLACEHOLDER + "\n", "\n"):
        return ""
    return result


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_inputs(events: list[_INPUT]) -> None:
    if not events:
        return
    library = ctypes.WinDLL("user32", use_last_error=True)
    library.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    library.SendInput.restype = wintypes.UINT
    array = (_INPUT * len(events))(*events)
    if library.SendInput(len(events), array, ctypes.sizeof(_INPUT)) != len(events):
        raise GuestComposerError("send_input_incomplete")


def _key(vk: int, *, scan: int = 0, flags: int = 0) -> _INPUT:
    return _INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


def _send_unicode(text: str) -> None:
    events: list[_INPUT] = []
    encoded = text.encode("utf-16-le", errors="strict")
    for index in range(0, len(encoded), 2):
        scan = int.from_bytes(encoded[index:index + 2], "little")
        events.extend((_key(0, scan=scan, flags=0x0004),
                       _key(0, scan=scan, flags=0x0006)))
    _send_inputs(events)


def _select_all_delete() -> None:
    pressed = [_key(0x11), _key(0x41)]
    try:
        _send_inputs(pressed + [_key(0x41, flags=0x0002), _key(0x11, flags=0x0002),
                                _key(0x2E), _key(0x2E, flags=0x0002)])
    finally:
        try:
            _send_inputs([_key(0x41, flags=0x0002), _key(0x11, flags=0x0002)])
        except GuestComposerError:
            pass


def _wait_for_exact_text(control: Any, expected: str, *,
                         scope_guard: Callable[[], bool],
                         focus_guard: Callable[[Any], bool],
                         timeout_seconds: float = 2.0,
                         interval_seconds: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not scope_guard() or not focus_guard(control):
            raise GuestComposerError("composer_focus_or_scope_drift")
        if read_composer_text(control) == expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_seconds)


def _focus_composer(control: Any, *, scope_guard: Callable[[], bool],
                    focus_guard: Callable[[Any], bool],
                    timeout_seconds: float = 2.0,
                    interval_seconds: float = 0.05) -> None:
    """Preserve verified focus or allow asynchronous UIA focus to settle."""
    if not scope_guard():
        raise GuestComposerError("composer_focus_drift")
    # Electron can redirect a redundant SetFocus to a hidden renderer HWND.
    # Keep an already verified editor focus within the certified QQ window.
    if focus_guard(control):
        return
    control.SetFocus()
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not scope_guard():
            raise GuestComposerError("composer_focus_drift")
        if focus_guard(control):
            return
        if time.monotonic() >= deadline:
            raise GuestComposerError("composer_focus_drift")
        time.sleep(interval_seconds)


def write_with_text_pattern(control: Any, text: str, *,
                            scope_guard: Callable[[], bool],
                            focus_guard: Callable[[Any], bool],
                            sender: Callable[[str], None] = _send_unicode,
                            batch_units: int = 32) -> None:
    if not scope_guard():
        raise GuestComposerError("composer_scope_rejected")
    if read_composer_text(control) != "":
        raise GuestComposerError("composer_not_empty")
    _focus_composer(control, scope_guard=scope_guard, focus_guard=focus_guard)
    # A user draft may have appeared while the provider committed focus.
    if read_composer_text(control) != "":
        raise GuestComposerError("composer_not_empty")
    for start in range(0, len(text), batch_units):
        if not scope_guard() or not focus_guard(control):
            raise GuestComposerError("composer_focus_or_scope_drift")
        sender(text[start:start + batch_units])
        if not scope_guard() or not focus_guard(control):
            raise GuestComposerError("composer_focus_or_scope_drift")
    if not _wait_for_exact_text(control, text, scope_guard=scope_guard,
                                focus_guard=focus_guard):
        raise GuestComposerError("composer_readback_mismatch")


def clear_with_local_selection(control: Any, *, clear_action: Callable[[], None],
                               expected_text: str, scope_guard: Callable[[], bool],
                               focus_guard: Callable[[Any], bool]) -> None:
    if not scope_guard() or read_composer_text(control) != expected_text:
        raise GuestComposerError("composer_clear_precondition_failed")
    _focus_composer(control, scope_guard=scope_guard, focus_guard=focus_guard)
    if read_composer_text(control) != expected_text:
        raise GuestComposerError("composer_clear_precondition_failed")
    clear_action()
    if not _wait_for_exact_text(control, "", scope_guard=scope_guard,
                                focus_guard=focus_guard):
        raise GuestComposerError("composer_clear_not_verified")
