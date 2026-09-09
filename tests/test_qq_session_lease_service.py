from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.live_driver.session_lease import (
    LiveSessionScope,
    SessionLeaseManager,
)
from messenger_ai.qq_session_lease_service import (
    QQSessionLeaseService,
    build_live_scope,
    create_production_session_lease_service,
)
from messenger_ai.webui.session_lease import SessionLeaseServiceError

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class ScopePort:
    def __init__(self, value: LiveSessionScope) -> None:
        self.value = value
        self.error: Exception | None = None

    def __call__(self) -> LiveSessionScope:
        if self.error is not None:
            raise self.error
        return self.value


def scope(**changes: object) -> LiveSessionScope:
    values = {
        "process_id": 41,
        "process_started_at": NOW - timedelta(minutes=2),
        "window_handle": 9001,
        "header_digest": "a" * 64,
        "structure_digest": "b" * 64,
        "environment_fingerprint": "c" * 64,
        "selector_pack_version": "q3-v1",
        "pending_application_digest": "d" * 64,
    }
    values.update(changes)
    return build_live_scope(**values)  # type: ignore[arg-type]


def service(clock: Clock, port: ScopePort) -> QQSessionLeaseService:
    return QQSessionLeaseService(
        manager=SessionLeaseManager(hmac_key=b"k" * 32, clock=clock),
        read_live_scope=port,
    )


def confirm(service: QQSessionLeaseService, session_id: str):
    prepared = service.prepare(session_id=session_id)
    return service.confirm(
        session_id=session_id,
        challenge_id=prepared.challenge_id,
        challenge_csrf=prepared.csrf_token,
        idempotency_key="confirm-key-1",
        confirmed=True,
    )


def test_challenge_cannot_be_stolen_by_another_webui_session() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = service(clock, port)
    prepared = subject.prepare(session_id="session-a")

    with pytest.raises(SessionLeaseServiceError) as stolen:
        subject.confirm(
            session_id="session-b",
            challenge_id=prepared.challenge_id,
            challenge_csrf=prepared.csrf_token,
            idempotency_key="other-key",
            confirmed=True,
        )

    valid = subject.confirm(
        session_id="session-a",
        challenge_id=prepared.challenge_id,
        challenge_csrf=prepared.csrf_token,
        idempotency_key="confirm-key",
        confirmed=True,
    )
    assert stolen.value.reason_code == "challenge_not_found"
    assert valid.automatic_eligible is False


def test_lease_is_private_to_its_creating_webui_session() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = service(clock, port)
    lease = confirm(subject, "session-a")

    assert subject.status(session_id="session-b").state == "unbound"
    with pytest.raises(SessionLeaseServiceError) as unauthorized:
        subject.revoke(
            session_id="session-b",
            lease_id=lease.lease_id,
            idempotency_key="revoke-key",
        )

    assert unauthorized.value.reason_code == "lease_not_found"
    assert subject.status(session_id="session-a").state == "active"


def test_status_revalidates_live_scope_and_returns_safe_drift_state() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = service(clock, port)
    lease = confirm(subject, "session-a")
    port.value = scope(header_digest="e" * 64)

    status = subject.status(session_id="session-a")

    assert status.state == "invalidated"
    assert status.lease_id == lease.lease_id
    assert status.reason_code == "live_scope_drift"
    assert status.automatic_eligible is False


def test_scope_reader_failure_permanently_invalidates_lease() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = service(clock, port)
    lease = confirm(subject, "session-a")
    port.error = RuntimeError("private detail")

    failed = subject.status(session_id="session-a")
    port.error = None
    recovered = subject.status(session_id="session-a")

    assert failed.state == "invalidated"
    assert failed.reason_code == "live_scope_unavailable"
    assert recovered.state == "invalidated"
    assert recovered.reason_code == "lease_invalidated"
    assert recovered.lease_id == lease.lease_id


def test_revoke_replay_is_idempotent_and_only_hashes_its_key() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = service(clock, port)
    lease = confirm(subject, "session-a")

    first = subject.revoke(
        session_id="session-a", lease_id=lease.lease_id, idempotency_key="revoke-key"
    )
    second = subject.revoke(
        session_id="session-a", lease_id=lease.lease_id, idempotency_key="revoke-key"
    )

    assert first == second
    assert first.state == "revoked"
    assert "revoke-key" not in repr(subject.__dict__)


def test_confirm_result_and_audit_expose_only_anonymous_identity_prefix() -> None:
    clock = Clock()
    port = ScopePort(scope())
    manager = SessionLeaseManager(hmac_key=b"k" * 32, clock=clock)
    subject = QQSessionLeaseService(manager=manager, read_live_scope=port)

    result = confirm(subject, "session-a")

    assert not hasattr(result, "lease_identity_hmac")
    assert len(result.lease_identity_prefix) == 12
    assert (
        manager.audit_entries()[-1].lease_identity_prefix
        == result.lease_identity_prefix
    )


def test_production_factory_and_build_scope_are_pure_dependency_injection() -> None:
    clock = Clock()
    port = ScopePort(scope())
    subject = create_production_session_lease_service(
        dpapi_hmac_key=b"p" * 32,
        read_live_scope=port,
        clock=clock,
    )

    prepared = subject.prepare(session_id="session-a")

    assert prepared.scope_summary == "当前可见 QQ 会话"
    assert prepared.expires_at == NOW + timedelta(minutes=5)
