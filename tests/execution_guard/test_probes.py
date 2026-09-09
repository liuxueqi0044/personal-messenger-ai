from __future__ import annotations

from messenger_ai.domain import Platform
from messenger_ai.execution_guard import (
    ActionPhase,
    GuardedActionType,
    ProbeDefinition,
    ProbeRunner,
    SupportLevel,
)


async def test_probe_runner_registers_versioned_exact_environment_report(
    harness,
) -> None:
    async def success(token):
        return "evidence"

    operations = {
        GuardedActionType.OBSERVE: (ActionPhase.READ, "uia.read"),
        GuardedActionType.RESOLVE: (ActionPhase.READ, "uia.find"),
        GuardedActionType.COMPOSE: (ActionPhase.PREPARE, "uia.value_pattern.set"),
        GuardedActionType.SEND: (ActionPhase.COMMIT, "uia.invoke_pattern.invoke"),
        GuardedActionType.VERIFY: (ActionPhase.VERIFY, "uia.text_pattern.get"),
    }
    probes = [
        ProbeDefinition(
            action_type=kind,
            phase=phase,
            requested_operations=(operation,),
            action=success,
        )
        for kind, (phase, operation) in operations.items()
    ]
    report = await ProbeRunner(harness.guard).run(
        Platform.QQ, "probe-report-v2", "fixture-v2", probes
    )
    assert report.capabilities.confidence == 1
    assert report.capabilities.send_background is SupportLevel.SUPPORTED
    fingerprint = harness.fingerprints[Platform.QQ]
    assert (
        harness.guard.registry.get(Platform.QQ, "probe-report-v2", fingerprint.digest)
        == report.capabilities
    )


async def test_failed_probe_never_claims_supported(harness) -> None:
    async def failure(token):
        raise RuntimeError("probe failed")

    report = await ProbeRunner(harness.guard).run(
        Platform.QQ,
        "failed-probe-v1",
        "fixture-v1",
        [
            ProbeDefinition(
                action_type=GuardedActionType.SEND,
                phase=ActionPhase.COMMIT,
                requested_operations=("uia.invoke_pattern.invoke",),
                action=failure,
            )
        ],
    )
    assert report.capabilities.send_background is SupportLevel.UNSUPPORTED
    assert report.capabilities.confidence == 0
