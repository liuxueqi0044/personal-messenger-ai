import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import struct
import time

import pytest

from messenger_ai.adapters.qq.navigation.contracts import (
    ContactTarget, DesktopActionResult, NavigationBudget, NavigationDecision,
    NavigationFrame, NavigationProviderResult, NavigationRegion, NavigationRect,
)
from messenger_ai.runtime.navigation import NavigationCoordinator
from messenger_ai.runtime.navigation_state import NavigationTaskStore


NOW = datetime(2026, 10, 2, tzinfo=UTC)
BOX = NavigationRect(left=2, top=2, right=30, bottom=15)
SEARCH = NavigationRect(left=32, top=2, right=60, bottom=15)


def target():
    return ContactTarget(account_id="a", conversation_id="c", binding_id="b", binding_revision=2,
                         display_name="Test contact", identity_mode="persistent")


def frame(index=0, *, pixels=b"same", search=True, **updates):
    png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 64, 32) + pixels
    regions = [NavigationRegion(kind="candidate", bbox=BOX), NavigationRegion(kind="list", bbox=BOX)]
    if search:
        regions.append(NavigationRegion(kind="search", bbox=SEARCH))
    return NavigationFrame(frame_id=f"frame-{index}", run_id="run", session_epoch="session",
                           surface_epoch="surface", worker_epoch="worker", desktop_lease_id="desktop",
                           control_revision=1, binding_id="b", binding_revision=2, process_id=12,
                           window_handle=13, captured_at=NOW, screen_width=64, screen_height=32,
                           crop_origin_x=0, crop_origin_y=0, crop_width=64, crop_height=32,
                           allowed_regions=tuple(regions), privacy_mask_applied=True, png_bytes=png,
                           dpi_scale=1).model_copy(update=updates)


@dataclass(frozen=True)
class TestLease:
    account_id: str = "a"
    conversation_id: str = "c"
    binding_id: str = "b"
    binding_revision: int = 2
    run_id: str = "run"
    session_epoch: str = "session"
    surface_epoch: str = "surface"
    worker_epoch: str = "worker"
    desktop_lease_id: str = "desktop"
    control_revision: int = 1
    process_id: int = 12
    window_handle: int = 13
    frame_id: str = "frame-1"
    issued_at: datetime = NOW
    expires_at: datetime = NOW + timedelta(seconds=10)
    issued_monotonic_ns: int = 0
    expires_monotonic_ns: int = 10_000_000_000

    def is_fresh(self, *, now, now_monotonic_ns):
        return (self.issued_at <= now < self.expires_at and
                self.issued_monotonic_ns <= now_monotonic_ns < self.expires_monotonic_ns)


TestLease.__test__ = False


@dataclass(frozen=True)
class Verification:
    verified: bool
    lease: TestLease | None = None
    error_code: str | None = None


class Verifier:
    def __init__(self, results=None):
        self.results = list(results or [Verification(False, error_code="active_chat_not_current")])
        self.calls = []

    async def verify(self, target, current_frame, *, deadline_at):
        self.calls.append(current_frame.frame_id)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Desktop:
    def __init__(self, *, search=True, stale=False, wrong_next_scope=False):
        self.search = search
        self.stale = stale
        self.wrong_next_scope = wrong_next_scope
        self.captures = 0
        self.actions = []

    def revoke_round(self):
        pass

    async def capture(self, target, *, deadline_at):
        self.captures += 1
        return frame(self.captures, search=self.search)

    async def execute(self, request, decision, *, cancel_event=None):
        self.actions.append(decision)
        self.captures += 1
        return DesktopActionResult(status="stale" if self.stale else "action_attempted",
                                   frame_id=request.frame.frame_id,
                                   next_frame=None if self.stale else frame(
                                       self.captures, search=self.search,
                                       binding_id="wrong" if self.wrong_next_scope else "b"))


class Navigator:
    def __init__(self, actions=("candidate_opened",), *, stale=False, delay=0):
        self.actions = list(actions)
        self.calls = []
        self.stale = stale
        self.delay = delay
        self.cancel_seen = False
        self.started = asyncio.Event()

    async def decide(self, request, *, cancel_event=None):
        self.calls.append(request.frame.frame_id)
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancel_seen = cancel_event.is_set()
            raise
        action = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        parameters = {"frame_id": "stale-frame" if self.stale else request.frame.frame_id, "action": action}
        if action == "click_candidate":
            parameters.update(bbox=BOX, observed_label="Test contact")
        elif action == "wait":
            parameters.update(wait_ms=1)
        return NavigationProviderResult(frame_id=parameters["frame_id"], model="fake", latency_ms=0,
                                        decision=NavigationDecision(**parameters))


