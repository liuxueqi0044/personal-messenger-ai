from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock

import pytest

from messenger_ai.observability import SecretNotFoundError


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self._lock = Lock()

    def set_secret(self, name: str, value: bytes) -> None:
        with self._lock:
            self.values[name] = bytes(value)

    def get_secret(self, name: str) -> bytes:
        with self._lock:
            if name not in self.values:
                raise SecretNotFoundError("secret does not exist")
            return self.values[name]

    def delete_secret(self, name: str) -> bool:
        with self._lock:
            return self.values.pop(name, None) is not None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def memory_secrets() -> MemorySecretStore:
    return MemorySecretStore()
