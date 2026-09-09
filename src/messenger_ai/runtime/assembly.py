from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from messenger_ai.hub.service import HubService, SQLiteHubStore
from messenger_ai.llm.providers import ModelProvider
from messenger_ai.memory.service import MemoryService
from messenger_ai.memory.store import SQLiteMemoryStore
from messenger_ai.pacing import PacingScheduler

from .contracts import V5MessengerDriver
from .coordinator import RuntimeCoordinator
from .state import RuntimeState


@dataclass(frozen=True)
class RuntimeApplication:
    data_dir: Path
    state: RuntimeState
    hub: HubService
    memory: MemoryService
    pacing: PacingScheduler
    planner_provider: ModelProvider
    driver: V5MessengerDriver
    coordinator: RuntimeCoordinator


def assemble_runtime(*, data_dir: str | Path, planner_provider: ModelProvider,
                     driver: V5MessengerDriver) -> RuntimeApplication:
    """Construct the production authorities with durable, separate SQLite stores."""
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = RuntimeState(root / "runtime.sqlite3")
    hub = HubService(SQLiteHubStore(root / "hub.sqlite3"))
    memory = MemoryService(SQLiteMemoryStore(root / "memory.sqlite3"))
    pacing = PacingScheduler(root / "pacing.sqlite3")
    coordinator = RuntimeCoordinator(state=state, hub=hub, memory=memory, pacing=pacing)
    coordinator.recover()
    return RuntimeApplication(root, state, hub, memory, pacing, planner_provider, driver, coordinator)
