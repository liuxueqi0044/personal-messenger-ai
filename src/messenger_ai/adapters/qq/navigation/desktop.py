"""Trusted-backend adaptation for finite QQ navigation operations.

This module contains no Windows access, screenshots, model calls, sending,
clipboard implementation, or shell. A production backend must implement the
local proof/capture operations and enforce deadline/cancellation at the actual
input boundary. Navigation still cannot certify the conversation it opens.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import AsyncIterator, Protocol

from pydantic import Field, field_validator

from .contracts import (
    ContactTarget, DesktopActionResult, DesktopActionStatus, NavigationAction,
    NavigationDecision, NavigationFrame, NavigationModel, NavigationRegion,
    NavigationRequest, _aware, validate_decision,
)


class NavigationDesktopScope(NavigationModel):
    """Fresh, trusted local state; model output never populates these fields."""

    account_id: str = Field(min_length=1, max_length=256, strict=True)
    conversation_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_id: str = Field(min_length=1, max_length=256, strict=True)
    binding_revision: int = Field(ge=1, strict=True)
    run_id: str = Field(min_length=1, max_length=128, strict=True)
    session_epoch: str = Field(min_length=1, max_length=128, strict=True)
    surface_epoch: str = Field(min_length=1, max_length=128, strict=True)
    worker_epoch: str = Field(min_length=1, max_length=128, strict=True)
    desktop_lease_id: str = Field(min_length=1, max_length=128, strict=True)
    lease_expires_at: datetime
    control_revision: int = Field(ge=0, strict=True)
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    screen_origin_x: int = Field(strict=True)
    screen_origin_y: int = Field(strict=True)
    screen_width: int = Field(gt=0, strict=True)
    screen_height: int = Field(gt=0, strict=True)
    crop_origin_x: int = Field(strict=True)
    crop_origin_y: int = Field(strict=True)
    crop_width: int = Field(gt=0, strict=True)
    crop_height: int = Field(gt=0, strict=True)
    dpi_scale: float = Field(gt=0, le=8)
    observed_at: datetime
    foreground: bool = Field(strict=True)
    paused: bool = Field(strict=True)
    has_owned_draft: bool = Field(strict=True)
    has_commit_obligation: bool = Field(strict=True)

    _observed_aware = field_validator("observed_at")(_aware)
    _expiry_aware = field_validator("lease_expires_at")(_aware)


class TrustedNavigationBackend(Protocol):
    """QQ-scoped local capabilities only.

    ``relevant_region_digest(current=False)`` derives its hash from the frozen
    frame pixels, not live pixels or a model claim. ``current=True`` independently
    captures the corresponding current QQ ROI. Regions exclude message bodies
    and composer. Input methods check the supplied deadline/cancel_event at the
    last synchronous input boundary and never issue delayed background input.
    ``set_query`` additionally proves search focus at that boundary; it never
    writes to a chat composer. ``dismiss_known`` supports only close/cancel.
    """

    async def bound_capture(self, target: ContactTarget, *, deadline_at: datetime) -> NavigationFrame: ...
    async def current_scope(self, target: ContactTarget) -> NavigationDesktopScope: ...
    async def relevant_region_digest(
        self, frame: NavigationFrame, region: NavigationRegion, *, current: bool
    ) -> str: ...
    async def search_focused(self, frame: NavigationFrame, region: NavigationRegion) -> bool: ...
    async def click(
        self, frame: NavigationFrame, x: int, y: int, *, deadline_at: datetime,
        cancel_event: asyncio.Event | None,
    ) -> None: ...
    async def scroll(
        self, frame: NavigationFrame, x: int, y: int, direction: str, amount: int,
        *, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> None: ...
    async def set_query(
        self, frame: NavigationFrame, region: NavigationRegion, query: str,
        *, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> None: ...
    async def dismiss_known(
        self, frame: NavigationFrame, region: NavigationRegion, x: int, y: int,
        *, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> None: ...


class VerifiedClickNavigationBackend(Protocol):
    """Optional producer capability that verifies and clicks in one command.

    The producer independently owns the frozen frame and selected candidate or
    search region, captures and compares its current ROI, and checks native
    ownership, guard, cancellation and deadline immediately before one input.
    The frame is consumed after input. This is never a claim that an earlier
    digest RPC remains current, and does not grant query, scroll or send rights.
    """

    async def verified_click(
        self, frame: NavigationFrame, region: NavigationRegion, x: int, y: int,
        *, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> None: ...


class NavigationDesktopError(RuntimeError):
    def __init__(self, code: str, status: DesktopActionStatus = DesktopActionStatus.REJECTED):
        self.code = code
        self.status = status
        super().__init__(code)


_FRAME_SCOPE_FIELDS = (
    "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
    "control_revision", "binding_id", "binding_revision", "process_id", "window_handle",
    "screen_origin_x", "screen_origin_y", "screen_width", "screen_height",
    "crop_origin_x", "crop_origin_y", "crop_width", "crop_height", "dpi_scale",
)


@dataclass
class _RoundCapability:
    target: tuple[str, str, str, int]
    deadline_at: datetime
    cancel_event: asyncio.Event
    external_cancel_event: asyncio.Event | None
    active: bool = True


class _LinkedCancellationEvent(asyncio.Event):
    """Polling sees source revocation synchronously, before mirror tasks run."""

    def __init__(self, sources: tuple[asyncio.Event, ...]):
        super().__init__()
        self.sources = sources

    def is_set(self) -> bool:
        return super().is_set() or any(source.is_set() for source in self.sources)

    async def wait(self) -> bool:
        if self.is_set():
            return True
        return await super().wait()


class ScopedDesktopOperator:
    """Serialize one target round and validate every one-step local action."""

    def __init__(
        self, *, backend: TrustedNavigationBackend, operation_lock: asyncio.Lock | None = None,
        max_frame_age_seconds: float = 20, max_scope_age_seconds: float = 5,
    ) -> None:
        if not 0 < max_frame_age_seconds <= 45 or not 0 < max_scope_age_seconds <= 10:
            raise ValueError("navigation frame/scope age limits must be positive and bounded")
        self.backend = backend
        self.operation_lock = operation_lock if operation_lock is not None else asyncio.Lock()
        self._step_lock = asyncio.Lock()
        self.max_frame_age_seconds = max_frame_age_seconds
        self.max_scope_age_seconds = max_scope_age_seconds
        # Child tasks inherit the same capability object. Expiring the object
        # revokes even a child that outlives its parent's ContextVar reset.
        self._round_capability: ContextVar[_RoundCapability | None] = ContextVar(
            f"qq_navigation_round_{id(self)}", default=None
        )

    @staticmethod
    def _target_key(target: ContactTarget) -> tuple[str, str, str, int]:
        return target.account_id, target.conversation_id, target.binding_id, target.binding_revision

    def _check_live(self, deadline_at: datetime, cancel_event: asyncio.Event | None) -> float:
        _aware(deadline_at)
        if cancel_event is not None and cancel_event.is_set():
            raise NavigationDesktopError("navigation_cancelled", DesktopActionStatus.CANCELLED)
        capability = self._round_capability.get()
        if capability is not None and (not capability.active or capability.cancel_event.is_set()):
            raise NavigationDesktopError("navigation_round_released", DesktopActionStatus.CANCELLED)
        effective = min(deadline_at, capability.deadline_at) if capability is not None else deadline_at
        remaining = (effective - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise NavigationDesktopError("navigation_deadline_expired", DesktopActionStatus.STALE)
        return remaining

    def revoke_round(self) -> None:
        """Synchronously revoke this context's round before bounded cleanup.

        This invalidates inherited capabilities and guards for future inputs.
        It cannot undo an input already handed to the operating system. The
        original round must still exit to release its serialization lock.
        """
        capability = self._round_capability.get()
        if capability is not None:
            capability.active = False
            capability.cancel_event.set()
        self._round_capability.set(None)

    @asynccontextmanager
    async def round(
        self, target: ContactTarget, *, deadline_at: datetime,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        """Keep the same desktop owner while the coordinator awaits its model.

        The backend's short lease must independently remain valid. Reply model
        generation or pacing must occur outside this context.
        """
        capability = self._round_capability.get()
        if capability is not None:
            if capability.target != self._target_key(target):
                raise NavigationDesktopError("navigation_round_target_changed")
            self._check_live(deadline_at, cancel_event)
            yield
            return
        remaining = self._check_live(deadline_at, cancel_event)
        acquire = asyncio.create_task(self.operation_lock.acquire())
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        acquired = False
        token = None
        mirror = None
        try:
            try:
                async with asyncio.timeout(remaining):
                    if cancellation is not None:
                        done, _ = await asyncio.wait((acquire, cancellation), return_when=asyncio.FIRST_COMPLETED)
                        if cancellation in done or cancel_event.is_set():
                            raise NavigationDesktopError("navigation_cancelled", DesktopActionStatus.CANCELLED)
                    await acquire
                    acquired = True
            except TimeoutError as exc:
                raise NavigationDesktopError("navigation_lock_deadline", DesktopActionStatus.STALE) from exc
            self._check_live(deadline_at, cancel_event)
            capability = _RoundCapability(self._target_key(target), deadline_at, asyncio.Event(), cancel_event)
            token = self._round_capability.set(capability)
            if cancel_event is not None:
                async def mirror_cancellation() -> None:
                    await cancel_event.wait()
                    capability.cancel_event.set()
                mirror = asyncio.create_task(mirror_cancellation())
            yield
        finally:
            if capability is not None:
                capability.active = False
                capability.cancel_event.set()
            try:
                if token is not None:
                    try:
                        self._round_capability.reset(token)
                    except ValueError:
                        # The parent can synchronously revoke its capability,
                        # then run bounded __aexit__ in an inherited child task.
                        # ContextVar tokens cannot be reset across contexts.
                        self._round_capability.set(None)
                for pending in (acquire, cancellation, mirror):
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.gather(*(pending for pending in (acquire, cancellation, mirror) if pending is not None), return_exceptions=True)
            finally:
                # Cancellation while draining event waiters must not leak the
                # lock. acquire/cancel may finish in the same loop iteration.
                acquired = acquired or (acquire.done() and not acquire.cancelled() and acquire.exception() is None and acquire.result())
                if acquired:
                    self.operation_lock.release()

    def _validate_scope(
        self, target: ContactTarget, frame: NavigationFrame, scope: NavigationDesktopScope,
        *, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> None:
        self._check_live(deadline_at, cancel_event)
        now = datetime.now(UTC)
        if scope.paused:
            raise NavigationDesktopError("navigation_paused", DesktopActionStatus.CANCELLED)
        if scope.has_owned_draft or scope.has_commit_obligation:
            raise NavigationDesktopError("navigation_write_obligation_present")
        if not scope.foreground:
            raise NavigationDesktopError("navigation_foreground_changed", DesktopActionStatus.STALE)
        if (scope.account_id, scope.conversation_id, scope.binding_id, scope.binding_revision) != self._target_key(target):
            raise NavigationDesktopError("navigation_target_scope_changed", DesktopActionStatus.STALE)
        if scope.lease_expires_at <= now:
            raise NavigationDesktopError("navigation_desktop_lease_expired", DesktopActionStatus.STALE)
        frame_age = (now - frame.captured_at).total_seconds()
        scope_age = (now - scope.observed_at).total_seconds()
        if not 0 <= frame_age <= self.max_frame_age_seconds or not 0 <= scope_age <= self.max_scope_age_seconds:
            raise NavigationDesktopError("navigation_observation_expired", DesktopActionStatus.STALE)
        if any(getattr(frame, field) != getattr(scope, field) for field in _FRAME_SCOPE_FIELDS):
            raise NavigationDesktopError("navigation_frame_scope_changed", DesktopActionStatus.STALE)

    async def _scope_check(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime,
        cancel_event: asyncio.Event | None,
    ) -> None:
        remaining = self._check_live(deadline_at, cancel_event)
        async with asyncio.timeout(remaining):
            scope = await self.backend.current_scope(target)
        self._validate_scope(target, frame, scope, deadline_at=deadline_at, cancel_event=cancel_event)

    @asynccontextmanager
    async def _step(
        self, deadline_at: datetime, cancel_event: asyncio.Event | None,
    ) -> AsyncIterator[asyncio.Event]:
        # A capability is inherited by children; it does not authorize two
        # simultaneous input steps within the same round.
        acquired = False
        capability = self._round_capability.get()
        sources = {source for source in (
            capability.cancel_event if capability is not None else None, cancel_event,
        ) if source is not None}
        input_cancel = _LinkedCancellationEvent(tuple(sources))
        mirrors: list[asyncio.Task] = []
        try:
            remaining = self._check_live(deadline_at, cancel_event)
            async with asyncio.timeout(remaining):
                await self._step_lock.acquire()
                acquired = True
            self._check_live(deadline_at, cancel_event)
            for source in sources:
                if source.is_set():
                    input_cancel.set()
                async def mirror_signal(event: asyncio.Event = source) -> None:
                    await event.wait()
                    input_cancel.set()
                mirrors.append(asyncio.create_task(mirror_signal()))
            yield input_cancel
        finally:
            # Even a backend child that swallowed task cancellation loses its
            # permission when the step ends. Round and step signals are joined,
            # so a coordinator may use its own per-action cancellation event.
            input_cancel.set()
            try:
                for mirror in mirrors:
                    mirror.cancel()
                if mirrors:
                    await asyncio.gather(*mirrors, return_exceptions=True)
            finally:
                if acquired:
                    self._step_lock.release()

    async def capture(self, target: ContactTarget, *, deadline_at: datetime) -> NavigationFrame:
        async with self.round(target, deadline_at=deadline_at), self._step(deadline_at, None):
            remaining = self._check_live(deadline_at, None)
            async with asyncio.timeout(remaining):
                frame = await self.backend.bound_capture(target, deadline_at=deadline_at)
            if frame.binding_id != target.binding_id or frame.binding_revision != target.binding_revision:
                raise NavigationDesktopError("navigation_capture_target_changed", DesktopActionStatus.STALE)
            await self._scope_check(target, frame, deadline_at=deadline_at, cancel_event=None)
            return frame

    @staticmethod
    def _region(request: NavigationRequest, decision: NavigationDecision) -> NavigationRegion | None:
        kind = {
            NavigationAction.CLICK_CANDIDATE: "candidate", NavigationAction.OPEN_SEARCH: "search",
            NavigationAction.SET_TARGET_QUERY: "search", NavigationAction.SCROLL_LIST: "list",
            NavigationAction.DISMISS_KNOWN_OVERLAY: "overlay",
        }.get(decision.action)
        if kind is None:
            return None
        return next(region for region in request.frame.allowed_regions if
                    region.kind == kind and (decision.bbox is None or region.bbox.contains(decision.bbox))
                    and (decision.overlay_id is None or region.overlay_id == decision.overlay_id))

    async def execute(
        self, request: NavigationRequest, decision: NavigationDecision,
        *, cancel_event: asyncio.Event | None = None,
    ) -> DesktopActionResult:
        attempted = False
        try:
            validate_decision(request, decision)
            async with self.round(request.target, deadline_at=request.deadline_at, cancel_event=cancel_event), self._step(request.deadline_at, cancel_event) as input_cancel:
                await self._scope_check(request.target, request.frame, deadline_at=request.deadline_at, cancel_event=cancel_event)
                region = self._region(request, decision)
                verified_click = None
                if decision.action in {NavigationAction.CLICK_CANDIDATE, NavigationAction.OPEN_SEARCH}:
                    verified_click = getattr(self.backend, "verified_click", None)
                    if verified_click is not None and not callable(verified_click):
                        raise NavigationDesktopError("navigation_verified_click_unavailable")
                remaining = self._check_live(request.deadline_at, cancel_event)
                async with asyncio.timeout(remaining):
                    if region is not None and verified_click is None:
                        expected = await self.backend.relevant_region_digest(request.frame, region, current=False)
                        actual = await self.backend.relevant_region_digest(request.frame, region, current=True)
                        if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
                            raise NavigationDesktopError("navigation_region_changed", DesktopActionStatus.STALE)
                    if decision.action == NavigationAction.SET_TARGET_QUERY:
                        if not await self.backend.search_focused(request.frame, region):
                            raise NavigationDesktopError("navigation_search_focus_unproven")
                    # Local proof can itself take time; recheck before input.
                    await self._scope_check(request.target, request.frame, deadline_at=request.deadline_at, cancel_event=cancel_event)
                    capability = self._round_capability.get()
                    guard = {
                        "deadline_at": min(request.deadline_at, capability.deadline_at),
                        "cancel_event": input_cancel,
                    }
                    if decision.bbox is not None:
                        # Pixels are already physical; DPI must not be applied again.
                        x = request.frame.crop_origin_x + (decision.bbox.left + decision.bbox.right) // 2
                        y = request.frame.crop_origin_y + (decision.bbox.top + decision.bbox.bottom) // 2
                    if decision.action in {NavigationAction.CLICK_CANDIDATE, NavigationAction.OPEN_SEARCH}:
                        attempted = True
                        if verified_click is not None:
                            await verified_click(request.frame, region, x, y, **guard)
                        else:
                            await self.backend.click(request.frame, x, y, **guard)
                    elif decision.action == NavigationAction.SCROLL_LIST:
                        attempted = True
                        await self.backend.scroll(request.frame, x, y, decision.scroll_direction, decision.scroll_amount, **guard)
                    elif decision.action == NavigationAction.SET_TARGET_QUERY:
                        attempted = True
                        query = request.target.trusted_queries[decision.query_alias_index]
                        await self.backend.set_query(request.frame, region, query, **guard)
                    elif decision.action == NavigationAction.DISMISS_KNOWN_OVERLAY:
                        attempted = True
                        await self.backend.dismiss_known(request.frame, region, x, y, **guard)
                    elif decision.action == NavigationAction.WAIT:
                        duration = min(decision.wait_ms / 1000, self._check_live(request.deadline_at, cancel_event))
                        try:
                            await asyncio.wait_for(input_cancel.wait(), timeout=duration)
                        except TimeoutError:
                            pass
                        self._check_live(request.deadline_at, cancel_event)
                    else:
                        # candidate_opened/unable are model observations, not input.
                        raise NavigationDesktopError("navigation_decision_is_not_an_action")
                    self._check_live(request.deadline_at, cancel_event)
                    frame = await self.backend.bound_capture(request.target, deadline_at=request.deadline_at)
                    await self._scope_check(request.target, frame, deadline_at=request.deadline_at, cancel_event=cancel_event)
                    return DesktopActionResult(status=DesktopActionStatus.ACTION_ATTEMPTED, frame_id=request.frame.frame_id, next_frame=frame)
        except asyncio.CancelledError:
            raise
        except NavigationDesktopError as exc:
            return DesktopActionResult(status=exc.status, frame_id=request.frame.frame_id, error_code=exc.code)
        except (ValueError, StopIteration):
            return DesktopActionResult(status=DesktopActionStatus.REJECTED, frame_id=request.frame.frame_id, error_code="navigation_decision_invalid")
        except TimeoutError:
            return DesktopActionResult(
                status=DesktopActionStatus.ACTION_ATTEMPTED if attempted else DesktopActionStatus.STALE,
                frame_id=request.frame.frame_id,
                error_code="navigation_local_result_unavailable" if attempted else "navigation_deadline_expired",
            )
        except Exception:
            return DesktopActionResult(
                status=DesktopActionStatus.ACTION_ATTEMPTED if attempted else DesktopActionStatus.REJECTED,
                frame_id=request.frame.frame_id, error_code="navigation_local_result_unavailable" if attempted else "navigation_backend_unavailable",
            )
