from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from test_v5_daemon_acceptance import _build


def test_pause_ack_waits_for_current_tick_and_stops_future_observation(tmp_path):
    app, _harness, _ = _build(tmp_path, 3)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        observed = 0

        async def blocking_observe(_driver, _conversation_id):
            nonlocal observed
            observed += 1
            entered.set()
            await release.wait()

        app.coordinator.observe_driver = blocking_observe
        tick = asyncio.create_task(app.tick())
        await entered.wait()
        pause = asyncio.create_task(app.set_global_pause_from_control(
            paused=True,
            reason="runtime_control:pause:test",
        ))
        await asyncio.sleep(0)
        assert not pause.done()

        release.set()
        await tick
        await pause
        assert app.state.global_control()[1] is True
        await app.tick()
        assert observed == 1

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

        release.set()
        assert await dispatch == (True, None)
        await pause
        assert app.state.global_control()[1] is True
        assert await app.dispatch_exact_with_control_fence(
            pacing_plan_id="later-plan"
        ) == (False, None)

    asyncio.run(run())
