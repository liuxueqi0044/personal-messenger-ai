from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.navigation import (
    NavigationDecision, NavigationDesktopError, NavigationDesktopScope, ScopedDesktopOperator,
)

from .test_contracts import click, request


class Backend:
    def __init__(self, req):
        self.frame = req.frame
        self.target = req.target
        self.scope_overrides = {}
        self.events = []
        self.roi_changed = False
        self.focused = True
        self.scope_calls = 0
        self.scope_change_after_roi = False

    async def bound_capture(self, target, *, deadline_at):
        self.events.append(("capture", target.binding_id))
        self.frame = self.frame.model_copy(update={
            "frame_id": f"frame-{len(self.events)}", "captured_at": datetime.now(UTC),
        })
        return self.frame

    async def current_scope(self, target):
        self.scope_calls += 1
        fields = (
            "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
            "control_revision", "binding_id", "binding_revision", "process_id", "window_handle",
            "screen_origin_x", "screen_origin_y", "screen_width", "screen_height",
            "crop_origin_x", "crop_origin_y", "crop_width", "crop_height", "dpi_scale",
        )
        values = {field: getattr(self.frame, field) for field in fields}
        values.update(
            account_id=target.account_id, conversation_id=target.conversation_id,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=45),
            observed_at=datetime.now(UTC), foreground=True, paused=False,
            has_owned_draft=False, has_commit_obligation=False,
        )
        values.update(self.scope_overrides)
        if self.scope_change_after_roi and self.scope_calls > 1:
            values["control_revision"] += 1
        return NavigationDesktopScope.model_validate(values)

    async def relevant_region_digest(self, frame, region, *, current):
        self.events.append(("roi", current))
        return "b" * 64 if current and self.roi_changed else "a" * 64

    async def search_focused(self, frame, region):
        self.events.append(("search_focus",))
        return self.focused

    async def click(self, frame, x, y, **guard):
        assert not guard["cancel_event"].is_set()
        self.events.append(("click", x, y))

    async def scroll(self, frame, x, y, direction, amount, **guard):
        assert not guard["cancel_event"].is_set()
        self.events.append(("scroll", x, y, direction, amount))

    async def set_query(self, frame, region, query, **guard):
        assert not guard["cancel_event"].is_set()
        self.events.append(("query", query))

    async def dismiss_known(self, frame, region, x, y, **guard):
        assert not guard["cancel_event"].is_set()
        self.events.append(("dismiss", region.overlay_id, x, y))


@pytest.mark.asyncio
async def test_bbox_center_converts_to_guest_physical_pixels_without_double_dpi():
    req = request()
    backend = Backend(req)
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "action_attempted" and result.next_frame is not None
    assert ("click", 130, 150) in backend.events
    assert result.next_frame.frame_id != req.frame.frame_id
    assert [event for event in backend.events if event[0] == "roi"] == [("roi", False), ("roi", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("run_id", "other-run"), ("session_epoch", "other-session"),
    ("surface_epoch", "other-surface"), ("worker_epoch", "other-worker"),
    ("desktop_lease_id", "other-lease"), ("control_revision", 7),
    ("binding_id", "other-binding"), ("binding_revision", 3),
    ("process_id", 99), ("window_handle", 99), ("crop_origin_x", 100),
    ("crop_width", 500), ("screen_width", 1400), ("dpi_scale", 2.0),
    ("account_id", "other-account"), ("conversation_id", "other-conversation"),
])
async def test_scope_drift_is_rejected_before_any_input(field, value):
    req = request()
    backend = Backend(req)
    backend.scope_overrides[field] = value
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "stale"
    assert not any(event[0] == "click" for event in backend.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["has_owned_draft", "has_commit_obligation"])
async def test_navigation_cannot_run_with_existing_write_obligations(field):
    req = request()
    backend = Backend(req)
    backend.scope_overrides[field] = True
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "rejected" and result.error_code == "navigation_write_obligation_present"
    assert not backend.events


