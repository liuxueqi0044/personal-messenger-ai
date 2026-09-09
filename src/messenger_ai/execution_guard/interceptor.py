from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any


class ActionNotAllowed(ValueError):
    pass


DEFAULT_ALLOWED_OPERATIONS = frozenset(
    {
        "uia.find",
        "uia.read",
        "uia.text_pattern.get",
        "uia.value_pattern.set",
        "uia.invoke_pattern.invoke",
        "uia.selection_item_pattern.select",
        "msaa.read",
        "msaa.invoke",
        "window.capture",
        "window.message",
        "window.query",
        "process.query",
        "ocr.local",
        "clipboard.sequence.read",
    }
)


class ActionInterceptor:
    """Fail-closed operation gate used by adapters for every OS interaction."""

    _forbidden_fragments = (
        "setforegroundwindow",
        "switchtothiswindow",
        "sendinput",
        "mouse_event",
        "keybd_event",
        "sendkeys",
        "pyautogui.click",
        "pyautogui.write",
        "pyautogui.typewrite",
        "pyautogui.hotkey",
        "pyautogui.press",
        "pyautogui.mouse",
        "clipboard.write",
        "clipboard.set",
        "clipboard.copy",
        "pyperclip.copy",
        "setclipboarddata",
        "setclipboardtext",
        "window.move",
        "window.maximize",
        "window.resize",
    )

    def __init__(
        self, allowed_operations: Iterable[str] = DEFAULT_ALLOWED_OPERATIONS
    ) -> None:
        self._allowed = frozenset(
            self._normalize(value) for value in allowed_operations
        )

    @staticmethod
    def _normalize(operation: str) -> str:
        return "".join(operation.strip().lower().split())

    def validate(
        self, operations: Iterable[str], fallback_operations: Iterable[str] = ()
    ) -> None:
        fallbacks = tuple(fallback_operations)
        if fallbacks:
            raise ActionNotAllowed("fallback OS operations are forbidden in D0")
        for operation in operations:
            normalized = self._normalize(operation)
            if any(fragment in normalized for fragment in self._forbidden_fragments):
                raise ActionNotAllowed(f"D0 forbidden OS operation: {operation}")
            if normalized not in self._allowed:
                raise ActionNotAllowed(f"OS operation is not allowlisted: {operation}")

    async def invoke(
        self, operation: str, call: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        self.validate((operation,))
        result = call(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


GUEST_FOREGROUND_ALLOWED_OPERATIONS = DEFAULT_ALLOWED_OPERATIONS | frozenset(
    {"guest.window.activate", "guest.keyboard.write_text", "guest.keyboard.press_enter"}
)


class GuestForegroundInterceptor(ActionInterceptor):
    """Narrow input gate for a dedicated VM guest desktop."""

    def __init__(self) -> None:
        super().__init__(GUEST_FOREGROUND_ALLOWED_OPERATIONS)
