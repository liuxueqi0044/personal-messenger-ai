from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from typing import Any, Protocol

from messenger_ai.domain import Platform

from .models import AdapterCapabilities, EnvironmentFingerprint


class EnvironmentSource(Protocol):
    def read_environment(
        self, platform: Platform
    ) -> (
        Mapping[str, Any]
        | EnvironmentFingerprint
        | Awaitable[Mapping[str, Any] | EnvironmentFingerprint]
    ): ...


class EnvironmentFingerprinter:
    """Builds stable fingerprints without owning any OS access implementation."""

    def __init__(self, source: EnvironmentSource) -> None:
        self._source = source

    async def capture(self, platform: Platform) -> EnvironmentFingerprint:
        raw = self._source.read_environment(platform)
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, EnvironmentFingerprint):
            fingerprint = raw
        else:
            fingerprint = EnvironmentFingerprint(platform=platform, **dict(raw))
        if fingerprint.platform is not platform:
            raise ValueError("environment source returned the wrong platform")
        return fingerprint


class CapabilityRegistry:
    """Exact-match registry. Capabilities are never inferred across environments."""

    def __init__(self) -> None:
        self._records: dict[tuple[Platform, str, str], AdapterCapabilities] = {}

    def register(
        self,
        capabilities: AdapterCapabilities,
        fingerprint: EnvironmentFingerprint | None = None,
    ) -> None:
        if fingerprint is not None:
            if fingerprint.platform is not capabilities.platform:
                raise ValueError("capability and fingerprint platforms differ")
            if fingerprint.client_version != capabilities.client_version:
                raise ValueError("capability and fingerprint client versions differ")
            if fingerprint.digest != capabilities.environment_fingerprint:
                raise ValueError(
                    "capability does not match the environment fingerprint"
                )
        key = (
            capabilities.platform,
            capabilities.capability_version,
            capabilities.environment_fingerprint,
        )
        self._records[key] = capabilities

    def get(
        self, platform: Platform, capability_version: str, environment_fingerprint: str
    ) -> AdapterCapabilities | None:
        return self._records.get(
            (platform, capability_version, environment_fingerprint)
        )

    def unregister(
        self, platform: Platform, capability_version: str, environment_fingerprint: str
    ) -> None:
        self._records.pop((platform, capability_version, environment_fingerprint), None)

    def all_for(self, platform: Platform) -> tuple[AdapterCapabilities, ...]:
        return tuple(
            record
            for (candidate, _, _), record in self._records.items()
            if candidate is platform
        )
