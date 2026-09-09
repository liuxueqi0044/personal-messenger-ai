from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field

from messenger_ai.domain import DomainModel, ErrorCode, Platform
from messenger_ai.execution_guard import (
    ActionPhase,
    AdapterCapabilities,
    CancellationToken,
    ExecutionGuard,
    GuardedAction,
    GuardedActionType,
    SupportLevel,
)


class ReadOnlyProbeDriver(Protocol):
    async def query_process(self, token: CancellationToken) -> dict: ...

    async def query_window(self, process: dict, token: CancellationToken) -> dict: ...

    async def inspect_uia(self, window: dict, token: CancellationToken) -> dict: ...

    async def inspect_msaa(self, window: dict, token: CancellationToken) -> dict: ...


class ReadOnlyProbeEvidence(DomainModel):
    process: dict = Field(default_factory=dict)
    window: dict = Field(default_factory=dict)
    uia: dict = Field(default_factory=dict)
    msaa: dict = Field(default_factory=dict)


class CapabilityDecision(DomainModel):
    capabilities: AdapterCapabilities
    error_code: ErrorCode
    conclusion: str
    evidence: ReadOnlyProbeEvidence
    real_send_attempted: bool = False


@dataclass(frozen=True)
class _ProbeValue:
    evidence: ReadOnlyProbeEvidence


class WechatReadOnlyCapabilityProbe:
    """Discovery-only probe; it can never promote send to supported by itself."""

    _OPERATIONS = ("process.query", "window.query", "uia.find", "msaa.read")

    def __init__(
        self,
        *,
        guard: ExecutionGuard,
        driver: ReadOnlyProbeDriver,
        capability_version: str,
        fixture_suite_version: str,
    ) -> None:
        self._guard = guard
        self._driver = driver
        self._capability_version = capability_version
        self._fixture_version = fixture_suite_version

    async def run(self) -> CapabilityDecision:
        fingerprint = await self._guard.fingerprinter.capture(Platform.WECHAT)
        provisional_version = f"{self._capability_version}:read-only-probe"
        provisional = AdapterCapabilities(
            platform=Platform.WECHAT,
            capability_version=provisional_version,
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=SupportLevel.SUPPORTED,
            confidence=0,
            fixture_suite_version=self._fixture_version,
        )
        self._guard.registry.register(provisional, fingerprint)
        try:
            result = await self._guard.run(
                GuardedAction(
                    platform=Platform.WECHAT,
                    action_type=GuardedActionType.OBSERVE,
                    phase=ActionPhase.READ,
                    requested_operations=self._OPERATIONS,
                    fallback_operations=(),
                    capability_version=provisional_version,
                    environment_fingerprint=fingerprint.digest,
                    target_process_id=fingerprint.process_id,
                    target_window_handle=fingerprint.window_handle,
                    timeout_seconds=10,
                ),
                self._probe_action,
            )
        finally:
            self._guard.registry.unregister(
                Platform.WECHAT, provisional_version, fingerprint.digest
            )

        evidence = (
            result.value.evidence
            if result.succeeded and isinstance(result.value, _ProbeValue)
            else ReadOnlyProbeEvidence()
        )
        requires_foreground = bool(
            evidence.window.get("requires_foreground")
            or evidence.uia.get("requires_foreground")
            or evidence.msaa.get("requires_foreground")
        )
        error_code = (
            ErrorCode.FOREGROUND_REQUIRED
            if requires_foreground
            else ErrorCode.CAPABILITY_UNSUPPORTED
        )
        capabilities = AdapterCapabilities(
            platform=Platform.WECHAT,
            capability_version=self._capability_version,
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=(
                SupportLevel.SUPPORTED
                if result.succeeded and bool(evidence.window.get("readable"))
                else SupportLevel.UNSUPPORTED
            ),
            resolve_background=SupportLevel.UNSUPPORTED,
            compose_background=SupportLevel.UNSUPPORTED,
            send_background=SupportLevel.UNSUPPORTED,
            verify_background=SupportLevel.UNSUPPORTED,
            requires_visible_window=requires_foreground,
            requires_unminimized=requires_foreground,
            confidence=1,
            verified_at=datetime.now(UTC),
            fixture_suite_version=self._fixture_version,
        )
        self._guard.registry.register(capabilities, fingerprint)
        return CapabilityDecision(
            capabilities=capabilities,
            error_code=error_code,
            conclusion=(
                "foreground interaction would be required; D0 send is rejected"
                if requires_foreground
                else "read-only evidence cannot prove a complete D0 send and verification path"
            ),
            evidence=evidence,
            real_send_attempted=False,
        )

    async def _probe_action(self, token: CancellationToken) -> _ProbeValue:
        token.raise_if_cancelled()
        process = await self._guard.interceptor.invoke(
            "process.query", self._driver.query_process, token
        )
        window = await self._guard.interceptor.invoke(
            "window.query", self._driver.query_window, process, token
        )
        uia = await self._guard.interceptor.invoke(
            "uia.find", self._driver.inspect_uia, window, token
        )
        msaa = await self._guard.interceptor.invoke(
            "msaa.read", self._driver.inspect_msaa, window, token
        )
        return _ProbeValue(
            ReadOnlyProbeEvidence(process=process, window=window, uia=uia, msaa=msaa)
        )
