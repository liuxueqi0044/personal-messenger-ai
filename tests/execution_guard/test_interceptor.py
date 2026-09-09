from __future__ import annotations

from pathlib import Path

import pytest

from messenger_ai.execution_guard import ActionInterceptor, ActionNotAllowed, scan_file


@pytest.mark.parametrize(
    "operation",
    [
        "user32.SetForegroundWindow",
        "user32.SwitchToThisWindow",
        "user32.SendInput",
        "win32api.mouse_event",
        "win32api.keybd_event",
        "System.Windows.Forms.SendKeys.SendWait",
        "pyautogui.click",
        "pyautogui.write",
        "pyautogui.hotkey",
        "clipboard.write",
        "pyperclip.copy",
        "window.move",
    ],
)
def test_d0_malicious_operations_are_rejected(operation: str) -> None:
    with pytest.raises(ActionNotAllowed):
        ActionInterceptor().validate((operation,))


def test_unknown_and_fallback_operations_are_rejected() -> None:
    interceptor = ActionInterceptor()
    with pytest.raises(ActionNotAllowed, match="not allowlisted"):
        interceptor.validate(("custom.best_effort_input",))
    with pytest.raises(ActionNotAllowed, match="fallback"):
        interceptor.validate(("uia.read",), ("uia.value_pattern.set",))


def test_allowlisted_background_operation_can_be_invoked() -> None:
    ActionInterceptor().validate(
        ("uia.read", "window.capture", "clipboard.sequence.read")
    )


def test_static_scan_finds_real_calls_but_not_comments(tmp_path: Path) -> None:
    source = tmp_path / "malicious.py"
    source.write_text(
        "import pyautogui as p\n"
        "from ctypes import windll\n"
        "# SendInput in a comment is not a call\n"
        "p.click(1, 2)\n"
        "windll.user32.SetForegroundWindow(42)\n",
        encoding="utf-8",
    )
    findings = scan_file(source)
    assert {finding.call for finding in findings} == {
        "pyautogui.click",
        "ctypes.windll.user32.SetForegroundWindow",
    }
