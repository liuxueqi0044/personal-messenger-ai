"""Current-chat-only QQ execution core. The parent must own the desktop and Job.

This worker never locates or selects a conversation. Independent normal-UI
profile proof precedes a cold draft; after that only the original current-chat
fence may be read. N1 leases are deliberately not accepted by this interface.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from messenger_ai.adapters.qq.models import (
    QQBubble, QQCertifiedDirectIdentity, QQConversation, QQIdentityBinding, QQSelectorPack,
)
from messenger_ai.adapters.qq.navigation.contracts import ContactTarget, NavigationFrame, NavigationModel
from messenger_ai.adapters.qq.navigation.identity import (
    CurrentChatWitness, ProfileIdentityExpectation, verify_current_chat,
)
from messenger_ai.adapters.qq.navigation.profile_verifier import (
    ProfileVerificationContext, ValidatedProfileAcquisition, _local_preflight,
    _remaining_proof_life, assemble_current_chat_evidence,
)
from messenger_ai.adapters.qq.navigation.windows_backend import WindowsNavigationConfig
from messenger_ai.runtime.staged_preparation import (
    DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket,
)
from .contracts import (
    PreparedBubbleAnchor, PreparedTargetIdentity, PreparedVerificationEvidence,
    WorkerCommand, WorkerKind, WorkerResult, WorkerStatus,
)
from .current_chat_structure import current_chat_structure_digest
from .sequence_alignment import SnapshotAlignmentError
from .worker import _new_suffix
from .message_cursor import MessageCursorStore


class HybridWorkerError(RuntimeError):
    def __init__(self, code: str, *, cleanup_required: bool = False):
        super().__init__(code)
        self.code, self.cleanup_required = code, cleanup_required


class HybridWorkerConfig(NavigationModel):
    navigation: WindowsNavigationConfig
    selector_pack: QQSelectorPack
    bindings: tuple[QQIdentityBinding, ...]
    targets: tuple[ContactTarget, ...]
    expectations: tuple[ProfileIdentityExpectation, ...]
    helper_path: str = Field(min_length=1)
    vault_path: str = Field(min_length=1)
    prepare_write_reserve_seconds: float = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def _fixed_scope(self):
        if UUID(self.navigation.expected_worker_epoch).int == 0:
            raise ValueError("hybrid worker epoch must be nonzero")
        if not Path(self.helper_path).is_absolute() or not Path(self.vault_path).is_absolute():
            raise ValueError("hybrid helper and vault require absolute trusted paths")
        maps = [{x.binding_id: x for x in rows} for rows in (self.bindings, self.targets, self.expectations)]
        if any(len(m) != len(rows) for m, rows in zip(maps, (self.bindings, self.targets, self.expectations))):
            raise ValueError("hybrid bindings must be unique")
        if not maps[0] or maps[0].keys() != maps[1].keys() or maps[0].keys() != maps[2].keys():
            raise ValueError("hybrid identity scope must be complete")
        for key, binding in maps[0].items():
            target, expectation = maps[1][key], maps[2][key]
            if (target.identity_mode != "persistent" or binding.conversation_type != "direct"
                    or target.account_id != binding.account_id
                    or target.conversation_id != binding.hub_conversation_id
                    or any(getattr(target, f) != getattr(expectation, f) for f in
                           ("account_id", "conversation_id", "binding_id", "binding_revision"))
                    or expectation.client_version != self.selector_pack.client_version
                    or expectation.environment_fingerprint != self.selector_pack.environment_fingerprint
                    or expectation.selector_pack_version != self.selector_pack.fixture_suite_version):
                raise ValueError("hybrid identity configuration mismatch")
        return self


@dataclass(frozen=True)
class HybridSnapshot:
    witness: CurrentChatWitness
    bubbles: tuple[QQBubble, ...]
    composer_text: str


class HybridCurrentChatPort(Protocol):
    """Synchronous value-only reads; never retain a COM phase across capture."""
    def begin(self, mode: str, *, deadline_at: datetime) -> None: ...
    def frame(self, target: ContactTarget) -> NavigationFrame: ...
    def snapshot(self, target: ContactTarget) -> HybridSnapshot: ...
    def capture(self, target: ContactTarget, frame: NavigationFrame,
                expectation: ProfileIdentityExpectation, *, deadline_at: datetime) -> ValidatedProfileAcquisition: ...
    def write(self, target: ContactTarget, text: str, *, before_action: Callable[[], None]) -> None: ...
    def send(self, target: ContactTarget, *, before_action: Callable[[], None]) -> None: ...
    def clear(self, target: ContactTarget, text: str, *, before_action: Callable[[], None]) -> None: ...
    def allow_paused_cleanup_revision(self, target: ContactTarget, original: int, current: int) -> bool: ...


_FENCE_FIELDS = (
    "account_id", "conversation_id", "binding_id", "binding_revision", "run_id",
    "session_epoch", "surface_epoch", "worker_epoch", "observation_epoch", "desktop_lease_id",
    "control_revision", "process_id", "process_started_at_100ns", "window_handle",
    "header_digest", "selected_row_runtime_id_hash", "active_chat_structure_digest",
)


def semantic_sequence_digest(bubbles: tuple[QQBubble, ...]) -> str:
    """Use the persisted cursor's schema, without writing or reanchoring it."""
    return MessageCursorStore.semantic_snapshot_token([b.model_dump(mode="json") for b in bubbles])


@dataclass
class _Prepared:
    request: object
    frame: NavigationFrame
    snapshot: HybridSnapshot
    proof: QQCertifiedDirectIdentity
    portable: PreparedVerificationEvidence
    ticket: object | None = None
    operation_id: UUID | None = None
    segment_ref: str | None = None
    commit_attempted: bool = False


