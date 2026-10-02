"""Finite current-chat worker lifetime under the existing desktop owner.

Production starts only the fixed hybrid factory. Bounded shared mailboxes carry
closed business requests, never a model, locator, shell, or executable path.
The parent assigns the worker to its Job before granting permission. A failed
worker/helper reap retains the entered desktop context; only retry_cleanup may
release it. Killing a worker does not clear a QQ draft or a durable obligation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import multiprocessing as mp
import re
import threading
import time
from typing import Literal
from uuid import UUID, uuid4
import hashlib

from pydantic import Field, ValidationError, field_validator, model_validator

from messenger_ai.adapters.qq.navigation.contracts import NavigationModel, _aware
from messenger_ai.adapters.qq.navigation.supervised_profile import _WindowsProfileJob
from messenger_ai.runtime.staged_preparation import (
    DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket, validate_ticket,
    PreparationFailureDiagnostic, matching_prepare_failure,
)
from .contracts import PreparedVerificationEvidence, WorkerCommand, WorkerKind, WorkerResult
from .hybrid_worker import HybridWorkerConfig, HybridWorkerError


_MAILBOX_LIMIT = 262144
_REAP_SECONDS = 0.5


class HybridProcessError(RuntimeError):
    def __init__(self, code: str, *, cleanup_required: bool = False, prepare_failure=None):
        super().__init__(code)
        self.code, self.cleanup_required = code, cleanup_required
        self.prepare_failure = prepare_failure


class _BusinessFailure(HybridProcessError):
    """Correlated live worker failure; retain it for exact owned cleanup."""


class HybridPreparedDraft(NavigationModel):
    ticket: PreparedDraftTicket
    prepared_evidence: PreparedVerificationEvidence

    @model_validator(mode="after")
    def _proof_scope(self):
        ticket, proof = self.ticket, self.prepared_evidence
        if (proof.owner_binding_id != ticket.binding_id
                or proof.target_identity.binding_id != ticket.binding_id
                or proof.target_identity.conversation_type != "direct"
                or proof.target_identity.process_id != ticket.process_id
                or proof.target_identity.window_handle != ticket.window_handle
                or proof.text_hash != ticket.body_hash
                or proof.segment_ref != f"{ticket.pacing_plan_id}:{ticket.segment_index}"
                or hashlib.sha256(proof.model_dump_json().encode()).hexdigest() != ticket.evidence_digest):
            raise ValueError("hybrid portable proof does not match its draft ticket")
        return self


class _Call(NavigationModel):
    request_id: UUID = Field(default_factory=uuid4)
    worker_epoch: UUID
    action: Literal["execute", "prepare_draft", "adopt_prepared", "abort_draft"]
    deadline_at: datetime
    stop_at_monotonic_ns: int = Field(gt=0, strict=True)
    command: WorkerCommand | None = None
    request: DraftPreparationRequest | None = None
    ticket: PreparedDraftTicket | None = None
    owner: DraftPreparationRequest | PreparedDraftTicket | None = None
    expected_sequence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    _time = field_validator("deadline_at")(_aware)

    @model_validator(mode="after")
    def _arguments(self):
        required = {"execute": {"command"}, "prepare_draft": {"request", "expected_sequence_digest"},
                    "adopt_prepared": {"ticket", "command"}, "abort_draft": {"owner"}}[self.action]
        present = {key for key in ("command", "request", "ticket", "owner", "expected_sequence_digest")
                   if getattr(self, key) is not None}
        if required != present or self.worker_epoch.int == 0:
            raise ValueError("hybrid IPC requires exact action arguments and a worker epoch")
        return self


class _Reply(NavigationModel):
    request_id: UUID
    worker_epoch: UUID
    action: Literal["execute", "prepare_draft", "adopt_prepared", "abort_draft"]
    result: WorkerResult | None = None
    prepared: HybridPreparedDraft | None = None
    cleanup: DraftCleanupResult | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$")
    cleanup_required: bool = Field(default=False, strict=True)
    prepare_failure: PreparationFailureDiagnostic | None = None

    @model_validator(mode="after")
    def _outcome(self):
        if self.prepare_failure is not None and (self.action != "prepare_draft" or self.error_code is None):
            raise ValueError("prepare failure diagnostics require a failed preparation")
        if sum(value is not None for value in (self.result, self.prepared, self.cleanup, self.error_code)) != 1:
            raise ValueError("hybrid reply requires one outcome")
        if self.error_code is None:
            required = "prepared" if self.action == "prepare_draft" else "cleanup" if self.action == "abort_draft" else "result"
            if getattr(self, required) is None or self.cleanup_required:
                raise ValueError("hybrid reply outcome does not match action")
        return self


def _production_factory(config, revoked, *, deadline_at, stop_at):
    from .hybrid_worker import create_hybrid_worker
    return create_hybrid_worker(config, revoked, deadline_at=deadline_at, stop_at=stop_at)


class _ChildRevocation:
    """Final worker boundaries see this call's original dual-clock deadline."""
    def __init__(self, event, deadline_at, stop_at):
        self.event, self.deadline_at, self.stop_ns = event, deadline_at, int(stop_at * 1e9)
        self.call = None

    def is_set(self):
        deadline = min(self.deadline_at, self.call.deadline_at) if self.call else self.deadline_at
        stop_ns = min(self.stop_ns, self.call.stop_at_monotonic_ns) if self.call else self.stop_ns
        return self.event.is_set() or datetime.now(UTC) >= deadline or time.monotonic_ns() >= stop_ns

    def set(self):
        self.event.set()


