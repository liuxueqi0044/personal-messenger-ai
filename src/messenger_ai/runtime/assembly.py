from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from messenger_ai.domain import SendOperation
from messenger_ai.domain.clock import ClockPort
from messenger_ai.hub.service import HubService, SQLiteHubStore
from messenger_ai.llm.planner import ReplyPlanner
from messenger_ai.llm.providers import ModelProvider
from messenger_ai.memory.service import MemoryService
from messenger_ai.memory.store import SQLiteMemoryStore
from messenger_ai.pacing import DueForRevalidation, PacingScheduler
from messenger_ai.policy import (
    AuthorizationService,
    CapabilitySnapshot,
    PolicyEngine,
    SQLiteAuthorizationStore,
)
from messenger_ai.rules.service import AtomicRulePackStore

from .contracts import V5MessengerDriver
from .coordinator import RuntimeCoordinator
from .due_dispatch import DueCoordinator, DueNavigationPreflight
from .planning import PlanningCoordinator
from .send_dispatcher import SendDispatcher
from .state import RuntimeState, VerifiedSendStorePaths
from .staged_preparation import StagedPreparationPort


@dataclass
class RuntimeApplication:
    data_dir: Path
    state: RuntimeState
    hub: HubService
    memory: MemoryService
    pacing: PacingScheduler
    rules: AtomicRulePackStore
    planner_provider: ModelProvider
    driver: V5MessengerDriver
    coordinator: RuntimeCoordinator
    planning: PlanningCoordinator
    due: DueCoordinator
    model_concurrency: int = 2
    _rr_index: int = 0
    _planning_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    _tick_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _pause_requested: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _pause_request_count: int = field(default=0, init=False, repr=False)
    _pause_request_tokens: set[object] = field(
        default_factory=set, init=False, repr=False
    )

    async def tick(self) -> dict[str, int]:
        async with self._tick_lock:
            return await self._tick_once()

    async def _tick_once(self) -> dict[str, int]:
        _, globally_paused, _ = self.state.global_control()
        globally_paused = globally_paused or self._pause_requested.is_set()
        rows = self.state.connection.execute(
            "SELECT conversation_id FROM runtime_conversations ORDER BY conversation_id"
        ).fetchall()
        active_ids = getattr(self.driver, "observation_conversation_ids", None)
        if active_ids is not None:
            rows = [row for row in rows if row["conversation_id"] in active_ids]
        observed = 0
        if rows and not globally_paused:
            conversation_id = rows[self._rr_index % len(rows)]["conversation_id"]
            self._rr_index += 1
            await self.coordinator.observe_driver(self.driver, conversation_id)
            observed = 1
        # A file-control pause request can arrive while a slow OBSERVE is
        # draining under this tick lock.  Re-read both the durable state and
        # the in-memory request fence before this same tick can start planning
        # or enter the send path.
        _, globally_paused, _ = self.state.global_control()
        globally_paused = globally_paused or self._pause_requested.is_set()
        delivered = self.coordinator.dispatch_events()
        finished = {task for task in self._planning_tasks if task.done()}
        for task in finished:
            task.result()
        self._planning_tasks -= finished
        available = max(0, self.model_concurrency - len(self._planning_tasks))
        started = 0
        if not globally_paused:
            for row in self.state.claim_planning_jobs(limit=available):
                self._planning_tasks.add(asyncio.create_task(self.planning.run_claimed(row)))
                started += 1
        sent = int(await self.due.dispatch_one()) if not globally_paused else 0
        return {"observed": observed, "events": delivered, "planning_started": started, "due": sent}

    def begin_global_pause_from_control(self) -> object:
        """Install a synchronous fence before acknowledging a pause request."""

        token = object()
        self._pause_request_tokens.add(token)
        self._pause_request_count = len(self._pause_request_tokens)
        self._pause_requested.set()
        return token

    def cancel_global_pause_from_control(self, token: object) -> None:
        """Release a fence whose acknowledgement could not be published."""

        self._release_global_pause_token(token)

    def _release_global_pause_token(self, token: object) -> None:
        if token not in self._pause_request_tokens:
            raise RuntimeError("global pause fence token is not active")
        self._pause_request_tokens.remove(token)
        self._pause_request_count = len(self._pause_request_tokens)
        if not self._pause_request_tokens:
            self._pause_requested.clear()

    async def set_global_pause_from_control(
        self,
        *,
        paused: bool,
        reason: str,
        pause_fence_token: object | None = None,
    ) -> int:
        """Fence new work immediately, then publish pause after UI work drains."""
        if paused:
            token = (
                pause_fence_token
                if pause_fence_token is not None
                else self.begin_global_pause_from_control()
            )
            if token not in self._pause_request_tokens:
                raise RuntimeError("global pause fence token is not active")
            try:
                async with self._tick_lock:
                    revision, current, _ = self.state.global_control()
                    if current:
                        return revision
                    if not self.state.set_global_pause(
                        paused=True,
                        expected_revision=revision,
                        reason=reason,
                    ):
                        raise RuntimeError(
                            "global pause revision changed during control request"
                        )
                    return revision + 1
            finally:
                self._release_global_pause_token(token)
        if pause_fence_token is not None:
            raise RuntimeError("resume cannot consume a pause fence token")
        async with self._tick_lock:
            revision, current, _ = self.state.global_control()
            if current == paused:
                return revision
            if not self.state.set_global_pause(
                paused=paused,
                expected_revision=revision,
                reason=reason,
            ):
                raise RuntimeError("global pause revision changed during control request")
            return revision + 1

    async def dispatch_exact_with_control_fence(self, **kwargs):
        """Dispatch one exact plan while sharing the global pause/UI fence."""

        async with self._tick_lock:
            _, globally_paused, _ = self.state.global_control()
            if globally_paused or self._pause_requested.is_set():
                return False, None
            return await self.due.dispatch_exact(**kwargs)

    async def run_until_idle(self, *, max_ticks: int = 100) -> None:
        quiet_ticks = 0
        for _ in range(max_ticks):
            report = await self.tick()
            if self._planning_tasks:
                await asyncio.gather(*tuple(self._planning_tasks))
                self._planning_tasks.clear()
                continue
            pending = self.state.connection.execute(
                "SELECT 1 FROM runtime_event_outbox WHERE status='pending' LIMIT 1"
            ).fetchone()
            jobs = self.state.connection.execute(
                "SELECT 1 FROM runtime_planning_jobs WHERE status='pending' LIMIT 1"
            ).fetchone()
            if not pending and not jobs and not report["due"] and not report["events"] and not report["planning_started"]:
                quiet_ticks += 1
            else:
                quiet_ticks = 0
            contact_count = self.state.connection.execute("SELECT COUNT(*) FROM runtime_conversations").fetchone()[0]
            if quiet_ticks >= max(1, int(contact_count)):
                return
        raise RuntimeError("runtime did not become idle")

    async def run_forever(self, *, poll_interval: float = .25,
                          stop_event: asyncio.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            await self.tick()
            if stop_event is None:
                await asyncio.sleep(poll_interval)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        """Idempotently stop in-flight planning and close every owned authority."""
        for task in tuple(self._planning_tasks):
            task.cancel()
        if self._planning_tasks:
            await asyncio.gather(*tuple(self._planning_tasks), return_exceptions=True)
            self._planning_tasks.clear()
        for obj in (self.driver, self.planner_provider):
            close_async = getattr(obj, "aclose", None)
            if callable(close_async):
                await close_async()
            else:
                close = getattr(obj, "close", None) or getattr(obj, "stop", None)
                if callable(close):
                    await asyncio.to_thread(close)
        auth_store = getattr(self.due.authorization, "_store", None)
        for obj in (auth_store, self.rules, self.pacing, self.memory.store,
                    self.hub.store, self.state):
            close = getattr(obj, "close", None)
            if callable(close):
                close()
            elif hasattr(obj, "connection"):
                obj.connection.close()


def assemble_runtime(*, data_dir: str | Path, planner_provider: ModelProvider,
                     driver: V5MessengerDriver, capability: CapabilitySnapshot,
                     authorization_signing_key: bytes, model_concurrency: int = 2,
                     content_policy_checks_enabled: bool = True,
                     clock: ClockPort | None = None,
                     recover_persistent_state: bool = True,
                     initially_paused: bool = False,
                     initial_pause_reason: str = "initial_global_pause",
                     navigation_preflight: DueNavigationPreflight | None = None,
                     due_operation_recovery: Callable[[DueForRevalidation, SendOperation], Awaitable[SendOperation | None]] | None = None,
                     staged_preparation: StagedPreparationPort | None = None,
                     staged_monotonic_ns_clock: Callable[[], int] = time.monotonic_ns) -> RuntimeApplication:
    if model_concurrency < 1:
        raise ValueError("model_concurrency must be positive")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = RuntimeState(
        root / "runtime.sqlite3",
        verified_send_stores=VerifiedSendStorePaths(
            hub=root / "hub.sqlite3",
            pacing=root / "pacing.sqlite3",
        ),
        initially_paused=initially_paused,
        initial_pause_reason=initial_pause_reason,
    )
    if initially_paused:
        revision, paused, _reason = state.global_control()
        if not paused and not state.set_global_pause(
            paused=True,
            expected_revision=revision,
            reason=initial_pause_reason,
        ):
            state.close()
            raise RuntimeError("initial global pause fence changed")
    hub = HubService(SQLiteHubStore(root / "hub.sqlite3"), clock=clock)
    memory = MemoryService(SQLiteMemoryStore(root / "memory.sqlite3"), clock=clock)
    pacing = PacingScheduler(root / "pacing.sqlite3", clock=clock)
    rules = AtomicRulePackStore(str(root / "rules.sqlite3"))
    engine = PolicyEngine(
        hub.now,
        content_policy_checks_enabled=content_policy_checks_enabled,
    )
    authorization = AuthorizationService(
        engine, SQLiteAuthorizationStore(root / "authorization.sqlite3"),
        authorization_signing_key, hub.now)
    coordinator = RuntimeCoordinator(state=state, hub=hub, memory=memory, pacing=pacing)
    planning = PlanningCoordinator(
        state=state, hub=hub, memory=memory, rules=rules,
        planner=ReplyPlanner(planner_provider), policy=engine, pacing=pacing, capability=capability)
    dispatcher = SendDispatcher(
        state=state, hub=hub, pacing=pacing, authorization=authorization, driver=driver)
    due = DueCoordinator(
        state=state, hub=hub, pacing=pacing, rules=rules,
        authorization=authorization, dispatcher=dispatcher, capability_provider=lambda: capability,
        navigation_preflight=navigation_preflight, due_operation_recovery=due_operation_recovery,
        staged_preparation=staged_preparation, monotonic_ns_clock=staged_monotonic_ns_clock)
    if recover_persistent_state:
        coordinator.recover()
        state.recover_planning_jobs()
    app = RuntimeApplication(root, state, hub, memory, pacing, rules, planner_provider, driver,
                             coordinator, planning, due, model_concurrency)
    if staged_preparation is not None:
        due.control_cancelled = app._pause_requested.is_set
    return app
