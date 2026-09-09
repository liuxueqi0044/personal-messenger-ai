from __future__ import annotations

from messenger_ai.observability import (
    AutomationLevel,
    ClientEnvironmentFingerprint,
    FixtureAttestation,
    FixtureKind,
    VersionController,
)


def fingerprint(*, client_version: str = "9.9.26") -> ClientEnvironmentFingerprint:
    return ClientEnvironmentFingerprint(
        platform="qq",
        client_version=client_version,
        client_file_signature="a" * 64,
        windows_version="Windows 11 24H2",
        dpi_scale=1.25,
        theme="light",
        window_mode="normal",
        window_fingerprint="b" * 64,
        adapter_version="m3-v1",
    )


def attestation(clock, kind, **changes) -> FixtureAttestation:
    values = {
        "kind": kind,
        "fixture_suite_version": "suite-v1",
        "passed": True,
        "sample_count": 500 if kind is FixtureKind.READ_ONLY else 100,
        "dedicated_test_account": kind is not FixtureKind.READ_ONLY,
        "verified_at": clock.now(),
    }
    values.update(changes)
    return FixtureAttestation(**values)


def test_unknown_environment_is_immediately_quarantined_at_l0(tmp_path, clock):
    controller = VersionController(tmp_path / "matrix.json", clock.now)
    decision = controller.observe(fingerprint())
    assert decision.changed
    assert decision.maximum_level is AutomationLevel.L0_OBSERVE_DISABLED
    assert decision.send_quarantined
    assert decision.reason_code == "UNKNOWN_FINGERPRINT"


def test_read_then_dedicated_send_then_soak_are_required_in_order(tmp_path, clock):
    controller = VersionController(tmp_path / "matrix.json", clock.now)
    current = fingerprint()
    controller.observe(current)
    read = controller.attest(current, attestation(clock, FixtureKind.READ_ONLY))
    send = controller.attest(
        current, attestation(clock, FixtureKind.DEDICATED_TEST_SEND)
    )
    soak = controller.attest(
        current,
        attestation(clock, FixtureKind.SOAK, sample_count=1, soak_days=7),
    )
    assert read.maximum_level is AutomationLevel.L1_DRAFT_ONLY
    assert read.send_quarantined
    assert send.maximum_level is AutomationLevel.L2_HUMAN_APPROVED
    assert not send.send_quarantined
    assert soak.maximum_level is AutomationLevel.L3_WHITELIST_AUTO


def test_logged_in_main_account_is_not_a_test_account_attestation(tmp_path, clock):
    controller = VersionController(tmp_path / "matrix.json", clock.now)
    current = fingerprint()
    controller.observe(current)
    controller.attest(current, attestation(clock, FixtureKind.READ_ONLY))
    rejected = controller.attest(
        current,
        attestation(
            clock,
            FixtureKind.DEDICATED_TEST_SEND,
            dedicated_test_account=False,
            sample_count=1000,
        ),
    )
    assert rejected.maximum_level is AutomationLevel.L1_DRAFT_ONLY
    assert rejected.send_quarantined
    assert rejected.reason_code == "TEST_ACCOUNT_SEND_REJECTED"


def test_any_send_fixture_safety_failure_prevents_promotion(tmp_path, clock):
    controller = VersionController(tmp_path / "matrix.json", clock.now)
    current = fingerprint()
    controller.observe(current)
    controller.attest(current, attestation(clock, FixtureKind.READ_ONLY))
    rejected = controller.attest(
        current,
        attestation(
            clock,
            FixtureKind.DEDICATED_TEST_SEND,
            wrong_recipient_count=1,
        ),
    )
    assert rejected.maximum_level is AutomationLevel.L1_DRAFT_ONLY
    assert rejected.send_quarantined


def test_client_version_change_does_not_inherit_old_capabilities(tmp_path, clock):
    matrix = tmp_path / "matrix.json"
    controller = VersionController(matrix, clock.now)
    old = fingerprint()
    controller.observe(old)
    controller.attest(old, attestation(clock, FixtureKind.READ_ONLY))
    controller.attest(old, attestation(clock, FixtureKind.DEDICATED_TEST_SEND))
    assert controller.current("qq").maximum_level is AutomationLevel.L2_HUMAN_APPROVED

    new = fingerprint(client_version="9.9.27")
    changed = controller.observe(new)
    assert changed.maximum_level is AutomationLevel.L0_OBSERVE_DISABLED
    assert changed.send_quarantined
    reloaded = VersionController(matrix, clock.now)
    assert reloaded.current("qq").fingerprint_hash == new.digest
    assert len(reloaded.records()) == 2
