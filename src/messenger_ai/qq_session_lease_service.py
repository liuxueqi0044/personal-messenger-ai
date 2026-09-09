"""Production composition for the fail-closed QQ session-lease core.

The adapter only associates opaque WebUI sessions with core challenges and
leases.  It deliberately contains no Windows automation, subprocess, logging,
or persistence code.  A caller supplies the read-only live-scope reader and a
DPAPI-unprotected HMAC key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.live_driver.session_lease import (
    LiveSessionScope,
    SessionLeaseError,
    SessionLeaseManager,
    SessionLeaseReason,
)
from messenger_ai.webui.session_lease import (
    ConfirmResult,
    PrepareResult,
    RevokeResult,
    SessionLeaseService,
    SessionLeaseServiceError,
    StatusResult,
)

_SCOPE_UNAVAILABLE = "live_scope_unavailable"
_UNBOUND = "unbound"
_INVALIDATED = "invalidated"
_EXPIRED = "expired"
_REVOKED = "revoked"

_CORE_REASON_CODES = {
    SessionLeaseReason.CHALLENGE_NOT_FOUND: "challenge_not_found",
    SessionLeaseReason.CHALLENGE_EXPIRED: "challenge_expired",
    SessionLeaseReason.CHALLENGE_REPLAYED: "challenge_replayed",
    SessionLeaseReason.CSRF_MISMATCH: "csrf_mismatch",
    SessionLeaseReason.IDEMPOTENCY_REPLAYED: "idempotency_replayed",
    SessionLeaseReason.CONFIRMATION_REQUIRED: "explicit_confirmation_required",
    SessionLeaseReason.SCOPE_DRIFT: "live_scope_drift",
    SessionLeaseReason.LEASE_CONFLICT: "lease_conflict",
    SessionLeaseReason.LEASE_NOT_FOUND: "lease_not_found",
    SessionLeaseReason.LEASE_EXPIRED: "lease_expired",
    SessionLeaseReason.LEASE_REVOKED: "lease_revoked",
    SessionLeaseReason.LEASE_INVALIDATED: "lease_invalidated",
    SessionLeaseReason.SCOPE_UNAVAILABLE: "live_scope_unavailable",
}


def build_live_scope(
    *,
    process_id: int,
    process_started_at: datetime,
    window_handle: int,
    header_digest: str,
    structure_digest: str,
    environment_fingerprint: str,
    selector_pack_version: str,
    pending_application_digest: str,
) -> LiveSessionScope:
    """Construct the dependency-injected Q0/Q1 snapshot used by this service.

    The caller is responsible for collecting these data using its approved
    read-only implementation.  This helper never discovers a window, drives
    QQ, starts a process, or invokes a command.
    """

    return LiveSessionScope(
        process_id=process_id,
        process_started_at=process_started_at,
        window_handle=window_handle,
        header_digest=header_digest,
        structure_digest=structure_digest,
        environment_fingerprint=environment_fingerprint,
        selector_pack_version=selector_pack_version,
        pending_application_digest=pending_application_digest,
    )


class QQSessionLeaseService(SessionLeaseService):
    """Thread-safe WebUI Protocol implementation over ``SessionLeaseManager``."""

    def __init__(
        self,
        *,
        manager: SessionLeaseManager,
        read_live_scope: Callable[[], LiveSessionScope],
    ) -> None:
        self._manager = manager
        self._read_live_scope = read_live_scope
        self._challenge_sessions: dict[UUID, str] = {}
        self._lease_sessions: dict[UUID, str] = {}
        self._revoke_results: dict[tuple[str, UUID, str], RevokeResult] = {}
        self._lock = RLock()

    def prepare(self, *, session_id: str) -> PrepareResult:
        self._require_session_id(session_id)
        scope = self._scope_or_error()
        try:
            challenge = self._manager.prepare(scope)
        except Exception as exc:  # noqa: BLE001 - no core detail crosses the UI boundary
            raise self._service_error(exc) from None
        with self._lock:
            self._challenge_sessions[challenge.challenge_id] = session_id
        return PrepareResult(
            challenge_id=str(challenge.challenge_id),
            csrf_token=challenge.csrf_token,
            expires_at=challenge.expires_at,
        )

    def confirm(
        self,
        *,
        session_id: str,
        challenge_id: str,
        challenge_csrf: str,
        idempotency_key: str,
        confirmed: bool,
    ) -> ConfirmResult:
        self._require_session_id(session_id)
        parsed_challenge_id = self._parse_uuid(challenge_id, "challenge_not_found")
        with self._lock:
            if self._challenge_sessions.get(parsed_challenge_id) != session_id:
                raise SessionLeaseServiceError("challenge_not_found")
            try:
                lease = self._manager.confirm(
                    challenge_id=parsed_challenge_id,
                    csrf_token=challenge_csrf,
                    idempotency_key=idempotency_key,
                    confirmed=confirmed,
                    confirmation_id=uuid4(),
                    read_live_scope=self._read_live_scope,
                )
            except Exception as exc:  # noqa: BLE001 - do not expose core or scope errors
                # Core confirmation attempts are one-time; do not leave a stale
                # WebUI ownership record after the accepted session uses one.
                self._challenge_sessions.pop(parsed_challenge_id, None)
                raise self._service_error(exc) from None
            self._challenge_sessions.pop(parsed_challenge_id, None)
            self._lease_sessions[lease.lease_id] = session_id
        return ConfirmResult(
            lease_id=str(lease.lease_id),
            expires_at=lease.expires_at,
            lease_identity_prefix=lease.lease_identity_prefix,
            automatic_eligible=False,
        )

    def status(self, *, session_id: str) -> StatusResult:
        self._require_session_id(session_id)
        with self._lock:
            lease_id = self._lease_for_session(session_id)
            if lease_id is None:
                return StatusResult(state=_UNBOUND, automatic_eligible=False)
            try:
                lease = self._manager.validate(
                    lease_id,
                    read_live_scope=self._read_live_scope,
                )
            except SessionLeaseError as exc:
                return self._safe_status(lease_id, exc.reason)
            except Exception:  # noqa: BLE001 - safe status must not leak port errors
                self._manager.invalidate(lease_id, SessionLeaseReason.SCOPE_UNAVAILABLE)
                return StatusResult(
                    state=_INVALIDATED,
                    lease_id=str(lease_id),
                    automatic_eligible=False,
                    reason_code=_SCOPE_UNAVAILABLE,
                )
        return StatusResult(
            state=lease.status.value,
            lease_id=str(lease.lease_id),
            expires_at=lease.expires_at,
            automatic_eligible=False,
        )

    def revoke(
        self, *, session_id: str, lease_id: str, idempotency_key: str
    ) -> RevokeResult:
        self._require_session_id(session_id)
        parsed_lease_id = self._parse_uuid(lease_id, "lease_not_found")
        idempotency_digest = self._idempotency_digest(idempotency_key)
        key = (session_id, parsed_lease_id, idempotency_digest)
        with self._lock:
            if self._lease_sessions.get(parsed_lease_id) != session_id:
                raise SessionLeaseServiceError("lease_not_found")
            prior = self._revoke_results.get(key)
            if prior is not None:
                return prior
            try:
                lease = self._manager.revoke(parsed_lease_id)
            except Exception as exc:  # noqa: BLE001 - fixed code facade
                raise self._service_error(exc) from None
            result = RevokeResult(
                lease_id=str(lease.lease_id), state=lease.status.value
            )
            self._revoke_results[key] = result
            return result

    @staticmethod
    def _require_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise SessionLeaseServiceError("invalid_session")

    def _scope_or_error(self) -> LiveSessionScope:
        try:
            scope = self._read_live_scope()
        except Exception:  # noqa: BLE001 - read-only port failures are opaque
            raise SessionLeaseServiceError(_SCOPE_UNAVAILABLE) from None
        if not isinstance(scope, LiveSessionScope):
            raise SessionLeaseServiceError(_SCOPE_UNAVAILABLE)
        return scope

    @staticmethod
    def _parse_uuid(value: str, reason_code: str) -> UUID:
        try:
            return UUID(value)
        except (TypeError, ValueError):
            raise SessionLeaseServiceError(reason_code) from None

    @staticmethod
    def _idempotency_digest(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise SessionLeaseServiceError("idempotency_key_required")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _lease_for_session(self, session_id: str) -> UUID | None:
        for lease_id, owner in reversed(tuple(self._lease_sessions.items())):
            if owner == session_id:
                return lease_id
        return None

    @staticmethod
    def _service_error(exc: Exception) -> SessionLeaseServiceError:
        if isinstance(exc, SessionLeaseServiceError):
            return exc
        if isinstance(exc, SessionLeaseError):
            return SessionLeaseServiceError(_CORE_REASON_CODES[exc.reason])
        return SessionLeaseServiceError(_SCOPE_UNAVAILABLE)

    @staticmethod
    def _safe_status(lease_id: UUID, reason: SessionLeaseReason) -> StatusResult:
        if reason is SessionLeaseReason.LEASE_EXPIRED:
            state = _EXPIRED
        elif reason is SessionLeaseReason.LEASE_REVOKED:
            state = _REVOKED
        else:
            state = _INVALIDATED
        return StatusResult(
            state=state,
            lease_id=str(lease_id),
            automatic_eligible=False,
            reason_code=_CORE_REASON_CODES[reason],
        )


def create_production_session_lease_service(
    *,
    dpapi_hmac_key: bytes,
    read_live_scope: Callable[[], LiveSessionScope],
    clock: Callable[[], datetime] | None = None,
    challenge_ttl: timedelta = timedelta(minutes=5),
    lease_ttl: timedelta = timedelta(hours=12),
) -> QQSessionLeaseService:
    """Build the service from a key unprotected by the caller's user DPAPI.

    DPAPI access deliberately stays outside this module so this composition is
    testable and cannot silently change Windows credential or desktop state.
    """

    return QQSessionLeaseService(
        manager=SessionLeaseManager(
            hmac_key=dpapi_hmac_key,
            clock=clock,
            challenge_ttl=challenge_ttl,
            lease_ttl=lease_ttl,
        ),
        read_live_scope=read_live_scope,
    )


__all__ = [
    "QQSessionLeaseService",
    "build_live_scope",
    "create_production_session_lease_service",
]