@pytest.mark.asyncio
async def test_pause_foreground_age_lease_and_roi_changes_prevent_input():
    for changes in (
        {"paused": True}, {"foreground": False},
        {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"observed_at": datetime.now(UTC) - timedelta(seconds=10)},
    ):
        req = request()
        backend = Backend(req)
        backend.scope_overrides.update(changes)
        result = await ScopedDesktopOperator(backend=backend).execute(req, click())
        assert result.status in {"cancelled", "stale"}
        assert not any(event[0] == "click" for event in backend.events)
    req = request()
    backend = Backend(req)
    backend.roi_changed = True
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "stale" and result.error_code == "navigation_region_changed"
    assert not any(event[0] == "click" for event in backend.events)


@pytest.mark.asyncio
async def test_scope_is_checked_again_after_readonly_roi_proof():
    req = request()
    backend = Backend(req)
    backend.scope_change_after_roi = True
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "stale"
    assert not any(event[0] == "click" for event in backend.events)


@pytest.mark.asyncio
async def test_query_comes_only_from_trusted_target_and_requires_search_focus():
    req = request()
    backend = Backend(req)
    decision = NavigationDecision(frame_id=req.frame.frame_id, action="set_target_query", query_alias_index=1)
    result = await ScopedDesktopOperator(backend=backend).execute(req, decision)
    assert result.status == "action_attempted"
    assert ("query", "trusted-qq-id") in backend.events
    backend = Backend(req)
    backend.focused = False
    result = await ScopedDesktopOperator(backend=backend).execute(req, decision)
    assert result.status == "rejected" and result.error_code == "navigation_search_focus_unproven"
    assert not any(event[0] == "query" for event in backend.events)


@pytest.mark.asyncio
async def test_closed_overlay_and_scroll_use_trusted_regions():
    req = request()
    backend = Backend(req)
    decision = NavigationDecision(
        frame_id="frame-1", action="dismiss_known_overlay", overlay_id="known-close",
        bbox={"left": 303, "top": 3, "right": 325, "bottom": 25},
    )
    result = await ScopedDesktopOperator(backend=backend).execute(req, decision)
    assert result.status == "action_attempted" and ("dismiss", "known-close", 394, 114) in backend.events
    backend = Backend(req)
    decision = NavigationDecision(
        frame_id="frame-1", action="scroll_list", bbox=click().bbox,
        scroll_direction="down", scroll_amount=2,
    )
    result = await ScopedDesktopOperator(backend=backend).execute(req, decision)
    assert result.status == "action_attempted" and ("scroll", 130, 150, "down", 2) in backend.events


@pytest.mark.asyncio
async def test_invalid_candidates_and_model_done_claims_never_become_desktop_input():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    result = await operator.execute(req, click(bbox={"left": 150, "top": 40, "right": 160, "bottom": 50}))
    assert result.status == "rejected" and not backend.events
    for action in ("candidate_opened", "unable"):
        result = await operator.execute(req, NavigationDecision(frame_id="frame-1", action=action))
        assert result.status == "rejected"
    assert not any(event[0] in {"click", "scroll", "query", "dismiss"} for event in backend.events)


@pytest.mark.asyncio
async def test_old_frame_and_expired_deadline_never_click():
    req = request()
    req = req.model_copy(update={"frame": req.frame.model_copy(update={"captured_at": datetime.now(UTC) - timedelta(seconds=25)})})
    backend = Backend(req)
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "stale" and not backend.events
    req = request().model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)})
    backend = Backend(req)
    result = await ScopedDesktopOperator(backend=backend).execute(req, click())
    assert result.status == "stale" and not backend.events


@pytest.mark.asyncio
async def test_wait_is_cancellable_and_produces_no_input():
    req = request()
    backend = Backend(req)
    cancellation = asyncio.Event()
    result = asyncio.create_task(ScopedDesktopOperator(backend=backend).execute(
        req, NavigationDecision(frame_id="frame-1", action="wait", wait_ms=1000), cancel_event=cancellation,
    ))
    await asyncio.sleep(0.01)
    cancellation.set()
    outcome = await asyncio.wait_for(result, timeout=0.2)
    assert outcome.status == "cancelled"
    assert not any(event[0] in {"click", "scroll", "query", "dismiss"} for event in backend.events)


