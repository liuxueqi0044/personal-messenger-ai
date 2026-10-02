"""Trusted runtime assembly of one QQ hybrid actor's finite desktop round.

The parent only reads runtime control values and publishes the guard. Native
QQ access remains inside the fixed supervised worker. The same supplied lock
must also be used by navigation; a failed worker reap retains that lock and its
publisher until HybridWorkerSession.retry_cleanup confirms worker death.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
import time
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelectorPack
from messenger_ai.adapters.qq.navigation.contracts import ContactTarget, _aware
from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState
from messenger_ai.adapters.qq.vm_driver.hybrid_process import HybridProcessError, HybridWorkerProcess
from messenger_ai.adapters.qq.vm_driver.hybrid_worker import HybridWorkerConfig
from .qq_guard import QQNavigationGuardPublisher, QQGuardPublicationError

if TYPE_CHECKING:
    from .qq_hybrid_config import QQHybridSettings


Purpose = Literal["observe", "draft", "verify", "health"]


class HybridLiveSnapshot(Protocol):
    def __call__(self, *, target: ContactTarget, purpose: Purpose, worker_epoch: UUID,
                 deadline_at: datetime, desktop_lease_id: str,
                 observation_epoch: str) -> NavigationGuardState: ...


class QQHybridRoundError(HybridProcessError):
    pass


class _DesktopOwner:
    """Already acquired lock; process __aexit__ proves reap before calling us."""

    def __init__(self, lock, publisher, *, stop_at, epoch, actor_task, on_released):
        self.lock, self.publisher, self.stop_at = lock, publisher, stop_at
        self.epoch, self.actor_task, self.on_released = epoch, actor_task, on_released
        self.worker: HybridWorkerProcess | None = None
        self.monitor: asyncio.Task | None = None
        self.entered, self.released = False, False
        self.closing, self.revoked = False, False

    async def __aenter__(self):
        if self.entered or self.released or not self.lock.locked():
            raise QQHybridRoundError("qq_hybrid_desktop_owner_invalid")
        self.entered = True

    async def __aexit__(self, *_):
        if self.released:
            return
        # This is also checked on explicit retry. No caller may release the
        # shared navigation lock merely because a terminate call returned.
        recovering = False
        if self.worker is not None:
            self.worker.close()  # Public proof of reap; usually already closed.
            recovering = self.worker.cleanup_pending
        if self.publisher is not None:
            budget = .5 if recovering else max(.001, self.stop_at-time.monotonic())
            async with asyncio.timeout(budget):
                await self.publisher.aclose()
        if self.monitor is not None:
            cancelling = asyncio.current_task().cancelling()
            self.monitor.cancel()
            # The monitor has no native/IPC await and cannot submit actions.
            try:
                await self.monitor
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() > cancelling:
                    raise
        self.lock.release()
        self.released = True
        self.on_released(self)


class QQHybridRoundFactory:
    """Production factory with fixed config and a real live control source."""

    def __init__(self, settings: QQHybridSettings, selector_pack: QQSelectorPack,
                 bindings: tuple[QQIdentityBinding, ...], operation_lock: asyncio.Lock,
                 live_snapshot: HybridLiveSnapshot, *,
                 publisher_factory=QQNavigationGuardPublisher,
                 _worker_factory=HybridWorkerProcess):
        if not isinstance(operation_lock, asyncio.Lock) or not callable(live_snapshot):
            raise TypeError("QQ hybrid rounds require a shared lock and trusted live snapshot")
        self.settings = settings
        self.selector_pack = QQSelectorPack.model_validate(selector_pack.model_dump())
        self.bindings = {binding.binding_id: QQIdentityBinding.model_validate(binding.model_dump()) for binding in bindings}
        if len(self.bindings) != len(bindings) or self.bindings.keys() != settings.targets.keys():
            raise ValueError("QQ hybrid round binding scope must be complete and unique")
        self.operation_lock, self.live_snapshot = operation_lock, live_snapshot
        self._publisher_factory, self._worker_factory = publisher_factory, _worker_factory
        self._used_epochs: set[UUID] = set()
        self._owners: dict[UUID, _DesktopOwner] = {}
        directory = Path(settings.guard_directory)
        if not directory.is_absolute():
            raise ValueError("QQ hybrid guards require an absolute directory")
        self._directory = directory.resolve()
        # Validate every trusted target/config now, before any lock or file.
        for binding_id in self.bindings:
            self._config(binding_id, uuid4(), self._directory / f"qq-hybrid-config-check-{uuid4()}.json")

    def _config(self, binding_id, epoch, path):
        return HybridWorkerConfig(
            navigation=self.settings.navigation_config(worker_epoch=epoch, guard_state_path=str(path)),
            selector_pack=self.selector_pack, bindings=(self.bindings[binding_id],),
            targets=(self.settings.targets[binding_id],), expectations=(self.settings.expectations[binding_id],),
            helper_path=str(self.settings.helper_path), vault_path=str(self.settings.vault_path),
            prepare_write_reserve_seconds=self.settings.prepare_write_reserve_seconds)

    def _snapshot(self, *, target, purpose, epoch, deadline, lease, observation, config):
        try:
            value = self.live_snapshot(target=target, purpose=purpose, worker_epoch=epoch,
                deadline_at=deadline, desktop_lease_id=lease, observation_epoch=observation)
            if not isinstance(value, NavigationGuardState):
                if inspect.iscoroutine(value):
                    value.close()
                raise ValueError
            value = NavigationGuardState.model_validate(value.model_dump(warnings=False))
            nav, now = config.navigation, datetime.now(UTC)
            if (value.target != target or value.run_id != nav.expected_run_id
                    or value.session_epoch != self.settings.session_epoch
                    or value.surface_epoch != self.settings.surface_epoch
                    or value.worker_epoch != str(epoch) or value.desktop_lease_id != lease
                    or value.observation_epoch != observation
                    or value.process_id != nav.window.process_id or value.window_handle != nav.window.window_handle
                    or value.process_started_at_100ns != nav.expected_process_started_at_100ns
                    or not now < value.lease_expires_at <= deadline
                    or not 0 <= (now-value.published_at).total_seconds() <= 5):
                raise ValueError
            return value
        except Exception:
            raise QQHybridRoundError("qq_hybrid_live_snapshot_unproven") from None

    @staticmethod
    def _revoke_owner(owner):
        owner.revoked = True
        try:
            if owner.worker is not None:
                owner.worker.revoke()
        except HybridProcessError:
            pass  # Actor-owned protected exit retries reap and retains on failure.
        finally:
            # The actor owns the ContextVars and exits its original context.
            # Never dispatch context __aexit__ in this heartbeat task.
            if (not owner.closing and not owner.actor_task.done()
                    and owner.actor_task is not asyncio.current_task()):
                owner.actor_task.cancel()

    @classmethod
    async def _watch_publisher(cls, owner):
        await owner.publisher.failed_event.wait()
        cls._revoke_owner(owner)

    def _released(self, owner):
        if self._owners.get(owner.epoch) is owner:
            self._owners.pop(owner.epoch)

    def _refresh_owner(self, epoch):
        owner = self._owners.get(epoch) if isinstance(epoch, UUID) else None
        capability = owner.worker._owner_round if owner is not None and owner.worker is not None else None
        if (owner is None or not owner.entered or owner.released or owner.closing or owner.revoked
                or not self.operation_lock.locked() or owner.actor_task.done()
                or capability is None or not capability.active):
            raise QQHybridRoundError("qq_hybrid_guard_refresh_owner_unavailable")
        return owner

    async def refresh_guard(self, worker_epoch: UUID):
        """Fence durable runtime changes before IPC; no UI/capability transfer."""
        owner = self._refresh_owner(worker_epoch)
        try:
            async with asyncio.timeout(max(0, owner.stop_at-.5-time.monotonic())):
                await owner.publisher.publish_now()
            self._refresh_owner(worker_epoch)
            owner.publisher.raise_if_failed()
        except BaseException as exc:
            self._revoke_owner(owner)
            if isinstance(exc, asyncio.CancelledError):
                raise
            code = exc.code if isinstance(exc, QQGuardPublicationError) else "qq_hybrid_guard_refresh_failed"
            raise QQHybridRoundError(code, cleanup_required=owner.worker.mutation_may_have_occurred) from None

    @asynccontextmanager
    async def __call__(self, *, binding_id: str, purpose: Purpose, worker_epoch: UUID,
                       deadline_at: datetime):
        _aware(deadline_at)
        if (binding_id not in self.bindings or purpose not in {"observe", "draft", "verify", "health"}
                or not isinstance(worker_epoch, UUID) or worker_epoch.int == 0):
            raise QQHybridRoundError("qq_hybrid_round_scope_invalid")
        if worker_epoch in self._used_epochs:
            raise QQHybridRoundError("qq_hybrid_worker_epoch_reused")
        self._used_epochs.add(worker_epoch)
        now, started = datetime.now(UTC), time.monotonic()
        duration = min(self.settings.max_seconds, 45, (deadline_at-now).total_seconds())
        if duration <= 1:
            raise QQHybridRoundError("qq_hybrid_round_deadline_expired")
        deadline, stop_at = now + timedelta(seconds=duration), started + duration
        path = self._directory / f"qq-hybrid-{worker_epoch}.json"
        if path.exists():
            raise QQHybridRoundError("qq_hybrid_guard_path_reused")
        config = self._config(binding_id, worker_epoch, path)
        target = self.settings.targets[binding_id]
        lease, observation = str(uuid4()), str(uuid4())
        acquired, owner, worker = False, None, None
        try:
            async with asyncio.timeout(max(0, stop_at-.5-time.monotonic())):
                await self.operation_lock.acquire()
            acquired = True
            source = lambda: self._snapshot(target=target, purpose=purpose, epoch=worker_epoch,
                deadline=deadline, lease=lease, observation=observation, config=config)
            initial = source()  # Actual control values; no synthetic flags.
            self._directory.mkdir(parents=True, exist_ok=True)
            publisher = self._publisher_factory(path, initial, source)
            owner = _DesktopOwner(self.operation_lock, publisher, stop_at=stop_at, epoch=worker_epoch,
                                  actor_task=asyncio.current_task(), on_released=self._released)
            self._owners[worker_epoch] = owner
            async with asyncio.timeout(max(0, stop_at-.5-time.monotonic())):
                await publisher.start()
            publisher.raise_if_failed()
            # Startup and lock wait consume the original monotonic budget.
            # An intervening UTC rollback must not give the child watchdog a
            # fresh 45 seconds measured from the later process entry.
            remaining = min(self.settings.max_seconds, stop_at-time.monotonic())
            if remaining <= 1:
                raise QQHybridRoundError("qq_hybrid_round_deadline_expired")
            worker = self._worker_factory(config, max_seconds=remaining)
            if not isinstance(worker, HybridWorkerProcess) or worker.worker_epoch != worker_epoch:
                raise QQHybridRoundError("qq_hybrid_worker_factory_invalid")
            owner.worker = worker
            async with worker.protect_desktop_round(owner, deadline_at=deadline):
                owner.monitor = asyncio.create_task(self._watch_publisher(owner))
                publisher.raise_if_failed()
                try:
                    yield worker
                finally:
                    owner.closing = True
                # Failure during the round has already synchronously revoked
                # the worker and cancelled its actor. Do not turn a proven
                # successful reap/exit into a false physical cleanup hold.
        except QQGuardPublicationError as exc:
            raise QQHybridRoundError(exc.code, cleanup_required=bool(worker and worker.mutation_may_have_occurred)) from None
        finally:
            if acquired and owner is None:
                # No publisher, worker, or subprocess exists at this stage.
                self.operation_lock.release()
            elif owner is not None and not owner.released:
                if worker is None or not worker.cleanup_pending:
                    # Covers yield-before-entry failures. After a failed reap,
                    # protect_desktop_round retains this owner for retry_cleanup.
                    if worker is not None:
                        worker.revoke()
                        worker.close()
                    await owner.__aexit__(None, None, None)


__all__ = ["QQHybridRoundFactory", "QQHybridRoundError", "HybridLiveSnapshot"]
