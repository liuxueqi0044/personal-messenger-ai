"""Publish real parent control snapshots within one fixed desktop lease.

This owns a file heartbeat only. It never grants a lease, controls a worker,
clears a draft, sends input, or claims that the desktop has been cleaned up.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import time

from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState


class QQGuardPublicationError(RuntimeError):
    """A content-free local reason; source data/exception text is never kept."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_FIXED_FIELDS = (
    "schema_version", "target", "run_id", "session_epoch", "surface_epoch", "worker_epoch",
    "observation_epoch", "desktop_lease_id", "process_id", "window_handle", "process_started_at_100ns",
)


class QQNavigationGuardPublisher:
    def __init__(self, path: str | Path, initial: NavigationGuardState,
                 live_snapshot: Callable[[], NavigationGuardState], *, interval: float = .5,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 monotonic_clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("QQ guard path must be absolute")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 0 < interval <= 1):
            raise ValueError("QQ guard interval must be positive and at most one second")
        if not callable(live_snapshot):
            raise TypeError("QQ guard requires a live snapshot source")
        self.initial = self._validated(initial)
        self.live_snapshot, self.interval = live_snapshot, float(interval)
        self.clock, self.monotonic_clock = clock, monotonic_clock
        self.failed_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failure_code: str | None = None
        self._closed = False
        self._started = False
        self._starting = False
        self._lease_expires_at = self.initial.lease_expires_at
        self._control_revision = self.initial.control_revision
        self._lease_stop = None
        self._last_monotonic = None
        self._last_publication = None
        self._last_success = None
        self._publication_lock = asyncio.Lock()

    @staticmethod
    def _validated(value) -> NavigationGuardState:
        if not isinstance(value, NavigationGuardState):
            if inspect.iscoroutine(value):
                value.close()
            raise QQGuardPublicationError("qq_guard_snapshot_invalid")
        try:
            # model_copy/model_construct are not validation boundaries. Dump
            # nested models to values before parsing the complete closed type.
            return NavigationGuardState.model_validate(value.model_dump(warnings=False))
        except Exception:
            raise QQGuardPublicationError("qq_guard_snapshot_invalid") from None

    def _fail(self, code: str):
        if self._failure_code is None:
            self._failure_code = code
            self.failed_event.set()
        self._stop.set()

    def raise_if_failed(self) -> None:
        if self._failure_code is not None:
            raise QQGuardPublicationError(self._failure_code) from None

    def _now(self):
        try:
            now, tick = self.clock(), self.monotonic_clock()
            if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                    or isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick)):
                raise ValueError
            now = now.astimezone(UTC)
            if tick < 0 or self._last_monotonic is not None and tick < self._last_monotonic:
                raise ValueError
            self._last_monotonic = tick
            return now, tick
        except Exception:
            raise QQGuardPublicationError("qq_guard_clock_invalid") from None

    def _ensure_live(self):
        now, tick = self._now()
        if (now >= self._lease_expires_at
                or self._lease_stop is not None and tick >= self._lease_stop):
            raise QQGuardPublicationError("qq_guard_lease_expired")
        if self._last_success is not None and (
                not 0 <= (now-self._last_publication).total_seconds() <= 5
                or not 0 <= tick-self._last_success <= 5):
            raise QQGuardPublicationError("qq_guard_publication_stale")
        return now, tick

    def _snapshot(self):
        now, tick = self._ensure_live()
        try:
            snapshot = self.live_snapshot()
        except Exception:
            raise QQGuardPublicationError("qq_guard_source_failed") from None
        snapshot = self._validated(snapshot)
        now, tick = self._ensure_live()
        if (any(getattr(snapshot, field) != getattr(self.initial, field) for field in _FIXED_FIELDS)
                or snapshot.lease_expires_at > self._lease_expires_at):
            raise QQGuardPublicationError("qq_guard_scope_changed")
        if snapshot.control_revision < self._control_revision:
            raise QQGuardPublicationError("qq_guard_control_regressed")
        if not 0 <= (now-snapshot.published_at).total_seconds() <= 5:
            raise QQGuardPublicationError("qq_guard_source_stale")
        self._control_revision = snapshot.control_revision
        self._lease_expires_at = snapshot.lease_expires_at
        self._lease_stop = min(self._lease_stop, tick+(snapshot.lease_expires_at-now).total_seconds())
        if snapshot.lease_expires_at <= now:
            raise QQGuardPublicationError("qq_guard_lease_expired")
        return snapshot.model_copy(update={"published_at": now}), tick

    async def _publish(self):
        # Start, heartbeat and explicit commit fences serialize the complete
        # source/read/replace sequence, including Windows sharing retries.
        async with self._publication_lock:
            await self._publish_locked()

    async def _publish_locked(self):
        retry_stop = time.monotonic()+.25
        while not self._stop.is_set():
            snapshot, _tick = self._snapshot()
            try:
                payload = snapshot.model_dump_json().encode("utf-8")
            except Exception:
                raise QQGuardPublicationError("qq_guard_snapshot_invalid") from None
            if len(payload) > 32768:
                raise QQGuardPublicationError("qq_guard_snapshot_invalid")
            temporary = None
            try:
                fd, name = tempfile.mkstemp(prefix=".qq-guard-", suffix=".tmp", dir=self.path.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                now, _tick = self._ensure_live()
                if not 0 <= (now-snapshot.published_at).total_seconds() <= 5:
                    raise QQGuardPublicationError("qq_guard_publication_stale")
                if self._stop.is_set():
                    return
                os.replace(temporary, self.path)
                temporary = None
                now, tick = self._ensure_live()
                if not 0 <= (now-snapshot.published_at).total_seconds() <= 5:
                    raise QQGuardPublicationError("qq_guard_publication_stale")
                self._last_publication, self._last_success = snapshot.published_at, tick
                return
            except PermissionError:
                if time.monotonic() >= retry_stop:
                    raise QQGuardPublicationError("qq_guard_replace_failed") from None
            except QQGuardPublicationError:
                raise
            except Exception:
                raise QQGuardPublicationError("qq_guard_write_failed") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass  # Only an owned .tmp; never delete the live guard.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=.025)
            except TimeoutError:
                pass

    async def publish_now(self) -> None:
        """Flush actual current flags before an input RPC; never renew a lease."""
        self.raise_if_failed()
        if self._closed:
            raise QQGuardPublicationError("qq_guard_publisher_closed")
        if not self._started:
            raise QQGuardPublicationError("qq_guard_publisher_not_started")
        try:
            await self._publish()
            if self._stop.is_set():
                raise QQGuardPublicationError("qq_guard_publication_cancelled")
            self.raise_if_failed()
        except asyncio.CancelledError:
            self._fail("qq_guard_publication_cancelled")
            raise
        except QQGuardPublicationError as exc:
            self._fail(exc.code)
            self.raise_if_failed()

    async def start(self):
        self.raise_if_failed()
        if self._closed:
            raise QQGuardPublicationError("qq_guard_publisher_closed")
        if self._started:
            return self
        if self._starting:
            raise QQGuardPublicationError("qq_guard_start_in_progress")
        self._starting = True
        try:
            now, tick = self._now()
            if not 0 <= (now-self.initial.published_at).total_seconds() <= 5:
                raise QQGuardPublicationError("qq_guard_source_stale")
            self._lease_stop = tick+(self.initial.lease_expires_at-now).total_seconds()
            await self._publish()
            if self._stop.is_set():
                raise QQGuardPublicationError("qq_guard_publication_cancelled")
            self._started = True
            self._task = asyncio.create_task(self._run())
            self._task.add_done_callback(self._completed)
            return self
        except asyncio.CancelledError:
            self._fail("qq_guard_publication_cancelled")
            raise
        except QQGuardPublicationError as exc:
            self._fail(exc.code)
            self.raise_if_failed()
        finally:
            self._starting = False

    async def _run(self):
        try:
            while not self._stop.is_set():
                now, tick = self._ensure_live()
                delay = min(self.interval, (self._lease_expires_at-now).total_seconds(), self._lease_stop-tick)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0, delay))
                except TimeoutError:
                    pass
                if not self._stop.is_set():
                    await self._publish()
        except asyncio.CancelledError:
            if not self._stop.is_set():
                self._fail("qq_guard_publication_cancelled")
        except QQGuardPublicationError as exc:
            self._fail(exc.code)
        except Exception:
            self._fail("qq_guard_publication_failed")

    def _completed(self, task):
        # Cancellation before the coroutine starts must also wake supervision.
        if task.cancelled():
            if not self._stop.is_set():
                self._fail("qq_guard_publication_cancelled")
        elif task.exception() is not None:
            self._fail("qq_guard_publication_failed")

    async def aclose(self):
        self._closed = True
        self._stop.set()
        if self._task is not None:
            if self._task.cancelled():
                return
            try:
                await self._task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, exc_type, _exc, _tb):
        await self.aclose()
        if exc_type is None:
            self.raise_if_failed()
