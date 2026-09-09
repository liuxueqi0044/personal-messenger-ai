from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.live_driver.session_lease import (
    LeaseStatus,
    LiveSessionScope,
    SessionLeaseError,
    SessionLeaseManager,
    SessionLeaseReason,
)

NOW = datetime(2026, 9, 9, 9, tzinfo=UTC)
HASH = "a" * 64


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def scope(**changes: object) -> LiveSessionScope:
    values = {
        "process_id": 41,
        "process_started_at": NOW - timedelta(minutes=10),
        "window_handle": 9001,
        "header_digest": HASH,
        "structure_digest": "b" * 64,
        "environment_fingerprint": "c" * 64,
        "selector_pack_version": "qq-q3-v1",
        "pending_application_digest": "d" * 64,
    }
    values.update(changes)
    return LiveSessionScope(**values)


def manager(clock: Clock, **changes: object) -> SessionLeaseManager:
    values: dict[str, object] = {"hmac_key": b"k" * 32, "clock": clock}
    values.update(changes)
    return SessionLeaseManager(**values)  # type: ignore[arg-type]


def test_hmac_key_requires_at_least_32_bytes() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        SessionLeaseManager(hmac_key=b"k" * 31)


def confirm(
    service: SessionLeaseManager,
    challenge_id: object,
    csrf_token: str,
    *,
    current: LiveSessionScope | None = None,
    confirmed: bool = True,
    idempotency_key: str = "request-1",
):
    return service.confirm(
        challenge_id=challenge_id,  # type: ignore[arg-type]
        csrf_token=csrf_token,
        idempotency_key=idempotency_key,
        confirmed=confirmed,
        confirmation_id=uuid4(),
        read_live_scope=lambda: current or scope(),
    )


def test_confirm_creates_only_read_only_non_automatic_lease() -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())

    lease = confirm(service, challenge.challenge_id, challenge.csrf_token)

    assert lease.status is LeaseStatus.ACTIVE
    assert lease.automatic_eligible is False
    assert lease.scope == scope()
    assert len(lease.lease_identity_hmac) == 64
    assert lease.expires_at == NOW + timedelta(hours=12)


def test_expired_challenge_and_replay_fail_closed() -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())
    clock.now += timedelta(minutes=5)

    with pytest.raises(SessionLeaseError) as expired:
        confirm(service, challenge.challenge_id, challenge.csrf_token)
    with pytest.raises(SessionLeaseError) as replay:
        confirm(
            service,
            challenge.challenge_id,
            challenge.csrf_token,
            idempotency_key="next",
        )

    assert expired.value.reason is SessionLeaseReason.CHALLENGE_EXPIRED
    assert replay.value.reason is SessionLeaseReason.CHALLENGE_REPLAYED


def test_bad_csrf_and_missing_confirmation_consume_challenge() -> None:
    clock = Clock()
    service = manager(clock)
    csrf_challenge = service.prepare(scope())
    with pytest.raises(SessionLeaseError) as csrf:
        confirm(service, csrf_challenge.challenge_id, "wrong-token")

    confirmation_challenge = service.prepare(scope(window_handle=9002))
    with pytest.raises(SessionLeaseError) as explicit:
        confirm(
            service,
            confirmation_challenge.challenge_id,
            confirmation_challenge.csrf_token,
            confirmed=False,
        )

    assert csrf.value.reason is SessionLeaseReason.CSRF_MISMATCH
    assert explicit.value.reason is SessionLeaseReason.CONFIRMATION_REQUIRED


