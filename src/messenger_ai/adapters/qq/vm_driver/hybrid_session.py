"""One finite actor owns the desktop ContextVar across business caller tasks.

The trusted factory supplies an already protected process, scoped to the new
epoch and target. Callers pass only the existing closed business values. A
retired worker never becomes a new draft owner; an unproven reap blocks every
new round. Process termination cannot undo a draft or an attempted send.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import time
from typing import AsyncContextManager, Callable, Literal
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.navigation.contracts import NavigationModel, _aware
from messenger_ai.runtime.staged_preparation import (
    DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket,
)
from .contracts import WorkerCommand, WorkerKind, WorkerResult
from .hybrid_process import HybridPreparedDraft, HybridProcessError, HybridWorkerProcess, _BusinessFailure


Purpose = Literal["observe", "draft", "verify", "health"]
RoundFactory = Callable[..., AsyncContextManager[HybridWorkerProcess]]


class HybridSessionStatus(NavigationModel):
    state: Literal["idle", "starting", "active", "closing", "cleanup_required", "closed"]
    purpose: Purpose | None = None
    worker_epoch: UUID | None = None
    cleanup_required: bool = False


class _Cancellation:
    def __init__(self, external=None):
        self.external, self.local = external, asyncio.Event()

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())

    def set(self):
        self.local.set()


@dataclass
class _RPC:
    action: Literal["execute", "prepare_draft", "adopt_prepared", "abort_draft"]
    args: tuple
    deadline_at: datetime
    end_ns: int
    cancellation: _Cancellation
    result: asyncio.Future


@dataclass
class _Actor:
    binding_id: str
    purpose: Purpose
    epoch: UUID
    deadline_at: datetime
    stop_at: float
    ready: asyncio.Future
    finished: asyncio.Future
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    task: asyncio.Task | None = None
    endpoint: HybridWorkerProcess | None = None
    owner: DraftPreparationRequest | None = None
    ticket: PreparedDraftTicket | None = None
    operation_id: UUID | None = None
    verification_command: WorkerCommand | None = None
    verification_used: bool = False
    started: bool = False
    closing: bool = False
    error: HybridProcessError | None = None
    cleanup_required: bool = False
    current: _RPC | None = None
    child: asyncio.Task | None = None


def _consume(future):
    if not future.cancelled():
        future.exception()


class HybridWorkerSession:
    """A single actor and bounded queue, with no general command interpreter."""

    def __init__(self, round_factory: RoundFactory, *, max_seconds: float = 45,
                 health_binding_id: str | None = None):
        if isinstance(max_seconds, bool) or not 1 < max_seconds <= 45:
            raise ValueError("hybrid session lifetime must be bounded by 45 seconds")
        if health_binding_id is not None and (not isinstance(health_binding_id, str) or not health_binding_id):
            raise ValueError("health requires a trusted binding")
        self._factory, self.max_seconds = round_factory, max_seconds
        self._health_binding_id = health_binding_id
        self._actor: _Actor | None = None
        self._gate = asyncio.Lock()
        self._closed = False

    def status_snapshot(self) -> HybridSessionStatus:
        actor = self._actor
        if actor is None:
            return HybridSessionStatus(state="closed" if self._closed else "idle")
        state = ("cleanup_required" if actor.cleanup_required else "closing" if actor.closing
                 else "active" if actor.ready.done() else "starting")
        return HybridSessionStatus(state=state, purpose=actor.purpose, worker_epoch=actor.epoch,
                                   cleanup_required=actor.cleanup_required)

    def _new(self, binding_id, purpose, deadline_at, *, owner=None, command=None, end_ns=None):
        _aware(deadline_at)
        if self._closed:
            raise HybridProcessError("hybrid_session_closed")
        if not isinstance(binding_id, str) or not binding_id:
            raise HybridProcessError("hybrid_session_binding_required")
        if self._actor is not None:
            raise HybridProcessError("hybrid_session_cleanup_required" if self._actor.cleanup_required
                                     else "hybrid_session_busy")
        now, tick = datetime.now(UTC), time.monotonic_ns()
        duration = min(self.max_seconds, (deadline_at - now).total_seconds())
        if end_ns is not None:
            duration = min(duration, (end_ns - tick) / 1e9)
        if duration <= 1:
            raise HybridProcessError("hybrid_session_deadline_exhausted")
        loop = asyncio.get_running_loop()
        actor = _Actor(binding_id, purpose, uuid4(), now + timedelta(seconds=duration),
                       time.monotonic() + duration, loop.create_future(), loop.create_future(),
                       owner=owner, verification_command=command)
        self._actor = actor
        actor.task = asyncio.create_task(self._run(actor), name="qq-hybrid-session")
        return actor

    async def _ready(self, actor, cancellation):
        while not actor.ready.done():
            if cancellation.is_set():
                self._stop(actor)
                await self._finished(actor)
                raise HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None)
            if time.monotonic() >= actor.stop_at - .5 or datetime.now(UTC) >= actor.deadline_at:
                self._stop(actor)
                await self._finished(actor)
                raise HybridProcessError("hybrid_session_deadline_exhausted", cleanup_required=actor.owner is not None)
            await asyncio.sleep(.01)
        if actor.error is not None:
            await self._finished(actor)
            raise actor.error
        if actor.closing or actor.endpoint is None:
            raise HybridProcessError("hybrid_session_round_unavailable", cleanup_required=actor.owner is not None)

    @staticmethod
    def _stop(actor):
        actor.closing = True
        if actor.current is not None:
            actor.current.cancellation.set()
        if actor.endpoint is not None:
            try:
                actor.endpoint.revoke()  # Does not reset another task's ContextVar.
            except HybridProcessError as exc:
                actor.error, actor.cleanup_required = exc, True
        if actor.child is not None:
            actor.child.cancel()
        # A factory enter has no business IPC yet. Interrupt its lock wait;
        # context entry/exit still belongs to the actor task.
        if actor.started and not actor.ready.done() and actor.task is not None:
            actor.task.cancel()

    async def _finished(self, actor):
        # Never wait beyond the original lifetime. A late task remains owned
        # and cannot authorize a replacement factory or report successful close.
        until = min(actor.stop_at, time.monotonic() + 1)
        while not actor.finished.done() and time.monotonic() < until:
            await asyncio.sleep(.005)
        if not actor.finished.done():
            actor.cleanup_required = True
            actor.error = HybridProcessError("hybrid_session_cleanup_required", cleanup_required=True)
            raise actor.error
        if actor.cleanup_required:
            raise actor.error or HybridProcessError("hybrid_session_cleanup_required", cleanup_required=True)
        if self._actor is actor:
            self._actor = None

    async def _retire(self, actor):
        self._stop(actor)
        await self._finished(actor)

    async def _run(self, actor):
        context, entered = None, False
        actor.started = True
        try:
            if actor.closing:
                return
            context = self._factory(binding_id=actor.binding_id, purpose=actor.purpose,
                                    worker_epoch=actor.epoch, deadline_at=actor.deadline_at)
            # The actor itself enters: moving __aenter__ to a helper task would
            # lose the desktop capability before the first queued RPC.
            async with asyncio.timeout(max(0, actor.stop_at - .5 - time.monotonic())):
                endpoint = await context.__aenter__()
            entered, actor.endpoint = True, endpoint
            if (not isinstance(endpoint, HybridWorkerProcess) or endpoint.worker_epoch != actor.epoch
                    or endpoint.config.navigation.expected_worker_epoch != str(actor.epoch)
                    or actor.binding_id not in {target.binding_id for target in endpoint.config.targets}):
                raise HybridProcessError("hybrid_session_factory_scope_mismatch")
            actor.ready.set_result(None)
            while not actor.closing:
                if time.monotonic() >= actor.stop_at - .5 or datetime.now(UTC) >= actor.deadline_at:
                    break
                try:
                    rpc = await asyncio.wait_for(actor.queue.get(), timeout=.01)
                except TimeoutError:
                    continue
                actor.current = rpc
                if rpc.cancellation.is_set():
                    rpc.result.set_exception(HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None))
                    break
                actor.child = asyncio.create_task(self._dispatch(actor, rpc))
                while not actor.child.done():
                    if (actor.closing or rpc.cancellation.is_set() or time.monotonic_ns() >= rpc.end_ns
                            or datetime.now(UTC) >= rpc.deadline_at or time.monotonic() >= actor.stop_at - .5):
                        self._stop(actor)
                        break
                    await asyncio.sleep(.005)
                child = actor.child
                if not child.done():
                    child.add_done_callback(_consume)
                    if not rpc.result.done():
                        rpc.result.set_exception(HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None))
                    break
                try:
                    value = child.result()
                    if actor.closing or rpc.cancellation.is_set():
                        raise HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None)
                    if rpc.action == "prepare_draft":
                        actor.ticket = value.ticket
                    elif rpc.action == "adopt_prepared":
                        actor.operation_id = rpc.args[1].operation_id
                    rpc.result.set_result(value)
                except _BusinessFailure as exc:
                    rpc.result.set_exception(exc)  # Exact request ownership may still be aborted.
                except BaseException as exc:
                    rpc.result.set_exception(exc if isinstance(exc, (HybridProcessError, asyncio.CancelledError, TimeoutError))
                                             else HybridProcessError("hybrid_session_worker_failed", cleanup_required=actor.owner is not None))
                    break
                finally:
                    actor.child, actor.current = None, None
                if actor.purpose != "draft" or rpc.action == "abort_draft":
                    break
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError) or not actor.closing:
                actor.error = exc if isinstance(exc, HybridProcessError) else HybridProcessError("hybrid_session_round_unavailable")
        finally:
            actor.closing = True
            if not actor.ready.done():
                actor.ready.set_result(None)
            if actor.child is not None:
                actor.child.cancel()
                actor.child.add_done_callback(_consume)
            # Exit the factory on this same actor task. The process first
            # proves worker/helper death and retains its desktop lock on failure.
            if entered:
                try:
                    if isinstance(actor.endpoint, HybridWorkerProcess):
                        try:
                            actor.endpoint.revoke()
                        except HybridProcessError:
                            # An unsuccessful termination call must not skip
                            # protected __aexit__, which owns the reap/lock.
                            pass
                    async with asyncio.timeout(max(.001, actor.stop_at - time.monotonic())):
                        await context.__aexit__(None, None, None)
                    actor.cleanup_required = False
                except BaseException as exc:
                    actor.cleanup_required = True
                    actor.error = exc if isinstance(exc, HybridProcessError) else HybridProcessError("hybrid_session_cleanup_required", cleanup_required=True)
            while not actor.queue.empty():
                pending = actor.queue.get_nowait()
                if not pending.result.done():
                    pending.result.set_exception(HybridProcessError("hybrid_session_round_unavailable", cleanup_required=actor.owner is not None))
            if actor.current is not None and not actor.current.result.done():
                actor.current.result.set_exception(HybridProcessError("hybrid_session_round_unavailable", cleanup_required=actor.owner is not None))
            actor.finished.set_result(None)

    async def _dispatch(self, actor, rpc):
        endpoint, args = actor.endpoint, rpc.args
        if rpc.action == "execute":
            return await endpoint.execute(args[0], cancel_event=rpc.cancellation)
        if rpc.action == "prepare_draft":
            return await endpoint.prepare_draft(args[0], expected_sequence_digest=args[1], cancel_event=rpc.cancellation)
        if rpc.action == "adopt_prepared":
            return await endpoint.adopt_prepared(args[0], args[1], cancel_event=rpc.cancellation)
        return await endpoint.abort_draft(args[0], deadline_at=rpc.deadline_at, cancel_event=rpc.cancellation)

    async def _rpc(self, actor, action, args, deadline_at, *, end_ns=None, cancel_event=None, retire=False):
        cancellation = _Cancellation(cancel_event)
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume)
        try:
            await self._ready(actor, cancellation)
            deadline_at = min(_aware(deadline_at), actor.deadline_at)
            duration = min((deadline_at - datetime.now(UTC)).total_seconds(), actor.stop_at - .5 - time.monotonic())
            stop_ns = time.monotonic_ns() + int(duration * 1e9)
            if end_ns is not None:
                stop_ns = min(stop_ns, end_ns)
            rpc = _RPC(action, args, deadline_at, stop_ns, cancellation, future)
            while actor.queue.full():
                if cancellation.is_set() or time.monotonic_ns() >= stop_ns or actor.closing:
                    raise HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None)
                await asyncio.sleep(.005)
            if cancellation.is_set() or time.monotonic_ns() >= stop_ns or actor.closing:
                raise HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None)
            actor.queue.put_nowait(rpc)
            while not future.done():
                if cancellation.is_set() or time.monotonic_ns() >= stop_ns:
                    raise HybridProcessError("hybrid_session_revoked", cleanup_required=actor.owner is not None)
                await asyncio.sleep(.005)
            value = future.result()
            if retire:
                await self._retire(actor)
            return value
        except _BusinessFailure:
            if retire:
                await self._retire(actor)
            raise
        except BaseException:
            cancellation.set()
            self._stop(actor)
            await self._finished(actor)
            raise

    def _draft(self, binding_id, ticket=None):
        actor = self._actor
        if (actor is None or actor.purpose != "draft" or actor.closing or actor.binding_id != binding_id
                or (ticket is not None and (actor.ticket != ticket or ticket.worker_epoch != str(actor.epoch)))):
            raise HybridProcessError("hybrid_session_owner_unavailable", cleanup_required=True)
        return actor

    async def prepare_draft(self, request: DraftPreparationRequest, *, expected_sequence_digest: str,
                            cancel_event=None) -> HybridPreparedDraft:
        request = DraftPreparationRequest.model_validate(request.model_dump())
        if (not isinstance(expected_sequence_digest, str) or len(expected_sequence_digest) != 64
                or any(char not in "0123456789abcdef" for char in expected_sequence_digest)):
            raise HybridProcessError("hybrid_session_command_invalid")
        async with self._gate:
            actor = self._new(request.binding_id, "draft", request.deadline_at, owner=request,
                              end_ns=request.deadline_monotonic_ns)
        return await self._rpc(actor, "prepare_draft", (request, expected_sequence_digest), request.deadline_at,
                               end_ns=request.deadline_monotonic_ns, cancel_event=cancel_event)

    async def adopt_prepared(self, ticket: PreparedDraftTicket, command: WorkerCommand, *, cancel_event=None) -> WorkerResult:
        ticket = PreparedDraftTicket.model_validate(ticket.model_dump())
        command = WorkerCommand.model_validate(command.model_dump())
        actor = self._draft(ticket.binding_id, ticket)
        if command.binding_id != ticket.binding_id or command.deadline is None:
            raise HybridProcessError("hybrid_session_command_invalid")
        return await self._rpc(actor, "adopt_prepared", (ticket, command), min(ticket.expires_at, command.deadline),
                               end_ns=ticket.expires_monotonic_ns, cancel_event=cancel_event)

    async def abort_draft(self, owner: DraftPreparationRequest | PreparedDraftTicket, *, deadline_at: datetime,
                          cancel_event=None) -> DraftCleanupResult:
        if not isinstance(owner, (DraftPreparationRequest, PreparedDraftTicket)):
            raise HybridProcessError("hybrid_session_command_invalid")
        owner = type(owner).model_validate(owner.model_dump())
        actor = self._draft(owner.binding_id, owner if isinstance(owner, PreparedDraftTicket) else None)
        if isinstance(owner, DraftPreparationRequest) and actor.owner != owner:
            raise HybridProcessError("hybrid_session_owner_unavailable", cleanup_required=True)
        return await self._rpc(actor, "abort_draft", (owner,), deadline_at, cancel_event=cancel_event, retire=True)

    async def execute(self, command: WorkerCommand, *, cancel_event=None) -> WorkerResult:
        command = WorkerCommand.model_validate(command.model_dump())
        if command.deadline is None or command.selection_handoff is not None:
            raise HybridProcessError("hybrid_session_command_invalid")
        if command.kind in {WorkerKind.OBSERVE, WorkerKind.HEALTH}:
            binding = command.binding_id or (self._health_binding_id if command.kind is WorkerKind.HEALTH else None)
            async with self._gate:
                actor = self._new(binding, "observe" if command.kind is WorkerKind.OBSERVE else "health", command.deadline)
            return await self._rpc(actor, "execute", (command,), command.deadline, cancel_event=cancel_event, retire=True)
        if command.kind is WorkerKind.VERIFY:
            actor = self._actor
            if (actor is None or actor.purpose != "verify" or actor.verification_used
                    or actor.verification_command != command or actor.closing):
                raise HybridProcessError("hybrid_session_verification_round_required")
            actor.verification_used = True
            return await self._rpc(actor, "execute", (command,), command.deadline, cancel_event=cancel_event)
        if command.kind not in {WorkerKind.COMMIT, WorkerKind.ABORT}:
            raise HybridProcessError("hybrid_session_command_invalid")
        actor = self._draft(command.binding_id)
        if actor.operation_id is None or command.operation_id != actor.operation_id:
            raise HybridProcessError("hybrid_session_command_invalid")
        return await self._rpc(actor, "execute", (command,), command.deadline, cancel_event=cancel_event)

    @asynccontextmanager
    async def verification_round(self, command: WorkerCommand):
        command = WorkerCommand.model_validate(command.model_dump())
        if command.kind is not WorkerKind.VERIFY or command.prepared_evidence is None or command.deadline is None:
            raise HybridProcessError("hybrid_session_command_invalid")
        async with self._gate:
            if self._actor is not None:
                await self._retire(self._actor)
            actor = self._new(command.binding_id, "verify", command.deadline, command=command)
        try:
            await self._ready(actor, _Cancellation())
            yield self
        finally:
            await self._retire(actor)

    def close(self):
        """Revoke/kill synchronously; only aclose confirms context release."""
        self._closed = True
        if self._actor is not None:
            self._stop(self._actor)

    async def aclose(self):
        self.close()
        if self._actor is not None:
            await self._finished(self._actor)

    async def retry_cleanup(self):
        actor = self._actor
        if actor is None:
            return
        if not actor.finished.done() or not actor.cleanup_required or actor.endpoint is None:
            raise HybridProcessError("hybrid_session_cleanup_required", cleanup_required=True)
        await actor.endpoint.retry_cleanup()
        if actor.endpoint.cleanup_pending:
            raise HybridProcessError("hybrid_session_cleanup_required", cleanup_required=True)
        actor.cleanup_required = False
        if self._actor is actor:
            self._actor = None


__all__ = ["HybridWorkerSession", "HybridSessionStatus"]