class HybridQQWorker:
    def __init__(self, config: HybridWorkerConfig, port: HybridCurrentChatPort, *,
                 revoked=None, clock=lambda: datetime.now(UTC), monotonic_ns=time.monotonic_ns,
                 deadline_at: datetime | None = None, stop_at: float | None = None):
        self.config, self.port = config, port
        self.revoked, self.clock, self.monotonic_ns = revoked or threading.Event(), clock, monotonic_ns
        self.worker_epoch = UUID(config.navigation.expected_worker_epoch)
        self._bindings = {x.binding_id: x for x in config.bindings}
        self._targets = {x.binding_id: x for x in config.targets}
        self._expectations = {x.binding_id: x for x in config.expectations}
        self._lock = threading.Lock()
        self._prepared: _Prepared | None = None
        self._used_owners: set[UUID] = set()
        self._used_operations: set[UUID] = set()
        self._deadline = None
        self._mode = "idle"
        self._end_ns = self._last_ns = 0
        self._global_deadline = deadline_at
        self._global_end_ns = int(stop_at * 1e9) if stop_at is not None else None

    def _begin(self, deadline_at: datetime, mode: str):
        now, tick = self.clock(), self.monotonic_ns()
        if deadline_at.tzinfo is None or now.tzinfo is None or type(tick) is not int or tick < 0:
            raise HybridWorkerError("hybrid_deadline_invalid")
        deadline_at = min(deadline_at, self._global_deadline) if self._global_deadline else deadline_at
        self._deadline, self._last_ns = deadline_at, tick
        self._end_ns = tick + int((deadline_at - now).total_seconds() * 1e9)
        if self._global_end_ns is not None:
            self._end_ns = min(self._end_ns, self._global_end_ns)
        self._live()
        self._mode = mode
        self.port.begin(mode, deadline_at=deadline_at)

    def _live(self) -> float:
        now, tick = self.clock(), self.monotonic_ns()
        if self.revoked.is_set() or type(tick) is not int or tick < self._last_ns:
            raise HybridWorkerError("hybrid_revoked")
        self._last_ns = tick
        remaining = min((self._deadline - now).total_seconds(), (self._end_ns - tick) / 1e9)
        if remaining <= 0:
            raise HybridWorkerError("hybrid_deadline_exhausted")
        return remaining

    def _effective_deadline(self):
        return min(self._deadline, self.clock() + timedelta(seconds=self._live()))

    def _target(self, binding_id):
        if binding_id not in self._targets:
            raise HybridWorkerError("unknown_binding")
        return self._targets[binding_id]

    def _discard_fence(self):
        discard = getattr(self.port, "discard_fence", None)
        if callable(discard):
            discard()

    def close(self):
        """Release native handles only; closing never clears or sends a draft."""
        close = getattr(self.port, "close", None)
        if callable(close):
            close()

    def _snapshot(self, target, frame, *, allow_draft=False):
        self._live()
        snap = self.port.snapshot(target)
        self._live()
        if not isinstance(snap, HybridSnapshot) or type(snap.composer_text) is not str:
            raise HybridWorkerError("hybrid_snapshot_invalid")
        witness = snap.witness
        context = ProfileVerificationContext(
            process_started_at_100ns=self.config.navigation.expected_process_started_at_100ns,
            observation_epoch=witness.observation_epoch,
        )
        check_frame = frame
        if self._mode == "abort" and witness.control_revision != frame.control_revision:
            allow = getattr(self.port, "allow_paused_cleanup_revision", None)
            if (witness.control_revision <= frame.control_revision or not callable(allow)
                    or allow(target, frame.control_revision, witness.control_revision) is not True):
                raise HybridWorkerError("identity_scope_mismatch")
            check_frame = frame.model_copy(update={"control_revision": witness.control_revision})
        # Only the actual composer-empty constraint differs during owned draft
        # continuity; all identity/scope/group/tail metadata remains mandatory.
        _local_preflight(witness.model_copy(update={"composer_empty": True}) if allow_draft else witness,
                         target, check_frame, context)
        if witness.composer_empty is not (snap.composer_text == ""):
            raise HybridWorkerError("hybrid_composer_metadata_mismatch")
        if witness.captured_at < frame.captured_at:
            raise HybridWorkerError("identity_capture_order_invalid")
        _remaining_proof_life(witness.captured_at, witness.captured_monotonic_ns,
                              now=self.clock(), tick=self.monotonic_ns())
        if any(b.direction.value == "unknown" for b in snap.bubbles):
            raise HybridWorkerError("direction_unknown")
        binding = self._bindings[target.binding_id]
        bubbles = tuple(b.model_copy(update={"conversation_internal_id": binding.platform_conversation_id})
                        for b in snap.bubbles)
        return HybridSnapshot(witness, bubbles, snap.composer_text)

    def _full_identity(self, target):
        if self._prepared is not None:
            raise HybridWorkerError("ui_reserved")
        self._live()
        frame = self.port.frame(target)
        nav = self.config.navigation
        if (frame.run_id != nav.expected_run_id or frame.worker_epoch != nav.expected_worker_epoch
                or frame.process_id != nav.window.process_id or frame.window_handle != nav.window.window_handle):
            raise HybridWorkerError("identity_scope_mismatch")
        before = self._snapshot(target, frame)
        expectation = self._expectations[target.binding_id]
        captured = self.port.capture(target, frame, expectation, deadline_at=self._effective_deadline())
        after = self._snapshot(target, frame)
        context = ProfileVerificationContext(process_started_at_100ns=nav.expected_process_started_at_100ns,
                                             observation_epoch=before.witness.observation_epoch)
        evidence = assemble_current_chat_evidence(target, frame, expectation, captured,
                                                  before.witness, after.witness, context)
        remaining = _remaining_proof_life(captured.acquisition.profile_captured_at,
            captured.acquisition.profile_captured_monotonic_ns, now=self.clock(), tick=self.monotonic_ns())
        result = verify_current_chat(target, frame, expectation, evidence, now=self.clock(),
            deadline_at=self._effective_deadline(), now_monotonic_ns=self.monotonic_ns(),
            expected_process_started_at_100ns=nav.expected_process_started_at_100ns,
            expected_observation_epoch=context.observation_epoch, ttl_seconds=remaining)
        if not result.verified:
            raise HybridWorkerError(result.error_code)
        if semantic_sequence_digest(before.bubbles) != semantic_sequence_digest(after.bubbles):
            raise HybridWorkerError("stale_context")
        proof = QQCertifiedDirectIdentity(profile_id_hmac=captured.profile.profile_id_hmac,
            conversation_type="direct", client_version=expectation.client_version,
            selector_pack_version=expectation.selector_pack_version,
            group_marker_probe_complete=True, group_marker_count=0,
            process_id=frame.process_id, window_handle=frame.window_handle,
            header_digest=after.witness.header_digest,
            right_region_digest=after.witness.active_chat_structure_digest)
        return frame, after, proof

    def _current(self, record, *, composer=None, messages=False):
        target = self._target(record.request.binding_id)
        snap = self._snapshot(target, record.frame, allow_draft=True)
        fields = tuple(f for f in _FENCE_FIELDS if not (self._mode == "abort" and f == "control_revision"))
        if any(getattr(snap.witness, f) != getattr(record.snapshot.witness, f) for f in fields):
            raise HybridWorkerError("target_drift")
        if composer is not None and snap.composer_text != composer:
            raise HybridWorkerError("composer_drift")
        if messages and semantic_sequence_digest(snap.bubbles) != semantic_sequence_digest(record.snapshot.bubbles):
            raise HybridWorkerError("stale_context")
        return snap

    def _conversation(self, target, snap, proof):
        # This is the pre-existing business/cursor association, not the fresh
        # proof. Portable evidence below retains the actual profile signature.
        binding = self._bindings[target.binding_id]
        return QQConversation(internal_id=self._bindings[target.binding_id].platform_conversation_id,
            display_name=target.display_name, participant_signature=binding.participant_signature,
            last_message_key=snap.bubbles[-1].message_key if snap.bubbles else "",
            tree_digest=snap.witness.active_chat_structure_digest)

    def _portable(self, target, frame, snap, proof, *, text_hash, segment_ref):
        return PreparedVerificationEvidence(owner_binding_id=target.binding_id,
            target_identity=PreparedTargetIdentity(binding_id=target.binding_id,
                participant_signature=proof.participant_signature, conversation_type="direct",
                process_id=frame.process_id, window_handle=frame.window_handle),
            before_bubbles=tuple(PreparedBubbleAnchor(direction=b.direction.value,
                message_key=b.message_key, conversation_internal_id=b.conversation_internal_id,
                text_hash=b.text_hash) for b in snap.bubbles), text_hash=text_hash, segment_ref=segment_ref)

    def prepare_draft(self, request: DraftPreparationRequest, *, expected_sequence_digest: str) -> PreparedDraftTicket:
        """Cold owned draft only: no Hub operation or send authorization exists."""
        with self._lock:
            try:
                request = DraftPreparationRequest.model_validate(request.model_dump(warnings=False))
                if self._prepared is not None:
                    raise HybridWorkerError("ui_reserved")
                if request.reservation_id in self._used_owners:
                    raise HybridWorkerError("reservation_already_used")
                if (len(expected_sequence_digest) != 64
                        or any(c not in "0123456789abcdef" for c in expected_sequence_digest)):
                    raise HybridWorkerError("source_snapshot_unproven")
                self._begin(request.deadline_at, "idle")
                self._end_ns = min(self._end_ns, request.deadline_monotonic_ns)
                self._live()
                if self.clock() < request.requested_at or self.monotonic_ns() < request.requested_monotonic_ns:
                    raise HybridWorkerError("hybrid_request_future")
                target = self._target(request.binding_id)
                binding = self._bindings[request.binding_id]
                if (request.account_id != target.account_id or request.conversation_id != target.conversation_id
                        or request.contact_id != binding.contact_id or request.binding_revision != target.binding_revision):
                    raise HybridWorkerError("operation_binding_mismatch")
                frame, snap, proof = self._full_identity(target)
                if not snap.bubbles or semantic_sequence_digest(snap.bubbles) != expected_sequence_digest:
                    raise HybridWorkerError("stale_context")
                if frame.control_revision != request.global_revision:
                    raise HybridWorkerError("identity_scope_mismatch")
                segment_ref = f"{request.pacing_plan_id}:{request.segment_index}"
                portable = self._portable(target, frame, snap, proof,
                                          text_hash=request.body_hash, segment_ref=segment_ref)
                if self._live() <= self.config.prepare_write_reserve_seconds:
                    raise HybridWorkerError("prepare_write_budget_exhausted")
                record = _Prepared(request, frame, snap, proof, portable)
                self._prepared = record
                self._used_owners.add(request.reservation_id)
                self.port.begin("owned", deadline_at=self._effective_deadline())
                self.port.write(target, request.body,
                                before_action=lambda: self._current(record, composer="", messages=True))
                # The transport independently requires empty entry and exact
                # readback. No profile or selection is permitted after write.
                post = self._current(record, composer=request.body, messages=True)
                remaining = min(15, self._live(), (request.deadline_monotonic_ns - self.monotonic_ns()) / 1e9)
                issued, tick = self.clock(), self.monotonic_ns()
                delta_us = int(remaining * 1e6)
                if delta_us <= 0:
                    raise HybridWorkerError("hybrid_deadline_exhausted")
                ticket = PreparedDraftTicket(**request.model_dump(exclude={"body"}),
                    **{f: getattr(post.witness, f) for f in (
                        "run_id", "session_epoch", "surface_epoch", "worker_epoch", "process_id",
                        "window_handle", "process_started_at_100ns")},
                    issued_at=issued, expires_at=issued + timedelta(microseconds=delta_us),
                    issued_monotonic_ns=tick, expires_monotonic_ns=tick + delta_us * 1000,
                    evidence_digest=hashlib.sha256(portable.model_dump_json().encode()).hexdigest(),
                    expected_sequence_digest=expected_sequence_digest)
                record.ticket = ticket
                return ticket
            except Exception as exc:
                raise HybridWorkerError(self._error_code(exc), cleanup_required=self._prepared is not None) from None
            finally:
                self._discard_fence()

    def adopt_prepared(self, ticket: PreparedDraftTicket, command: WorkerCommand) -> WorkerResult:
        """One locked in-memory reassignment, with no UI or proof refresh."""
        with self._lock:
            try:
                record = self._prepared
                if record is None or record.ticket is None or ticket != record.ticket:
                    raise HybridWorkerError("prepared_ticket_mismatch")
                if (record.operation_id is not None or command.operation_id is None
                        or command.operation_id in self._used_operations):
                    raise HybridWorkerError("prepared_ticket_already_adopted")
                if not ticket.is_fresh(now=self.clock(), now_monotonic_ns=self.monotonic_ns()):
                    raise HybridWorkerError("prepared_ticket_expired")
                self._live()
                if (command.kind is not WorkerKind.PREPARE or command.selection_handoff is not None
                        or command.deadline is None or command.deadline <= self.clock()
                        or command.binding_id != ticket.binding_id
                        or command.binding_revision != ticket.binding_revision
                        or command.conversation_revision != ticket.conversation_revision
                        or command.text != record.request.body
                        or command.segment_ref != record.portable.segment_ref):
                    raise HybridWorkerError("operation_binding_mismatch")
                record.operation_id, record.segment_ref = command.operation_id, command.segment_ref
                self._used_operations.add(command.operation_id)
                return self._result(command, WorkerStatus.OK,
                    evidence={"prepared_evidence": record.portable.model_dump(mode="json")})
            except Exception as exc:
                return self._result(command, WorkerStatus.FAILED_SAFE, self._error_code(exc))

    def prepared_evidence(self, ticket: PreparedDraftTicket) -> PreparedVerificationEvidence:
        """Copy the exact cold prepared fingerprint, without UI or authority."""
        with self._lock:
            record = self._prepared
            if (record is None or record.ticket is None or record.ticket != ticket
                    or ticket.worker_epoch != str(self.worker_epoch)):
                raise HybridWorkerError("prepared_ticket_mismatch")
            return record.portable.model_copy(deep=True)

    def abort_draft(self, owner: DraftPreparationRequest | PreparedDraftTicket, *, deadline_at: datetime) -> DraftCleanupResult:
        with self._lock:
            record = self._prepared
            if (record is None or owner.reservation_id != record.request.reservation_id
                    or owner.nonce != record.request.nonce
                    or any(getattr(owner, f) != getattr(record.request, f) for f in
                           DraftPreparationRequest.model_fields if f != "body")):
                return DraftCleanupResult(status="not_owned", reservation_id=owner.reservation_id,
                                          nonce=owner.nonce, error_code="abort_not_owner")
            try:
                self._begin(deadline_at, "abort")
                self._abort(record)
                return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=owner.nonce)
            except Exception as exc:
                return DraftCleanupResult(status="cleanup_required", reservation_id=owner.reservation_id,
                                          nonce=owner.nonce, error_code=self._error_code(exc))
            finally:
                self._discard_fence()

    def execute(self, command: WorkerCommand) -> WorkerResult:
        with self._lock:
            try:
                if command.deadline is None or command.selection_handoff is not None:
                    raise HybridWorkerError("hybrid_command_invalid")
                if command.kind in {WorkerKind.SELECT_ONLY, WorkerKind.VERIFY_SELECTION_ONLY, WorkerKind.PREPARE}:
                    raise HybridWorkerError("hybrid_command_unsupported")
                self._begin(command.deadline, "health" if command.kind is WorkerKind.HEALTH else
                            "verify" if command.kind is WorkerKind.VERIFY else
                            "abort" if command.kind is WorkerKind.ABORT else
                            "owned" if self._prepared else "idle")
                if command.kind is WorkerKind.HEALTH:
                    if self._prepared is not None:
                        raise HybridWorkerError("ui_reserved")
                    metadata = self.port.health()
                    self._live()
                    return self._result(command, WorkerStatus.OK, evidence=metadata)
                target = self._target(command.binding_id)
                if command.binding_revision != target.binding_revision:
                    raise HybridWorkerError("operation_binding_mismatch")
                if command.kind is WorkerKind.OBSERVE:
                    frame, snap, proof = self._full_identity(target)
                    return self._result(command, WorkerStatus.OK, evidence={
                        "target": self._conversation(target, snap, proof).model_dump(mode="json"),
                        "bubbles": [b.model_dump(mode="json") for b in snap.bubbles],
                        "complete": True, "gap": False,
                        "semantic_sequence_digest": semantic_sequence_digest(snap.bubbles),
                    })
                if command.kind is WorkerKind.COMMIT:
                    return self._commit(command)
                if command.kind is WorkerKind.VERIFY:
                    return self._verify(command)
                if command.kind is WorkerKind.ABORT:
                    record = self._record_for(command)
                    self._abort(record)
                    return self._result(command, WorkerStatus.OK)
                raise HybridWorkerError("hybrid_command_unsupported")
            except Exception as exc:
                code = self._error_code(exc)
                uncertain = command.kind is WorkerKind.VERIFY or (
                    command.kind is WorkerKind.COMMIT and self._prepared is not None
                    and self._prepared.commit_attempted)
                return self._result(command, WorkerStatus.UNCERTAIN if uncertain else WorkerStatus.FAILED_SAFE,
                    code, {"cleanup_required": self._prepared is not None})
            finally:
                self._discard_fence()

    @staticmethod
    def _error_code(exc):
        from messenger_ai.adapters.qq.navigation.profile_verifier import ProfileAcquisitionError
        from .guest_composer import GuestComposerError
        if isinstance(exc, (HybridWorkerError, ProfileAcquisitionError)):
            return exc.code
        # Preserve only the closed composer codes we actually raise. Native
        # exceptions and unknown/provider exception text may contain UI data.
        composer_codes = frozenset({
            "composer_has_no_readable_pattern", "send_input_incomplete",
            "composer_focus_or_scope_drift", "composer_focus_drift",
            "composer_scope_rejected", "composer_not_empty",
            "composer_readback_mismatch", "composer_value_not_writable",
            "composer_clear_precondition_failed", "composer_clear_not_verified",
        })
        if (isinstance(exc, GuestComposerError) and len(exc.args) == 1
                and type(exc.args[0]) is str and exc.args[0] in composer_codes):
            return exc.args[0]
        return "hybrid_ui_action_failed"

    def _record_for(self, command):
        record = self._prepared
        if record is None or record.operation_id != command.operation_id or command.operation_id is None:
            raise HybridWorkerError("not_prepared")
        request = record.request
        if (command.binding_id != request.binding_id or command.binding_revision != request.binding_revision
                or command.conversation_revision != request.conversation_revision
                or command.segment_ref not in (None, record.segment_ref)
                or command.text not in (None, request.body)):
            raise HybridWorkerError("operation_binding_mismatch")
        if command.prepared_evidence is not None and command.prepared_evidence != record.portable:
            raise HybridWorkerError("prepared_evidence_mismatch")
        return record

    def _commit(self, command):
        record = self._record_for(command)
        if record.commit_attempted:
            raise HybridWorkerError("commit_already_attempted")
        self._current(record, composer=record.request.body, messages=True)
        def boundary():
            if record.commit_attempted:
                raise HybridWorkerError("commit_already_attempted")
            # The port rechecks the just-read region/identity using uncached
            # controls immediately before Invoke; no second whole-tree phase.
            self._live()
            record.commit_attempted = True
        self.port.send(self._target(command.binding_id), before_action=boundary)
        if not record.commit_attempted:
            raise HybridWorkerError("hybrid_input_boundary_missing")
        return self._result(command, WorkerStatus.OK,
                            evidence={"prepared_evidence": record.portable.model_dump(mode="json")})

    def _verify(self, command):
        if command.operation_id is None:
            raise HybridWorkerError("not_prepared")
        record = self._prepared
        if record is not None:
            record = self._record_for(command)
            if not record.commit_attempted:
                raise HybridWorkerError("commit_not_attempted")
            snap = self._current(record, composer="")
            portable, proof = record.portable, record.proof
            target = self._target(command.binding_id)
        else:
            portable = command.prepared_evidence
            if portable is None or portable.owner_binding_id != command.binding_id:
                raise HybridWorkerError("not_prepared")
            target = self._target(command.binding_id)
            frame, snap, proof = self._full_identity(target)
            expected = self._portable(target, frame, snap, proof,
                                      text_hash=portable.text_hash, segment_ref=portable.segment_ref)
            if expected.target_identity != portable.target_identity:
                raise HybridWorkerError("target_drift")
        if command.prepared_evidence is not None and command.prepared_evidence != portable:
            raise HybridWorkerError("prepared_evidence_mismatch")
        if (command.segment_ref not in (None, portable.segment_ref)
                or command.text is not None and hashlib.sha256(command.text.encode()).hexdigest() != portable.text_hash):
            raise HybridWorkerError("prepared_evidence_mismatch")
        try:
            new = _new_suffix([x.model_dump(mode="json") for x in portable.before_bubbles],
                              [b.model_dump(mode="json") for b in snap.bubbles])
        except SnapshotAlignmentError as exc:
            raise HybridWorkerError(str(exc)) from None
        receipts = [b for b in new if b["direction"] == "outbound"
                    and hashlib.sha256(b["text"].encode("utf-8")).hexdigest() == portable.text_hash]
        if len(receipts) != 1:
            raise HybridWorkerError("outbound_receipt_not_unique")
        self._prepared = None
        # QQBubble carries message fields, while identity belongs to its
        # independently verified conversation. Attach the preserved business
        # association only after the full profile and receipt checks above.
        receipt = {**receipts[0], "participant_signature": self._bindings[target.binding_id].participant_signature}
        return self._result(command, WorkerStatus.OK, evidence={"receipt": receipt,
            "conversation": self._conversation(target, snap, proof).model_dump(mode="json"),
            "operation_id": str(command.operation_id)})

    def _abort(self, record):
        if record.commit_attempted:
            raise HybridWorkerError("committed_cannot_abort")
        snap = self._current(record)
        if snap.composer_text not in ("", record.request.body):
            raise HybridWorkerError("needs_manual_cleanup")
        if snap.composer_text:
            def boundary():
                self._live()
            self.port.clear(self._target(record.request.binding_id), record.request.body,
                            before_action=boundary)
        self._current(record, composer="")
        self._prepared = None

    def _result(self, command, status, error_code=None, evidence=None):
        return WorkerResult(request_id=command.request_id, kind=command.kind, status=status,
            worker_epoch=self.worker_epoch, operation_id=command.operation_id,
            binding_id=command.binding_id, binding_revision=command.binding_revision,
            conversation_revision=command.conversation_revision, error_code=error_code, evidence=evidence or {})


