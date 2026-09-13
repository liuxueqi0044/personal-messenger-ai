from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

from .models import (QQBubble, QQCertifiedDirectIdentity, QQConversation, QQSelector,
                     QQSessionObservedDirectIdentity, QQWindow)


class QQAccessibilityPort(Protocol):
    """Injected Windows/UIA boundary. It has no foreground or input fallback."""

    def find_main_windows(
        self, selector: QQSelector
    ) -> Awaitable[list[QQWindow]] | list[QQWindow]: ...

    def tree_digest(self, window: QQWindow) -> Awaitable[str] | str: ...

    def list_conversations(
        self, window: QQWindow, selector: QQSelector
    ) -> Awaitable[list[QQConversation]] | list[QQConversation]: ...

    def select_conversation(
        self, window: QQWindow, conversation: QQConversation, selector: QQSelector
    ) -> Awaitable[bool] | bool: ...

    def write_composer(
        self, window: QQWindow, text: str, selector: QQSelector
    ) -> Awaitable[None] | None: ...

    def read_composer(
        self, window: QQWindow, selector: QQSelector
    ) -> Awaitable[str] | str: ...

    def invoke_send(
        self, window: QQWindow, selector: QQSelector
    ) -> Awaitable[None] | None: ...

    def list_bubbles(
        self, window: QQWindow, selector: QQSelector
    ) -> Awaitable[list[QQBubble]] | list[QQBubble]: ...


class QQCurrentIdentityCertifier(Protocol):
    """Certify the already-selected conversation; it never chooses a row."""

    def certify_current(
        self, window: QQWindow, candidate: QQConversation
    ) -> QQCertifiedDirectIdentity | QQSessionObservedDirectIdentity: ...


class QQCandidateLocator(Protocol):
    """Use volatile hints to locate rows; returned candidates are uncertified."""

    def locate_candidates(
        self, binding: object, visible: list[QQConversation]
    ) -> list[QQConversation]: ...
