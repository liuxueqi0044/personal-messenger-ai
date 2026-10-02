"""Narrow capabilities consumed by the runtime navigation coordinator."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from .contracts import (
    ContactTarget, DesktopActionResult, NavigationDecision, NavigationFrame,
    NavigationProviderResult, NavigationRequest,
)

if TYPE_CHECKING:
    # N2 owns this result, and must use a closed target-bound lease/result.
    from .identity import ActiveChatVerificationResult


class VisionNavigator(Protocol):
    async def decide(
        self, request: NavigationRequest, *, cancel_event: asyncio.Event | None = None
    ) -> NavigationProviderResult: ...


class DesktopOperator(Protocol):
    async def capture(
        self, target: ContactTarget, *, deadline_at: datetime
    ) -> NavigationFrame: ...

    async def execute(
        self, request: NavigationRequest, decision: NavigationDecision,
        *, cancel_event: asyncio.Event | None = None,
    ) -> DesktopActionResult: ...


class CurrentChatVerifier(Protocol):
    async def verify(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime
    ) -> ActiveChatVerificationResult: ...