@pytest.mark.asyncio
async def test_wait_listens_to_outer_round_cancellation_with_separate_action_event():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    outer_cancel, action_cancel = asyncio.Event(), asyncio.Event()
    async with operator.round(req.target, deadline_at=req.deadline_at, cancel_event=outer_cancel):
        result = asyncio.create_task(operator.execute(
            req, NavigationDecision(frame_id="frame-1", action="wait", wait_ms=2000), cancel_event=action_cancel,
        ))
        await asyncio.sleep(0.01)
        outer_cancel.set()
        outcome = await asyncio.wait_for(result, timeout=0.2)
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_round_allows_child_tasks_but_revokes_inherited_cap_after_exit():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    release_child = asyncio.Event()
    async def stale_child():
        await release_child.wait()
        return await operator.execute(req, click())
    async with operator.round(req.target, deadline_at=req.deadline_at):
        frame = await asyncio.wait_for(asyncio.create_task(operator.capture(req.target, deadline_at=req.deadline_at)), timeout=0.2)
        scoped_req = req.model_copy(update={"frame": frame})
        decision = click(frame_id=frame.frame_id)
        outcome = await asyncio.wait_for(asyncio.create_task(operator.execute(scoped_req, decision)), timeout=0.2)
        assert outcome.status == "action_attempted"
        child = asyncio.create_task(stale_child())
    prior = len(backend.events)
    release_child.set()
    result = await child
    assert result.status == "cancelled" and len(backend.events) == prior
    assert not operator.operation_lock.locked()


@pytest.mark.asyncio
async def test_round_rejects_different_target_and_does_not_leak_lock():
    req = request()
    operator = ScopedDesktopOperator(backend=Backend(req))
    async with operator.round(req.target, deadline_at=req.deadline_at):
        with pytest.raises(NavigationDesktopError, match="target_changed"):
            await operator.capture(req.target.model_copy(update={"binding_id": "another"}), deadline_at=req.deadline_at)
    assert not operator.operation_lock.locked()


@pytest.mark.asyncio
async def test_cancelled_lock_wait_cannot_release_another_owners_lock():
    req = request()
    lock = asyncio.Lock()
    await lock.acquire()
    cancellation = asyncio.Event()
    operator = ScopedDesktopOperator(backend=Backend(req), operation_lock=lock)
    running = asyncio.create_task(operator.execute(req, click(), cancel_event=cancellation))
    await asyncio.sleep(0)
    cancellation.set()
    result = await running
    assert result.status == "cancelled" and lock.locked()
    lock.release()


@pytest.mark.asyncio
async def test_inherited_children_are_serialized_by_step_lock():
    req = request()
    class Serialized(Backend):
        calls_inside = 0
        max_calls_inside = 0
        async def bound_capture(self, target, *, deadline_at):
            self.calls_inside += 1
            self.max_calls_inside = max(self.calls_inside, self.max_calls_inside)
            await asyncio.sleep(0.01)
            result = await super().bound_capture(target, deadline_at=deadline_at)
            self.calls_inside -= 1
            return result
    backend = Serialized(req)
    operator = ScopedDesktopOperator(backend=backend)
    async with operator.round(req.target, deadline_at=req.deadline_at):
        await asyncio.gather(
            operator.capture(req.target, deadline_at=req.deadline_at),
            operator.capture(req.target, deadline_at=req.deadline_at),
        )
    assert backend.max_calls_inside == 1


@pytest.mark.asyncio
async def test_round_exit_revokes_backend_input_guard_for_detached_child():
    req = request()
    started = asyncio.Event()
    release = asyncio.Event()
    class Delayed(Backend):
        async def click(self, frame, x, y, **guard):
            started.set()
            await release.wait()
            if guard["cancel_event"].is_set():
                raise NavigationDesktopError("navigation_cancelled", status="cancelled")
            self.events.append(("click", x, y))
    backend = Delayed(req)
    operator = ScopedDesktopOperator(backend=backend)
    async with operator.round(req.target, deadline_at=req.deadline_at):
        child = asyncio.create_task(operator.execute(req, click()))
        await started.wait()
    release.set()
    result = await child
    assert result.status == "cancelled"
    assert not any(event[0] == "click" for event in backend.events)


