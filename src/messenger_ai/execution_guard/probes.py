from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from pydantic.config import ConfigDict

from messenger_ai.domain import DomainModel, Platform

from .guard import ExecutionGuard
from .models import (
    ActionPhase,
    AdapterCapabilities,
    GuardedAction,
    GuardedActionType,
    GuardedResult,
    SupportLevel,
)


class ProbeDefinition(DomainModel):
    action_type: GuardedActionType
    phase: ActionPhase
    requested_operations: tuple[str, ...]
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    action: Any

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class ProbeReport(DomainModel):
    capabilities: AdapterCapabilities
    results: dict[GuardedActionType, GuardedResult]


class ProbeRunner:
    """Runs exact-environment probes through the same production guard path."""

    def __init__(self, guard: ExecutionGuard) -> None:
        self.guard = guard

    async def run(
        self,
        platform: Platform,
        capability_version: str,
        fixture_suite_version: str,
        probes: Sequence[ProbeDefinition],
    ) -> ProbeReport:
        fingerprint = await self.guard.fingerprinter.capture(platform)
        probe_version = f"__probe__:{uuid4()}"
        provisional = AdapterCapabilities(
            platform=platform,
            capability_version=probe_version,
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            resolve_background=SupportLevel.SUPPORTED,
            compose_background=SupportLevel.SUPPORTED,
            send_background=SupportLevel.SUPPORTED,
            verify_background=SupportLevel.SUPPORTED,
            confidence=0,
            fixture_suite_version=fixture_suite_version,
        )
        self.guard.registry.register(provisional, fingerprint)
        results: dict[GuardedActionType, GuardedResult] = {}
        try:
            for probe in probes:
                request = GuardedAction(
                    platform=platform,
                    action_type=probe.action_type,
                    phase=probe.phase,
                    requested_operations=probe.requested_operations,
                    capability_version=probe_version,
                    environment_fingerprint=fingerprint.digest,
                    target_process_id=fingerprint.process_id,
                    target_window_handle=fingerprint.window_handle,
                    timeout_seconds=probe.timeout_seconds,
                )
                results[probe.action_type] = await self.guard.run(request, probe.action)
                if self.guard.stop.active or await self.guard.circuit_breaker.is_open(
                    platform
                ):
                    break
        finally:
            self.guard.registry.unregister(platform, probe_version, fingerprint.digest)

        levels = {
            kind: (
                SupportLevel.SUPPORTED
                if kind in results and results[kind].succeeded
                else SupportLevel.UNSUPPORTED
            )
            for kind in GuardedActionType
        }
        probed_kinds = {probe.action_type for probe in probes}
        confidence = (
            sum(levels[kind] is SupportLevel.SUPPORTED for kind in probed_kinds)
            / len(probed_kinds)
            if probed_kinds
            else 0.0
        )
        capabilities = AdapterCapabilities(
            platform=platform,
            capability_version=capability_version,
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=levels[GuardedActionType.OBSERVE],
            resolve_background=levels[GuardedActionType.RESOLVE],
            compose_background=levels[GuardedActionType.COMPOSE],
            send_background=levels[GuardedActionType.SEND],
            verify_background=levels[GuardedActionType.VERIFY],
            confidence=confidence,
            verified_at=datetime.now(UTC),
            fixture_suite_version=fixture_suite_version,
        )
        self.guard.registry.register(capabilities, fingerprint)
        if all(result.succeeded for result in results.values()) and len(results) == len(
            probes
        ):
            await self.guard.circuit_breaker.reset(platform)
        return ProbeReport(capabilities=capabilities, results=results)
