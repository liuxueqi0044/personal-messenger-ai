from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from messenger_ai.domain import Platform


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


@dataclass(frozen=True)
class CircuitSnapshot:
    state: CircuitState
    consecutive_failures: int
    reason: str | None


class CircuitBreaker:
    """Safety breaker with explicit, audited reset; there is no automatic half-open state."""

    def __init__(self, failure_threshold: int = 3) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        self.failure_threshold = failure_threshold
        self._failures: defaultdict[Platform, int] = defaultdict(int)
        self._reasons: dict[Platform, str] = {}
        self._lock = asyncio.Lock()

    async def record_success(self, platform: Platform) -> None:
        async with self._lock:
            if platform not in self._reasons:
                self._failures[platform] = 0

    async def record_failure(self, platform: Platform, reason: str) -> bool:
        async with self._lock:
            if platform in self._reasons:
                return True
            self._failures[platform] += 1
            if self._failures[platform] >= self.failure_threshold:
                self._reasons[platform] = reason
                return True
            return False

    async def trip(self, platform: Platform, reason: str) -> None:
        async with self._lock:
            self._failures[platform] = max(
                self.failure_threshold, self._failures[platform]
            )
            self._reasons[platform] = reason

    async def reset(self, platform: Platform) -> None:
        async with self._lock:
            self._failures[platform] = 0
            self._reasons.pop(platform, None)

    async def is_open(self, platform: Platform) -> bool:
        async with self._lock:
            return platform in self._reasons

    async def snapshot(self, platform: Platform) -> CircuitSnapshot:
        async with self._lock:
            reason = self._reasons.get(platform)
            return CircuitSnapshot(
                state=CircuitState.OPEN if reason is not None else CircuitState.CLOSED,
                consecutive_failures=self._failures[platform],
                reason=reason,
            )


class PlatformMutex:
    def __init__(self) -> None:
        self._locks: defaultdict[Platform, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, platform: Platform) -> None:
        await self._locks[platform].acquire()

    def release(self, platform: Platform) -> None:
        lock = self._locks[platform]
        if not lock.locked():
            raise RuntimeError("platform mutex is not held")
        lock.release()

    def locked(self, platform: Platform) -> bool:
        return self._locks[platform].locked()

    @asynccontextmanager
    async def hold(self, platform: Platform):
        await self.acquire(platform)
        try:
            yield
        finally:
            self.release(platform)


class CancellationToken:
    def __init__(self, parent_event: asyncio.Event | None = None) -> None:
        self._event = asyncio.Event()
        self._parent_event = parent_event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(
            self._parent_event and self._parent_event.is_set()
        )

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        if self.cancelled:
            return
        own = asyncio.create_task(self._event.wait())
        if self._parent_event is None:
            await own
            return
        parent = asyncio.create_task(self._parent_event.wait())
        _, pending = await asyncio.wait(
            (own, parent), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


class EmergencyStop:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    @property
    def active(self) -> bool:
        return self._event.is_set()

    def trigger(self, reason: str = "emergency stop") -> None:
        self.reason = reason
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def token(self) -> CancellationToken:
        return CancellationToken(self._event)

    def reset(self) -> None:
        self.reason = None
        self._event = asyncio.Event()