@pytest.mark.parametrize(
    "changed",
    [
        {"process_id": 42},
        {"process_started_at": NOW - timedelta(minutes=9)},
        {"window_handle": 9002},
        {"header_digest": "e" * 64},
        {"structure_digest": "e" * 64},
        {"environment_fingerprint": "e" * 64},
        {"selector_pack_version": "qq-q3-v2"},
        {"pending_application_digest": "e" * 64},
    ],
)
def test_each_bound_scope_fact_must_match_at_confirmation(
    changed: dict[str, object],
) -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())

    with pytest.raises(SessionLeaseError) as exc:
        confirm(
            service,
            challenge.challenge_id,
            challenge.csrf_token,
            current=scope(**changed),
        )

    assert exc.value.reason is SessionLeaseReason.SCOPE_DRIFT


def test_validation_closes_lease_for_runtime_scope_drift_and_expiry() -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())
    lease = confirm(service, challenge.challenge_id, challenge.csrf_token)

    with pytest.raises(SessionLeaseError) as drift:
        service.validate(
            lease.lease_id, read_live_scope=lambda: scope(header_digest="f" * 64)
        )
    with pytest.raises(SessionLeaseError) as closed:
        service.validate(lease.lease_id, read_live_scope=scope)

    assert drift.value.reason is SessionLeaseReason.SCOPE_DRIFT
    assert closed.value.reason is SessionLeaseReason.LEASE_INVALIDATED

    second_challenge = service.prepare(scope(window_handle=9002))
    second = confirm(
        service,
        second_challenge.challenge_id,
        second_challenge.csrf_token,
        current=scope(window_handle=9002),
        idempotency_key="request-2",
    )
    clock.now += timedelta(hours=12)
    with pytest.raises(SessionLeaseError) as expired:
        service.validate(
            second.lease_id, read_live_scope=lambda: scope(window_handle=9002)
        )
    assert expired.value.reason is SessionLeaseReason.LEASE_EXPIRED


def test_conflict_does_not_replace_an_existing_lease() -> None:
    clock = Clock()
    service = manager(clock)
    first_challenge = service.prepare(scope())
    first = confirm(service, first_challenge.challenge_id, first_challenge.csrf_token)
    conflicting_challenge = service.prepare(scope())

    with pytest.raises(SessionLeaseError) as conflict:
        confirm(
            service,
            conflicting_challenge.challenge_id,
            conflicting_challenge.csrf_token,
            idempotency_key="request-2",
        )

    assert conflict.value.reason is SessionLeaseReason.LEASE_CONFLICT
    assert (
        service.validate(first.lease_id, read_live_scope=scope).status
        is LeaseStatus.ACTIVE
    )


def test_only_one_active_lease_can_exist_across_scopes() -> None:
    clock = Clock()
    service = manager(clock)
    first = service.prepare(scope())
    confirm(service, first.challenge_id, first.csrf_token)
    second_scope = scope(window_handle=9002)
    second = service.prepare(second_scope)

    with pytest.raises(SessionLeaseError) as conflict:
        confirm(
            service,
            second.challenge_id,
            second.csrf_token,
            current=second_scope,
            idempotency_key="request-2",
        )

    assert conflict.value.reason is SessionLeaseReason.LEASE_CONFLICT


def test_challenge_is_consumed_once_under_concurrent_replay() -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())

    def submit(index: int) -> str:
        try:
            confirm(
                service,
                challenge.challenge_id,
                challenge.csrf_token,
                idempotency_key=f"request-{index}",
            )
        except SessionLeaseError as exc:
            return exc.reason.value
        return "created"

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(submit, range(1_000)))

    assert results.count("created") == 1
    assert set(results) <= {"created", SessionLeaseReason.CHALLENGE_REPLAYED.value}


def test_revoke_is_idempotent_and_audit_contains_only_identity_prefix() -> None:
    clock = Clock()
    service = manager(clock)
    challenge = service.prepare(scope())
    lease = confirm(service, challenge.challenge_id, challenge.csrf_token)

    revoked = service.revoke(lease.lease_id)
    repeated = service.revoke(lease.lease_id)

    assert revoked.status is repeated.status is LeaseStatus.REVOKED
    audit = service.audit_entries()
    assert audit[-1].lease_identity_prefix == lease.lease_identity_prefix
