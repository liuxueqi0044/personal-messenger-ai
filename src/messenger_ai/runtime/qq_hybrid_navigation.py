"""Finite production QQ navigation, with no sending or parent native access."""
from __future__ import annotations

import asyncio
from contextvars import Context
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
import time
from typing import Protocol
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.navigation.contracts import (
    ContactTarget, NavigationBudget, NavigationOutcome, NavigationStatus,
)
from messenger_ai.adapters.qq.navigation.ports import VisionNavigator
from messenger_ai.adapters.qq.navigation.profile_verifier import (
    ProfileCurrentChatVerifier, ProfileVerificationContext,
)
from messenger_ai.adapters.qq.navigation.supervised_profile import (
    SupervisedProfileConfig, SupervisedProfileSource,
)
from messenger_ai.adapters.qq.navigation.windows_backend import (
    NavigationGuardState, ProcessScopedDesktopOperator, WindowsNavigationBackend,
)
from .due_dispatch import DueNavigationPreflightResult
from .navigation import NavigationCoordinator, RuntimeNavigationResult
from .navigation_state import NavigationTaskStore
from .qq_guard import QQNavigationGuardPublisher
from .qq_hybrid_config import QQHybridSettings


class NavigationLiveSnapshot(Protocol):
    def __call__(self, *, target: ContactTarget, purpose: str, worker_epoch: UUID,
                 deadline_at: datetime, desktop_lease_id: str,
                 observation_epoch: str) -> NavigationGuardState: ...


class QQHybridNavigationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Cancellation(asyncio.Event):
    """Read the caller signal synchronously at the final native boundary."""
    def __init__(self, external):
        super().__init__()
        self.external = external

    def is_set(self):
        return super().is_set() or (self.external is not None and self.external.is_set())

    async def wait(self):
        if self.is_set():
            return True
        return await super().wait()


