from __future__ import annotations

import pytest

from messenger_ai.domain import ErrorCode, Platform
from messenger_ai.execution_guard import (
    AdapterCapabilities,
    CapabilityRegistry,
    GuardErrorCode,
    GuardResultStatus,
    SupportLevel,
)


async def test_exact_capability_version_is_required(harness) -> None:
    result = await harness.guard.run(
        harness.request(capability_version="stale-cap-v0"), lambda token: "must-not-run"
    )
    assert result.status is GuardResultStatus.REJECTED
    assert result.error_code is GuardErrorCode.CAPABILITY_VERSION_MISMATCH


async def test_environment_change_rejects_and_quarantines(harness) -> None:
    harness.environments.values[Platform.QQ]["theme"] = "dark"
    result = await harness.guard.run(harness.request(), lambda token: "must-not-run")
    assert result.status is GuardResultStatus.REJECTED
    assert result.error_code is GuardErrorCode.ENVIRONMENT_FINGERPRINT_MISMATCH

    second = await harness.guard.run(harness.request(), lambda token: "must-not-run")
    assert second.status is GuardResultStatus.QUARANTINED
    assert second.error_code is ErrorCode.ADAPTER_QUARANTINED


def test_registry_rejects_cross_environment_registration(harness) -> None:
    fingerprint = harness.fingerprints[Platform.QQ]
    capability = AdapterCapabilities(
        platform=Platform.QQ,
        capability_version="bad",
        client_version=fingerprint.client_version,
        environment_fingerprint="0" * 64,
        observe_background=SupportLevel.SUPPORTED,
        confidence=1,
        fixture_suite_version="fixture-v1",
    )
    with pytest.raises(ValueError, match="does not match"):
        CapabilityRegistry().register(capability, fingerprint)
