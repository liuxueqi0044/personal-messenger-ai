from __future__ import annotations

import asyncio
import time

from messenger_ai.domain import ErrorCode, Platform
from messenger_ai.execution_guard import (
    ActionPhase,
    CircuitBreaker,
    GuardedActionType,
    GuardErrorCode,
    GuardResultStatus,
)


async def test_same_platform_actions_are_serialized(harness) -> None:
    active = 0
    maximum = 0

    async def action(token):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        active -= 1
        return "ok"

    results = await asyncio.gather(
        *(harness.guard.run(harness.request(timeout=1), action) for _ in range(8))
    )
    assert maximum == 1
    assert all(result.succeeded for result in results)


async def test_different_platforms_do_not_share_a_mutex(harness) -> None:
    both_running = asyncio.Event()
    count = 0

    async def action(token):
        nonlocal count
        count += 1
        if count == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), 0.2)
        return "ok"

    qq, wechat = await asyncio.gather(
        harness.guard.run(harness.request(Platform.QQ), action),
        harness.guard.run(harness.request(Platform.WECHAT), action),
    )
    assert qq.succeeded and wechat.succeeded


async def test_timeout_releases_platform_lock(harness) -> None:
    async def slow(token):
        await asyncio.sleep(10)

    first = await harness.guard.run(harness.request(timeout=0.04), slow)
    assert first.status is GuardResultStatus.TIMED_OUT
    assert first.error_code is GuardErrorCode.ACTION_TIMED_OUT
    assert not harness.guard.platform_mutex.locked(Platform.QQ)

    second = await harness.guard.run(
        harness.request(timeout=0.5), lambda token: "released"
    )
    assert second.succeeded
    assert second.value == "released"


async def test_commit_timeout_is_uncertain_not_safe_retry(harness) -> None:
    async def slow(token):
        await asyncio.sleep(10)

    result = await harness.guard.run(
        harness.request(
            timeout=0.03,
            action_type=GuardedActionType.SEND,
            phase=ActionPhase.COMMIT,
            operations=("uia.invoke_pattern.invoke",),
        ),
        slow,
    )
    assert result.status is GuardResultStatus.TIMED_OUT
    assert result.error_code is ErrorCode.SEND_UNCERTAIN


async def test_commit_exception_is_uncertain_not_failed_safe(harness) -> None:
    async def failed_during_commit(token):
        raise OSError("transport state unknown")

    result = await harness.guard.run(
        harness.request(
            action_type=GuardedActionType.SEND,
            phase=ActionPhase.COMMIT,
            operations=("uia.invoke_pattern.invoke",),
        ),
        failed_during_commit,
    )
    assert result.status is GuardResultStatus.FAILED
    assert result.error_code is ErrorCode.SEND_UNCERTAIN


async def test_consecutive_failures_open_the_platform_circuit(harness) -> None:
    harness.guard.circuit_breaker = CircuitBreaker(failure_threshold=2)

    async def broken(token):
        raise RuntimeError("adapter crash")

    assert (
        await harness.guard.run(harness.request(), broken)
    ).status is GuardResultStatus.FAILED
    assert (
        await harness.guard.run(harness.request(), broken)
    ).status is GuardResultStatus.FAILED
    blocked = await harness.guard.run(harness.request(), lambda token: "must-not-run")
    assert blocked.status is GuardResultStatus.QUARANTINED
    assert blocked.error_code is ErrorCode.ADAPTER_QUARANTINED


async def test_emergency_stop_cancels_running_prepare_and_blocks_new_actions(
    harness,
) -> None:
    started = asyncio.Event()

    async def preparing(token):
        started.set()
        while True:
            token.raise_if_cancelled()
            await asyncio.sleep(0.01)

    running = asyncio.create_task(
        harness.guard.run(
            harness.request(
                action_type=GuardedActionType.COMPOSE,
                phase=ActionPhase.PREPARE,
                operations=("uia.value_pattern.set",),
                timeout=5,
            ),
            preparing,
        )
    )
    await started.wait()
    before = time.monotonic()
    await harness.guard.emergency_stop("operator pressed stop")
    result = await asyncio.wait_for(running, timeout=0.9)
    assert time.monotonic() - before < 1
    assert result.status is GuardResultStatus.CANCELLED
    assert result.error_code is GuardErrorCode.EMERGENCY_STOPPED

    blocked = await harness.guard.run(harness.request(), lambda token: "must-not-run")
    assert blocked.status is GuardResultStatus.REJECTED
    assert blocked.error_code is GuardErrorCode.EMERGENCY_STOPPED


async def test_contention_is_detected_and_immediately_quarantines(harness) -> None:
    async def moving_pointer(token):
        harness.desktop.state.pointer_position = (900, 900)
        return "bad"

    result = await harness.guard.run(harness.request(), moving_pointer)
    assert result.status is GuardResultStatus.CONTENDED
    assert result.error_code is GuardErrorCode.DESKTOP_CONTENTION
    assert {event.resource for event in result.contention_events} == {
        "pointer_position"
    }
    assert await harness.guard.circuit_breaker.is_open(Platform.QQ)


async def test_declared_forbidden_action_never_executes(harness) -> None:
    executed = False

    async def malicious(token):
        nonlocal executed
        executed = True

    result = await harness.guard.run(
        harness.request(operations=("user32.SendInput",)), malicious
    )
    assert result.status is GuardResultStatus.REJECTED
    assert result.error_code is GuardErrorCode.ACTION_NOT_ALLOWED
    assert not executed


async def test_caller_cancellation_stops_action_and_releases_lock(harness) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def running(token):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    outer = asyncio.create_task(harness.guard.run(harness.request(timeout=5), running))
    await started.wait()
    outer.cancel()
    result = await outer
    await asyncio.wait_for(cancelled.wait(), 0.5)
    assert result.status is GuardResultStatus.CANCELLED
    assert not harness.guard.platform_mutex.locked(Platform.QQ)
