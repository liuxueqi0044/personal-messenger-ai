from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from messenger_ai.domain import ErrorCode, Platform

from .capabilities import CapabilityRegistry, EnvironmentFingerprinter
from .contention import ContentionMonitor
from .controls import CancellationToken, CircuitBreaker, EmergencyStop, PlatformMutex
from .interceptor import ActionInterceptor, ActionNotAllowed
from .models import (
    ActionPhase,
    AdapterCapabilities,
    GuardDecision,
    GuardedAction,
    GuardedResult,
    GuardErrorCode,
    GuardResultStatus,
    SupportLevel,
)

GuardedCallable = Callable[[CancellationToken], Awaitable[Any] | Any]


class ExecutionGuard:
    def __init__(
        self,
        registry: CapabilityRegistry,
        fingerprinter: EnvironmentFingerprinter,
        interceptor: ActionInterceptor,
        contention_monitor: ContentionMonitor,
        platform_mutex: PlatformMutex | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        emergency_stop: EmergencyStop | None = None,
        cancellation_grace_seconds: float = 0.5,
    ) -> None:
        if not 0 < cancellation_grace_seconds < 1:
            raise ValueError("cancellation grace must be positive and below one second")
        self.registry = registry
        self.fingerprinter = fingerprinter
        self.interceptor = interceptor
        self.contention_monitor = contention_monitor
        self.platform_mutex = platform_mutex or PlatformMutex()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.stop = emergency_stop or EmergencyStop()
        self.cancellation_grace_seconds = cancellation_grace_seconds

    async def capabilities(
        self,
        platform: Platform,
        capability_version: str,
        environment_fingerprint: str,
    ) -> AdapterCapabilities | None:
        return self.registry.get(platform, capability_version, environment_fingerprint)

    async def check(self, request: GuardedAction) -> GuardDecision:
        if self.stop.active:
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.EMERGENCY_STOPPED,
                reason=self.stop.reason or "emergency stop is active",
            )
        if await self.circuit_breaker.is_open(request.platform):
            snapshot = await self.circuit_breaker.snapshot(request.platform)
            return GuardDecision(
                allowed=False,
                error_code=ErrorCode.ADAPTER_QUARANTINED,
                reason=snapshot.reason or "adapter is quarantined",
            )
        try:
            self.interceptor.validate(
                request.requested_operations, request.fallback_operations
            )
        except ActionNotAllowed as exc:
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.ACTION_NOT_ALLOWED,
                reason=str(exc),
            )

        try:
            current = await self.fingerprinter.capture(request.platform)
        except Exception as exc:  # noqa: BLE001 - an OS probe must fail closed
            await self.circuit_breaker.trip(
                request.platform,
                f"environment fingerprint unavailable: {type(exc).__name__}",
            )
            return GuardDecision(
                allowed=False,
                error_code=ErrorCode.ADAPTER_QUARANTINED,
                reason="environment fingerprint could not be verified",
            )
        if current.digest != request.environment_fingerprint:
            await self.circuit_breaker.trip(
                request.platform,
                "environment fingerprint changed during guarded execution",
            )
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.ENVIRONMENT_FINGERPRINT_MISMATCH,
                reason="current environment is not the probed environment",
            )
        if (
            request.target_process_id is not None
            and current.process_id != request.target_process_id
        ):
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.TARGET_MISMATCH,
                reason="target process does not match the environment fingerprint",
            )
        if (
            request.target_window_handle is not None
            and current.window_handle != request.target_window_handle
        ):
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.TARGET_MISMATCH,
                reason="target window does not match the environment fingerprint",
            )

        capabilities = self.registry.get(
            request.platform,
            request.capability_version,
            request.environment_fingerprint,
        )
        if capabilities is None:
            same_environment = any(
                record.environment_fingerprint == request.environment_fingerprint
                for record in self.registry.all_for(request.platform)
            )
            code = (
                GuardErrorCode.CAPABILITY_VERSION_MISMATCH
                if same_environment
                else ErrorCode.CAPABILITY_UNSUPPORTED
            )
            return GuardDecision(
                allowed=False,
                error_code=code,
                reason="no exact capability version and environment match",
            )
        if capabilities.client_version != current.client_version:
            await self.circuit_breaker.trip(request.platform, "client version changed")
            return GuardDecision(
                allowed=False,
                error_code=GuardErrorCode.ENVIRONMENT_FINGERPRINT_MISMATCH,
                reason="client version differs from the capability report",
            )
        if capabilities.level_for(request.action_type) is not SupportLevel.SUPPORTED:
            return GuardDecision(
                allowed=False,
                error_code=ErrorCode.CAPABILITY_UNSUPPORTED,
                reason=f"{request.action_type.value} is not fully supported",
                capability_hash=capabilities.capability_hash,
            )
        return GuardDecision(
            allowed=True,
            reason="exact capability and environment match",
            capability_hash=capabilities.capability_hash,
        )

    async def quarantine(self, platform: Platform, reason: str) -> None:
        await self.circuit_breaker.trip(platform, reason)

    async def release_quarantine(self, platform: Platform) -> None:
        """Explicit recovery hook; the Hub must place human audit around its use."""
        await self.circuit_breaker.reset(platform)

    async def emergency_stop(self, reason: str = "emergency stop") -> None:
        self.stop.trigger(reason)
        # The synchronous event set above is the authoritative sub-one-second barrier.
        await asyncio.sleep(0)

    async def run(
        self, request: GuardedAction, action: GuardedCallable
    ) -> GuardedResult:
        started = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds

        decision = await self.check(request)
        if not decision.allowed:
            return self._rejected(request, decision, started)

        try:
            acquired = await self._acquire_or_stop(
                request, max(0, deadline - loop.time())
            )
        except asyncio.CancelledError:
            return GuardedResult(
                action_id=request.action_id,
                status=GuardResultStatus.CANCELLED,
                error_code=GuardErrorCode.ACTION_CANCELLED,
                reason="guard caller cancelled before the action started",
                started_at=started,
            )
        if acquired is not None:
            return GuardedResult(
                action_id=request.action_id,
                status=acquired[0],
                error_code=acquired[1],
                reason=acquired[2],
                started_at=started,
            )

        task: asyncio.Task[Any] | None = None
        stop_task: asyncio.Task[Any] | None = None
        token: CancellationToken | None = None
        try:
            # Close the wait-for-lock TOCTOU window.
            decision = await self.check(request)
            if not decision.allowed:
                return self._rejected(request, decision, started)

            before = await self.contention_monitor.snapshot()
            token = self.stop.token()
            task = asyncio.create_task(self._invoke(action, token))
            stop_task = asyncio.create_task(self.stop.wait())
            remaining = max(0, deadline - loop.time())
            done, _ = await asyncio.wait(
                (task, stop_task),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if task in done:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                try:
                    value = task.result()
                except asyncio.CancelledError:
                    return await self._cancelled_result(request, started, before)
                except Exception as exc:  # noqa: BLE001 - isolate arbitrary adapter failures
                    await self.circuit_breaker.record_failure(
                        request.platform, type(exc).__name__
                    )
                    after = await self.contention_monitor.snapshot()
                    events = self.contention_monitor.compare(before, after)
                    if events:
                        await self.circuit_breaker.trip(
                            request.platform, "desktop contention detected"
                        )
                    return GuardedResult(
                        action_id=request.action_id,
                        status=GuardResultStatus.CONTENDED
                        if events
                        else GuardResultStatus.FAILED,
                        error_code=(
                            GuardErrorCode.DESKTOP_CONTENTION
                            if events
                            else (
                                ErrorCode.SEND_UNCERTAIN
                                if request.phase is ActionPhase.COMMIT
                                else ErrorCode.FAILED_SAFE
                            )
                        ),
                        reason=f"guarded action failed: {type(exc).__name__}",
                        started_at=started,
                        contention_events=events,
                    )

                after = await self.contention_monitor.snapshot()
                events = self.contention_monitor.compare(before, after)
                if events:
                    await self.circuit_breaker.trip(
                        request.platform, "desktop contention detected"
                    )
                    return GuardedResult(
                        action_id=request.action_id,
                        status=GuardResultStatus.CONTENDED,
                        error_code=GuardErrorCode.DESKTOP_CONTENTION,
                        reason="protected desktop state changed during the action",
                        started_at=started,
                        contention_events=events,
                    )
                await self.circuit_breaker.record_success(request.platform)
                return GuardedResult(
                    action_id=request.action_id,
                    status=GuardResultStatus.SUCCEEDED,
                    value=value,
                    started_at=started,
                )

            token.cancel()
            task.cancel()
            stop_task.cancel()
            await self._drain_cancelled(task, stop_task)
            after = await self.contention_monitor.snapshot()
            events = self.contention_monitor.compare(before, after)

            if self.stop.active:
                code = (
                    ErrorCode.SEND_UNCERTAIN
                    if request.phase is ActionPhase.COMMIT
                    else GuardErrorCode.EMERGENCY_STOPPED
                )
                status = GuardResultStatus.CANCELLED
                reason = "emergency stop cancelled the action"
            else:
                code = (
                    ErrorCode.SEND_UNCERTAIN
                    if request.phase is ActionPhase.COMMIT
                    else GuardErrorCode.ACTION_TIMED_OUT
                )
                status = GuardResultStatus.TIMED_OUT
                reason = "guarded action exceeded its timeout"
            await self.circuit_breaker.record_failure(request.platform, reason)
            if events:
                await self.circuit_breaker.trip(
                    request.platform, "desktop contention detected"
                )
                status, code, reason = (
                    GuardResultStatus.CONTENDED,
                    GuardErrorCode.DESKTOP_CONTENTION,
                    "protected desktop state changed during cancellation",
                )
            return GuardedResult(
                action_id=request.action_id,
                status=status,
                error_code=code,
                reason=reason,
                started_at=started,
                contention_events=events,
            )
        except asyncio.CancelledError:
            if token is not None:
                token.cancel()
            cancellable = tuple(
                candidate for candidate in (task, stop_task) if candidate is not None
            )
            for candidate in cancellable:
                candidate.cancel()
            if cancellable:
                await self._drain_cancelled(*cancellable)
            return GuardedResult(
                action_id=request.action_id,
                status=GuardResultStatus.CANCELLED,
                error_code=GuardErrorCode.ACTION_CANCELLED,
                reason="guard caller cancelled the action",
                started_at=started,
            )
        except Exception as exc:  # noqa: BLE001 - injected guard dependencies fail closed
            await self.circuit_breaker.record_failure(
                request.platform, f"guard dependency failed: {type(exc).__name__}"
            )
            return GuardedResult(
                action_id=request.action_id,
                status=GuardResultStatus.FAILED,
                error_code=(
                    ErrorCode.SEND_UNCERTAIN
                    if request.phase is ActionPhase.COMMIT
                    else ErrorCode.FAILED_SAFE
                ),
                reason=f"guard dependency failed closed: {type(exc).__name__}",
                started_at=started,
            )
        finally:
            self.platform_mutex.release(request.platform)

    async def _acquire_or_stop(
        self, request: GuardedAction, timeout: float
    ) -> tuple[GuardResultStatus, ErrorCode | GuardErrorCode, str] | None:
        lock_task = asyncio.create_task(self.platform_mutex.acquire(request.platform))
        stop_task = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                (lock_task, stop_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            lock_task.cancel()
            stop_task.cancel()
            await asyncio.gather(lock_task, stop_task, return_exceptions=True)
            raise
        if lock_task in done:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            return None
        lock_task.cancel()
        stop_task.cancel()
        await asyncio.gather(lock_task, stop_task, return_exceptions=True)
        if self.stop.active:
            return (
                GuardResultStatus.CANCELLED,
                GuardErrorCode.EMERGENCY_STOPPED,
                "emergency stop prevented the action from starting",
            )
        await self.circuit_breaker.record_failure(
            request.platform, "platform mutex timeout"
        )
        return (
            GuardResultStatus.TIMED_OUT,
            GuardErrorCode.ACTION_TIMED_OUT,
            "timed out waiting for the platform mutex",
        )

    async def _invoke(self, action: GuardedCallable, token: CancellationToken) -> Any:
        value = action(token)
        return await value if inspect.isawaitable(value) else value

    async def _drain_cancelled(self, *tasks: asyncio.Task[Any]) -> None:
        # asyncio.wait has a hard upper bound; wait_for can overrun while waiting
        # for a coroutine that suppresses cancellation.
        await asyncio.wait(tasks, timeout=self.cancellation_grace_seconds)

    async def _cancelled_result(self, request, started, before):
        after = await self.contention_monitor.snapshot()
        events = self.contention_monitor.compare(before, after)
        if events:
            await self.circuit_breaker.trip(
                request.platform, "desktop contention detected"
            )
        if events:
            code = GuardErrorCode.DESKTOP_CONTENTION
        elif self.stop.active and request.phase is ActionPhase.COMMIT:
            code = ErrorCode.SEND_UNCERTAIN
        elif self.stop.active:
            code = GuardErrorCode.EMERGENCY_STOPPED
        else:
            code = GuardErrorCode.ACTION_CANCELLED
        return GuardedResult(
            action_id=request.action_id,
            status=GuardResultStatus.CONTENDED
            if events
            else GuardResultStatus.CANCELLED,
            error_code=code,
            reason=(
                "emergency stop cancelled the action"
                if self.stop.active
                else "guarded action was cancelled"
            ),
            started_at=started,
            contention_events=events,
        )

    @staticmethod
    def _rejected(
        request: GuardedAction, decision: GuardDecision, started: datetime
    ) -> GuardedResult:
        status = (
            GuardResultStatus.QUARANTINED
            if decision.error_code is ErrorCode.ADAPTER_QUARANTINED
            else GuardResultStatus.REJECTED
        )
        return GuardedResult(
            action_id=request.action_id,
            status=status,
            error_code=decision.error_code,
            reason=decision.reason,
            started_at=started,
        )