class _CleanupJournal:
    """Only content-free ownership; a restart cannot silently clear a hold."""
    def __init__(self, store):
        self.store = store
        with store._transaction():
            store.connection.execute("""CREATE TABLE IF NOT EXISTS runtime_nav_cleanup_obligations(
                worker_epoch TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                task_id TEXT NOT NULL, guard_path TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def held(self, account_id):
        with self.store._lock:
            return self.store.connection.execute(
                "SELECT 1 FROM runtime_nav_cleanup_obligations WHERE account_id=? LIMIT 1",
                (account_id,)).fetchone() is not None

    def begin(self, round_):
        with self.store._transaction():
            if self.held(round_.target.account_id):
                raise QQHybridNavigationError("qq_navigation_cleanup_required")
            self.store.connection.execute(
                "INSERT INTO runtime_nav_cleanup_obligations VALUES(?,?,?,?,?)",
                (str(round_.epoch), round_.target.account_id, round_.task_id,
                 str(round_.path), datetime.now(UTC).isoformat()))

    def finish(self, round_):
        with self.store._transaction():
            self.store.connection.execute(
                "DELETE FROM runtime_nav_cleanup_obligations WHERE worker_epoch=? AND task_id=?",
                (str(round_.epoch), round_.task_id))


class _DeferredBackend:
    """Windows backend construction starts a process; defer it until admission."""
    def __init__(self, config, target, deadline):
        self.config, self.target, self.deadline = config, target, deadline
        self.backend = None
        self.starting = False

    def start(self):
        self.starting = True
        self.backend = WindowsNavigationBackend(self.config, self.target, deadline_at=self.deadline)
        self.starting = False

    def revoke(self):
        if self.backend is not None:
            self.backend.revoke()

    def close(self):
        if self.starting and self.backend is None:
            # A constructor failure cannot prove an untracked child died.
            raise QQHybridNavigationError("qq_navigation_worker_start_uncertain")
        if self.backend is not None:
            self.backend.close()

    def __getattr__(self, name):
        if self.backend is None:
            raise QQHybridNavigationError("qq_navigation_worker_not_admitted")
        return getattr(self.backend, name)


class _RoundOwner:
    """The profile wrapper exits this only after its Job has been reaped."""
    def __init__(self, episode, context):
        self.episode, self.context = episode, context
        self.entered = self.exit_attempted = self.released = False

    async def __aenter__(self):
        await self.context.__aenter__()
        self.entered = True

    async def __aexit__(self, *_):
        if self.released:
            return
        episode = self.episode
        episode.operator.revoke_round()
        # Confirm navigation process death before stopping its guard and
        # releasing the original shared lock. A failure retains this owner.
        episode.backend.close()
        if episode.publisher is not None:
            await episode.publisher.aclose()
        if self.exit_attempted:
            await episode.operator.retry_cleanup()
        else:
            self.exit_attempted = True
            await self.context.__aexit__(None, None, None)
        self.released = True


class _NavigationRound:
    # Profile and navigation each reserve .5s for their Job; allow OS overhead.
    cleanup_reserve_seconds = 1.1

    def __init__(self, service, target, task_id, cancel_event):
        self.service, self.target, self.task_id = service, target, task_id
        self.epoch, self.lease, self.observation = uuid4(), str(uuid4()), str(uuid4())
        self.path = service.directory / f"qq-navigation-{self.epoch}.json"
        self.cancel_event = cancel_event
        self.backend = self.operator = self.profile = self.publisher = None
        self.owner = self.protected = self.monitor = None
        self.deadline = None
        self.entered = self.exit_started = self.cleaned = self.journaled = False
        self._cleanup_lock = asyncio.Lock()

    def snapshot(self):
        try:
            value = self.service.live_snapshot(target=self.target, purpose="navigation",
                worker_epoch=self.epoch, deadline_at=self.deadline,
                desktop_lease_id=self.lease, observation_epoch=self.observation)
            if not isinstance(value, NavigationGuardState):
                if inspect.iscoroutine(value):
                    value.close()
                raise ValueError
            value = NavigationGuardState.model_validate(value.model_dump(warnings=False))
            settings, now = self.service.settings, datetime.now(UTC)
            if (value.target != self.target or value.run_id != settings.run_id
                    or value.session_epoch != settings.session_epoch
                    or value.surface_epoch != settings.surface_epoch
                    or value.worker_epoch != str(self.epoch)
                    or value.desktop_lease_id != self.lease or value.observation_epoch != self.observation
                    or value.process_id != settings.window.process_id
                    or value.window_handle != settings.window.window_handle
                    or value.process_started_at_100ns != settings.process_started_at_100ns
                    or not now < value.lease_expires_at <= self.deadline
                    or not 0 <= (now-value.published_at).total_seconds() <= 5):
                raise ValueError
            if self.journaled and (value.paused or value.has_owned_draft or value.has_commit_obligation):
                self.cancel_event.set()
                self.revoke_round()
            return value
        except Exception:
            raise QQHybridNavigationError("qq_navigation_live_snapshot_unproven") from None

    def round(self, target, *, deadline_at, cancel_event=None):
        if target != self.target or self.deadline is not None:
            raise QQHybridNavigationError("qq_navigation_round_scope_invalid")
        self.deadline = deadline_at
        config = self.service.settings.navigation_config(
            worker_epoch=self.epoch, guard_state_path=str(self.path))
        self.backend = _DeferredBackend(config, target, deadline_at)
        self.operator = ProcessScopedDesktopOperator(backend=self.backend,
            operation_lock=self.service.operation_lock)
        expectation = self.service.settings.expectations[target.binding_id]
        self.profile = SupervisedProfileSource(SupervisedProfileConfig(
            helper_path=self.service.settings.helper_path, vault_path=self.service.settings.vault_path,
            guard_state_path=str(self.path),
            **{key: getattr(expectation, key) for key in (
                "environment_fingerprint", "selector_pack_version", "client_version", "hmac_key_id")},
            process_id=config.window.process_id, window_handle=config.window.window_handle,
            process_started_at_100ns=config.expected_process_started_at_100ns,
            run_id=config.expected_run_id, worker_epoch=config.expected_worker_epoch,
            max_seconds=self.service.settings.max_seconds))
        self.owner = _RoundOwner(self, self.operator.round(target,
            deadline_at=deadline_at, cancel_event=self.cancel_event))
        self.protected = self.profile.protect_desktop_round(self.owner,
            target=target, deadline_at=deadline_at)
        return self

    async def __aenter__(self):
        try:
            await self.protected.__aenter__()
            self.entered = True
            if self.service.journal.held(self.target.account_id):
                raise QQHybridNavigationError("qq_navigation_cleanup_required")
            initial = self.snapshot()
            if (self.cancel_event.is_set() or initial.paused or initial.has_owned_draft
                    or initial.has_commit_obligation):
                raise QQHybridNavigationError("qq_navigation_control_blocked")
            if self.path.exists():
                raise QQHybridNavigationError("qq_navigation_guard_path_reused")
            self.service.directory.mkdir(parents=True, exist_ok=True)
            self.publisher = QQNavigationGuardPublisher(self.path, initial, self.snapshot)
            await self.publisher.start()  # Must succeed before any worker RPC.
            self.publisher.raise_if_failed()
            self.service.journal.begin(self)
            self.journaled = True
            self.backend.start()
            self.monitor = asyncio.create_task(self._watch_publisher())
            return self
        except BaseException:
            self.revoke_round()
            await self._cleanup()
            raise

    async def _watch_publisher(self):
        await self.publisher.failed_event.wait()
        self.cancel_event.set()
        try:
            self.revoke_round()
        except Exception:
            # Cleanup still has to prove both trees dead; termination failure
            # does not refund the journal or leave a task exception containing
            # native details. The actor sees the cancellation immediately.
            pass

    def revoke_round(self):
        # Both must be attempted even if native termination of one fails.
        failed = False
        for owner, method in ((self.profile, "revoke"), (self.operator, "revoke_round")):
            if owner is not None:
                try:
                    getattr(owner, method)()
                except Exception:
                    failed = True
        if failed:
            raise QQHybridNavigationError("qq_navigation_revocation_failed") from None

    async def __aexit__(self, *_):
        await self._cleanup()

    async def _cleanup(self):
        async with self._cleanup_lock:
            if self.cleaned:
                return
            self.revoke_round()
            if self.monitor is not None:
                self.monitor.cancel()
            if self.entered and not self.exit_started:
                self.exit_started = True
                await self.protected.__aexit__(None, None, None)
            elif self.profile is not None:
                await self.profile.retry_cleanup()
            if self.owner is not None and self.owner.entered and not self.owner.released:
                await self.owner.__aexit__(None, None, None)
            # An acquisition cancelled before entry did not create a process.
            if self.backend is not None:
                self.backend.close()
            if self.publisher is not None:
                await self.publisher.aclose()
            if self.journaled:
                self.service.journal.finish(self)
            self.cleaned = True

    async def capture(self, target, *, deadline_at):
        await self.publisher.publish_now()
        if self.cancel_event.is_set():
            raise QQHybridNavigationError("qq_navigation_control_blocked")
        return await self.operator.capture(target, deadline_at=deadline_at)

    async def execute(self, request, decision, *, cancel_event=None):
        await self.publisher.publish_now()
        if self.cancel_event.is_set():
            raise QQHybridNavigationError("qq_navigation_control_blocked")
        return await self.operator.execute(request, decision, cancel_event=cancel_event)

    async def witness(self, target, frame, *, deadline_at):
        await self.publisher.publish_now()
        if self.cancel_event.is_set():
            raise QQHybridNavigationError("qq_navigation_control_blocked")
        return await self.backend.local_witness(target)


class _LocalWitness:
    def __init__(self, episode):
        self.episode = episode

    async def snapshot(self, target, frame, *, deadline_at):
        return await self.episode.witness(target, frame, deadline_at=deadline_at)


class _ProfileSource:
    def __init__(self, episode):
        self.episode = episode

    async def capture(self, target, frame, expectation, *, deadline_at):
        await self.episode.publisher.publish_now()
        if self.episode.cancel_event.is_set():
            raise QQHybridNavigationError("qq_navigation_control_blocked")
        return await self.episode.profile.capture(target, frame, expectation, deadline_at=deadline_at)


class QQHybridNavigationService:
    def __init__(self, settings: QQHybridSettings, navigator: VisionNavigator,
                 store: NavigationTaskStore, operation_lock: asyncio.Lock,
                 live_snapshot: NavigationLiveSnapshot):
        if not isinstance(operation_lock, asyncio.Lock) or not callable(live_snapshot):
            raise TypeError("QQ navigation requires a shared lock and trusted live snapshot")
        if not 1.1 < settings.max_seconds <= 45:
            raise ValueError("QQ navigation deadline must fit its cleanup reserve")
        self.settings, self.navigator, self.store = settings, navigator, store
        self.operation_lock, self.live_snapshot = operation_lock, live_snapshot
        self.directory = Path(settings.guard_directory)
        if not self.directory.is_absolute():
            raise ValueError("QQ navigation guard directory must be absolute")
        self.journal = _CleanupJournal(store)
        self._rounds: dict[UUID, _NavigationRound] = {}
        self._active = self._closed = False

    def _held_result(self, target, pending_input_key, code, *, retry=False):
        task = self.store.ensure_task(target, pending_input_key, now=datetime.now(UTC))
        return RuntimeNavigationResult(outcome=NavigationOutcome(
            status=NavigationStatus.RETRY_WAIT if retry else NavigationStatus.NEEDS_ATTENTION,
            binding_id=target.binding_id, binding_revision=target.binding_revision, error_code=code),
            task_id=task.task_id, retry_at=datetime.now(UTC)+timedelta(seconds=10) if retry else None)

    async def navigate(self, binding_id: str, pending_input_key: str,
                       cancel_event: asyncio.Event | None = None) -> RuntimeNavigationResult:
        started = time.monotonic()
        target = self.settings.targets[binding_id]
        if self._closed:
            return self._held_result(target, pending_input_key, "qq_navigation_cleanup_required")
        if self._active:
            return self._held_result(target, pending_input_key, "qq_navigation_episode_in_progress", retry=True)
        if self.journal.held(target.account_id):
            return self._held_result(target, pending_input_key, "qq_navigation_cleanup_required")
        task = self.store.ensure_task(target, pending_input_key, now=datetime.now(UTC))
        self._active = True
        combined = _Cancellation(cancel_event)
        mirror = None
        if cancel_event is not None:
            if cancel_event.is_set():
                combined.set()
            async def mirror_cancel():
                await cancel_event.wait()
                combined.set()
            mirror = asyncio.create_task(mirror_cancel())
        episode = _NavigationRound(self, target, task.task_id, combined)
        self._rounds[episode.epoch] = episode
        verifier = ProfileCurrentChatVerifier(
            expectation_lookup=lambda current: self.settings.expectations.get(current.binding_id)
                if current == target else None,
            context_source=lambda *_: ProfileVerificationContext(
                process_started_at_100ns=self.settings.process_started_at_100ns,
                observation_epoch=episode.observation),
            local_witness_source=_LocalWitness(episode), profile_source=_ProfileSource(episode))
        coordinator = NavigationCoordinator(self.navigator, episode, verifier, self.store,
            budget=NavigationBudget(total_timeout_seconds=self.settings.max_seconds,
                request_timeout_seconds=min(15, self.settings.max_seconds)))
        try:
            result = await coordinator.navigate(target, pending_input_key=pending_input_key,
                                                cancel_event=combined)
            if episode.backend is not None and not episode.cleaned:
                await self._retry_round_cleanup(episode,
                    timeout=max(0, self.settings.max_seconds-(time.monotonic()-started)))
            if episode.journaled and not episode.cleaned:
                return self._held_result(target, pending_input_key, "qq_navigation_cleanup_required")
            if episode.publisher is not None:
                try:
                    episode.publisher.raise_if_failed()
                except Exception:
                    return self._held_result(target, pending_input_key, "qq_navigation_guard_failed")
            return result
        except BaseException:
            episode.cancel_event.set()
            episode.revoke_round()
            if episode.backend is not None and not episode.cleaned:
                await self._retry_round_cleanup(episode,
                    timeout=max(0, self.settings.max_seconds-(time.monotonic()-started)))
            raise
        finally:
            if mirror is not None:
                mirror.cancel()
            if episode.backend is not None and not episode.cleaned and not episode.journaled:
                if not self.journal.held(target.account_id):
                    self.journal.begin(episode)
                    episode.journaled = True
            if episode.cleaned or episode.backend is None:
                self._rounds.pop(episode.epoch, None)
            self._active = False

    @staticmethod
    async def _retry_round_cleanup(episode, *, timeout):
        if timeout < episode.cleanup_reserve_seconds:
            return False
        cleanup = asyncio.create_task(episode._cleanup(), context=Context())
        def consume(task):
            if not task.cancelled():
                task.exception()
        cleanup.add_done_callback(consume)
        try:
            done, _ = await asyncio.wait({cleanup}, timeout=min(2, timeout))
            if cleanup not in done:
                cleanup.cancel()
                return False
            cleanup.result()
            return True
        except asyncio.CancelledError:
            cleanup.cancel()
            raise
        except Exception:
            return False

    async def retry_cleanup(self) -> bool:
        """Retry owned trees only; an orphaned restart hold stays blocked."""
        if self._active:
            return False
        for epoch, episode in tuple(self._rounds.items()):
            # Never inherit a revoked round token from the original actor.
            if not await self._retry_round_cleanup(episode, timeout=2):
                return False
            if episode.cleaned:
                self._rounds.pop(epoch, None)
        return not any(self.journal.held(target.account_id) for target in self.settings.targets.values())

    async def aclose(self):
        self._closed = True
        for episode in tuple(self._rounds.values()):
            episode.cancel_event.set()
            episode.revoke_round()
        return await self.retry_cleanup()

    def due_preflight(self):
        return DueNavigationPreflight(self)


class DueNavigationPreflight:
    """Rechecks navigation scope; its lease is never a draft/send capability."""
    def __init__(self, service: QQHybridNavigationService):
        self.service = service

    async def check(self, due, *, binding_revision, conversation_revision, global_revision):
        matches = [key for key, target in self.service.settings.targets.items()
            if target.conversation_id == due.conversation_id
            and self.service.settings.contact_ids[key] == due.contact_id
            and target.binding_revision == binding_revision]
        if len(matches) != 1:
            return DueNavigationPreflightResult(status="needs_attention",
                error_code="qq_navigation_due_target_unproven")
        result = await self.service.navigate(matches[0], f"due/{due.event_id}")
        if result.outcome.status == NavigationStatus.CANDIDATE_OPENED and result.active_chat_lease:
            lease = result.active_chat_lease
            try:
                value = self.service.live_snapshot(target=self.service.settings.targets[matches[0]],
                    purpose="navigation", worker_epoch=UUID(lease.worker_epoch),
                    deadline_at=lease.expires_at, desktop_lease_id=lease.desktop_lease_id,
                    observation_epoch=lease.observation_epoch)
                value = NavigationGuardState.model_validate(value.model_dump(warnings=False))
                if (not lease.is_fresh(now=datetime.now(UTC), now_monotonic_ns=time.monotonic_ns())
                        or value.target != self.service.settings.targets[matches[0]]
                        or any(getattr(lease, name) != getattr(value, name) for name in (
                            "run_id", "session_epoch", "surface_epoch", "worker_epoch", "observation_epoch",
                            "desktop_lease_id", "control_revision", "process_id", "window_handle",
                            "process_started_at_100ns"))
                        or value.control_revision != global_revision
                        or not datetime.now(UTC) < value.lease_expires_at <= lease.expires_at
                        or not 0 <= (datetime.now(UTC)-value.published_at).total_seconds() <= 5
                        or value.paused or value.has_owned_draft or value.has_commit_obligation):
                    raise ValueError
            except Exception:
                return DueNavigationPreflightResult(status="needs_attention",
                    error_code="qq_navigation_due_scope_changed")
            return DueNavigationPreflightResult(status="ready")
        return DueNavigationPreflightResult(
            status="retry_wait" if result.outcome.status == NavigationStatus.RETRY_WAIT else "needs_attention",
            retry_at=result.retry_at if result.outcome.status == NavigationStatus.RETRY_WAIT else None,
            error_code=result.outcome.error_code or "qq_navigation_due_unproven")


__all__ = ["QQHybridNavigationService", "DueNavigationPreflight", "NavigationLiveSnapshot"]