def _last_prepare_failure(worker, request):
    try:
        return matching_prepare_failure(getattr(worker, "last_prepare_failure", None), request)
    except BaseException:
        return None


def _hybrid_main(config_raw, epoch, deadline_at, stop_at, permitted, revoked,
                 request_ready, reply_ready, inbox, inbox_size, outbox, outbox_size, factory):
    """All UI is synchronous here; helper creation waits for Job permission."""
    worker = None
    try:
        def live(call=None):
            deadline = min(deadline_at, call.deadline_at) if call else deadline_at
            end_ns = min(int(stop_at * 1e9), call.stop_at_monotonic_ns) if call else int(stop_at * 1e9)
            if revoked.is_set() or datetime.now(UTC) >= deadline or time.monotonic_ns() >= end_ns:
                raise HybridProcessError("hybrid_process_revoked")
        while not permitted.wait(.01):
            live()
        live()
        config = HybridWorkerConfig.model_validate(config_raw)
        if UUID(config.navigation.expected_worker_epoch) != epoch:
            raise HybridProcessError("hybrid_process_scope_mismatch")
        input_guard = _ChildRevocation(revoked, deadline_at, stop_at)
        worker = factory(config, input_guard, deadline_at=deadline_at, stop_at=stop_at)
        while True:
            live()
            if not request_ready.wait(.01):
                continue
            size = inbox_size.value
            if not 0 < size <= _MAILBOX_LIMIT:
                raise HybridProcessError("hybrid_process_request_invalid")
            call = _Call.model_validate_json(bytes(inbox[:size]))
            request_ready.clear()
            if call.worker_epoch != epoch:
                raise HybridProcessError("hybrid_process_scope_mismatch")
            live(call)
            input_guard.call = call
            try:
                if call.action == "execute":
                    reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action,
                                   result=worker.execute(call.command))
                elif call.action == "prepare_draft":
                    ticket = worker.prepare_draft(call.request, expected_sequence_digest=call.expected_sequence_digest)
                    prepared = HybridPreparedDraft(ticket=ticket, prepared_evidence=worker.prepared_evidence(ticket))
                    reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action, prepared=prepared)
                elif call.action == "adopt_prepared":
                    reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action,
                                   result=worker.adopt_prepared(call.ticket, call.command))
                else:
                    reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action,
                                   cleanup=worker.abort_draft(call.owner, deadline_at=call.deadline_at))
            except HybridWorkerError as exc:
                code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", exc.code) else "hybrid_process_worker_failed"
                reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action,
                               error_code=code, cleanup_required=bool(exc.cleanup_required),
                               prepare_failure=matching_prepare_failure(exc.prepare_failure, call.request)
                               if call.action == "prepare_draft" else None)
            except Exception:
                reply = _Reply(request_id=call.request_id, worker_epoch=epoch, action=call.action,
                               error_code="hybrid_process_worker_failed", cleanup_required=call.action != "execute",
                               prepare_failure=_last_prepare_failure(worker, call.request)
                               if call.action == "prepare_draft" else None)
            live(call)  # Late results never become a usable acknowledgement.
            raw = reply.model_dump_json().encode("utf-8")
            if len(raw) > _MAILBOX_LIMIT:
                raise HybridProcessError("hybrid_process_reply_too_large")
            outbox[:len(raw)] = raw
            outbox_size.value = len(raw)
            live(call)
            reply_ready.set()
            input_guard.call = None
    except BaseException:
        pass  # No native exception, secret, or traceback crosses IPC.
    finally:
        if worker is not None and callable(getattr(worker, "close", None)):
            try:
                worker.close()  # Resources only; never clear an owned QQ draft.
            except BaseException:
                pass


