from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

import pytest

from messenger_ai.domain import Platform
from messenger_ai.execution_guard import (
    ActionInterceptor,
    ActionPhase,
    AdapterCapabilities,
    CapabilityRegistry,
    CircuitBreaker,
    DesktopState,
    EmergencyStop,
    EnvironmentFingerprint,
    EnvironmentFingerprinter,
    ExecutionGuard,
    GuardedAction,
    GuardedActionType,
    PlatformMutex,
    SnapshotContentionMonitor,
    SupportLevel,
)


class FakeEnvironmentSource:
    def __init__(self) -> None:
        self.values = {
            Platform.QQ: self._values("9.9.26.44343", 101, 1001),
            Platform.WECHAT: self._values("4.1.12.55", 202, 2002),
        }

    @staticmethod
    def _values(client_version: str, process_id: int, window_handle: int) -> dict:
        return {
            "client_version": client_version,
            "windows_version": "test-windows",
            "dpi_scale": 1.25,
            "theme": "light",
            "window_mode": "normal",
            "window_signature": f"window-{window_handle}",
            "process_signature": f"process-{process_id}",
            "process_id": process_id,
            "window_handle": window_handle,
        }

    def read_environment(self, platform: Platform) -> dict:
        return dict(self.values[platform])


class FakeDesktopReader:
    def __init__(self) -> None:
        self.state = DesktopState(
            foreground_window=777,
            keyboard_focus=778,
            pointer_position=(10, 20),
            clipboard_revision=1,
            window_state_digest="stable",
        )

    def read_state(self) -> DesktopState:
        return self.state.model_copy(deep=True)


@dataclass
class GuardHarness:
    guard: ExecutionGuard
    environments: FakeEnvironmentSource
    desktop: FakeDesktopReader
    fingerprints: dict[Platform, EnvironmentFingerprint]
    version: str = "cap-v1"

    def request(
        self,
        platform: Platform = Platform.QQ,
        *,
        action_type: GuardedActionType = GuardedActionType.OBSERVE,
        phase: ActionPhase = ActionPhase.READ,
        operations: tuple[str, ...] = ("uia.read",),
        fallback_operations: tuple[str, ...] = (),
        timeout: float = 1.0,
        capability_version: str | None = None,
    ) -> GuardedAction:
        fingerprint = self.fingerprints[platform]
        return GuardedAction(
            platform=platform,
            action_type=action_type,
            phase=phase,
            requested_operations=operations,
            fallback_operations=fallback_operations,
            capability_version=capability_version or self.version,
            environment_fingerprint=fingerprint.digest,
            target_process_id=fingerprint.process_id,
            target_window_handle=fingerprint.window_handle,
            timeout_seconds=timeout,
        )


def pytest_pyfunc_call(pyfuncitem):
    """Keep M2 tests runnable even when the optional pytest-asyncio extra is absent."""
    if inspect.iscoroutinefunction(
        pyfuncitem.obj
    ) and not pyfuncitem.config.pluginmanager.hasplugin("asyncio"):
        kwargs = {
            name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
    return None


@pytest.fixture
def harness() -> GuardHarness:
    environments = FakeEnvironmentSource()
    fingerprinter = EnvironmentFingerprinter(environments)
    desktop = FakeDesktopReader()
    registry = CapabilityRegistry()
    fingerprints = {
        platform: EnvironmentFingerprint(
            platform=platform, **environments.values[platform]
        )
        for platform in (Platform.QQ, Platform.WECHAT)
    }
    for platform, fingerprint in fingerprints.items():
        registry.register(
            AdapterCapabilities(
                platform=platform,
                capability_version="cap-v1",
                client_version=fingerprint.client_version,
                environment_fingerprint=fingerprint.digest,
                observe_background=SupportLevel.SUPPORTED,
                resolve_background=SupportLevel.SUPPORTED,
                compose_background=SupportLevel.SUPPORTED,
                send_background=SupportLevel.SUPPORTED,
                verify_background=SupportLevel.SUPPORTED,
                confidence=1,
                fixture_suite_version="fixture-v1",
            ),
            fingerprint,
        )
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=fingerprinter,
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(desktop),
        platform_mutex=PlatformMutex(),
        circuit_breaker=CircuitBreaker(failure_threshold=3),
        emergency_stop=EmergencyStop(),
        cancellation_grace_seconds=0.2,
    )
    return GuardHarness(guard, environments, desktop, fingerprints)
