from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from messenger_ai.execution_guard import CancellationToken

from .models import CapturedSendEvidence, OutboundBubble, SendRoute, TargetRef


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class CurrentPolicyVersions:
    """Small projection of active policy state; it has no M7 implementation coupling."""

    rule_version: str
    policy_version: str


class CurrentPolicyVersionPort(Protocol):
    async def current_versions(
        self, conversation_id: str
    ) -> CurrentPolicyVersions | None: ...


class WechatSendDriver(Protocol):
    """Semantic driver boundary. Implementations receive no fallback action API."""

    @property
    def route(self) -> SendRoute: ...

    async def capture(
        self, target: TargetRef, token: CancellationToken
    ) -> CapturedSendEvidence: ...

    async def select_target(
        self, target: TargetRef, token: CancellationToken
    ) -> bool: ...

    async def compose_text(
        self, target: TargetRef, text: str, token: CancellationToken
    ) -> None: ...

    async def read_composer_hash(
        self, target: TargetRef, token: CancellationToken
    ) -> str: ...

    async def commit(self, target: TargetRef, token: CancellationToken) -> str: ...

    async def read_outbound_since(
        self,
        target: TargetRef,
        after_sequence: int,
        token: CancellationToken,
    ) -> tuple[OutboundBubble, ...]: ...