class _OwnedProcess:
    def __init__(self, config, epoch, *, deadline_at, stop_at, context, job_factory, factory):
        self.deadline_at, self.stop_at = deadline_at, stop_at
        self.permitted, self.revoked = context.Event(), context.Event()
        self.request_ready, self.reply_ready = context.Event(), context.Event()
        self.inbox, self.outbox = context.RawArray("B", _MAILBOX_LIMIT), context.RawArray("B", _MAILBOX_LIMIT)
        self.inbox_size, self.outbox_size = context.RawValue("i", 0), context.RawValue("i", 0)
        self._job, self._closed, self._process = job_factory(), False, None
        self._watch_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        try:
            self._process = context.Process(target=_hybrid_main, args=(
                config.model_dump(), epoch, deadline_at, stop_at, self.permitted, self.revoked,
                self.request_ready, self.reply_ready, self.inbox, self.inbox_size,
                self.outbox, self.outbox_size, factory), daemon=True)
        except BaseException:
            self._job.close()
            raise HybridProcessError("hybrid_process_supervision_unavailable") from None

    def start(self):
        self._watchdog.start()
        try:
            self._process.start()
            self._job.assign(self._process)
        except BaseException:
            raise HybridProcessError("hybrid_process_supervision_unavailable") from None
        if self.revoked.is_set() or datetime.now(UTC) >= self.deadline_at or time.monotonic() >= self.stop_at:
            raise HybridProcessError("hybrid_process_deadline_exhausted")
        self.permitted.set()

    def _watch(self):
        if not self._watch_stop.wait(max(0, self.stop_at - time.monotonic())):
            try:
                self.revoke()
            except BaseException:
                pass  # A failed termination still requires a proven empty Job.

    def revoke(self):
        self.revoked.set()
        self._job.terminate()

    def publish(self, call):
        raw = call.model_dump_json().encode("utf-8")
        if len(raw) > _MAILBOX_LIMIT:
            raise HybridProcessError("hybrid_process_request_too_large")
        self.reply_ready.clear()
        self.outbox_size.value = 0
        self.inbox[:len(raw)] = raw
        self.inbox_size.value = len(raw)
        self.request_ready.set()

    def read(self):
        size = self.outbox_size.value
        if not 0 < size <= _MAILBOX_LIMIT:
            raise HybridProcessError("hybrid_process_response_invalid")
        try:
            return _Reply.model_validate_json(bytes(self.outbox[:size]))
        except (ValidationError, ValueError):
            raise HybridProcessError("hybrid_process_response_invalid") from None

    def close(self, *, retry=False):
        if self._closed:
            return
        self.revoked.set()
        self._watch_stop.set()
        # Ordinary close belongs to the original lifetime, even if an earlier
        # failed close is retried by context cleanup. Explicit recovery may
        # spend another short reap interval; it cannot authorize business IPC.
        until = time.monotonic() + _REAP_SECONDS
        if not retry:
            until = min(until, self.stop_at + _REAP_SECONDS)
        try:
            self._job.terminate()
            process = self._process
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=max(0, min(.2, until - time.monotonic())))
                if process.is_alive():
                    process.kill()
                    process.join(timeout=max(0, until - time.monotonic()))
            while self._job.empty() is not True and time.monotonic() < until:
                time.sleep(.005)
            if (process is not None and process.pid is not None and process.is_alive()) or self._job.empty() is not True:
                raise HybridProcessError("hybrid_process_reap_failed")
            self._job.close()
            if process is not None:
                process.close()
            self._closed = True
        except BaseException:
            raise HybridProcessError("hybrid_process_reap_failed", cleanup_required=True) from None


