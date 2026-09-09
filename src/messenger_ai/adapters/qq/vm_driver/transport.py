from __future__ import annotations

"""Concrete Windows UIA boundary.

This module deliberately has no SendKeys, clipboard, mouse, or keyboard fallback.
It uses UIA ValuePattern for composition and UIA InvokePattern for the visible
Send button.  The import is deferred so offline tests and the host package do not
need Windows-only dependencies.
"""

import hashlib
import os
import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from messenger_ai.adapters.qq.models import BubbleDirection, QQBubble, QQConversation, QQSelector, QQWindow


class UIAUnavailable(RuntimeError):
    pass


class WindowsUIAQQAccessibility:
    """Real QQ NT accessibility transport for a *guest* Windows desktop."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise UIAUnavailable("QQ VM transport can only run on Windows")
        if os.environ.get("PERSONAL_MESSENGER_VM_GUEST") != "1":
            raise UIAUnavailable("refusing UI automation outside a certified VM guest")
        try:
            probe = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress"],
                check=True, capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            machine = json.loads(probe.stdout)
            manufacturer = str(machine.get("Manufacturer", "")).lower()
            model = str(machine.get("Model", "")).lower()
        except Exception as exc:
            raise UIAUnavailable("guest machine identity could not be certified") from exc
        if "virtualbox" not in model or not any(name in manufacturer for name in ("innotek", "oracle")):
            raise UIAUnavailable("refusing UI automation: machine is not a certified VirtualBox guest")
        try:
            import uiautomation as auto  # type: ignore[import-not-found]
        except ImportError as exc:
            raise UIAUnavailable("install personal-messenger-ai[qq-vm] in the guest") from exc
        self._auto = auto

    def find_main_windows(self, selector: QQSelector) -> list[QQWindow]:
        root = self._auto.GetRootControl()
        # Main windows are direct desktop children. Never breadth-first scan the
        # whole desktop before a QQ root has been identified.
        controls = [item for item in root.GetChildren() if self._matches(item, selector, ())]
        result: list[QQWindow] = []
        for control in controls:
            handle = int(getattr(control, "NativeWindowHandle", 0) or 0)
            pid = int(getattr(control, "ProcessId", 0) or 0)
            if handle > 0 and pid > 0:
                result.append(QQWindow(process_id=pid, window_handle=handle, class_name=str(getattr(control, "ClassName", "")), title=str(getattr(control, "Name", ""))))
        return result

    def tree_digest(self, window: QQWindow) -> str:
        control = self._window(window)
        rows = []
        for item in self._descendants(control):
            rows.append("\x1f".join((str(getattr(item, "ControlTypeName", "")), str(getattr(item, "AutomationId", "")), str(getattr(item, "ClassName", "")))))
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def list_conversations(self, window: QQWindow, selector: QQSelector) -> list[QQConversation]:
        digest = self.tree_digest(window)
        result = []
        for item in self._select(self._window(window), selector):
            name = str(getattr(item, "Name", ""))
            automation_id = str(getattr(item, "AutomationId", ""))
            class_name = str(getattr(item, "ClassName", ""))
            # A label is presentation only; without a client-provided UIA id the
            # binding cannot be certified and is deliberately omitted.
            if not automation_id:
                continue
            signature = hashlib.sha256(f"{automation_id}|{class_name}".encode()).hexdigest()
            result.append(QQConversation(internal_id=automation_id, display_name=name, participant_signature=signature, tree_digest=digest))
        return result

    def select_conversation(self, window: QQWindow, conversation: QQConversation, selector: QQSelector) -> None:
        matches = [item for item in self._select(self._window(window), selector) if self._conversation_id(item) == conversation.internal_id]
        if len(matches) != 1:
            raise UIAUnavailable("conversation is absent or ambiguous")
        pattern = matches[0].GetSelectionItemPattern()
        if pattern is None:
            raise UIAUnavailable("conversation has no SelectionItemPattern")
        pattern.Select()
        if not bool(getattr(pattern, "IsSelected", False)):
            raise UIAUnavailable("conversation selection could not be independently confirmed")

    def write_composer(self, window: QQWindow, text: str, selector: QQSelector) -> None:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("composer is absent or ambiguous")
        pattern = matches[0].GetValuePattern()
        if pattern is None or bool(getattr(pattern, "IsReadOnly", False)):
            raise UIAUnavailable("composer has no writable ValuePattern")
        pattern.SetValue(text)

    def read_composer(self, window: QQWindow, selector: QQSelector) -> str:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("composer is absent or ambiguous")
        pattern = matches[0].GetValuePattern()
        if pattern is None:
            raise UIAUnavailable("composer has no ValuePattern")
        return str(pattern.Value)

    def invoke_send(self, window: QQWindow, selector: QQSelector) -> None:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("send button is absent or ambiguous")
        pattern = matches[0].GetInvokePattern()
        if pattern is None:
            raise UIAUnavailable("send button has no InvokePattern")
        pattern.Invoke()

    def list_bubbles(self, window: QQWindow, selector: QQSelector) -> list[QQBubble]:
        digest = self.tree_digest(window)
        result = []
        for index, item in enumerate(self._select(self._window(window), selector)):
            text = str(getattr(item, "Name", ""))
            class_name = str(getattr(item, "ClassName", ""))
            automation_id = str(getattr(item, "AutomationId", ""))
            classes = " ".join(str(getattr(node, "ClassName", "")) for node in self._descendants(item)) + " " + class_name
            if "container--self" in classes:
                direction = BubbleDirection.OUTBOUND
            elif "container--other" in classes or "container--peer" in classes:
                direction = BubbleDirection.INBOUND
            else:
                direction = BubbleDirection.UNKNOWN
            # UIA often lacks a native message id. This is only a snapshot
            # fingerprint, never a durable cursor or de-duplication key.
            stable = automation_id or hashlib.sha256(f"{class_name}|{text}".encode()).hexdigest()
            result.append(QQBubble(conversation_internal_id="visible-current-conversation", message_key=stable, direction=direction, text=text, observed_at=datetime.now(UTC), tree_digest=digest))
        return result

    def _window(self, window: QQWindow) -> Any:
        control = self._auto.ControlFromHandle(window.window_handle)
        if control is None or int(getattr(control, "ProcessId", 0) or 0) != window.process_id:
            raise UIAUnavailable("QQ window no longer matches the worker target")
        return control

    def _select(self, root: Any, selector: QQSelector) -> list[Any]:
        return [item for item, ancestors in self._walk(root) if self._matches(item, selector, ancestors)]

    def _descendants(self, root: Any) -> Iterable[Any]:
        for item, _ancestors in self._walk(root):
            yield item

    def _walk(self, root: Any) -> Iterable[tuple[Any, tuple[str, ...]]]:
        queue = [(item, ()) for item in root.GetChildren()]
        while queue:
            item, ancestors = queue.pop(0)
            yield item, ancestors
            item_id = str(getattr(item, "AutomationId", ""))
            queue.extend((child, ancestors + ((item_id,) if item_id else ())) for child in item.GetChildren())

    @staticmethod
    def _conversation_id(item: Any) -> str:
        automation_id = str(getattr(item, "AutomationId", ""))
        if automation_id:
            return automation_id
        raw = f"{getattr(item, 'AutomationId', '')}|{getattr(item, 'ClassName', '')}|{getattr(item, 'Name', '')}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _matches(item: Any, selector: QQSelector, ancestors: tuple[str, ...]) -> bool:
        if str(getattr(item, "ControlTypeName", "")) != selector.control_type:
            return False
        if selector.automation_id and str(getattr(item, "AutomationId", "")) != selector.automation_id:
            return False
        if selector.class_name and str(getattr(item, "ClassName", "")) != selector.class_name:
            return False
        return not selector.ancestor_automation_ids or tuple(selector.ancestor_automation_ids) == ancestors[-len(selector.ancestor_automation_ids):]
