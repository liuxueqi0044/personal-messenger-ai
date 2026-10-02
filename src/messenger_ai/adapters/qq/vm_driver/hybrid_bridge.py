"""Durable V2 draft ownership over the existing QQ cursor and operation journal.

A cold reservation is reversible and grants no send authority. Only the Hub's
new operation may adopt it, once, after the original authorization checks.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from messenger_ai.domain import SendOperation, SendStatus
from messenger_ai.runtime.contracts import ObservationBatch
from messenger_ai.runtime.staged_preparation import (
    DraftCleanupResult, DraftPreparationRequest, PreparedDraftTicket,
    StagedPreparationError, validate_ticket,
)
from .bridge import QQVMDriverBridge, _TERMINAL, _receipt_fingerprint
from .contracts import PreparedVerificationEvidence, WorkerCommand, WorkerKind, WorkerStatus


class QQHybridDriverBridge(QQVMDriverBridge):
    """Retain V1 business IDs and cursor semantics; replace only UI execution.

    ``scope_guard`` reads current durable control/artifact facts. The optional
    verification context factory must retire/reap the original process before
    granting a new process the same exclusive desktop, with a new epoch.
    """

    def __init__(self, *, scope_guard, expected_profile_signatures, verification_round=None, guard_refresh=None,
                 clock=lambda: datetime.now(UTC), monotonic_ns=time.monotonic_ns,
                 **kwargs):
        if not callable(scope_guard):
            raise TypeError("hybrid scope guard is required")
        if kwargs.get("bootstrap_last_inbound"):
            raise ValueError("hybrid cannot bootstrap an existing session")
        kwargs["recover_persistent_state"] = False
        kwargs["selection_refresh_retry_enabled"] = False
        super().__init__(**kwargs)
        if (set(expected_profile_signatures) != set(self._by_id)
                or any(not isinstance(x, str) or not x.startswith("qq-profile-hmac:")
                       or len(x.removeprefix("qq-profile-hmac:")) != 64
                       or any(c not in "0123456789abcdef" for c in x.removeprefix("qq-profile-hmac:"))
                       for x in expected_profile_signatures.values())):
            self._cursor.close()
            self._db.close()
            raise ValueError("hybrid persistent profile anchors are required")
        self._profile_signatures = dict(expected_profile_signatures)
        self._scope_guard = scope_guard
        self._verification_round = verification_round
        self._guard_refresh = guard_refresh
        self._clock, self._monotonic_ns = clock, monotonic_ns
        self._operation_cancellation = {}
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS qq_v2_draft_reservations(
          reservation_id TEXT PRIMARY KEY, nonce TEXT NOT NULL,
          account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
          request_json TEXT NOT NULL, expected_sequence_digest TEXT NOT NULL,
          status TEXT NOT NULL, ticket_json TEXT, evidence_json TEXT,
          operation_id TEXT UNIQUE, error_code TEXT,
          authorization_expires_at TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(nonce)
        );
        """)
        if "authorization_expires_at" not in {row["name"] for row in self._db.execute("PRAGMA table_info(qq_v2_draft_reservations)")}:
            self._db.execute("ALTER TABLE qq_v2_draft_reservations ADD COLUMN authorization_expires_at TEXT")

    def binding_id_for(self, conversation_id):
        return self._bindings[conversation_id].binding_id

    @property
    def observation_conversation_ids(self):
        return tuple(self._bindings)

    def worker_status_snapshot(self):
        status = self._worker.status_snapshot()
        return {"schema": "qq_hybrid_session_v2", **status.model_dump(mode="json")}

    def health(self):
        from .contracts import WorkerResult
        if self._worker.status_snapshot().cleanup_required:
            command = WorkerCommand(kind=WorkerKind.HEALTH)
            return WorkerResult(request_id=command.request_id, kind=command.kind,
                status=WorkerStatus.UNAVAILABLE, worker_epoch=UUID(int=0),
                error_code="hybrid_session_cleanup_required")
        if self._last_health is not None:
            return self._last_health
        command = WorkerCommand(kind=WorkerKind.HEALTH)
        return WorkerResult(request_id=command.request_id, kind=command.kind,
            status=WorkerStatus.UNAVAILABLE, worker_epoch=UUID(int=0), error_code="health_not_probed")

    def has_cleanup_obligation(self, account_id):
        if self._db.execute("""SELECT 1 FROM qq_v2_draft_reservations
            WHERE account_id=? AND status IN
            ('preparing','prepared','adopt_intent','adopted','cleanup_required') LIMIT 1""",
            (account_id,)).fetchone():
            return True
        ids = [x.binding_id for x in self._bindings.values() if x.account_id == account_id]
        return any(self._db.execute("""SELECT 1 FROM qq_vm_ops WHERE binding_id=?
            AND status NOT IN ('verified','failed','send_uncertain','cancelled') LIMIT 1""",
            (binding_id,)).fetchone() for binding_id in ids)

    def _row(self, owner):
        row = self._db.execute("SELECT * FROM qq_v2_draft_reservations WHERE reservation_id=?",
                               (str(owner.reservation_id),)).fetchone()
        if row is None or row["nonce"] != str(owner.nonce):
            raise StagedPreparationError("hybrid_reservation_not_owned")
        request = DraftPreparationRequest.model_validate_json(row["request_json"])
        if isinstance(owner, DraftPreparationRequest):
            valid = request == owner
        else:
            valid = bool(row["ticket_json"] and
                PreparedDraftTicket.model_validate_json(row["ticket_json"]) == owner)
        if not valid:
            raise StagedPreparationError("hybrid_reservation_scope_mismatch")
        return row, request

    def _current(self, request, *, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise StagedPreparationError("hybrid_control_cancelled")
        binding = self._bindings.get(request.conversation_id)
        if (binding is None or binding.binding_id != request.binding_id
                or binding.account_id != request.account_id or binding.contact_id != request.contact_id
                or self._clock() >= request.deadline_at
                or self._monotonic_ns() >= request.deadline_monotonic_ns
                or self._scope_guard(request) is not True):
            raise StagedPreparationError("hybrid_preparation_scope_changed")

    def _cursor_expectation(self, request):
        conn = self._cursor.connection
        digest = self._cursor.snapshot_token(request.conversation_id)
        if digest is None:
            raise StagedPreparationError("hybrid_cursor_baseline_missing")
        rows = conn.execute("""SELECT local_key,payload_json,status FROM observation_outbox
            WHERE conversation_id=? ORDER BY outbox_id""", (request.conversation_id,)).fetchall()
        # RuntimeCoordinator's business key includes the original conversation
        # namespace. Never compare it with a raw QQ or local cursor key.
        inbound = {f"qq-uia/{request.conversation_id}/{x['local_key']}": x for x in rows
                   if json.loads(x["payload_json"]).get("direction") == "inbound"}
        if (not inbound or next(reversed(inbound)) != request.expected_last_message_key
                or any(key not in inbound or inbound[key]["status"] != "delivered"
                       for key in request.source_message_keys)
                or any(x["status"] != "delivered" for x in rows)):
            raise StagedPreparationError("hybrid_cursor_source_unsettled")
        return digest

    def retain_cleanup_hold(self, owner, *, reason):
        # A hold never adopts/replays an operation or guesses at the composer.
        self._row(owner)
        self._db.execute("""UPDATE qq_v2_draft_reservations
            SET status='cleanup_required',error_code=? WHERE reservation_id=? AND nonce=?""",
            (reason, str(owner.reservation_id), str(owner.nonce)))

    async def prepare_draft(self, request, *, cancel_event=None):
        DraftPreparationRequest.model_validate(request.model_dump())
        async with self._generation_lock:
            self._current(request, cancel_event=cancel_event)
            if self.has_cleanup_obligation(request.account_id):
                raise StagedPreparationError("hybrid_cleanup_obligation")
            digest = self._cursor_expectation(request)
            # Persist ownership before an IPC call can mutate the composer.
            try:
                self._db.execute("""INSERT INTO qq_v2_draft_reservations
                    (reservation_id,nonce,account_id,conversation_id,request_json,
                     expected_sequence_digest,status,created_at)
                    VALUES(?,?,?,?,?,?,'preparing',?)""",
                    (str(request.reservation_id), str(request.nonce), request.account_id,
                     request.conversation_id, request.model_dump_json(), digest,
                     self._clock().isoformat()))
            except Exception:
                raise StagedPreparationError("hybrid_reservation_replayed") from None
            try:
                prepared = await self._worker.prepare_draft(request,
                    expected_sequence_digest=digest, cancel_event=cancel_event)
                ticket = prepared.ticket
                proof = PreparedVerificationEvidence.model_validate(prepared.prepared_evidence.model_dump())
                validate_ticket(ticket, request, now=self._clock(), tick=self._monotonic_ns())
                if (ticket.expected_sequence_digest != digest
                        or proof.owner_binding_id != request.binding_id
                        or proof.target_identity.binding_id != request.binding_id
                        or proof.target_identity.participant_signature != self._profile_signatures[request.binding_id]
                        or proof.target_identity.conversation_type != "direct"
                        or (proof.target_identity.process_id, proof.target_identity.window_handle)
                           != (ticket.process_id, ticket.window_handle)
                        or proof.text_hash != request.body_hash
                        or proof.segment_ref != f"{request.pacing_plan_id}:{request.segment_index}"
                        or hashlib.sha256(proof.model_dump_json().encode()).hexdigest() != ticket.evidence_digest):
                    raise StagedPreparationError("hybrid_prepared_proof_mismatch")
                # Save even if control changed during the call, so exact owned
                # cleanup has its ticket. No permission is acquired here.
                self._db.execute("""UPDATE qq_v2_draft_reservations SET
                    ticket_json=?,evidence_json=? WHERE reservation_id=? AND nonce=?""",
                    (ticket.model_dump_json(), proof.model_dump_json(), str(request.reservation_id), str(request.nonce)))
                changed = self._db.execute("""UPDATE qq_v2_draft_reservations SET status='prepared'
                    WHERE reservation_id=? AND nonce=? AND status='preparing'""",
                    (str(request.reservation_id), str(request.nonce))).rowcount
                if changed != 1:
                    raise StagedPreparationError("hybrid_reservation_held")
                self._current(request, cancel_event=cancel_event)
                if self._cursor_expectation(request) != digest:
                    raise StagedPreparationError("hybrid_cursor_changed")
                return ticket
            except BaseException:
                self._db.execute("""UPDATE qq_v2_draft_reservations SET status='cleanup_required',
                    error_code='hybrid_prepare_unsettled' WHERE reservation_id=?""",
                    (str(request.reservation_id),))
                raise

    async def adopt_prepared(self, ticket, command, *, operation_id, segment_ref,
                             binding_revision, conversation_revision, cancel_event=None):
        async with self._generation_lock:
            row, request = self._row(ticket)
            self._current(request, cancel_event=cancel_event)
            validate_ticket(ticket, request, now=self._clock(), tick=self._monotonic_ns())
            if (row["status"] != "prepared" or row["operation_id"] is not None
                    or command.draft_id != request.draft_id or command.conversation_id != request.conversation_id
                    or command.text_hash != request.body_hash or command.expires_at <= self._clock()
                    or command.expected_last_message_key != request.expected_last_message_key
                    or command.idempotency_key != f"m10:{request.pacing_plan_id}:{request.segment_index}"
                    or segment_ref != f"{request.pacing_plan_id}:{request.segment_index}"
                    or (binding_revision, conversation_revision) != (request.binding_revision, request.conversation_revision)
                    or self._cursor_expectation(request) != row["expected_sequence_digest"]
                    or self._db.execute("""SELECT 1 FROM qq_vm_ops WHERE operation_id=?
                        OR idempotency_key=? OR segment_ref=?""",
                        (str(operation_id), command.idempotency_key, segment_ref)).fetchone()):
                raise StagedPreparationError("hybrid_adoption_scope_mismatch")
            # Durable one-use fence precedes the worker's in-memory rebind. A
            # lost acknowledgement requires cleanup, never a second adoption.
            changed = self._db.execute("""UPDATE qq_v2_draft_reservations SET status='adopt_intent',operation_id=?,authorization_expires_at=?
                WHERE reservation_id=? AND nonce=? AND status='prepared'""",
                (str(operation_id), command.expires_at.isoformat(), str(ticket.reservation_id), str(ticket.nonce))).rowcount
            if changed != 1:
                raise StagedPreparationError("hybrid_reservation_held")
            worker_command = WorkerCommand(kind=WorkerKind.PREPARE, binding_id=ticket.binding_id,
                operation_id=operation_id, segment_ref=segment_ref, text=request.body,
                binding_revision=binding_revision, conversation_revision=conversation_revision,
                deadline=ticket.expires_at)
            try:
                result = await self._worker.adopt_prepared(ticket, worker_command, cancel_event=cancel_event)
                result = self._correlated_result(worker_command, result)
                proof = PreparedVerificationEvidence.model_validate(result.evidence.get("prepared_evidence"))
                if (result.status is not WorkerStatus.OK or str(result.worker_epoch) != ticket.worker_epoch
                        or proof.model_dump_json() != row["evidence_json"]):
                    raise StagedPreparationError("hybrid_adoption_unproven")
                self._current(request, cancel_event=cancel_event)
                validate_ticket(ticket, request, now=self._clock(), tick=self._monotonic_ns())
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute("""INSERT INTO qq_vm_ops
                        (operation_id,idempotency_key,draft_id,conversation_id,binding_id,segment_ref,
                         binding_revision,conversation_revision,text_hash,status,commit_intent,error_code)
                        VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                        (str(operation_id), command.idempotency_key, str(command.draft_id),
                         command.conversation_id, ticket.binding_id, segment_ref, binding_revision,
                         conversation_revision, command.text_hash, SendStatus.PREPARED.value))
                    self._db.execute("INSERT INTO qq_vm_prepared_evidence VALUES(?,?)",
                        (str(operation_id), row["evidence_json"]))
                    changed = self._db.execute("""UPDATE qq_v2_draft_reservations SET status='adopted'
                        WHERE reservation_id=? AND nonce=? AND status='adopt_intent' AND operation_id=?""",
                        (str(ticket.reservation_id), str(ticket.nonce), str(operation_id))).rowcount
                    if changed != 1:
                        raise StagedPreparationError("hybrid_reservation_held")
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                self._operation_cancellation[operation_id] = cancel_event
                return self._operation(self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?",
                    (str(operation_id),)).fetchone())
            except BaseException:
                self.retain_cleanup_hold(ticket, reason="hybrid_adoption_unsettled")
                raise

    async def abort_owned_ticket(self, owner, *, deadline_at):
        async with self._generation_lock:
            try:
                row, _ = self._row(owner)
            except StagedPreparationError:
                return DraftCleanupResult(status="not_owned", reservation_id=owner.reservation_id,
                                          nonce=owner.nonce, error_code="hybrid_reservation_not_owned")
            if row["status"] == "cleaned":
                return DraftCleanupResult(status="cleaned", reservation_id=owner.reservation_id, nonce=owner.nonce)
            if row["operation_id"]:
                op = self._db.execute("SELECT commit_intent FROM qq_vm_ops WHERE operation_id=?",
                                      (row["operation_id"],)).fetchone()
                if op is not None and op["commit_intent"]:
                    self.retain_cleanup_hold(owner, reason="hybrid_commit_intent_cleanup_forbidden")
                    return DraftCleanupResult(status="cleanup_required", reservation_id=owner.reservation_id,
                        nonce=owner.nonce, error_code="hybrid_commit_intent_cleanup_forbidden")
            try:
                result = await self._worker.abort_draft(owner, deadline_at=deadline_at)
                if (not isinstance(result, DraftCleanupResult) or result.status != "cleaned"
                        or result.reservation_id != owner.reservation_id or result.nonce != owner.nonce):
                    raise StagedPreparationError("hybrid_owned_cleanup_unproven")
            except BaseException:
                self.retain_cleanup_hold(owner, reason="hybrid_owned_cleanup_unproven")
                raise
            self._db.execute("UPDATE qq_v2_draft_reservations SET status='cleaned',error_code=NULL WHERE reservation_id=?",
                             (str(owner.reservation_id),))
            if row["operation_id"]:
                self._db.execute("""UPDATE qq_vm_ops SET status='cancelled' WHERE operation_id=?
                    AND commit_intent=0 AND status='prepared'""", (row["operation_id"],))
            return result

    async def prepare_send(self, command, **kwargs):
        raise StagedPreparationError("hybrid_staged_ticket_required")

    async def observe_conversation(self, conversation_id, *, binding_revision, conversation_revision):
        binding = self._bindings[conversation_id]
        # A new process must not open a profile or adopt a baseline while any
        # prior process may have left a draft. Existing cursor data is required.
        if self.has_cleanup_obligation(binding.account_id) or not self._cursor.has_snapshot(conversation_id):
            return ObservationBatch(account_id=binding.account_id, contact_id=binding.contact_id,
                conversation_id=conversation_id, binding_revision=binding_revision,
                conversation_revision=conversation_revision, complete=False,
                gap_reason="driver_quarantine:hybrid_cleanup_or_baseline_required", messages=())
        return await super().observe_conversation(conversation_id,
            binding_revision=binding_revision, conversation_revision=conversation_revision)

    async def _request_before_deadline(self, worker, command):
        return await worker.execute(command)

    async def _request_with_selection_process_refresh(self, command):
        return self._correlated_result(command, await self._worker.execute(command))

    async def _recover_after_read_only_timeout(self, **kwargs):
        # Process recovery is explicit; it cannot navigate or replay a read.
        return False

    async def probe_health_async(self):
        command = WorkerCommand(kind=WorkerKind.HEALTH, deadline=self._clock() + timedelta(seconds=self._timeout))
        self._last_health = self._correlated_result(command, await self._worker.execute(command))
        return self._last_health

    def probe_health(self):
        raise RuntimeError("hybrid health requires probe_health_async")

    async def _commit_send_locked(self, operation):
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            raise StagedPreparationError("hybrid_operation_missing")
        stored = self._operation(row)
        if stored.status in _TERMINAL:
            return stored
        if row["commit_intent"]:
            stored.status, stored.error_code = SendStatus.UNCERTAIN, "send_uncertain"
            return self._persist(stored)
        if stored.status is not SendStatus.PREPARED:
            return stored
        owner_row = self._db.execute("SELECT * FROM qq_v2_draft_reservations WHERE operation_id=? AND status='adopted'",
                                     (str(operation.operation_id),)).fetchone()
        if owner_row is None:
            raise StagedPreparationError("hybrid_adopted_owner_missing")
        request = DraftPreparationRequest.model_validate_json(owner_row["request_json"])
        ticket = PreparedDraftTicket.model_validate_json(owner_row["ticket_json"])
        cancel = self._operation_cancellation.get(stored.operation_id)
        self._current(request, cancel_event=cancel)
        validate_ticket(ticket, request, now=self._clock(), tick=self._monotonic_ns())
        if self._cursor_expectation(request) != owner_row["expected_sequence_digest"]:
            raise StagedPreparationError("hybrid_cursor_changed")
        authority_deadline = datetime.fromisoformat(owner_row["authorization_expires_at"])
        if authority_deadline <= self._clock():
            raise StagedPreparationError("hybrid_authorization_expired")
        # Publish this operation's own new obligation before the child's fresh
        # read, so the heartbeat cannot mistake that normal handoff for drift.
        self._persist(stored, commit_intent=True)
        command = WorkerCommand(kind=WorkerKind.COMMIT, binding_id=row["binding_id"],
            operation_id=stored.operation_id, binding_revision=row["binding_revision"],
            conversation_revision=row["conversation_revision"], deadline=min(ticket.expires_at, authority_deadline))
        interruption = None
        try:
            if self._guard_refresh is not None:
                await self._guard_refresh(UUID(ticket.worker_epoch))
            self._current(request, cancel_event=cancel)
            validate_ticket(ticket, request, now=self._clock(), tick=self._monotonic_ns())
            if self._clock() >= authority_deadline:
                raise StagedPreparationError("hybrid_authorization_expired")
            result = self._correlated_result(command, await self._worker.execute(command, cancel_event=cancel))
            ok = result.status is WorkerStatus.OK and str(result.worker_epoch) == ticket.worker_epoch
        except BaseException as exc:
            ok = False
            if not isinstance(exc, Exception):
                interruption = exc
        stored.status = SendStatus.COMMITTED if ok else SendStatus.UNCERTAIN
        stored.error_code = None if ok else "send_uncertain"
        self._db.execute("UPDATE qq_v2_draft_reservations SET status=?,error_code=? WHERE reservation_id=?",
            ("committed" if ok else "cleanup_required", stored.error_code, str(request.reservation_id)))
        stored = self._persist(stored)
        if interruption is not None:
            raise interruption
        return stored

    async def _verify_send_locked(self, operation):
        row = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if row is None:
            raise StagedPreparationError("hybrid_operation_missing")
        stored = self._operation(row)
        if stored.status is not SendStatus.COMMITTED:
            return stored
        interruption = None
        try:
            proof = PreparedVerificationEvidence.model_validate_json(self._db.execute(
                "SELECT evidence_json FROM qq_vm_prepared_evidence WHERE operation_id=?",
                (str(operation.operation_id),)).fetchone()["evidence_json"])
            command = WorkerCommand(kind=WorkerKind.VERIFY, binding_id=row["binding_id"],
                operation_id=stored.operation_id, binding_revision=row["binding_revision"],
                conversation_revision=row["conversation_revision"], prepared_evidence=proof,
                deadline=self._clock() + timedelta(seconds=self._timeout))
            if self._verification_round is None:
                raise StagedPreparationError("hybrid_fresh_verify_round_missing")
            async with self._verification_round(command) as worker:
                result = self._correlated_result(command, await worker.execute(command))
            owner_row = self._db.execute("SELECT ticket_json FROM qq_v2_draft_reservations WHERE operation_id=?",
                                        (str(operation.operation_id),)).fetchone()
            old_epoch = PreparedDraftTicket.model_validate_json(owner_row["ticket_json"]).worker_epoch
            receipt = result.evidence.get("receipt")
            if (result.status is not WorkerStatus.OK or str(result.worker_epoch) == old_epoch
                    or not isinstance(receipt, dict) or receipt.get("direction") != "outbound"
                    or not isinstance(receipt.get("text"), str)
                    or hashlib.sha256(receipt["text"].encode()).hexdigest() != proof.text_hash
                    or not isinstance(receipt.get("message_key"), str) or not receipt["message_key"]
                    or receipt.get("conversation_internal_id") != self._by_id[row["binding_id"]].platform_conversation_id
                    or receipt.get("participant_signature") != self._by_id[row["binding_id"]].participant_signature
                    or result.evidence.get("operation_id") != str(stored.operation_id)):
                raise StagedPreparationError("hybrid_verification_unproven")
            stored.status, stored.error_code = SendStatus.VERIFIED, None
            self._db.execute("INSERT OR IGNORE INTO qq_vm_receipts(operation_id,conversation_id,receipt_fingerprint) VALUES(?,?,?)",
                             (str(stored.operation_id), row["conversation_id"],
                              # OBSERVE and the existing cursor serialize QQBubble,
                              # which has no per-message participant signature.
                              # Keep its fingerprint format after checking the
                              # separate receipt identity association above.
                              _receipt_fingerprint({key:value for key,value in receipt.items()
                                                    if key != "participant_signature"})))
        except BaseException as exc:
            stored.status, stored.error_code = SendStatus.UNCERTAIN, "send_uncertain"
            if not isinstance(exc, Exception):
                interruption = exc
        self._db.execute("UPDATE qq_v2_draft_reservations SET status=?,error_code=? WHERE operation_id=?",
            ("verified" if stored.status is SendStatus.VERIFIED else "cleanup_required",
             stored.error_code, str(stored.operation_id)))
        stored = self._persist(stored)
        if interruption is not None:
            raise interruption
        return stored

    async def abort_send(self, operation):
        row = self._db.execute("SELECT ticket_json FROM qq_v2_draft_reservations WHERE operation_id=?",
                              (str(operation.operation_id),)).fetchone()
        if row is None:
            stored = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?",
                                      (str(operation.operation_id),)).fetchone()
            if stored is not None and self._operation(stored).status in _TERMINAL:
                return self._operation(stored)
            raise StagedPreparationError("hybrid_adopted_owner_missing")
        ticket = PreparedDraftTicket.model_validate_json(row["ticket_json"])
        result = await self.abort_owned_ticket(ticket, deadline_at=self._clock() + timedelta(seconds=self._timeout))
        stored = self._db.execute("SELECT * FROM qq_vm_ops WHERE operation_id=?", (str(operation.operation_id),)).fetchone()
        if stored:
            return self._operation(stored)
        operation.status = SendStatus.CANCELLED if result.status == "cleaned" else SendStatus.UNCERTAIN
        return operation

    def close(self):
        self._worker.close()
        self._cursor.close()
        self._db.close()

    async def aclose(self):
        async with self._generation_lock:
            close_async = getattr(self._worker, "aclose", None)
            if callable(close_async):
                await close_async()
            else:
                self._worker.close()
            self._cursor.close()
            self._db.close()
