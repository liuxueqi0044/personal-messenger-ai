"""Fail-closed, human-confirmed leases for one visible QQ session.

This module deliberately has no desktop-control or message-send dependency.
It only compares caller-supplied, read-only scope snapshots and holds the
short-lived approval state in memory.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_LEASE_TTL = timedelta(hours=12)
_MAX_CHALLENGE_TTL = timedelta(minutes=5)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Q3LeaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    INVALIDATED = "invalidated"


class LeaseAuditAction(StrEnum):
    PREPARED = "challenge_prepared"
    CREATED = "lease_created"
    REVOKED = "lease_revoked"
    INVALIDATED = "lease_invalidated"
    CONFIRMATION_REJECTED = "confirmation_rejected"


class SessionLeaseReason(StrEnum):
    CHALLENGE_NOT_FOUND = "challenge_not_found"
    CHALLENGE_EXPIRED = "challenge_expired"
    CHALLENGE_REPLAYED = "challenge_replayed"
    CSRF_MISMATCH = "csrf_mismatch"
    IDEMPOTENCY_REPLAYED = "idempotency_replayed"
    CONFIRMATION_REQUIRED = "explicit_confirmation_required"
    SCOPE_DRIFT = "live_scope_drift"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_NOT_FOUND = "lease_not_found"
    LEASE_EXPIRED = "lease_expired"
    LEASE_REVOKED = "lease_revoked"
    LEASE_INVALIDATED = "lease_invalidated"
    SCOPE_UNAVAILABLE = "live_scope_unavailable"


class SessionLeaseError(ValueError):
    """A safe error that contains only a fixed reason code."""

    def __init__(self, reason: SessionLeaseReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class LiveSessionScope(Q3LeaseModel):
    """All facts that must stay exact for the lifetime of a lease."""

    platform: Literal["qq"] = "qq"
    process_id: int = Field(gt=0)
    process_started_at: datetime
    window_handle: int = Field(gt=0)
    header_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    structure_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector_pack_version: str = Field(min_length=1, max_length=256)
    pending_application_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("process_started_at")
    @classmethod
    def normalize_process_started_at(cls, value: datetime) -> datetime:
        return _aware(value, "process_started_at")

    @property
    def scope_digest(self) -> str:
        """An opaque identity for conflict indexes; no contact data is involved."""

        payload = "\x1f".join(
            (
                self.platform,
                str(self.process_id),
                self.process_started_at.isoformat(),
                str(self.window_handle),
                self.header_digest,
                self.structure_digest,
                self.environment_fingerprint,
                self.selector_pack_version,
                self.pending_application_digest,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LeaseChallenge(Q3LeaseModel):
    challenge_id: UUID = Field(default_factory=uuid4)
    csrf_token: str = Field(min_length=32, max_length=512)
    issued_at: datetime
    expires_at: datetime
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_times(cls, value: datetime, info: object) -> datetime:
        return _aware(value, getattr(info, "field_name", "timestamp"))


class SessionLease(Q3LeaseModel):
    lease_id: UUID = Field(default_factory=uuid4)
    scope: LiveSessionScope
    lease_identity_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_id: UUID
    confirmed_at: datetime
    expires_at: datetime
    status: LeaseStatus = LeaseStatus.ACTIVE
    automatic_eligible: Literal[False] = False
    closed_at: datetime | None = None
    close_reason: SessionLeaseReason | None = None

    @field_validator("confirmed_at", "expires_at", "closed_at")
    @classmethod
    def normalize_times(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _aware(value, getattr(info, "field_name", "timestamp"))

    @property
    def lease_identity_prefix(self) -> str:
        return self.lease_identity_hmac[:12]


class LeaseAuditEntry(Q3LeaseModel):
    sequence: int = Field(ge=1)
    action: LeaseAuditAction
    occurred_at: datetime
    reason: SessionLeaseReason | None = None
    challenge_id: UUID | None = None
    lease_id: UUID | None = None
    lease_identity_prefix: str | None = Field(default=None, pattern=r"^[0-9a-f]{12}$")

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")


class _ChallengeState:
    def __init__(
        self,
        challenge: LeaseChallenge,
        scope: LiveSessionScope,
        lease_identity_hmac: str,
    ) -> None:
        self.challenge = challenge
        self.scope = scope
        self.lease_identity_hmac = lease_identity_hmac
        self.consumed = False


class SessionLeaseManager:
    """Thread-safe, in-memory, one-time Q3 session lease state machine."""

    def __init__(
        self,
        *,
        hmac_key: bytes,
        clock: Callable[[], datetime] | None = None,
        challenge_ttl: timedelta = _MAX_CHALLENGE_TTL,
        lease_ttl: timedelta = _MAX_LEASE_TTL,
    ) -> None:
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ValueError("hmac_key must be at least 32 bytes")
        if challenge_ttl <= timedelta(0) or challenge_ttl > _MAX_CHALLENGE_TTL:
            raise ValueError("challenge_ttl must be positive and at most five minutes")
        if lease_ttl <= timedelta(0) or lease_ttl > _MAX_LEASE_TTL:
            raise ValueError("lease_ttl must be positive and at most twelve hours")
        self._hmac_key = hmac_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._challenge_ttl = challenge_ttl
        self._lease_ttl = lease_ttl
        self._challenges: dict[UUID, _ChallengeState] = {}
        self._leases: dict[UUID, SessionLease] = {}
        self._active_by_scope: dict[str, UUID] = {}
        self._idempotency_claims: set[str] = set()
        self._audit: list[LeaseAuditEntry] = []
        self._lock = RLock()

    def prepare(self, scope: LiveSessionScope) -> LeaseChallenge:
        """Create the only five-minute confirmation challenge for *scope*."""

        now = self._now()
        challenge = LeaseChallenge(
            csrf_token=secrets.token_urlsafe(32),
            issued_at=now,
            expires_at=now + self._challenge_ttl,
            scope_digest=scope.scope_digest,
        )
        with self._lock:
            self._challenges[challenge.challenge_id] = _ChallengeState(
                challenge,
                scope,
                self._new_lease_identity_hmac(scope),
            )
            self._audit_entry(
                LeaseAuditAction.PREPARED,
                now,
                challenge_id=challenge.challenge_id,
            )
        return challenge

    def confirm(
        self,
        *,
        challenge_id: UUID,
        csrf_token: str,
        idempotency_key: str,
        confirmed: bool,
        confirmation_id: UUID,
        read_live_scope: Callable[[], LiveSessionScope],
    ) -> SessionLease:
        """Consume a challenge and create one scoped read-only lease."""

        idempotency_digest = self._idempotency_digest(idempotency_key)
        now = self._now()
        with self._lock:
            state = self._consume_challenge(challenge_id, csrf_token, now)
            if idempotency_digest in self._idempotency_claims:
                self._reject(challenge_id, now, SessionLeaseReason.IDEMPOTENCY_REPLAYED)
            if not confirmed:
                self._reject(
                    challenge_id, now, SessionLeaseReason.CONFIRMATION_REQUIRED
                )
            current_scope = read_live_scope()
            if not isinstance(current_scope, LiveSessionScope):
                raise TypeError("read_live_scope must return LiveSessionScope")
            if current_scope != state.scope:
                self._reject(challenge_id, now, SessionLeaseReason.SCOPE_DRIFT)
            self._expire_active(now)
            if self._active_by_scope:
                self._reject(challenge_id, now, SessionLeaseReason.LEASE_CONFLICT)
            lease = SessionLease(
                scope=state.scope,
                lease_identity_hmac=state.lease_identity_hmac,
                confirmation_id=confirmation_id,
                confirmed_at=now,
                expires_at=now + self._lease_ttl,
            )
            self._leases[lease.lease_id] = lease
            self._active_by_scope[state.scope.scope_digest] = lease.lease_id
            self._idempotency_claims.add(idempotency_digest)
            self._audit_entry(
                LeaseAuditAction.CREATED,
                now,
                challenge_id=challenge_id,
                lease=lease,
            )
            return lease

    def validate(
        self,
        lease_id: UUID,
        *,
        read_live_scope: Callable[[], LiveSessionScope],
    ) -> SessionLease:
        """Return an active lease, otherwise close it permanently and fail."""

        now = self._now()
        with self._lock:
            lease = self._require_active(lease_id, now)
            current_scope = read_live_scope()
            if not isinstance(current_scope, LiveSessionScope):
                raise TypeError("read_live_scope must return LiveSessionScope")
            if current_scope != lease.scope:
                self._close(
                    lease, LeaseStatus.INVALIDATED, now, SessionLeaseReason.SCOPE_DRIFT
                )
                raise SessionLeaseError(SessionLeaseReason.SCOPE_DRIFT)
            return lease

    def revoke(self, lease_id: UUID) -> SessionLease:
        """Idempotently revoke a lease without granting any new capability."""

        now = self._now()
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SessionLeaseError(SessionLeaseReason.LEASE_NOT_FOUND)
            if lease.status is LeaseStatus.ACTIVE:
                self._close(
                    lease, LeaseStatus.REVOKED, now, SessionLeaseReason.LEASE_REVOKED
                )
            return self._leases[lease_id]

    def invalidate(
        self,
        lease_id: UUID,
        reason: SessionLeaseReason = SessionLeaseReason.SCOPE_UNAVAILABLE,
    ) -> SessionLease:
        """Permanently fail closed when live scope can no longer be proven."""

        if reason not in {
            SessionLeaseReason.SCOPE_DRIFT,
            SessionLeaseReason.SCOPE_UNAVAILABLE,
        }:
            raise ValueError("invalid lease invalidation reason")
        now = self._now()
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SessionLeaseError(SessionLeaseReason.LEASE_NOT_FOUND)
            if lease.status is LeaseStatus.ACTIVE:
                self._close(lease, LeaseStatus.INVALIDATED, now, reason)
            return self._leases[lease_id]

    def audit_entries(self) -> tuple[LeaseAuditEntry, ...]:
        with self._lock:
            return tuple(self._audit)

    def _now(self) -> datetime:
        return _aware(self._clock(), "clock")

    @staticmethod
    def _idempotency_digest(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("idempotency_key is required")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _new_lease_identity_hmac(self, scope: LiveSessionScope) -> str:
        nonce = bytearray(secrets.token_bytes(32))
        try:
            return hmac.new(
                self._hmac_key,
                b"qq-session-lease:v2\x00"
                + scope.scope_digest.encode("ascii")
                + b"\x00"
                + nonce,
                hashlib.sha256,
            ).hexdigest()
        finally:
            nonce[:] = b"\x00" * len(nonce)

    def _consume_challenge(
        self, challenge_id: UUID, csrf_token: str, now: datetime
    ) -> _ChallengeState:
        state = self._challenges.get(challenge_id)
        if state is None:
            raise SessionLeaseError(SessionLeaseReason.CHALLENGE_NOT_FOUND)
        if state.consumed:
            raise SessionLeaseError(SessionLeaseReason.CHALLENGE_REPLAYED)
        state.consumed = True
        if now >= state.challenge.expires_at:
            self._reject(challenge_id, now, SessionLeaseReason.CHALLENGE_EXPIRED)
        if not hmac.compare_digest(state.challenge.csrf_token, csrf_token):
            self._reject(challenge_id, now, SessionLeaseReason.CSRF_MISMATCH)
        return state

    def _reject(
        self, challenge_id: UUID, now: datetime, reason: SessionLeaseReason
    ) -> None:
        self._audit_entry(
            LeaseAuditAction.CONFIRMATION_REJECTED,
            now,
            reason=reason,
            challenge_id=challenge_id,
        )
        raise SessionLeaseError(reason)

    def _require_active(self, lease_id: UUID, now: datetime) -> SessionLease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise SessionLeaseError(SessionLeaseReason.LEASE_NOT_FOUND)
        if lease.status is LeaseStatus.REVOKED:
            raise SessionLeaseError(SessionLeaseReason.LEASE_REVOKED)
        if lease.status is LeaseStatus.INVALIDATED:
            raise SessionLeaseError(SessionLeaseReason.LEASE_INVALIDATED)
        if now >= lease.expires_at:
            self._close(
                lease, LeaseStatus.INVALIDATED, now, SessionLeaseReason.LEASE_EXPIRED
            )
            raise SessionLeaseError(SessionLeaseReason.LEASE_EXPIRED)
        return lease

    def _expire_active(self, now: datetime) -> None:
        for lease in tuple(self._leases.values()):
            if lease.status is LeaseStatus.ACTIVE and now >= lease.expires_at:
                self._close(
                    lease,
                    LeaseStatus.INVALIDATED,
                    now,
                    SessionLeaseReason.LEASE_EXPIRED,
                )

    def _close(
        self,
        lease: SessionLease,
        status: LeaseStatus,
        now: datetime,
        reason: SessionLeaseReason,
    ) -> None:
        closed = lease.model_copy(
            update={
                "status": status,
                "closed_at": now,
                "close_reason": reason,
            }
        )
        self._leases[lease.lease_id] = closed
        self._active_by_scope.pop(lease.scope.scope_digest, None)
        self._audit_entry(
            LeaseAuditAction.REVOKED
            if status is LeaseStatus.REVOKED
            else LeaseAuditAction.INVALIDATED,
            now,
            reason=reason,
            lease=closed,
        )

    def _audit_entry(
        self,
        action: LeaseAuditAction,
        occurred_at: datetime,
        *,
        reason: SessionLeaseReason | None = None,
        challenge_id: UUID | None = None,
        lease: SessionLease | None = None,
    ) -> None:
        self._audit.append(
            LeaseAuditEntry(
                sequence=len(self._audit) + 1,
                action=action,
                occurred_at=occurred_at,
                reason=reason,
                challenge_id=challenge_id,
                lease_id=None if lease is None else lease.lease_id,
                lease_identity_prefix=None
                if lease is None
                else lease.lease_identity_prefix,
            )
        )


__all__ = [
    "LeaseAuditAction",
    "LeaseAuditEntry",
    "LeaseChallenge",
    "LeaseStatus",
    "LiveSessionScope",
    "SessionLease",
    "SessionLeaseError",
    "SessionLeaseManager",
    "SessionLeaseReason",
]
