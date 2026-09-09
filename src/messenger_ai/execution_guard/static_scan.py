from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field

from messenger_ai.domain import DomainModel


class ForbiddenCall(DomainModel):
    path: str
    line: int = Field(ge=1)
    call: str
    rule: str


_EXACT_FORBIDDEN = {
    "setforegroundwindow",
    "switchtothiswindow",
    "sendinput",
    "mouse_event",
    "keybd_event",
    "sendkeys",
    "sendwait",
}
_PYAUTOGUI_INPUT = {
    "click",
    "write",
    "typewrite",
    "hotkey",
    "press",
    "keydown",
    "keyup",
    "move",
    "moveto",
    "drag",
    "dragto",
}
_CLIPBOARD_WRITES = {
    "copy",
    "setclipboarddata",
    "setclipboardtext",
    "set_text",
    "write",
}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def scan_file(path: Path) -> tuple[ForbiddenCall, ...]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_aliases: dict[str, str] = {}
    imported_calls: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported_calls[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}"
                )

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _dotted_name(node.func)
        if not call:
            continue
        parts = call.split(".")
        if parts[0] in module_aliases:
            call = ".".join((module_aliases[parts[0]], *parts[1:]))
        elif parts[0] in imported_calls:
            call = ".".join((imported_calls[parts[0]], *parts[1:]))
        elif call in imported_calls:
            call = imported_calls[call]
        lowered = call.lower()
        leaf = lowered.rsplit(".", 1)[-1]
        rule = None
        if leaf in _EXACT_FORBIDDEN:
            rule = "D0 global input or foreground API"
        elif "pyautogui" in lowered and leaf in _PYAUTOGUI_INPUT:
            rule = "D0 pyautogui input"
        elif (
            "clipboard" in lowered or "pyperclip" in lowered
        ) and leaf in _CLIPBOARD_WRITES:
            rule = "D0 clipboard write"
        if rule:
            findings.append(
                ForbiddenCall(path=str(path), line=node.lineno, call=call, rule=rule)
            )
    return tuple(findings)


def scan_paths(paths: Iterable[Path]) -> tuple[ForbiddenCall, ...]:
    findings = []
    for root in paths:
        candidates = (root,) if root.is_file() else root.rglob("*.py")
        for path in candidates:
            findings.extend(scan_file(path))
    return tuple(findings)
