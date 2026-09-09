"""Small, redacted contract between the WebUI and the QQ lease core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PrepareResult:
    challenge_id: str
    csrf_token: str
    expires_at: datetime
    scope_summary: str = "当前可见 QQ 会话"


@dataclass(frozen=True)
class ConfirmResult:
    lease_id: str
    expires_at: datetime
    lease_identity_prefix: str
    automatic_eligible: bool = False


@dataclass(frozen=True)
class StatusResult:
    state: str
    lease_id: str | None = None
    expires_at: datetime | None = None
    automatic_eligible: bool = False
    reason_code: str | None = None


@dataclass(frozen=True)
class RevokeResult:
    lease_id: str
    state: str


class SessionLeaseServiceError(ValueError):
    """Safe, fixed-code error; implementations must not include user data."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class SessionLeaseService(Protocol):
    def prepare(self, *, session_id: str) -> PrepareResult: ...

    def confirm(
        self,
        *,
        session_id: str,
        challenge_id: str,
        challenge_csrf: str,
        idempotency_key: str,
        confirmed: bool,
    ) -> ConfirmResult: ...

    def status(self, *, session_id: str) -> StatusResult: ...

    def revoke(
        self, *, session_id: str, lease_id: str, idempotency_key: str
    ) -> RevokeResult: ...