@dataclass
class _Round:
    deadline_at: datetime
    stop_at: float
    active: bool = True


class HybridWorkerProcess:
    """One immutable epoch and one <=45s desktop round, including cleanup."""
    cleanup_reserve_seconds = _REAP_SECONDS

    def __init__(self, config: HybridWorkerConfig, *, max_seconds: float = 45,
                 _factory=None, _context=None, _job_factory=None):
        if isinstance(max_seconds, bool) or not 1 < max_seconds <= 45:
            raise ValueError("hybrid lifetime must be bounded by 45 seconds")
        self._config = HybridWorkerConfig.model_validate(config.model_dump())
        self._epoch = UUID(self._config.navigation.expected_worker_epoch)
        if self._epoch.int == 0:
            raise ValueError("hybrid worker epoch must be nonzero")
        self.max_seconds = max_seconds
        self._factory = _factory or _production_factory
        self._context, self._job_factory = _context or mp.get_context("spawn"), _job_factory or _WindowsProfileJob
        self._round = ContextVar(f"hybrid_process_round_{id(self)}", default=None)
        self._owner_round = self._active = self._failed_context = None
        self._used = False
        self._request_lock, self._cleanup_lock = asyncio.Lock(), asyncio.Lock()
        self.mutation_may_have_occurred = False

    @property
    def config(self):
        return self._config

    @property
    def worker_epoch(self):
        return self._epoch

    @property
    def cleanup_pending(self):
        return self._active is not None or self._failed_context is not None

    @asynccontextmanager
    async def protect_desktop_round(self, desktop_context, *, deadline_at: datetime):
        _aware(deadline_at)
        if self._owner_round is not None or self._failed_context is not None or self._used:
            raise HybridProcessError("hybrid_process_cleanup_required" if self._failed_context else "hybrid_process_lifetime_used")
        now, started = datetime.now(UTC), time.monotonic()
        duration = min(self.max_seconds, (deadline_at - now).total_seconds())
        if duration <= 2 * _REAP_SECONDS:
            raise HybridProcessError("hybrid_process_deadline_exhausted")
        work_deadline, work_stop = now + timedelta(seconds=duration - _REAP_SECONDS), started + duration - _REAP_SECONDS
        async with asyncio.timeout(max(0, work_stop - time.monotonic())):
            await desktop_context.__aenter__()
        capability = _Round(work_deadline, work_stop)
        self._used, self._owner_round = True, capability
        token = self._round.set(capability)
        try:
            yield self
        finally:
            capability.active = False
            if self._owner_round is capability:
                self._owner_round = None
            # Parent may revoke first and dispatch __aexit__ in an inherited
            # child context. Never reset a different context's capability.
            if self._round.get() is None or self._round.get() is capability:
                try:
                    self._round.reset(token)
                except ValueError:
                    self._round.set(None)
            try:
                self.close()
                await desktop_context.__aexit__(None, None, None)
            except BaseException:
                self._failed_context = desktop_context
                raise

    def revoke(self):
        capability = self._owner_round
        if capability is not None:
            capability.active = False
            if self._round.get() is capability:
                self._round.set(None)
        if self._active is not None:
            try:
                self._active.revoke()
            except BaseException:
                raise HybridProcessError("hybrid_process_reap_failed", cleanup_required=True) from None

    def close(self, *, _retry=False):
        capability = self._owner_round
        if capability is not None:
            capability.active = False
        worker = self._active
        if worker is not None:
            worker.close(retry=_retry)
            if self._active is worker:
                self._active = None

    async def retry_cleanup(self):
        if self._round.get() is not None or self._owner_round is not None:
            raise HybridProcessError("hybrid_process_cleanup_context_active")
        async with self._cleanup_lock:
            self.close(_retry=True)
            context = self._failed_context
            if context is not None:
                await context.__aexit__(None, None, None)
                if self._failed_context is context:
                    self._failed_context = None

    def _live(self, call=None, cancel_event=None):
        capability = self._round.get()
        if capability is None or capability is not self._owner_round or not capability.active:
            raise HybridProcessError("hybrid_process_round_required")
        deadline = min(capability.deadline_at, call.deadline_at) if call else capability.deadline_at
        stop = min(capability.stop_at, call.stop_at_monotonic_ns / 1e9) if call else capability.stop_at
        if (cancel_event is not None and cancel_event.is_set()) or (self._active is not None and self._active.revoked.is_set()):
            raise HybridProcessError("hybrid_process_revoked", cleanup_required=self.mutation_may_have_occurred)
        if datetime.now(UTC) >= deadline or time.monotonic() >= stop:
            raise HybridProcessError("hybrid_process_deadline_exhausted", cleanup_required=self.mutation_may_have_occurred)
        return min((deadline - datetime.now(UTC)).total_seconds(), stop - time.monotonic())

    def _call(self, action, deadline_at, *, end_ns=None, **arguments):
        _aware(deadline_at)
        self._live()
        capability = self._round.get()
        # Each business deadline also includes a reap interval. The immutable
        # round already has its own reserve; taking min avoids renewing either.
        remaining = min((deadline_at - datetime.now(UTC)).total_seconds() - _REAP_SECONDS, self._live())
        if remaining <= 0:
            raise HybridProcessError("hybrid_process_deadline_exhausted", cleanup_required=self.mutation_may_have_occurred)
        stop_ns = min(int(capability.stop_at * 1e9), time.monotonic_ns() + int(remaining * 1e9))
        if end_ns is not None:
            stop_ns = min(stop_ns, end_ns - int(_REAP_SECONDS * 1e9))
        return _Call(worker_epoch=self._epoch, action=action,
            deadline_at=min(deadline_at - timedelta(seconds=_REAP_SECONDS), capability.deadline_at), stop_at_monotonic_ns=stop_ns, **arguments)

    async def _request(self, call, cancel_event):
        capability = self._round.get()
        try:
            async with asyncio.timeout(max(.001, self._live(call, cancel_event))):
                async with self._request_lock:
                    self._live(call, cancel_event)
                    if self._active is None:
                        worker = _OwnedProcess(self._config, self._epoch, deadline_at=capability.deadline_at,
                            stop_at=capability.stop_at, context=self._context, job_factory=self._job_factory, factory=self._factory)
                        self._active = worker  # Keep ownership even when start/assignment fails.
                        worker.start()
                    worker = self._active
                    self._live(call, cancel_event)
                    if call.action in {"prepare_draft", "adopt_prepared"} or call.command and call.command.kind is WorkerKind.COMMIT:
                        self.mutation_may_have_occurred = True
                    worker.publish(call)
                    while True:
                        self._live(call, cancel_event)
                        if worker.reply_ready.is_set():
                            reply = worker.read()
                            self._live(call, cancel_event)
                            if (reply.request_id != call.request_id or reply.worker_epoch != self._epoch or reply.action != call.action):
                                raise HybridProcessError("hybrid_process_response_mismatch")
                            if reply.error_code:
                                raise _BusinessFailure(reply.error_code, cleanup_required=reply.cleanup_required or self.mutation_may_have_occurred,
                                    prepare_failure=matching_prepare_failure(reply.prepare_failure, call.request)
                                    if call.action == "prepare_draft" else None)
                            if reply.result is not None:
                                result, command = reply.result, call.command
                                if (result.worker_epoch != self._epoch or any(getattr(result, field) != getattr(command, field)
                                        for field in ("request_id", "kind", "operation_id", "binding_id", "binding_revision", "conversation_revision"))):
                                    raise HybridProcessError("hybrid_process_response_mismatch")
                            if reply.prepared is not None:
                                ticket = reply.prepared.ticket
                                try:
                                    validate_ticket(ticket, call.request, now=datetime.now(UTC), tick=time.monotonic_ns())
                                except Exception:
                                    raise HybridProcessError("hybrid_process_response_mismatch") from None
                                nav = self._config.navigation
                                if (ticket.worker_epoch != nav.expected_worker_epoch or ticket.run_id != nav.expected_run_id
                                        or ticket.process_id != nav.window.process_id or ticket.window_handle != nav.window.window_handle
                                        or ticket.process_started_at_100ns != nav.expected_process_started_at_100ns
                                        or ticket.expected_sequence_digest != call.expected_sequence_digest):
                                    raise HybridProcessError("hybrid_process_response_mismatch")
                            if reply.cleanup is not None and (reply.cleanup.reservation_id != call.owner.reservation_id or reply.cleanup.nonce != call.owner.nonce):
                                raise HybridProcessError("hybrid_process_response_mismatch")
                            self._live(call, cancel_event)
                            return reply
                        if not worker._process.is_alive():
                            raise HybridProcessError("hybrid_process_worker_exited", cleanup_required=self.mutation_may_have_occurred)
                        await asyncio.sleep(.01)
        except _BusinessFailure:
            # A confirmed acknowledgement can keep its in-memory exact draft
            # ownership. The caller may abort it; timeout/cancellation/lost or
            # malformed acknowledgements below cannot grant that privilege.
            raise
        except BaseException as exc:
            try:
                self.revoke()
            finally:
                self.close()  # Must prove all Job members gone before error escapes.
            if isinstance(exc, (asyncio.CancelledError, HybridProcessError, TimeoutError)):
                raise
            raise HybridProcessError("hybrid_process_worker_failed", cleanup_required=self.mutation_may_have_occurred) from None

    async def execute(self, command: WorkerCommand, *, cancel_event=None) -> WorkerResult:
        command = WorkerCommand.model_validate(command.model_dump())
        if command.deadline is None or command.selection_handoff is not None:
            raise HybridProcessError("hybrid_process_command_invalid")
        return (await self._request(self._call("execute", command.deadline, command=command), cancel_event)).result

    async def prepare_draft(self, request: DraftPreparationRequest, *, expected_sequence_digest: str,
                            cancel_event=None) -> HybridPreparedDraft:
        request = DraftPreparationRequest.model_validate(request.model_dump())
        call = self._call("prepare_draft", request.deadline_at, end_ns=request.deadline_monotonic_ns,
                          request=request, expected_sequence_digest=expected_sequence_digest)
        return (await self._request(call, cancel_event)).prepared

    async def adopt_prepared(self, ticket: PreparedDraftTicket, command: WorkerCommand, *, cancel_event=None) -> WorkerResult:
        ticket, command = PreparedDraftTicket.model_validate(ticket.model_dump()), WorkerCommand.model_validate(command.model_dump())
        if ticket.worker_epoch != self._config.navigation.expected_worker_epoch or command.deadline is None or command.selection_handoff is not None:
            raise HybridProcessError("hybrid_process_command_invalid")
        call = self._call("adopt_prepared", min(command.deadline, ticket.expires_at),
                          end_ns=ticket.expires_monotonic_ns, ticket=ticket, command=command)
        return (await self._request(call, cancel_event)).result

    async def abort_draft(self, owner: DraftPreparationRequest | PreparedDraftTicket, *, deadline_at: datetime,
                          cancel_event=None) -> DraftCleanupResult:
        if not isinstance(owner, (DraftPreparationRequest, PreparedDraftTicket)):
            raise HybridProcessError("hybrid_process_command_invalid")
        owner = type(owner).model_validate(owner.model_dump())
        call = self._call("abort_draft", deadline_at, owner=owner)
        return (await self._request(call, cancel_event)).cleanup


__all__ = ["HybridWorkerProcess", "HybridPreparedDraft", "HybridProcessError"]