def coordinator(navigator=None, desktop=None, verifier=None, store=None, budget=None):
    return NavigationCoordinator(navigator or Navigator(), desktop or Desktop(), verifier or Verifier(),
                                 store or NavigationTaskStore(), budget=budget, clock=lambda: NOW,
                                 monotonic_ns_clock=lambda: 0)


@pytest.mark.asyncio
async def test_optional_current_probe_is_only_used_before_navigation():
    class InitialProbe(Verifier):
        initial_calls = 0

        async def verify_if_current(self, target, current_frame, *, deadline_at):
            self.initial_calls += 1
            return Verification(False,error_code="active_chat_not_current")

    verifier = InitialProbe([Verification(True,TestLease(frame_id="frame-2"))])
    instance = coordinator(Navigator(["click_candidate"]),verifier=verifier)
    result = await instance.navigate(target(),pending_input_key="normal-foreign-current")
    assert result.outcome.status == "candidate_opened"
    assert verifier.initial_calls == 1
    assert verifier.calls == ["frame-2"]
    assert len(instance.navigator.calls) == 1


@pytest.mark.asyncio
async def test_opened_candidate_identity_conflict_cannot_reuse_initial_probe():
    class InitialProbe(Verifier):
        initial_calls = 0

        async def verify_if_current(self, target, current_frame, *, deadline_at):
            self.initial_calls += 1
            return Verification(False,error_code="active_chat_not_current")

    verifier = InitialProbe([Verification(False,error_code="identity_profile_mismatch")])
    instance = coordinator(Navigator(["click_candidate"]),verifier=verifier)
    result = await instance.navigate(target(),pending_input_key="wrong-candidate")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "identity_profile_mismatch"
    assert verifier.initial_calls == 1
    assert verifier.calls == ["frame-2"]
    assert len(instance.navigator.calls) == 1
    assert len(instance.desktop.actions) == 1 and result.active_chat_lease is None


@pytest.mark.asyncio
async def test_already_current_requires_independent_verification_and_zero_model_calls():
    nav = Navigator()
    instance = coordinator(nav, verifier=Verifier([Verification(True, TestLease())]))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "candidate_opened"
    assert result.active_chat_lease == TestLease()
    assert result.outcome.model_requests == result.outcome.desktop_actions == 0
    assert nav.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [TestLease(binding_id="wrong"), TestLease(worker_epoch="wrong"),
                                    TestLease(expires_at=NOW), TestLease(issued_at=NOW + timedelta(seconds=1)),
                                    TestLease(issued_monotonic_ns=1), TestLease(expires_monotonic_ns=0), None])
async def test_verified_flag_cannot_grant_wrong_scope_or_expired_lease(lease):
    nav = Navigator()
    result = await coordinator(nav, verifier=Verifier([Verification(True, lease)])).navigate(
        target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.active_chat_lease is None
    assert nav.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["identity_profile_hmac_mismatch", "identity_correlation_missing", None])
async def test_initial_identity_conflict_does_not_blindly_navigate(code):
    nav = Navigator()
    result = await coordinator(nav, verifier=Verifier([Verification(False, error_code=code)])).navigate(
        target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert nav.calls == []


@pytest.mark.asyncio
async def test_model_candidate_claim_is_never_identity_proof():
    instance = coordinator()
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.active_chat_lease is None
    assert len(instance.verifier.calls) == 2
    assert instance.desktop.actions == []
    assert instance.store.get_task(result.task_id).pending_input_key == "pending-key"


@pytest.mark.asyncio
async def test_one_action_then_new_frame_then_independent_candidate_verification():
    nav = Navigator(["click_candidate"])  # Would repeat the click if called again.
    verifier = Verifier([Verification(False, error_code="active_chat_not_current"),
                         Verification(True, TestLease(frame_id="frame-2"))])
    instance = coordinator(nav, verifier=verifier)
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "candidate_opened"
    assert nav.calls == ["frame-1"]
    assert [decision.frame_id for decision in instance.desktop.actions] == ["frame-1"]
    assert verifier.calls == ["frame-1", "frame-2"]
    assert result.outcome.model_requests == 1
    assert result.outcome.desktop_actions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["identity_profile_mismatch", "identity_chat_drift", "identity_evidence_stale",
    "identity_composer_not_empty", "active_chat_not_current", None])