@pytest.mark.asyncio
async def test_coordinator_action_cancel_can_differ_from_outer_round_signal():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    outer_cancel = asyncio.Event()
    action_cancel = asyncio.Event()
    async with operator.round(req.target, deadline_at=req.deadline_at, cancel_event=outer_cancel):
        outcome = await asyncio.wait_for(asyncio.create_task(operator.execute(req, click(), cancel_event=action_cancel)), timeout=0.2)
    assert outcome.status == "action_attempted"
    assert ("click", 130, 150) in backend.events


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_outer", [True, False])
async def test_backend_guard_joins_round_and_per_action_cancellation(cancel_outer):
    req = request()
    started = asyncio.Event()
    continue_input = asyncio.Event()
    outer_cancel, action_cancel = asyncio.Event(), asyncio.Event()
    class Delayed(Backend):
        async def click(self, frame, x, y, **guard):
            started.set()
            await continue_input.wait()
            await asyncio.sleep(0)
            if guard["cancel_event"].is_set():
                raise NavigationDesktopError("navigation_cancelled", status="cancelled")
            self.events.append(("click", x, y))
    backend = Delayed(req)
    operator = ScopedDesktopOperator(backend=backend)
    async with operator.round(req.target, deadline_at=req.deadline_at, cancel_event=outer_cancel):
        child = asyncio.create_task(operator.execute(req, click(), cancel_event=action_cancel))
        await started.wait()
        (outer_cancel if cancel_outer else action_cancel).set()
        continue_input.set()
        outcome = await child
    assert outcome.status == "cancelled"
    assert not any(event[0] == "click" for event in backend.events)


@pytest.mark.asyncio
async def test_runtime_coordinator_and_scoped_operator_integrate_with_bounded_child_tasks():
    from messenger_ai.adapters.qq.navigation import NavigationProviderResult
    from messenger_ai.adapters.qq.navigation.identity import ActiveChatLease, ActiveChatVerificationResult
    from messenger_ai.runtime.navigation import NavigationCoordinator
    from messenger_ai.runtime.navigation_state import NavigationTaskStore

    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    class Navigator:
        calls = 0
        async def decide(self, request, *, cancel_event=None):
            self.calls += 1
            decision = (click(frame_id=request.frame.frame_id) if self.calls == 1
                        else NavigationDecision(frame_id=request.frame.frame_id, action="candidate_opened"))
            return NavigationProviderResult(frame_id=request.frame.frame_id, model="fake", latency_ms=0, decision=decision)
    class Verifier:
        calls = 0
        async def verify(self, target, frame, *, deadline_at):
            self.calls += 1
            if self.calls == 1:
                return ActiveChatVerificationResult(verified=False, error_code="active_chat_not_current")
            now = datetime.now(UTC)
            mono = time.monotonic_ns()
            fields = {name: getattr(frame, name) for name in (
                "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
                "control_revision", "process_id", "window_handle", "frame_id",
            )}
            lease = ActiveChatLease(
                **fields, lease_id="a" * 64, account_id=target.account_id, conversation_id=target.conversation_id,
                binding_id=target.binding_id, binding_revision=target.binding_revision,
                observation_epoch="obs", process_started_at_100ns=1, evidence_ref="evidence",
                evidence_digest="b" * 64, verification_method="profile_from_current_header_with_selected_row_fence",
                issued_at=now, expires_at=now + timedelta(seconds=10),
                issued_monotonic_ns=mono, expires_monotonic_ns=mono + 10_000_000_000,
            )
            return ActiveChatVerificationResult(verified=True, lease=lease)
    store = NavigationTaskStore()
    try:
        navigator, verifier = Navigator(), Verifier()
        result = await asyncio.wait_for(
            NavigationCoordinator(navigator, operator, verifier, store).navigate(
                req.target, pending_input_key="offline-pending", cancel_event=asyncio.Event(),
            ), timeout=0.5,
        )
        assert result.outcome.status == "candidate_opened"
        assert result.active_chat_lease is not None
        assert result.outcome.desktop_actions == 1 and result.outcome.model_requests == 1
        assert navigator.calls == 1 and verifier.calls == 2
        assert ("click", 130, 150) in backend.events
        assert not operator.operation_lock.locked()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_parent_revoke_and_cross_task_exit_do_not_leave_context_or_lock():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    context = operator.round(req.target, deadline_at=req.deadline_at)
    await context.__aenter__()
    release_child = asyncio.Event()
    async def late_child():
        await release_child.wait()
        return await operator.execute(req, click())
    child = asyncio.create_task(late_child())
    operator.revoke_round()
    operator.revoke_round()  # idempotent; no async or backend work
    assert operator._round_capability.get() is None
    await asyncio.wait_for(asyncio.create_task(context.__aexit__(None, None, None)), timeout=0.2)
    assert not operator.operation_lock.locked()
    release_child.set()
    outcome = await child
    assert outcome.status == "cancelled" and not backend.events
    assert operator._round_capability.get() is None
    # A distinct subsequent round is allowed after old cleanup completes.
    async with operator.round(req.target, deadline_at=req.deadline_at):
        assert operator.operation_lock.locked()
    assert not operator.operation_lock.locked()


