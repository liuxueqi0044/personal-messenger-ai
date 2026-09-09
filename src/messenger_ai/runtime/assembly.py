from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from messenger_ai.hub.service import HubService, SQLiteHubStore
from messenger_ai.domain.clock import ClockPort
from messenger_ai.llm.planner import ReplyPlanner
from messenger_ai.llm.providers import ModelProvider
from messenger_ai.memory.service import MemoryService
from messenger_ai.memory.store import SQLiteMemoryStore
from messenger_ai.pacing import PacingScheduler
from messenger_ai.policy import AuthorizationService, CapabilitySnapshot, PolicyEngine, SQLiteAuthorizationStore
from messenger_ai.rules.service import AtomicRulePackStore
from .contracts import V5MessengerDriver
from .coordinator import RuntimeCoordinator
from .due_dispatch import DueCoordinator
from .planning import PlanningCoordinator
from .send_dispatcher import SendDispatcher
from .state import RuntimeState


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

    async def tick(self) -> dict[str, int]:
        rows = self.state.connection.execute(
            "SELECT conversation_id FROM runtime_conversations ORDER BY conversation_id"
        ).fetchall()
        observed = 0
        if rows:
            conversation_id = rows[self._rr_index % len(rows)]["conversation_id"]
            self._rr_index += 1
            await self.coordinator.observe_driver(self.driver, conversation_id)
            observed = 1
        delivered = self.coordinator.dispatch_events()
        finished = {task for task in self._planning_tasks if task.done()}
        for task in finished:
            task.result()
        self._planning_tasks -= finished
        available = max(0, self.model_concurrency - len(self._planning_tasks))
        started = 0
        for row in self.state.claim_planning_jobs(limit=available):
            self._planning_tasks.add(asyncio.create_task(self.planning.run_claimed(row)))
            started += 1
        sent = int(await self.due.dispatch_one())
        return {"observed": observed, "events": delivered, "planning_started": started, "due": sent}

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

    async def run_forever(self, *, poll_interval: float = .25) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(poll_interval)

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
                     clock: ClockPort | None = None) -> RuntimeApplication:
    if model_concurrency < 1:
        raise ValueError("model_concurrency must be positive")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = RuntimeState(root / "runtime.sqlite3")
    hub = HubService(SQLiteHubStore(root / "hub.sqlite3"), clock=clock)
    memory = MemoryService(SQLiteMemoryStore(root / "memory.sqlite3"), clock=clock)
    pacing = PacingScheduler(root / "pacing.sqlite3", clock=clock)
    rules = AtomicRulePackStore(str(root / "rules.sqlite3"))
    engine = PolicyEngine(hub.now)
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
        authorization=authorization, dispatcher=dispatcher, capability_provider=lambda: capability)
    coordinator.recover()
    state.recover_planning_jobs()
    return RuntimeApplication(root, state, hub, memory, pacing, rules, planner_provider, driver,
                              coordinator, planning, due, model_concurrency)