async def test_post_click_full_identity_failure_stops_without_model_retry(code):
    instance = coordinator(Navigator(["click_candidate"]), verifier=Verifier([
        Verification(False, error_code="active_chat_not_current"), Verification(False, error_code=code)]))
    result = await instance.navigate(target(), pending_input_key="post-click")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == (code or "navigation_candidate_identity_unverified")
    assert result.active_chat_lease is None
    assert instance.verifier.calls == ["frame-1", "frame-2"]
    assert result.outcome.model_requests == result.outcome.desktop_actions == 1
    assert len(instance.navigator.calls) == len(instance.desktop.actions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_next_frame", "stale_with_next_frame", "nonfatal_error"])
async def test_unconfirmed_click_result_does_not_trigger_post_click_verification(kind):
    class IncompleteDesktop(Desktop):
        async def execute(self, request, decision, *, cancel_event=None):
            result = await super().execute(request, decision, cancel_event=cancel_event)
            update = ({"next_frame": None} if kind == "no_next_frame" else
                      {"status": "stale"} if kind == "stale_with_next_frame" else
                      {"error_code": "synthetic_action_warning"})
            return result.model_copy(update=update)
    instance = coordinator(Navigator(["click_candidate", "unable"]), desktop=IncompleteDesktop())
    result = await instance.navigate(target(), pending_input_key="unconfirmed-click")
    assert result.outcome.error_code == "navigation_model_unable"
    assert result.active_chat_lease is None and instance.verifier.calls == ["frame-1"]
    assert result.outcome.model_requests == 2 and result.outcome.desktop_actions == 1
    assert instance.desktop.captures == (2 if kind == "nonfatal_error" else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error,expected", [
    ("cancelled", None, "navigation_cancelled"),
    ("rejected", "synthetic_action_rejected", "synthetic_action_rejected"),
    ("action_attempted", "navigation_local_result_unavailable", "navigation_local_result_unavailable"),
])
async def test_failed_action_result_with_next_frame_never_starts_identity_or_replays(status, error, expected):
    class FailedDesktop(Desktop):
        async def execute(self, request, decision, *, cancel_event=None):
            result = await super().execute(request, decision, cancel_event=cancel_event)
            return result.model_copy(update={"status": status, "error_code": error})
    instance = coordinator(Navigator(["click_candidate"]), desktop=FailedDesktop())
    result = await instance.navigate(target(), pending_input_key="failed-click")
    assert result.outcome.error_code == expected and result.active_chat_lease is None
    assert instance.verifier.calls == ["frame-1"]
    assert instance.desktop.captures == 2
    assert result.outcome.model_requests == result.outcome.desktop_actions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [TestLease(frame_id="frame-1"),
    TestLease(frame_id="frame-2", worker_epoch="wrong-worker"),
    TestLease(frame_id="frame-2", process_id=99),
    TestLease(frame_id="frame-2", expires_at=NOW), None])
async def test_post_click_verified_flag_still_requires_exact_new_frame_scope_and_fresh_lease(lease):
    instance = coordinator(Navigator(["click_candidate"]), verifier=Verifier([
        Verification(False, error_code="active_chat_not_current"), Verification(True, lease)]))
    result = await instance.navigate(target(), pending_input_key="bad-post-click-lease")
    assert result.outcome.status == "needs_attention" and result.active_chat_lease is None
    assert result.outcome.error_code in {"navigation_verified_lease_scope_mismatch", "navigation_verified_lease_expired"}
    assert result.outcome.model_requests == result.outcome.desktop_actions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["same_frame", "wrong_binding", "future_frame"])
