from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_v5_daemon_acceptance import _build


def test_pause_ack_waits_for_current_tick_and_stops_future_observation(tmp_path):
    app, _harness, _ = _build(tmp_path, 3)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        observed = 0
        due_calls = 0

        async def blocking_observe(_driver, _conversation_id):
            nonlocal observed
            observed += 1
            entered.set()
            await release.wait()

        async def record_due():
            nonlocal due_calls
            due_calls += 1
            return False

        app.coordinator.observe_driver = blocking_observe
        app.due.dispatch_one = record_due
        tick = asyncio.create_task(app.tick())
        await entered.wait()
        pause = asyncio.create_task(app.set_global_pause_from_control(
            paused=True,
            reason="runtime_control:pause:test",
        ))
        await asyncio.sleep(0)
        assert not pause.done()
        assert app._pause_requested.is_set()
        assert app.state.global_control()[1] is False

        release.set()
        await tick
        await pause
        assert app.state.global_control()[1] is True
        assert due_calls == 0
        await app.tick()
        assert observed == 1
        assert due_calls == 0

    asyncio.run(run())


def test_pause_ack_waits_for_exact_one_shot_dispatch(tmp_path):
    app, _harness, _ = _build(tmp_path, 3)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_dispatch(**_kwargs):
            entered.set()
            await release.wait()
            return True, None

        app.due.dispatch_exact = blocking_dispatch
        dispatch = asyncio.create_task(
            app.dispatch_exact_with_control_fence(pacing_plan_id="plan")
        )
        await entered.wait()
        pause = asyncio.create_task(
            app.set_global_pause_from_control(
                paused=True,
                reason="runtime_control:pause:test-one-shot",
            )
        )
        await asyncio.sleep(0)
        assert not pause.done()
        assert app._pause_requested.is_set()
        assert app.state.global_control()[1] is False

        release.set()
        assert await dispatch == (True, None)
        await pause
        assert app.state.global_control()[1] is True
        assert await app.dispatch_exact_with_control_fence(
            pacing_plan_id="later-plan"
        ) == (False, None)

    asyncio.run(run())


def test_queued_exact_dispatch_cannot_overtake_pause_request(tmp_path):
    app, _harness, _ = _build(tmp_path, 3)

    async def run():
        tick_entered = asyncio.Event()
        tick_release = asyncio.Event()
        dispatch_called = False

        async def blocking_observe(_driver, _conversation_id):
            tick_entered.set()
            await tick_release.wait()

        async def forbidden_dispatch(**_kwargs):
            nonlocal dispatch_called
            dispatch_called = True
            return True, None

        app.coordinator.observe_driver = blocking_observe
        app.due.dispatch_exact = forbidden_dispatch
        tick = asyncio.create_task(app.tick())
        await tick_entered.wait()
        dispatch = asyncio.create_task(
            app.dispatch_exact_with_control_fence(pacing_plan_id="queued-plan")
        )
        await asyncio.sleep(0)
        pause = asyncio.create_task(
            app.set_global_pause_from_control(
                paused=True,
                reason="runtime_control:pause:queued-test",
            )
        )
        await asyncio.sleep(0)
        assert app._pause_requested.is_set()

        tick_release.set()
        await tick
        assert await dispatch == (False, None)
        await pause
        assert dispatch_called is False
        assert app.state.global_control()[1] is True

    asyncio.run(run())


def test_cancelled_pause_does_not_clear_another_pause_fence(tmp_path):
    app, _harness, _ = _build(tmp_path, 3)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_observe(_driver, _conversation_id):
            entered.set()
            await release.wait()

        app.coordinator.observe_driver = blocking_observe
        tick = asyncio.create_task(app.tick())
        await entered.wait()
        first = asyncio.create_task(app.set_global_pause_from_control(
            paused=True, reason="runtime_control:pause:first",
        ))
        second = asyncio.create_task(app.set_global_pause_from_control(
            paused=True, reason="runtime_control:pause:second",
        ))
        await asyncio.sleep(0)
        assert app._pause_request_count == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert app._pause_request_count == 1
        assert app._pause_requested.is_set()

        release.set()
        await tick
        await second
        assert app._pause_request_count == 0
        assert not app._pause_requested.is_set()
        assert app.state.global_control()[1] is True

    asyncio.run(run())
