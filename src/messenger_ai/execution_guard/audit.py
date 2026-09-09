from __future__ import annotations

from collections.abc import Callable

from .guard import ExecutionGuard, GuardedCallable
from .models import GuardedAction, GuardedResult


async def run_contention_audit(
    guard: ExecutionGuard,
    request_factory: Callable[[int], GuardedAction],
    action: GuardedCallable,
    iterations: int = 1000,
) -> tuple[GuardedResult, ...]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    results = []
    for index in range(iterations):
        result = await guard.run(request_factory(index), action)
        results.append(result)
        if result.contention_events or guard.stop.active:
            break
    return tuple(results)
