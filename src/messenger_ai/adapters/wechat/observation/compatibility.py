"""Fail-closed compatibility and binding policy."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from .models import (
    CompatibilityRecord,
    WindowBinding,
    WindowDescriptor,
    WindowEnvironment,
)


class CompatibilityError(RuntimeError):
    """Raised when a window/environment has not been explicitly verified."""


class CompatibilityMatrix:
    def __init__(self, records: tuple[CompatibilityRecord, ...] = ()) -> None:
        self._records = {record.fingerprint: record for record in records}

    def register(self, record: CompatibilityRecord) -> None:
        self._records[record.fingerprint] = record

    def lookup(self, environment: WindowEnvironment) -> CompatibilityRecord | None:
        return self._records.get(environment.fingerprint())

    def status(self, environment: WindowEnvironment) -> str:
        record = self.lookup(environment)
        return record.status if record else "unknown"

    def require_verified(self, environment: WindowEnvironment) -> CompatibilityRecord:
        record = self.lookup(environment)
        if record is None or record.status != "verified":
            reason = record.reason if record else "environment_not_in_matrix"
            raise CompatibilityError(f"unverified WeChat environment: {reason}")
        return record

    def records(self) -> tuple[CompatibilityRecord, ...]:
        return tuple(self._records.values())


class StrictWindowBinder:
    """Bind only an explicitly discovered Weixin window.

    This class does not discover windows, activate them, or capture a desktop.
    A zero handle (as returned by a non-interactive/background probe) is
    rejected, as are executable and compatibility mismatches.
    """

    def __init__(self, matrix: CompatibilityMatrix) -> None:
        self.matrix = matrix

    def bind(self, descriptor: WindowDescriptor) -> WindowBinding:
        if descriptor.handle <= 0 or descriptor.process_id <= 0:
            raise CompatibilityError("target window handle/process is unavailable")
        executable = descriptor.executable.lower()
        if executable not in {"weixin.exe", "wechat.exe"}:
            raise CompatibilityError("target is not the official WeChat executable")
        self.matrix.require_verified(descriptor.environment)
        if descriptor.identity_confidence < 0.95 or not descriptor.identity:
            raise CompatibilityError("conversation identity is not uniquely bound")
        binding_id = sha256(
            f"{descriptor.handle}:{descriptor.process_id}:{descriptor.environment.fingerprint()}".encode()
        ).hexdigest()
        return WindowBinding(
            handle=descriptor.handle,
            process_id=descriptor.process_id,
            executable=descriptor.executable,
            identity=descriptor.identity,
            identity_confidence=descriptor.identity_confidence,
            environment=descriptor.environment,
            bound_at=datetime.now(UTC),
            binding_id=binding_id,
            verified=True,
        )