@pytest.mark.asyncio
async def test_cancel_during_cross_task_cleanup_cannot_leak_round_lock():
    req = request()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    class SluggishEvent(asyncio.Event):
        async def wait(self):
            try:
                return await super().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await release.wait()
                return True
    operator = ScopedDesktopOperator(backend=Backend(req))
    context = operator.round(req.target, deadline_at=req.deadline_at, cancel_event=SluggishEvent())
    await context.__aenter__()
    await asyncio.sleep(0)  # let both cancellation waiters enter wait()
    operator.revoke_round()
    cleanup = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert not operator.operation_lock.locked()
    assert operator._round_capability.get() is None
    release.set()


@pytest.mark.asyncio
async def test_revoke_blocks_unsubmitted_input_but_does_not_claim_to_undo_prior_input():
    req = request()
    backend = Backend(req)
    operator = ScopedDesktopOperator(backend=backend)
    context = operator.round(req.target, deadline_at=req.deadline_at)
    await context.__aenter__()
    outcome = await operator.execute(req, click())
    assert outcome.status == "action_attempted" and ("click", 130, 150) in backend.events
    prior = tuple(backend.events)
    operator.revoke_round()
    await asyncio.create_task(context.__aexit__(None, None, None))
    assert tuple(backend.events) == prior


@pytest.mark.asyncio
async def test_revoke_immediately_invalidates_input_guard_before_async_mirrors_run():
    req = request()
    started, release = asyncio.Event(), asyncio.Event()
    guard_holder = []
    class Delayed(Backend):
        async def click(self, frame, x, y, **guard):
            guard_holder.append(guard["cancel_event"])
            started.set()
            await release.wait()
            if guard["cancel_event"].is_set():
                raise NavigationDesktopError("navigation_cancelled", status="cancelled")
            self.events.append(("click", x, y))
    backend = Delayed(req)
    operator = ScopedDesktopOperator(backend=backend)
    context = operator.round(req.target, deadline_at=req.deadline_at)
    await context.__aenter__()
    child = asyncio.create_task(operator.execute(req, click()))
    await started.wait()
    assert not guard_holder[0].is_set()
    operator.revoke_round()
    # No intervening await: immediate polling must already reject input.
    assert guard_holder[0].is_set()
    release.set()
    await asyncio.create_task(context.__aexit__(None, None, None))
    outcome = await child
    assert outcome.status == "cancelled"
    assert not any(event[0] == "click" for event in backend.events)
