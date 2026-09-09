from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Mapping
from typing import Any, Protocol

from pydantic import Field

from messenger_ai.domain import DomainModel

from .models import ContentionEvent


class DesktopState(DomainModel):
    foreground_window: int | str | None = None
    keyboard_focus: int | str | None = None
    pointer_position: tuple[int, int] | None = None
    clipboard_revision: int | str | None = None
    window_state_digest: str | None = None
    observed_monotonic: float | None = Field(default=None, ge=0)


class DesktopStateReader(Protocol):
    def read_state(
        self,
    ) -> (
        DesktopState | Mapping[str, Any] | Awaitable[DesktopState | Mapping[str, Any]]
    ): ...


class ContentionMonitor(ABC):
    @abstractmethod
    async def snapshot(self) -> DesktopState: ...

    @abstractmethod
    def compare(
        self, before: DesktopState, after: DesktopState
    ) -> tuple[ContentionEvent, ...]: ...


class SnapshotContentionMonitor(ContentionMonitor):
    """Read-only desktop audit. It never fetches clipboard content or injects input."""

    _protected_fields = (
        "foreground_window",
        "keyboard_focus",
        "pointer_position",
        "clipboard_revision",
        "window_state_digest",
    )

    def __init__(self, reader: DesktopStateReader) -> None:
        self._reader = reader

    async def snapshot(self) -> DesktopState:
        raw = self._reader.read_state()
        if inspect.isawaitable(raw):
            raw = await raw
        return raw if isinstance(raw, DesktopState) else DesktopState(**dict(raw))

    def compare(
        self, before: DesktopState, after: DesktopState
    ) -> tuple[ContentionEvent, ...]:
        events = []
        for field in self._protected_fields:
            old, new = getattr(before, field), getattr(after, field)
            if old != new:
                events.append(ContentionEvent(resource=field, before=old, after=new))
        return tuple(events)