def create_hybrid_worker(config: HybridWorkerConfig, revoked, *, deadline_at: datetime, stop_at: float) -> HybridQQWorker:
    """Fixed supervised-child factory. The owner must kill/reap the entire Job."""
    config = HybridWorkerConfig.model_validate(config)
    port = WindowsHybridCurrentChatPort(config, revoked, deadline_at=deadline_at, stop_at=stop_at)
    return HybridQQWorker(config, port, revoked=revoked, deadline_at=deadline_at, stop_at=stop_at)


class _BoundaryReads:
    """One read boundary, never retained by a fence or across a UI action.

    Opening reads may share the complete current phase graph. Closing/input
    boundaries always get a new index, so a provider value is read once *per
    boundary*, not reused from the proof it is intended to check.
    """
    def __init__(self, phase=None):
        from .guest_composer import get_uia_pattern
        from .phase_index import UIAPhaseIndex
        self._owned = not isinstance(phase, UIAPhaseIndex)
        self.index = (UIAPhaseIndex(window=None, root=None, pattern_loader=get_uia_pattern)
                      if self._owned else phase)
        self._children, self._proxies = {}, {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._children.clear()
        self._proxies.clear()
        if self._owned:
            self.index.close()

    def property(self, control, name, default=None):
        return self.index.property(control, name, default)

    def pattern(self, control, name, pattern_id):
        return self.index.pattern(control, name, pattern_id)

    def runtime(self, control):
        value = self.index.call(control, "GetRuntimeId")
        if not value or any(type(x) is not int for x in value):
            raise HybridWorkerError("identity_chat_correlation_unproven")
        return tuple(value)

    def children(self, control):
        if id(control) not in self._children:
            known = self.index.indexed_children(control)
            self._children[id(control)] = tuple(known if known is not None
                                                else self.index.call(control, "GetChildren"))
        return self._children[id(control)]

    def graph_parents(self):
        if self._owned:
            return None
        result = {}
        for parent in (self.index.root, *(x.control for x in self.index.nodes())):
            for child in self.children(parent):
                previous = result.setdefault(id(child), parent)
                if previous is not parent:
                    raise HybridWorkerError("hybrid_parent_scope_unproven")
        return result

    def proxy(self, control):
        # The unchanged decoder revisits ancestors/leaves several times. Its
        # read-only view shares values within this decode, retaining raw COM
        # wrappers via the index until the boundary has finished.
        if id(control) not in self._proxies:
            self._proxies[id(control)] = _MessageReadView(self, control)
        return self._proxies[id(control)]


class _MessageReadView:
    def __init__(self, reads, control):
        self._reads, self._control = reads, control

    def __getattr__(self, name):
        if name not in {"ClassName", "ControlTypeName", "Name"}:
            raise AttributeError(name)
        return self._reads.property(self._control, name, "")

    def GetChildren(self):
        return tuple(self._reads.proxy(x) for x in self._reads.children(self._control))

    def GetTextPattern(self):
        return self._reads.pattern(self._control, "GetTextPattern", 10014)


class WindowsHybridCurrentChatPort:
    """Normal UI only, fixed HWND, fresh selected marker, no locator or selection.

    The reused navigation handler is used only for its read implementations.
    Its action dispatcher is never exposed. A dedicated guard admits current
    reads while this worker retains its exact draft; it never admits navigation.
    """
    def __init__(self, config, revoked, *, deadline_at, stop_at, transport=None, surface=None):
        from messenger_ai.adapters.qq.navigation.windows_backend import (
            NavigationGuardState, WindowsNavigationCommandHandler,
        )
        self.config, self.revoked = config, revoked
        self.global_deadline, self.stop_at = deadline_at, stop_at
        self.deadline, self.mode = deadline_at, "idle"
        outer = self

        class ReadHandler(WindowsNavigationCommandHandler):
            def _guard(self, target):
                if (revoked.is_set() or datetime.now(UTC) >= outer.deadline
                        or time.monotonic() >= outer.stop_at):
                    raise HybridWorkerError("hybrid_revoked")
                with open(self.config.guard_state_path, "rb") as stream:
                    raw = stream.read(32769)
                if len(raw) > 32768:
                    raise HybridWorkerError("hybrid_guard_invalid")
                guard = NavigationGuardState.model_validate_json(raw)
                now = datetime.now(UTC)
                if (guard.target != target or guard.run_id != self.config.expected_run_id
                        or guard.worker_epoch != self.config.expected_worker_epoch
                        or guard.process_id != self.config.window.process_id
                        or guard.window_handle != self.config.window.window_handle
                        or guard.process_started_at_100ns != self.config.expected_process_started_at_100ns):
                    raise HybridWorkerError("identity_scope_mismatch")
                if (not 0 <= (now - guard.published_at).total_seconds() <= 5
                        or guard.lease_expires_at <= now):
                    raise HybridWorkerError("hybrid_guard_expired")
                if guard.paused and outer.mode not in {"abort", "health"}:
                    raise HybridWorkerError("hybrid_paused")
                if outer.mode == "idle" and (guard.has_owned_draft or guard.has_commit_obligation):
                    raise HybridWorkerError("ui_reserved")
                if outer.mode == "abort" and guard.has_commit_obligation:
                    raise HybridWorkerError("committed_cannot_abort")
                return guard

        self.handler = ReadHandler(config.navigation, revoked, deadline_at,
                                   transport=transport, surface=surface)
        self._pending_fence = None

    def begin(self, mode, *, deadline_at):
        if mode not in {"idle", "owned", "verify", "abort", "health"}:
            raise HybridWorkerError("hybrid_mode_invalid")
        self.mode = mode
        self.discard_fence()
        self.deadline = min(deadline_at, self.global_deadline)
        self.handler.deadline_at = self.deadline

    def close(self):
        self.discard_fence()
        self.handler.close()

    def discard_fence(self):
        self._pending_fence = None

    def allow_paused_cleanup_revision(self, target, original, current):
        guard = self.handler._guard(target)
        return (self.mode == "abort" and guard.paused and current > original
                and guard.control_revision == current and not guard.has_commit_obligation)

    def health(self):
        from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState
        with open(self.config.navigation.guard_state_path, "rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise HybridWorkerError("hybrid_guard_invalid")
        guard = NavigationGuardState.model_validate_json(raw)
        if guard.target not in self.config.targets:
            raise HybridWorkerError("identity_scope_mismatch")
        self.handler._guard(guard.target)
        if self.handler.surface.snapshot(self.config.navigation.window)[3] != self.config.navigation.expected_process_started_at_100ns:
            raise HybridWorkerError("identity_process_lifetime_mismatch")
        self.handler._guard(guard.target)
        return {"process_id": guard.process_id, "window_handle": guard.window_handle,
                "process_started_at_100ns": guard.process_started_at_100ns}

    def frame(self, target):
        if self.mode not in {"idle", "verify"}:
            raise HybridWorkerError("ui_reserved")
        from messenger_ai.adapters.qq.navigation.windows_backend import mask_bgra
        from .transport import _encode_bgra_png
        guard = self.handler._guard(target)
        nav = self.config.navigation
        bounds, screen, dpi, start = self.handler.surface.snapshot(nav.window)
        if start != nav.expected_process_started_at_100ns:
            raise HybridWorkerError("identity_process_lifetime_mismatch")
        left, top, right, bottom = bounds
        sx, sy, sw, sh = screen
        x, y = max(left, sx), max(top, sy)
        width, height = min(right, sx + sw) - x, min(bottom, sy + sh) - y
        if width <= 0 or height <= 0:
            raise HybridWorkerError("hybrid_window_geometry_invalid")
        raw = self.handler.surface.capture(nav.window, bounds)
        if len(raw) != (right - left) * (bottom - top) * 4:
            raise HybridWorkerError("hybrid_window_geometry_invalid")
        cropped = b"".join(raw[((y - top + line) * (right - left) + x - left) * 4:
                               ((y - top + line) * (right - left) + x - left + width) * 4]
                           for line in range(height))
        # The execution worker exposes no navigation regions or visible text.
        # Its own exact-window frame is scope only, never a visual identity proof.
        pixels = mask_bgra(width, height, cropped, ())
        after = self.handler._guard(target)
        if (self.handler.surface.snapshot(nav.window) != (bounds, screen, dpi, start)
                or after.model_dump(exclude={"published_at"}) != guard.model_dump(exclude={"published_at"})):
            raise HybridWorkerError("identity_scope_mismatch")
        return NavigationFrame(frame_id=str(uuid4()), captured_at=datetime.now(UTC),
            **{f: getattr(guard, f) for f in ("run_id", "session_epoch", "surface_epoch", "worker_epoch",
                "desktop_lease_id", "control_revision", "process_id", "window_handle")},
            binding_id=target.binding_id, binding_revision=target.binding_revision,
            screen_origin_x=sx, screen_origin_y=sy, screen_width=sw, screen_height=sh,
            crop_origin_x=x, crop_origin_y=y, crop_width=width, crop_height=height, dpi_scale=dpi,
            allowed_regions=(), privacy_mask_applied=True, png_bytes=_encode_bgra_png(width, height, pixels))

    def _properties(self, fence, reads):
        from .session_identity import _GROUP_MARKERS
        from messenger_ai.adapters.qq.navigation.windows_backend import selected_runtime_token
        selected = []
        for row in fence["rows"]:
            offscreen = reads.property(row, "IsOffscreen")
            if type(offscreen) is not bool:
                raise HybridWorkerError("hybrid_selection_metadata_invalid")
            if offscreen:
                continue
            pattern = reads.pattern(row, "GetSelectionItemPattern", 10010)
            value = reads.property(pattern, "IsSelected") if pattern is not None else None
            if value is not None and type(value) is not bool:
                raise HybridWorkerError("hybrid_selection_metadata_invalid")
            marker = self.config.navigation.row_selector.selected_class_name_token
            if value is True:
                selected.append((row, "selection_pattern"))
            elif marker and marker in str(reads.property(row, "ClassName", "")).split():
                selected.append((row, "selected_class"))
        if len(selected) != 1:
            raise HybridWorkerError("identity_chat_correlation_unproven")
        groups = 0
        # The baseline still probes the complete phase tree. Only descendants
        # proved to belong strictly to this exact composer are rebuilt after
        # an input: Chromium replaces placeholder/text DOM nodes while keeping
        # the certified editor. Every outside COM control stays frozen.
        nodes = fence["nodes"]
        if "group_static_nodes" in fence:
            nodes = (*fence["group_static_nodes"], *self._composer_descendants(fence, reads))
        for node in nodes:
            name = reads.property(node, "ClassName")
            if type(name) is not str:
                raise HybridWorkerError("identity_group_probe_incomplete")
            groups += bool(_GROUP_MARKERS & set(name.split()))
        if groups:
            raise HybridWorkerError("identity_conversation_not_direct")
        header = reads.property(fence["header"], "Name")
        if type(header) is not str or not header:
            raise HybridWorkerError("identity_chat_correlation_unproven")
        structure = current_chat_structure_digest(**{
            name: (str(reads.property(fence[name], "ClassName", "")), reads.runtime(fence[name]))
            for name in ("header", "messages", "composer")
        })
        return (selected_runtime_token(reads.runtime(selected[0][0])), selected[0][1],
                hashlib.sha256(header.encode()).hexdigest(), structure)

    def _composer_read(self, read):
        # Native calls are bounded by the supervising Job. These cheap local
        # checks also stop a long small-subtree walk without reloading the
        # guard or taking a window snapshot for each individual property.
        def current():
            if (self.revoked.is_set() or datetime.now(UTC) >= self.deadline
                    or time.monotonic() >= self.stop_at):
                raise HybridWorkerError("hybrid_revoked")
        current()
        result = read()
        current()
        return result

    def _composer_descendants(self, fence, reads, *, opening_graph=None):
        """Fresh small-tree scope/group proof, sharing only this boundary index.

        No Name or pattern reads, tree retry, or input occurs here. The editor
        itself remains a static identity control. RuntimeIds of the *outside*
        baseline are expected values, never cached live provider properties.
        """
        from .session_identity import _GROUP_MARKERS
        # Only normal editable document/container/text shapes are admitted.
        types = {"GroupControl", "TextControl", "PaneControl", "EditControl",
                 "CustomControl", "DocumentControl"}
        composer = fence["composer"]
        call = self._composer_read
        composer_id = call(lambda: reads.runtime(composer))
        composer_pid = call(lambda: reads.property(composer, "ProcessId"))
        if type(composer_pid) is not int or composer_pid != self.config.navigation.window.process_id:
            raise HybridWorkerError("hybrid_composer_scope_unproven")
        forbidden = fence.get("composer_outside_runtime_ids", frozenset())
        seen_controls, seen_ids, result = {id(composer)}, {composer_id}, []
        stack = [(composer, composer_id, 0)]
        while stack:
            parent, parent_id, depth = stack.pop()
            children = call(lambda: reads.children(parent))
            if len(children) > 256 or len(result) + len(children) > 256 or children and depth >= 32:
                raise HybridWorkerError("hybrid_composer_scope_unproven")
            for child in children:
                if child is None or id(child) in seen_controls:
                    raise HybridWorkerError("hybrid_composer_scope_unproven")
                seen_controls.add(id(child))
                rid = call(lambda: reads.runtime(child))
                if len(rid) > 64 or rid in seen_ids or rid in forbidden:
                    raise HybridWorkerError("hybrid_composer_scope_unproven")
                seen_ids.add(rid)
                pid = call(lambda: reads.property(child, "ProcessId"))
                kind = call(lambda: reads.property(child, "ControlTypeName"))
                name = call(lambda: reads.property(child, "ClassName"))
                if (type(pid) is not int or pid != self.config.navigation.window.process_id
                        or type(kind) is not str or kind not in types or type(name) is not str):
                    raise HybridWorkerError("hybrid_composer_scope_unproven")
                if _GROUP_MARKERS & set(name.split()):
                    raise HybridWorkerError("identity_conversation_not_direct")
                actual_parent = (opening_graph.get(id(child)) if opening_graph is not None
                                 else call(lambda: reads.index.call(child, "GetParentControl")))
                if actual_parent is None or call(lambda: reads.runtime(actual_parent)) != parent_id:
                    raise HybridWorkerError("hybrid_composer_scope_unproven")
                result.append(child)
                stack.append((child, rid, depth + 1))
        return tuple(result)

    def _partition_composer(self, fence, root, reads):
        # Partition only the actual completed phase graph. A locator/name/RID
        # guess must never remove a critical control from the static proof.
        graph = reads.graph_parents()
        if graph is None:
            raise HybridWorkerError("hybrid_composer_scope_unproven")
        descendants = self._composer_descendants(fence, reads, opening_graph=graph)
        descendant_controls = {id(node) for node in descendants}
        if (any(id(node) in descendant_controls for node in (
                *fence["rows"], fence["header"], fence["messages"], fence["send"]))
                or any(id(node) not in graph for node in descendants)):
            raise HybridWorkerError("hybrid_composer_scope_unproven")
        static = tuple(node for node in fence["nodes"] if id(node) not in descendant_controls)
        outside_ids = frozenset(self._composer_read(lambda node=node: reads.runtime(node))
                                for node in (root, *static))
        if any(reads.runtime(node) in outside_ids for node in descendants):
            raise HybridWorkerError("hybrid_composer_scope_unproven")
        fence["group_static_nodes"] = static
        fence["composer_outside_runtime_ids"] = outside_ids

    def _edges(self, controls, reads):
        # Direct-child membership on the critical controls' parent paths detects
        # a replaced header/region or a newly inserted selected row. It does not
        # recursively scan the QQ tree. Closing/input reads use a fresh index;
        # only the opening baseline can use the current phase's complete graph.
        return tuple((reads.runtime(parent), tuple(reads.runtime(c) for c in reads.children(parent)))
                     for parent in controls)

    def _parents(self, controls, root, reads):
        result, seen = [], set()
        root_id = reads.runtime(root)
        graph = reads.graph_parents()
        certified = {root_id}
        for control in controls:
            current, path = control, set()
            for _ in range(32):
                parent = (graph.get(id(current)) if graph is not None
                          else reads.index.call(current, "GetParentControl"))
                if parent is None:
                    raise HybridWorkerError("hybrid_parent_scope_unproven")
                rid = reads.runtime(parent)
                if rid in path:
                    raise HybridWorkerError("hybrid_parent_scope_unproven")
                path.add(rid)
                if rid not in seen:
                    seen.add(rid)
                    result.append(parent)
                if rid in certified:
                    certified.update(path)
                    break
                current = parent
            else:
                raise HybridWorkerError("hybrid_parent_scope_unproven")
        return tuple(result)

    def _tail(self, control, reads):
        if ("ml-root" not in str(reads.property(control, "ClassName", "")).split()
                or reads.property(control, "ProcessId") != self.config.navigation.window.process_id
                or reads.property(control, "IsOffscreen") is not False):
            raise HybridWorkerError("identity_message_tail_unproven")
        pattern = reads.pattern(control, "GetScrollPattern", 10004)
        if pattern is None:
            raise HybridWorkerError("identity_message_tail_unproven")
        scroll, percent, view = (reads.property(pattern, name) for name in (
            "VerticallyScrollable", "VerticalScrollPercent", "VerticalViewSize"))
        if (type(scroll) is not bool or any(type(x) not in (int, float) or not math.isfinite(x) for x in (percent, view))
                or not 0 < view <= 100 or scroll and percent != 100
                or not scroll and (percent != -1 or view != 100)):
            raise HybridWorkerError("identity_message_tail_unproven")

    def _region_bubbles(self, region, digest, *, reads):
        from .message_decoder import decode_message_region
        return tuple(QQBubble(conversation_internal_id="visible-current-conversation", message_key=x.message_key,
            direction=x.direction, text=x.text, observed_at=datetime.now(UTC), tree_digest=digest)
            for x in decode_message_region(reads.proxy(region)))

    def _check_fence(self, target, fence, *, composer_check, messages=True, opening_certified=False):
        from .guest_composer import read_composer_text
        if fence is None or fence["target"] != target:
            raise HybridWorkerError("hybrid_input_fence_missing")
        guard = self.handler._guard(target)
        if (guard.model_dump(exclude={"published_at"}) != fence["guard"].model_dump(exclude={"published_at"})
                or self.handler.surface.snapshot(self.config.navigation.window) != fence["surface"]):
            raise HybridWorkerError("target_drift")
        # snapshot has just established this opening proof in the same phase.
        # Input guards do not get this exception, and always read both sides.
        if not opening_certified:
            with _BoundaryReads() as reads:
                if (self._properties(fence, reads) != fence["properties"]
                        or self._edges(fence["parents"], reads) != fence["edges"]):
                    raise HybridWorkerError("target_drift")
                self._tail(fence["messages"], reads)
        # Decoder values are fresh even relative to the opening property scan:
        # a same-key edit/direction change must not inherit that scan's values.
        with _BoundaryReads() as reads:
            if messages and semantic_sequence_digest(self._region_bubbles(
                    fence["messages"], fence["properties"][3], reads=reads)) != fence["sequence"]:
                raise HybridWorkerError("stale_context")
        if not composer_check(read_composer_text(fence["composer"])):
            raise HybridWorkerError("composer_drift")
        # Region reads may yield to native providers. Re-read identity and
        # control/foreground/process/cancellation after the final content read.
        with _BoundaryReads() as reads:
            if (self._properties(fence, reads) != fence["properties"]
                    or self._edges(fence["parents"], reads) != fence["edges"]
                    or self.handler.surface.snapshot(self.config.navigation.window) != fence["surface"]
                    or self.handler._guard(target).model_dump(exclude={"published_at"}) != fence["guard"].model_dump(exclude={"published_at"})):
                raise HybridWorkerError("target_drift")
            self._tail(fence["messages"], reads)

    def snapshot(self, target):
        from .guest_composer import read_composer_text
        self.discard_fence()
        handler, nav = self.handler, self.config.navigation
        guard = handler._guard(target)
        surface = handler.surface.snapshot(nav.window)
        if surface[3] != nav.expected_process_started_at_100ns or guard.observation_epoch is None:
            raise HybridWorkerError("identity_process_lifetime_mismatch")
        with handler.transport.read_phase(nav.window) as phase:
            roots = {name: handler.transport._select(phase.root, selector) for name, selector in (
                ("header", nav.header_selector), ("messages", nav.message_selector),
                ("composer", nav.composer_selector), ("send", self.config.selector_pack.selector("send")))}
            if any(len(items) != 1 for items in roots.values()):
                raise HybridWorkerError("identity_chat_correlation_unproven")
            fence = dict(target=target, guard=guard, surface=surface,
                rows=tuple(handler.transport._select(phase.root, nav.row_selector)),
                nodes=tuple(handler.transport._descendants(phase.root)),
                **{name: items[0] for name, items in roots.items()})
            with _BoundaryReads(phase) as reads:
                fence["parents"] = self._parents((*fence["rows"], *(fence[x] for x in roots)), phase.root, reads)
                fence["edges"] = self._edges(fence["parents"], reads)
                properties = fence["properties"] = self._properties(fence, reads)
                self._partition_composer(fence, phase.root, reads)
                self._tail(fence["messages"], reads)
                bubbles = self._region_bubbles(fence["messages"], properties[3], reads=reads)
            text = read_composer_text(fence["composer"])
            fence["composer_text"] = text
            fence["sequence"] = semantic_sequence_digest(bubbles)
            # Exactly one full tree phase, followed by uncached critical reads.
            self._check_fence(target, fence, composer_check=lambda value: value == text, opening_certified=True)
        witness = CurrentChatWitness(**{f: getattr(guard, f) for f in (
            "run_id", "session_epoch", "surface_epoch", "worker_epoch", "observation_epoch", "desktop_lease_id",
            "control_revision", "process_id", "process_started_at_100ns", "window_handle")},
            account_id=target.account_id, conversation_id=target.conversation_id,
            binding_id=target.binding_id, binding_revision=target.binding_revision,
            captured_at=datetime.now(UTC), captured_monotonic_ns=time.monotonic_ns(),
            header_digest=properties[2], selected_row_runtime_id_hash=properties[0], selected_row_selection_source=properties[1],
            selected_row_candidate_count=1, active_chat_structure_digest=properties[3], conversation_type="direct",
            group_marker_probe_complete=True, group_marker_count=0, latest_tail=True, composer_empty=text == "")
        self._pending_fence = fence
        return HybridSnapshot(witness, bubbles, text)

    def capture(self, target, frame, expectation, *, deadline_at):
        from messenger_ai.adapters.qq.navigation.profile_verifier import capture_profile_acquisition
        from .profile_identity import _bounded_run
        if self.mode not in {"idle", "verify"}:
            raise HybridWorkerError("ui_reserved")
        self.discard_fence()
        def runner(command, **kwargs):
            guard = self.handler._guard(target)
            if any(getattr(guard, f) != getattr(frame, f) for f in (
                    "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
                    "control_revision", "process_id", "window_handle")):
                raise HybridWorkerError("identity_scope_mismatch")
            if self.handler.surface.snapshot(self.config.navigation.window)[3] != self.config.navigation.expected_process_started_at_100ns:
                raise HybridWorkerError("identity_process_lifetime_mismatch")
            return _bounded_run(command, **kwargs)
        return capture_profile_acquisition(self.config.helper_path,
            pid=frame.process_id, hwnd=frame.window_handle, vault=Path(self.config.vault_path),
            key_name="qq.identity.hmac", environment_fingerprint=expectation.environment_fingerprint,
            selector_pack_version=expectation.selector_pack_version,
            deadline=min(deadline_at, self.deadline), runner=runner)

    def _take_fence(self):
        fence = self._pending_fence
        self.discard_fence()
        if fence is None:
            raise HybridWorkerError("hybrid_input_fence_missing")
        return fence

    def write(self, target, text, *, before_action):
        from .guest_composer import get_uia_pattern, write_with_text_pattern, write_with_value_pattern
        if self.mode != "owned":
            raise HybridWorkerError("abort_not_owner")
        # Once only: the core obtains the full empty-composer/message baseline.
        # The input function thereafter uses uncached reads on this one fence.
        before_action()
        fence = self._take_fence()
        control = fence["composer"]
        pattern = get_uia_pattern(control, "GetValuePattern", 10002)
        writer = write_with_value_pattern if pattern is not None and not bool(getattr(pattern, "IsReadOnly", True)) else write_with_text_pattern
        def scope():
            self._check_fence(target, fence, composer_check=lambda value: text.startswith(value))
            return self.handler.transport._guest_scope(self.config.navigation.window)
        writer(control, text, scope_guard=scope,
               focus_guard=lambda c: self.handler.transport._composer_focused(c, self.config.navigation.window))

    def send(self, target, *, before_action):
        from .guest_composer import get_uia_pattern
        if self.mode != "owned":
            raise HybridWorkerError("abort_not_owner")
        from .guest_composer import read_composer_text
        fence = self._take_fence()
        text = read_composer_text(fence["composer"])
        pattern = get_uia_pattern(fence["send"], "GetInvokePattern", 10000)
        if pattern is None:
            raise HybridWorkerError("send_pattern_unavailable")
        self._check_fence(target, fence, composer_check=lambda value: value == text)
        # The core checked exact owned body in the preceding full snapshot;
        # preserve that exact value, including changes before this function.
        if text != fence["composer_text"]:
            raise HybridWorkerError("composer_drift")
        before_action()
        self._check_fence(target, fence, composer_check=lambda value: value == text)
        pattern.Invoke()

    def clear(self, target, text, *, before_action):
        from .guest_composer import _select_all_delete, clear_with_local_selection
        if self.mode != "abort":
            raise HybridWorkerError("abort_not_owner")
        fence = self._take_fence()
        before_action()
        def scope():
            self._check_fence(target, fence, composer_check=lambda value: value in ("", text), messages=False)
            return self.handler.transport._guest_scope(self.config.navigation.window)
        clear_with_local_selection(fence["composer"], clear_action=_select_all_delete,
            expected_text=text, scope_guard=scope,
            focus_guard=lambda c: self.handler.transport._composer_focused(c, self.config.navigation.window))