async def test_post_click_frame_is_validated_before_full_identity(kind):
    class BadFrameDesktop(Desktop):
        async def execute(self, request, decision, *, cancel_event=None):
            result = await super().execute(request, decision, cancel_event=cancel_event)
            updates = ({"frame_id": request.frame.frame_id} if kind == "same_frame" else
                       {"binding_revision": 99} if kind == "wrong_binding" else
                       {"captured_at": NOW + timedelta(seconds=1)})
            return result.model_copy(update={"next_frame": result.next_frame.model_copy(update=updates)})
    instance = coordinator(Navigator(["click_candidate"]), desktop=BadFrameDesktop())
    result = await instance.navigate(target(), pending_input_key="bad-post-click-frame")
    assert result.outcome.status == "needs_attention" and result.active_chat_lease is None
    assert instance.verifier.calls == ["frame-1"]
    assert result.outcome.model_requests == result.outcome.desktop_actions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel_event", "caller_cancel"])
async def test_post_click_identity_uses_remaining_deadline_and_revokes_round_on_interruption(mode):
    class BoundedDesktop(Desktop):
        cleanup_reserve_seconds = .02
        closed = revoked = False

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            self.deadline = deadline_at
            try:
                yield
            finally:
                self.closed = True

        def revoke_round(self):
            self.revoked = True

    class SlowVerifier(Verifier):
        def __init__(self):
            super().__init__()
            self.entered, self.cancelled = asyncio.Event(), False
            self.deadlines = []

        async def verify_if_current(self, target, current_frame, *, deadline_at):
            self.deadlines.append(deadline_at)
            return Verification(False, error_code="active_chat_not_current")

        async def verify(self, target, current_frame, *, deadline_at):
            self.calls.append(current_frame.frame_id)
            self.deadlines.append(deadline_at)
            self.entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    desktop, verifier, cancel = BoundedDesktop(), SlowVerifier(), asyncio.Event()
    instance = coordinator(Navigator(["click_candidate"]), desktop=desktop, verifier=verifier,
                           budget=NavigationBudget(total_timeout_seconds=.2, request_timeout_seconds=.2))
    task = asyncio.create_task(instance.navigate(target(), pending_input_key="post-click-interrupted", cancel_event=cancel))
    await asyncio.wait_for(verifier.entered.wait(), timeout=1)
    if mode == "cancel_event":
        cancel.set()
    elif mode == "caller_cancel":
        task.cancel()
    if mode == "caller_cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.active_chat_lease is None
        assert result.outcome.status == ("retry_wait" if mode == "timeout" else "cancelled")
    await asyncio.sleep(0)
    assert verifier.cancelled and desktop.closed and desktop.revoked
    assert verifier.calls == ["frame-2"]
    assert len(instance.navigator.calls) == len(desktop.actions) == 1
    assert verifier.deadlines == [NOW + timedelta(seconds=.18)] * 2
    assert desktop.deadline == NOW + timedelta(seconds=.18)
    stored = instance.store.connection.execute("SELECT deadline_at,status FROM runtime_nav_episodes").fetchone()
    assert datetime.fromisoformat(stored["deadline_at"]) == NOW + timedelta(seconds=.2)
    assert stored["status"] != "candidate_opened"


@pytest.mark.asyncio
async def test_post_click_verified_candidate_waits_for_successful_round_cleanup():
    class CleanupFailureDesktop(Desktop):
        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            raise RuntimeError("synthetic cleanup failure")
    instance = coordinator(Navigator(["click_candidate"]), desktop=CleanupFailureDesktop(), verifier=Verifier([
        Verification(False, error_code="active_chat_not_current"), Verification(True, TestLease(frame_id="frame-2"))]))
    result = await instance.navigate(target(), pending_input_key="post-click-cleanup")
    assert result.outcome.status == "needs_attention" and result.outcome.error_code == "navigation_round_failed"
    assert result.active_chat_lease is None
    assert result.outcome.model_requests == result.outcome.desktop_actions == 1
    assert instance.store.get_task(result.task_id).status == "needs_attention"


@pytest.mark.asyncio
async def test_stale_provider_frame_is_rejected_before_any_desktop_action():
    instance = coordinator(Navigator(stale=True))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_provider_frame_mismatch"
    assert instance.desktop.actions == []


@pytest.mark.asyncio
async def test_stale_desktop_decision_is_not_replayed_and_gets_fresh_model_request():
    nav = Navigator(["click_candidate", "unable"])
    instance = coordinator(nav, desktop=Desktop(stale=True))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_model_unable"
    assert nav.calls == ["frame-1", "frame-3"]
    assert len(instance.desktop.actions) == 1
    assert instance.verifier.calls == ["frame-1"]


