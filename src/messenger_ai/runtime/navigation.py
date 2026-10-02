"""Bounded, single-action QQ navigation before any business send operation.

The coordinator has no Hub, message cursor, composer, or sending capability.
Only independent current-chat verification can produce its successful lease.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from messenger_ai.adapters.qq.navigation.contracts import (
    ContactTarget, DesktopActionStatus, NavigationAction, NavigationBudget,
    NavigationDecision, NavigationFrame, NavigationOutcome, NavigationRegionKind,
    NavigationRequest, NavigationStatus, validate_decision,
)
from messenger_ai.adapters.qq.navigation.ports import CurrentChatVerifier, DesktopOperator, VisionNavigator

from .navigation_state import NavigationEpisode, NavigationTask, NavigationTaskStore

if TYPE_CHECKING:
    from messenger_ai.adapters.qq.navigation.identity import ActiveChatLease, ActiveChatVerificationResult

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RuntimeNavigationResult:
    outcome: NavigationOutcome
    task_id: str
    active_chat_lease: ActiveChatLease | None = None
    retry_at: datetime | None = None


class _NavigationTimeout(Exception):
    pass


class _NavigationCancelled(Exception):
    pass


class _NavigationRejected(Exception):
    def __init__(self, code: str):
        self.code = code


class _NavigationUnavailable(_NavigationRejected):
    pass


_DESKTOP_UNAVAILABLE_ERRORS = frozenset({
    "navigation_deadline_expired", "navigation_lock_deadline",
    "navigation_local_result_unavailable", "navigation_backend_unavailable",
    "navigation_worker_revoked", "navigation_worker_exited",
    "navigation_worker_response_mismatch", "navigation_worker_outcome_mismatch",
    "navigation_worker_command_failed", "navigation_worker_cleanup_required",
    "navigation_worker_reap_failed", "navigation_worker_job_unavailable",
})


def _consume_task(task: asyncio.Future) -> None:
    # A third-party coroutine can suppress cancellation. Discard late output;
    # no follow-up desktop action may be scheduled by this coordinator.
    if not task.cancelled():
        task.exception()


class NavigationCoordinator:
    def __init__(self, navigator: VisionNavigator, desktop: DesktopOperator,
                 verifier: CurrentChatVerifier, store: NavigationTaskStore, *,
                 budget: NavigationBudget | None = None,
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 monotonic_ns_clock: Callable[[], int] = time.monotonic_ns,
                 hard_identity_errors: frozenset[str] = frozenset(),
                 navigable_identity_errors: frozenset[str] = frozenset({"active_chat_not_current"})):
        self.navigator = navigator
        self.desktop = desktop
        self.verifier = verifier
        self.store = store
        self.budget = budget or NavigationBudget()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self.monotonic_ns_clock = monotonic_ns_clock
        self.hard_identity_errors = hard_identity_errors
        self.navigable_identity_errors = navigable_identity_errors

    @staticmethod
    def _frame_scope(frame: NavigationFrame, target: ContactTarget, *,
                     deadline_at: datetime, now: datetime) -> None:
        if (frame.binding_id != target.binding_id
                or frame.binding_revision != target.binding_revision
                or frame.captured_at >= deadline_at):
            raise _NavigationRejected("navigation_frame_scope_mismatch")
        if frame.captured_at > now:
            raise _NavigationRejected("navigation_frame_capture_in_future")

    @staticmethod
    def _lease_scope(result: ActiveChatVerificationResult, target: ContactTarget,
                     frame: NavigationFrame, now: datetime,
                     now_monotonic_ns: int) -> ActiveChatLease | None:
        if not result.verified:
            if result.lease is not None:
                raise _NavigationRejected("navigation_unverified_lease")
            return None
        lease = result.lease
        expected = {
            "account_id": target.account_id,
            "conversation_id": target.conversation_id,
            "binding_id": target.binding_id,
            "binding_revision": target.binding_revision,
            "frame_id": frame.frame_id,
            **{name: getattr(frame, name) for name in (
                "run_id", "session_epoch", "surface_epoch", "worker_epoch",
                "desktop_lease_id", "control_revision", "process_id", "window_handle",
            )},
        }
        if lease is None or any(getattr(lease, key, None) != value for key, value in expected.items()):
            raise _NavigationRejected("navigation_verified_lease_scope_mismatch")
        if not lease.is_fresh(now=now, now_monotonic_ns=now_monotonic_ns):
            raise _NavigationRejected("navigation_verified_lease_expired")
        return lease

    async def _bounded(self, awaitable: Awaitable[T], *, timeout: float,
                       external_cancel: asyncio.Event | None,
                       action_cancel: asyncio.Event) -> T:
        task = asyncio.ensure_future(awaitable)
        cancellation = asyncio.create_task(external_cancel.wait()) if external_cancel else None
        try:
            if timeout <= 0 or (external_cancel and external_cancel.is_set()):
                if external_cancel and external_cancel.is_set():
                    raise _NavigationCancelled()
                raise _NavigationTimeout()
            done, _ = await asyncio.wait(
                {task, cancellation} if cancellation else {task},
                timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
            if external_cancel and external_cancel.is_set():
                raise _NavigationCancelled()
            if task not in done:
                raise _NavigationTimeout()
            if task.cancelled():
                raise _NavigationCancelled()
            return task.result()
        except BaseException:
            action_cancel.set()
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume_task)
            raise
        finally:
            if cancellation is not None:
                cancellation.cancel()

    @staticmethod
    def _progress_key(frame: NavigationFrame, decision: NavigationDecision) -> str:
        # IDs/timestamps may change for identical pixels. Retry detection is
        # based on the navigation crop and action, not incidental frame IDs.
        payload = {
            "image": frame.sha256,
            "geometry": [frame.crop_origin_x, frame.crop_origin_y, frame.crop_width, frame.crop_height],
            "regions": [region.model_dump(mode="json") for region in frame.allowed_regions],
            "action": decision.model_dump(mode="json", exclude={"frame_id"}),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def navigate(self, target: ContactTarget, *, pending_input_key: str,
                       task_id: str | None = None,
                       cancel_event: asyncio.Event | None = None) -> RuntimeNavigationResult:
        started = self.monotonic()
        task = self.store.ensure_task(target, pending_input_key, now=self.clock(), task_id=task_id)
        start = self.store.begin_episode(
            task.task_id, now=self.clock(), total_timeout_seconds=self.budget.total_timeout_seconds,
        )
        if start.episode is None:
            return RuntimeNavigationResult(
                outcome=NavigationOutcome(status=NavigationStatus.RETRY_WAIT,
                    binding_id=target.binding_id, binding_revision=target.binding_revision,
                    error_code=start.error_code),
                task_id=task.task_id, retry_at=start.retry_at,
            )
        episode = start.episode
        try:
            result = await self._run_round(target, task=task, episode=episode,
                                           started=started, cancel_event=cancel_event)
            result = replace(result, outcome=result.outcome.model_copy(update={
                "elapsed_ms": max(0, int((self.monotonic() - started) * 1000)),
            }))
            if cancel_event is not None and cancel_event.is_set() and result.active_chat_lease is not None:
                result = replace(result, active_chat_lease=None, outcome=result.outcome.model_copy(update={
                    "status": NavigationStatus.CANCELLED, "error_code": "navigation_cancelled",
                }))
            elif (self.monotonic() - started >= self.budget.total_timeout_seconds
                    or self.clock() >= episode.deadline_at):
                result = replace(result, active_chat_lease=None, outcome=result.outcome.model_copy(update={
                    "status": NavigationStatus.RETRY_WAIT,
                    "error_code": "navigation_deadline_exhausted",
                    "elapsed_ms": max(0, int((self.monotonic() - started) * 1000)),
                }))
            if result.active_chat_lease is not None and not result.active_chat_lease.is_fresh(
                now=self.clock(), now_monotonic_ns=self.monotonic_ns_clock()
            ):
                result = replace(
                    result, active_chat_lease=None,
                    outcome=result.outcome.model_copy(update={
                        "status": NavigationStatus.NEEDS_ATTENTION,
                        "error_code": "navigation_lease_expired_during_round_exit",
                        "elapsed_ms": max(0, int((self.monotonic() - started) * 1000)),
                    }),
                )
            # A provisional verified candidate is settled only after the
            # desktop round has exited successfully. Failed cleanup must not
            # escape the durable failure window as a recorded success.
            saved = self.store.finish_episode(episode.episode_id, status=result.outcome.status.value,
                                             now=self.clock(), error_code=result.outcome.error_code)
            return replace(result, retry_at=saved.retry_at)
        except BaseException as exc:
            counts = self.store.get_episode(episode.episode_id)
            status = NavigationStatus.CANCELLED if (
                isinstance(exc, asyncio.CancelledError) or (cancel_event and cancel_event.is_set())
            ) else NavigationStatus.NEEDS_ATTENTION
            code = ("navigation_caller_cancelled" if isinstance(exc, asyncio.CancelledError)
                    else "navigation_round_cancelled" if status == NavigationStatus.CANCELLED
                    else exc.code if isinstance(exc, _NavigationRejected)
                    else "navigation_round_failed")
            if counts.status == "running":
                saved = self.store.finish_episode(episode.episode_id, status=status.value,
                                                 now=self.clock(), error_code=code)
                retry_at = saved.retry_at
            else:
                retry_at = self.clock() + timedelta(seconds=10)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            return RuntimeNavigationResult(
                outcome=NavigationOutcome(status=status, binding_id=target.binding_id,
                    binding_revision=target.binding_revision, model_requests=counts.model_requests,
                    desktop_actions=counts.desktop_actions,
                    elapsed_ms=max(0, int((self.monotonic() - started) * 1000)), error_code=code),
                task_id=task.task_id, retry_at=retry_at,
            )

    async def _run_round(self, target: ContactTarget, *, task: NavigationTask,
                         episode: NavigationEpisode, started: float,
                         cancel_event: asyncio.Event | None) -> RuntimeNavigationResult:
        round_factory = getattr(self.desktop, "round", None)
        if round_factory is None:
            return await self._navigate_episode(target, task=task, episode=episode,
                                                started=started, cancel_event=cancel_event)
        revoke = getattr(self.desktop, "revoke_round", None)
        if not callable(revoke):
            raise _NavigationRejected("navigation_round_not_revocable")
        reserve = getattr(self.desktop, "cleanup_reserve_seconds", 0.0)
        if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
                or not math.isfinite(reserve) or not 0 <= reserve < self.budget.total_timeout_seconds):
            raise _NavigationRejected("navigation_cleanup_reserve_invalid")
        work_deadline_at = episode.deadline_at - timedelta(seconds=reserve)
        work_monotonic_deadline = started + self.budget.total_timeout_seconds - reserve
        # The round capability expires at the work boundary. Cleanup may reap
        # its child process within the original, persisted total deadline.
        context = round_factory(target, deadline_at=work_deadline_at, cancel_event=cancel_event)
        entered = False
        exc_info = (None, None, None)

        def remaining() -> float:
            return max(0, min(self.budget.total_timeout_seconds - (self.monotonic() - started),
                              (episode.deadline_at - self.clock()).total_seconds()))

        try:
            # Enter in this task: its capability must propagate to bounded
            # child capture/execute calls. The trusted round acquisition must
            # respect cancellation/deadline; it performs no desktop input.
            async with asyncio.timeout(max(0, remaining() - reserve)):
                await context.__aenter__()
            entered = True
            return await self._navigate_episode(target, task=task, episode=episode,
                                                started=started, cancel_event=cancel_event,
                                                work_deadline_at=work_deadline_at,
                                                work_monotonic_deadline=work_monotonic_deadline)
        except BaseException:
            exc_info = sys.exc_info()
            raise
        finally:
            if entered:
                # Synchronous revocation clears the parent ContextVar and
                # invalidates inherited capabilities before cleanup can wait.
                revoke()
                cleanup = asyncio.create_task(context.__aexit__(*exc_info))
                cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
                try:
                    done, _ = await asyncio.wait(
                        {cleanup, cancellation} if cancellation else {cleanup}, timeout=remaining(),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    cleanup.cancel()
                    cleanup.add_done_callback(_consume_task)
                    raise
                finally:
                    if cancellation is not None:
                        cancellation.cancel()
                if cancel_event is not None and cancel_event.is_set():
                    cleanup.cancel()
                    cleanup.add_done_callback(_consume_task)
                    if not isinstance(exc_info[1], asyncio.CancelledError):
                        raise _NavigationCancelled()
                elif cleanup in done:
                    cleanup.result()
                else:
                    cleanup.cancel()
                    cleanup.add_done_callback(_consume_task)
                    # An uncooperative cleanup may finish later. Its result is
                    # discarded and its revoked capability can grant no input.
                    if not isinstance(exc_info[1], asyncio.CancelledError):
                        raise _NavigationRejected("navigation_round_cleanup_timeout")

    async def _navigate_episode(self, target: ContactTarget, *, task: NavigationTask,
                                episode: NavigationEpisode, started: float,
                                cancel_event: asyncio.Event | None,
                                work_deadline_at: datetime | None = None,
                                work_monotonic_deadline: float | None = None) -> RuntimeNavigationResult:
        deadline_at = work_deadline_at or episode.deadline_at
        monotonic_deadline = (work_monotonic_deadline if work_monotonic_deadline is not None
                              else started + self.budget.total_timeout_seconds)
        action_cancel = asyncio.Event()
        frame: NavigationFrame | None = None
        seen: dict[str, int] = {}
        search_recovery_used = False

        def remaining() -> float:
            return min(monotonic_deadline - self.monotonic(),
                       (deadline_at - self.clock()).total_seconds())

        async def bounded(awaitable: Awaitable[T], timeout: float | None = None) -> T:
            return await self._bounded(awaitable, timeout=min(remaining(), timeout or remaining()),
                                       external_cancel=cancel_event, action_cancel=action_cancel)

        def finish(status: NavigationStatus, error_code: str | None = None,
                   lease: ActiveChatLease | None = None) -> RuntimeNavigationResult:
            counts = self.store.get_episode(episode.episode_id)
            return RuntimeNavigationResult(
                outcome=NavigationOutcome(
                    status=status, binding_id=target.binding_id, binding_revision=target.binding_revision,
                    last_frame=frame, model_requests=counts.model_requests,
                    desktop_actions=counts.desktop_actions,
                    elapsed_ms=max(0, int((self.monotonic() - started) * 1000)), error_code=error_code,
                ), task_id=task.task_id, active_chat_lease=lease,
            )

        async def verify(*, initial: bool = False) -> tuple[ActiveChatLease | None, str | None]:
            assert frame is not None
            # A known different current header is normal before navigation.
            # This optional initial probe may skip expensive profile capture,
            # but every candidate opened by navigation uses full verification.
            initial_probe = getattr(self.verifier, "verify_if_current", None)
            check = initial_probe if initial and callable(initial_probe) else self.verifier.verify
            verification = await bounded(check(target, frame, deadline_at=deadline_at))
            return self._lease_scope(verification, target, frame, self.clock(),
                                     self.monotonic_ns_clock()), verification.error_code

        async def capture() -> NavigationFrame:
            captured = await bounded(self.desktop.capture(target, deadline_at=deadline_at))
            self._frame_scope(captured, target, deadline_at=deadline_at, now=self.clock())
            return captured

        async def execute(request: NavigationRequest, decision: NavigationDecision) -> tuple[NavigationFrame, bool]:
            if not self.store.reserve_desktop_action(episode.episode_id, now=self.clock(),
                                                     maximum=self.budget.max_desktop_actions):
                raise _NavigationRejected("navigation_action_budget_exhausted")
            result = await bounded(self.desktop.execute(request, decision, cancel_event=action_cancel))
            if result.frame_id != request.frame.frame_id:
                raise _NavigationRejected("navigation_action_frame_mismatch")
            if result.status == DesktopActionStatus.CANCELLED:
                raise _NavigationCancelled()
            if result.error_code in _DESKTOP_UNAVAILABLE_ERRORS:
                # A failed RPC may have already retired the finite worker,
                # including after input was attempted. Do not recapture or
                # replay against that capability; preserve its original cause.
                raise _NavigationUnavailable(result.error_code)
            if result.status == DesktopActionStatus.REJECTED:
                raise _NavigationRejected(result.error_code or "navigation_action_rejected")
            # A stale decision grants no action. Capture again and spend a new
            # model request; never execute its old coordinates on a new frame.
            if result.status == DesktopActionStatus.STALE or result.next_frame is None:
                return await capture(), False
            self._frame_scope(result.next_frame, target, deadline_at=deadline_at, now=self.clock())
            if result.next_frame.frame_id == request.frame.frame_id:
                raise _NavigationRejected("navigation_action_reused_frame")
            candidate_clicked = (decision.action == NavigationAction.CLICK_CANDIDATE
                                 and result.status == DesktopActionStatus.ACTION_ATTEMPTED
                                 and result.error_code is None)
            return result.next_frame, candidate_clicked

        try:
            frame = await capture()
            lease, identity_error = await verify(initial=True)
            if lease is not None:
                return finish(NavigationStatus.CANDIDATE_OPENED, lease=lease)
            if (identity_error not in self.navigable_identity_errors
                    or identity_error in self.hard_identity_errors):
                return finish(NavigationStatus.NEEDS_ATTENTION,
                              identity_error or "navigation_initial_identity_unverified")
            while remaining() > 0:
                request = NavigationRequest(target=target, frame=frame, deadline_at=deadline_at)
                if not self.store.reserve_model_request(episode.episode_id, now=self.clock(),
                                                        maximum=self.budget.max_model_requests):
                    return finish(NavigationStatus.RETRY_WAIT, "navigation_model_budget_exhausted")
                response = await bounded(self.navigator.decide(request, cancel_event=action_cancel),
                                         min(15, self.budget.request_timeout_seconds))
                if response.frame_id != frame.frame_id:
                    raise _NavigationRejected("navigation_provider_frame_mismatch")
                if response.cancelled:
                    raise _NavigationCancelled()
                if response.error is not None:
                    return finish(NavigationStatus.RETRY_WAIT, "navigation_provider_error")
                if response.decision is None:
                    raise _NavigationRejected("navigation_provider_missing_decision")
                try:
                    decision = validate_decision(request, response.decision)
                except ValueError as exc:
                    raise _NavigationRejected("navigation_invalid_decision") from exc
                if decision.action == NavigationAction.UNABLE:
                    return finish(NavigationStatus.NEEDS_ATTENTION, "navigation_model_unable")
                if decision.action == NavigationAction.CANDIDATE_OPENED:
                    lease, identity_error = await verify()
                    if lease is None:
                        return finish(NavigationStatus.NEEDS_ATTENTION,
                                      identity_error or "navigation_candidate_identity_unverified")
                    return finish(NavigationStatus.CANDIDATE_OPENED, lease=lease)
                progress_key = self._progress_key(frame, decision)
                seen[progress_key] = seen.get(progress_key, 0) + 1
                if seen[progress_key] >= 2:
                    # Recovery is a trusted bounded action, not a fabricated
                    # model coordinate. It uses a locally certified search box.
                    search = next((region for region in frame.allowed_regions
                                   if region.kind == NavigationRegionKind.SEARCH), None)
                    if search_recovery_used or search is None or decision.action in {
                        NavigationAction.OPEN_SEARCH, NavigationAction.SET_TARGET_QUERY,
                    }:
                        return finish(NavigationStatus.RETRY_WAIT, "navigation_no_progress")
                    decision = NavigationDecision(frame_id=frame.frame_id,
                                                  action=NavigationAction.OPEN_SEARCH, bbox=search.bbox)
                    validate_decision(request, decision)
                    search_recovery_used = True
                frame, candidate_clicked = await execute(request, decision)
                if candidate_clicked:
                    # Masked list pixels cannot establish that the chat opened.
                    # Spend the remaining original budget on independent full
                    # identity verification, not another model confirmation or
                    # click. The initial non-current probe is never used here.
                    lease, identity_error = await verify()
                    if lease is None:
                        return finish(NavigationStatus.NEEDS_ATTENTION,
                                      identity_error or "navigation_candidate_identity_unverified")
                    return finish(NavigationStatus.CANDIDATE_OPENED, lease=lease)
            return finish(NavigationStatus.RETRY_WAIT, "navigation_deadline_exhausted")
        except _NavigationCancelled:
            return finish(NavigationStatus.CANCELLED, "navigation_cancelled")
        except _NavigationTimeout:
            return finish(NavigationStatus.RETRY_WAIT, "navigation_timeout")
        except _NavigationUnavailable as exc:
            return finish(NavigationStatus.RETRY_WAIT, exc.code)
        except _NavigationRejected as exc:
            if exc.code == "navigation_action_budget_exhausted":
                return finish(NavigationStatus.RETRY_WAIT, exc.code)
            return finish(NavigationStatus.NEEDS_ATTENTION, exc.code)
        except asyncio.CancelledError:
            action_cancel.set()
            finish(NavigationStatus.CANCELLED, "navigation_caller_cancelled")
            raise
        except Exception:
            # Exception strings may contain private backend data. Persist only
            # a closed local code and keep the pending input for later recovery.
            return finish(NavigationStatus.RETRY_WAIT, "navigation_backend_error")