@pytest.mark.asyncio
async def test_stale_repeat_same_pixels_action_uses_trusted_search_then_stops():
    instance = coordinator(Navigator(["click_candidate"]), desktop=Desktop(stale=True))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_no_progress"
    assert [action.action.value for action in instance.desktop.actions] == ["click_candidate", "open_search"]
    assert instance.desktop.actions[1].bbox == SEARCH
    assert result.outcome.model_requests == 3


@pytest.mark.asyncio
async def test_stale_repeat_without_search_stops_without_second_identical_click():
    instance = coordinator(Navigator(["click_candidate"]), desktop=Desktop(search=False, stale=True))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_no_progress"
    assert len(instance.desktop.actions) == 1


@pytest.mark.asyncio
async def test_model_and_desktop_budgets_leave_pending_input_and_no_send_operation():
    instance = coordinator(Navigator(["wait"]), budget=NavigationBudget(max_model_requests=1))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "retry_wait"
    assert result.outcome.error_code == "navigation_model_budget_exhausted"
    assert result.outcome.model_requests == 1
    assert instance.store.get_task(result.task_id).pending_input_key == "pending-key"
    names = {row[0] for row in instance.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"runtime_nav_tasks", "runtime_nav_episodes", "runtime_nav_observation_recoveries"}
    assert instance.store.connection.execute("SELECT count(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 0
    action_limited = coordinator(Navigator(["wait"]), budget=NavigationBudget(max_desktop_actions=1))
    result = await action_limited.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_action_budget_exhausted"
    assert result.outcome.desktop_actions == 1


@pytest.mark.asyncio
async def test_wrong_target_after_action_cannot_produce_a_candidate_or_continue():
    instance = coordinator(Navigator(["click_candidate"]), desktop=Desktop(wrong_next_scope=True))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_frame_scope_mismatch"
    assert result.active_chat_lease is None
    assert len(instance.navigator.calls) == 1


@pytest.mark.asyncio
async def test_provider_timeout_is_bounded_and_reserved_request_survives_retry():
    navigator = Navigator(delay=10)
    instance = coordinator(navigator, budget=NavigationBudget(total_timeout_seconds=.2, request_timeout_seconds=.01))
    result = await asyncio.wait_for(instance.navigate(target(), pending_input_key="pending-key"), timeout=.5)
    await asyncio.sleep(0)
    assert result.outcome.error_code == "navigation_timeout"
    assert result.outcome.model_requests == 1
    assert navigator.cancel_seen
    retry = await instance.navigate(target(), pending_input_key="pending-key")
    assert retry.task_id == result.task_id
    assert retry.outcome.error_code == "navigation_cooldown"
    assert len(navigator.calls) == 1


@pytest.mark.asyncio
async def test_external_cancel_discards_provider_output_and_keeps_work():
    event = asyncio.Event()
    navigator = Navigator(delay=10)
    instance = coordinator(navigator)
    task = asyncio.create_task(instance.navigate(target(), pending_input_key="pending-key", cancel_event=event))
    await asyncio.wait_for(navigator.started.wait(), timeout=.5)
    event.set()
    result = await asyncio.wait_for(task, timeout=.2)
    await asyncio.sleep(0)
    assert result.outcome.status == "cancelled"
    assert result.outcome.model_requests == 1
    assert navigator.cancel_seen
    assert instance.desktop.actions == []
    assert instance.store.get_task(result.task_id).pending_input_key == "pending-key"


@pytest.mark.asyncio
async def test_provider_suppressing_cancellation_cannot_extend_deadline_or_drive_late_ui():
    class UncooperativeNavigator(Navigator):
        async def decide(self, request, *, cancel_event=None):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await asyncio.sleep(.03)
            return NavigationProviderResult(
                frame_id=request.frame.frame_id, model="fake", latency_ms=0,
                decision=NavigationDecision(frame_id=request.frame.frame_id, action="click_candidate",
                                            bbox=BOX, observed_label="Test contact"),
            )

    instance = coordinator(UncooperativeNavigator(), budget=NavigationBudget(
        total_timeout_seconds=.2, request_timeout_seconds=.01))
    result = await asyncio.wait_for(instance.navigate(target(), pending_input_key="pending-key"), timeout=.3)
    assert result.outcome.error_code == "navigation_timeout"
    await asyncio.sleep(.06)
    assert instance.desktop.actions == []
    assert instance.store.get_task(result.task_id).status == "retry_wait"


@pytest.mark.asyncio
async def test_caller_cancellation_is_persisted_then_propagated():
    navigator = Navigator(delay=10)
    instance = coordinator(navigator)
    work = asyncio.create_task(instance.navigate(target(), pending_input_key="pending-key"))
    await asyncio.wait_for(navigator.started.wait(), timeout=.5)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    task_id = instance.store.stable_task_id(target(), "pending-key")
    saved = instance.store.get_task(task_id)
    assert saved.status == "cancelled"
    assert saved.pending_input_key == "pending-key"
    assert saved.error_code == "navigation_caller_cancelled"


@pytest.mark.asyncio
async def test_optional_whole_round_capability_is_inherited_by_bounded_child_calls():
    class RoundDesktop(Desktop):
        def __init__(self):
            super().__init__()
            self.capability = ContextVar("test_round", default=None)
            self.entered = False
            self.exited = False

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            capability = {"active": True}
            token = self.capability.set(capability)
            self.entered = True
            try:
                yield
            finally:
                self.exited = True
                capability["active"] = False
                try:
                    self.capability.reset(token)
                except ValueError:
                    self.capability.set(None)

        def revoke_round(self):
            capability = self.capability.get()
            if capability is not None:
                capability["active"] = False
            self.capability.set(None)

        async def capture(self, target, *, deadline_at):
            assert self.capability.get()["active"]
            return await super().capture(target, deadline_at=deadline_at)

        async def execute(self, request, decision, *, cancel_event=None):
            assert self.capability.get()["active"]
            return await super().execute(request, decision, cancel_event=cancel_event)

    desktop = RoundDesktop()
    instance = coordinator(Navigator(["click_candidate", "candidate_opened"]), desktop=desktop,
                           verifier=Verifier([Verification(False, error_code="active_chat_not_current"),
                                              Verification(True, TestLease(frame_id="frame-2"))]))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "candidate_opened"
    assert desktop.entered and desktop.exited
    assert desktop.capability.get() is None


@pytest.mark.asyncio
async def test_round_cleanup_failure_invalidates_provisional_lease_and_counts_as_failure():
    class FailingRoundDesktop(Desktop):
        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            raise RuntimeError("private backend detail must not be persisted")

    instance = coordinator(desktop=FailingRoundDesktop(), verifier=Verifier([Verification(True, TestLease())]))
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "navigation_round_failed"
    assert result.active_chat_lease is None
    assert instance.store.get_task(result.task_id).status == "needs_attention"
    retry = await instance.navigate(target(), pending_input_key="pending-key")
    assert retry.outcome.error_code == "navigation_cooldown"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_action", [False, True])
async def test_future_frame_capture_is_rejected_before_any_further_action(after_action):
    class FutureFrameDesktop(Desktop):
        async def capture(self, target, *, deadline_at):
            captured = await super().capture(target, deadline_at=deadline_at)
            return captured if after_action else captured.model_copy(update={"captured_at": NOW + timedelta(seconds=1)})

        async def execute(self, request, decision, *, cancel_event=None):
            action = await super().execute(request, decision, cancel_event=cancel_event)
            return action.model_copy(update={
                "next_frame": action.next_frame.model_copy(update={"captured_at": NOW + timedelta(seconds=1)})
            })

    instance = coordinator(Navigator(["click_candidate"]), desktop=FutureFrameDesktop())
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "navigation_frame_capture_in_future"
    assert result.active_chat_lease is None
    assert len(instance.navigator.calls) == int(after_action)
    assert len(instance.desktop.actions) == int(after_action)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_clock", ["utc", "monotonic", "utc_backwards"])
async def test_cleanup_consuming_either_lease_clock_prevents_success_settlement(expired_clock):
    clock_state = {"utc": NOW, "monotonic_ns": 0}

    class DelayedCleanupDesktop(Desktop):
        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            if expired_clock == "utc":
                clock_state["utc"] += timedelta(seconds=11)
            elif expired_clock == "utc_backwards":
                clock_state["utc"] -= timedelta(seconds=1)
            else:
                clock_state["monotonic_ns"] += 11_000_000_000

    instance = NavigationCoordinator(
        Navigator(), DelayedCleanupDesktop(), Verifier([Verification(True, TestLease())]), NavigationTaskStore(),
        clock=lambda: clock_state["utc"], monotonic_ns_clock=lambda: clock_state["monotonic_ns"],
    )
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "navigation_lease_expired_during_round_exit"
    assert result.active_chat_lease is None
    saved = instance.store.get_task(result.task_id)
    assert saved.status == "needs_attention"
    assert saved.pending_input_key == "pending-key"
    assert instance.store.connection.execute("SELECT status FROM runtime_nav_episodes").fetchone()[0] == "needs_attention"


@pytest.mark.asyncio
async def test_cleanup_suppressing_cancel_is_bounded_and_parent_capability_is_revoked():
    class UncooperativeCleanupDesktop(Desktop):
        def __init__(self):
            super().__init__()
            self.capability = ContextVar("uncooperative_cleanup_round", default=None)
            self.cleanup_finished = asyncio.Event()
            self.late_input_blocked = False

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            capability = {"active": True}
            self.capability.set(capability)
            yield
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(.02)
            self.late_input_blocked = not capability["active"]
            self.capability.set(None)
            self.cleanup_finished.set()

        def revoke_round(self):
            capability = self.capability.get()
            if capability is not None:
                capability["active"] = False
            self.capability.set(None)

    desktop = UncooperativeCleanupDesktop()
    instance = coordinator(desktop=desktop, verifier=Verifier([Verification(True, TestLease())]),
                           budget=NavigationBudget(total_timeout_seconds=.1, request_timeout_seconds=.1))
    result = await asyncio.wait_for(instance.navigate(target(), pending_input_key="pending-key"), timeout=.5)
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "navigation_round_cleanup_timeout"
    assert result.active_chat_lease is None
    assert desktop.capability.get() is None
    saved = instance.store.get_task(result.task_id)
    assert saved.status == "needs_attention"
    await asyncio.wait_for(desktop.cleanup_finished.wait(), timeout=.3)
    assert desktop.late_input_blocked
    assert instance.store.get_task(result.task_id) == saved


@pytest.mark.asyncio
async def test_desktop_round_without_synchronous_revocation_is_rejected_before_acquisition():
    class UnrevocableRoundDesktop(Desktop):
        revoke_round = None

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            raise AssertionError("an unrevocable round must never be acquired")
            yield

    result = await coordinator(desktop=UnrevocableRoundDesktop()).navigate(target(), pending_input_key="pending-key")
    assert result.outcome.error_code == "navigation_round_not_revocable"
    assert result.outcome.model_requests == result.outcome.desktop_actions == 0


@pytest.mark.asyncio
async def test_cleanup_that_crosses_total_budget_discards_even_a_still_fresh_lease():
    monotonic_state = {"seconds": 0.0}

    class BudgetExhaustingCleanupDesktop(Desktop):
        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            monotonic_state["seconds"] = 2.0

    instance = NavigationCoordinator(
        Navigator(), BudgetExhaustingCleanupDesktop(), Verifier([Verification(True, TestLease())]), NavigationTaskStore(),
        clock=lambda: NOW, monotonic=lambda: monotonic_state["seconds"], monotonic_ns_clock=lambda: 0,
        budget=NavigationBudget(total_timeout_seconds=1, request_timeout_seconds=1),
    )
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "retry_wait"
    assert result.outcome.error_code == "navigation_deadline_exhausted"
    assert result.active_chat_lease is None
    assert result.outcome.elapsed_ms == 2000


@pytest.mark.asyncio
async def test_declared_cleanup_reserve_shortens_every_work_deadline_without_extending_episode():
    deadlines = []
    monotonic_state = {"seconds": 0.0}

    class ReservedDesktop(Desktop):
        cleanup_reserve_seconds = .2

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            deadlines.append(("round", deadline_at))
            yield
            monotonic_state["seconds"] += .15

        async def capture(self, target, *, deadline_at):
            deadlines.append(("capture", deadline_at))
            return await super().capture(target, deadline_at=deadline_at)

        async def execute(self, request, decision, *, cancel_event=None):
            deadlines.append(("execute", request.deadline_at))
            return await super().execute(request, decision, cancel_event=cancel_event)

    class RecordingNavigator(Navigator):
        async def decide(self, request, *, cancel_event=None):
            deadlines.append(("model", request.deadline_at))
            return await super().decide(request, cancel_event=cancel_event)

    class RecordingVerifier(Verifier):
        async def verify(self, target, current_frame, *, deadline_at):
            deadlines.append(("verify", deadline_at))
            return await super().verify(target, current_frame, deadline_at=deadline_at)

    instance = NavigationCoordinator(
        RecordingNavigator(("click_candidate", "candidate_opened")), ReservedDesktop(),
        RecordingVerifier([Verification(False, error_code="active_chat_not_current"),
                           Verification(True, TestLease(frame_id="frame-2"))]), NavigationTaskStore(),
        budget=NavigationBudget(total_timeout_seconds=1, request_timeout_seconds=1),
        clock=lambda: NOW, monotonic=lambda: monotonic_state["seconds"], monotonic_ns_clock=lambda: 0,
    )
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "candidate_opened"
    assert {stage for stage, _deadline in deadlines} == {"round", "capture", "verify", "model", "execute"}
    assert all(deadline == NOW + timedelta(seconds=.8) for _stage, deadline in deadlines)
    assert result.outcome.elapsed_ms == 150  # includes cleanup
    episode = instance.store.connection.execute("SELECT deadline_at FROM runtime_nav_episodes").fetchone()
    assert datetime.fromisoformat(episode[0]) == NOW + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_work_timeout_leaves_declared_reserve_for_synchronous_process_close():
    class SynchronousCloseDesktop(Desktop):
        cleanup_reserve_seconds = .2

        def __init__(self):
            super().__init__()
            self.closed = False
            self.revoked = False

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            time.sleep(.1)  # same event-loop blocking shape as terminate/join
            self.closed = True

        def revoke_round(self):
            self.revoked = True

    desktop = SynchronousCloseDesktop()
    instance = coordinator(Navigator(delay=10), desktop=desktop,
                           budget=NavigationBudget(total_timeout_seconds=.4, request_timeout_seconds=.4))
    started = time.monotonic()
    result = await instance.navigate(target(), pending_input_key="pending-key")
    elapsed = time.monotonic() - started
    assert result.outcome.status == "retry_wait"
    assert result.outcome.error_code == "navigation_timeout"
    assert elapsed < .45  # no-reserve work+close would exceed .5s
    assert desktop.closed and desktop.revoked
    assert result.active_chat_lease is None


@pytest.mark.asyncio
async def test_close_overrunning_its_declared_reserve_cannot_settle_a_success():
    monotonic_state = {"seconds": 0.0}

    class LateCloseDesktop(Desktop):
        cleanup_reserve_seconds = .2

        @asynccontextmanager
        async def round(self, target, *, deadline_at, cancel_event=None):
            yield
            monotonic_state["seconds"] = 1.1

    instance = NavigationCoordinator(
        Navigator(), LateCloseDesktop(), Verifier([Verification(True, TestLease())]), NavigationTaskStore(),
        clock=lambda: NOW, monotonic=lambda: monotonic_state["seconds"], monotonic_ns_clock=lambda: 0,
        budget=NavigationBudget(total_timeout_seconds=1, request_timeout_seconds=1),
    )
    result = await instance.navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "retry_wait"
    assert result.outcome.error_code == "navigation_deadline_exhausted"
    assert result.active_chat_lease is None
    assert result.outcome.elapsed_ms == 1100
    assert instance.store.get_task(result.task_id).status == "retry_wait"


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve", [True, -.1, float("nan"), float("inf"), ".2", 1, 2])
async def test_invalid_cleanup_reserve_is_rejected_before_round_or_capture(reserve):
    class InvalidReserveDesktop(Desktop):
        cleanup_reserve_seconds = reserve

        @asynccontextmanager
        async def round(self, *args, **kwargs):
            raise AssertionError("invalid reserve must not acquire desktop")
            yield

    desktop = InvalidReserveDesktop()
    result = await coordinator(desktop=desktop, budget=NavigationBudget(
        total_timeout_seconds=1, request_timeout_seconds=1,
    )).navigate(target(), pending_input_key="pending-key")
    assert result.outcome.status == "needs_attention"
    assert result.outcome.error_code == "navigation_cleanup_reserve_invalid"
    assert desktop.captures == 0
