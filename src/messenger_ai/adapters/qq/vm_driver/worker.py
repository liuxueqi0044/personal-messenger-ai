from __future__ import annotations

import copy
import hashlib
import json
import math
import multiprocessing as mp
import re
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from messenger_ai.adapters.qq.models import (
    QQCertifiedDirectIdentity,
    QQConversation,
    QQIdentityBinding,
    QQSelectorPack,
    QQSessionObservedDirectIdentity,
    QQWindow,
)
from messenger_ai.adapters.qq.ports import (
    QQAccessibilityPort,
    QQCandidateLocator,
    QQCurrentIdentityCertifier,
)

from .contracts import (
    PreparedBubbleAnchor,
    PreparedTargetIdentity,
    PreparedVerificationEvidence,
    SelectionHandoff,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
    verify_selection_handoff_auth,
)
from .sequence_alignment import SnapshotAlignmentError, unique_suffix_start
from .session_identity import QQSessionCandidateLocator, QQSessionIdentityCertifier
from .transport import UIAUnavailable, WindowsUIAQQAccessibility
from .visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE,
    ConversationRowPaletteProfile,
    ConversationSelectionActuator,
    ConversationSelectionStatus,
    DeepSeekVisualSelectionProvider,
    SelectionVisualAttestation,
    VisualSelectionConfig,
    runtime_id_digest,
)

CurrentDirectIdentity = QQCertifiedDirectIdentity | QQSessionObservedDirectIdentity


def _validated_prepare_write_reserve(value: object) -> float:
    """Return a finite reserve large enough to be an effective write fence."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("prepare write reserve must be a number")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("prepare write reserve must be a finite number") from exc
    if not math.isfinite(parsed) or parsed < 1.0 or parsed > 600:
        raise ValueError(
            "prepare write reserve must be at least 1 and no more than 600 seconds"
        )
    return parsed


_SAFE_EXCEPTION_MESSAGES = {
    "QQ window absent or ambiguous": "qq_window_absent_or_ambiguous",
    "binding proof drift after selection": "binding_proof_drift_after_selection",
    "binding target absent or ambiguous": "binding_target_absent_or_ambiguous",
    "candidate locator unavailable": "candidate_locator_unavailable",
    "conversation type evidence version drift": "conversation_type_evidence_version_drift",
    "current identity certifier unavailable": "current_identity_certifier_unavailable",
    "guest machine identity could not be certified": "guest_machine_identity_unavailable",
    "install personal-messenger-ai[qq-vm] in the guest": "qq_uia_dependency_missing",
    "refusing UI automation outside a certified VM guest": "guest_scope_not_certified",
    "refusing UI automation: machine is not a certified VirtualBox guest": "guest_machine_not_certified",
    "selection visual attestation drift": "selection_visual_attestation_drift",
    "selection visual attestation failed": "selection_visual_attestation_failed",
    "selection visual header is unproven": "selection_visual_header_unproven",
    "selection visual profile is unavailable": "selection_visual_profile_unavailable",
    "selection visual profile scope drift": "selection_visual_profile_scope_drift",
    "selection handoff is invalid": "selection_handoff_invalid",
    "operation identity lease is invalid": "operation_identity_lease_invalid",
}
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,95}$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _safe_visual_selection_evidence(value: object) -> dict[str, object]:
    """Copy only non-content visual action metadata with strict bounds."""

    if not isinstance(value, dict):
        return {}
    evidence: dict[str, object] = {}
    frame_sha256 = value.get("frame_sha256")
    if isinstance(frame_sha256, str) and _SAFE_SHA256.fullmatch(frame_sha256):
        evidence["frame_sha256"] = frame_sha256
    model = value.get("model")
    if isinstance(model, str) and _SAFE_MODEL_ID.fullmatch(model):
        evidence["model"] = model
    latency_ms = value.get("latency_ms")
    if (
        isinstance(latency_ms, int)
        and not isinstance(latency_ms, bool)
        and 0 <= latency_ms <= 300_000
    ):
        evidence["latency_ms"] = latency_ms
    visual_decision = value.get("visual_decision")
    if visual_decision in {"match", "not_match", "ambiguous"}:
        evidence["visual_decision"] = visual_decision
    visual_reason = value.get("visual_reason")
    if visual_reason in {
        "exact_label",
        "different_label",
        "label_missing",
        "multiple_primary_labels",
        "unreadable",
    }:
        evidence["visual_reason"] = visual_reason
    visual_confidence = value.get("visual_confidence")
    if (
        isinstance(visual_confidence, (int, float))
        and not isinstance(visual_confidence, bool)
        and 0 <= visual_confidence <= 1
    ):
        evidence["visual_confidence"] = float(visual_confidence)
    normalized_label_match = value.get("normalized_label_match")
    if isinstance(normalized_label_match, bool):
        evidence["normalized_label_match"] = normalized_label_match
    provider_error_category = value.get("provider_error_category")
    if provider_error_category in {
        "rate_limited",
        "server_error",
        "timeout",
        "network",
        "schema",
        "rejected",
        "unknown",
    }:
        evidence["provider_error_category"] = provider_error_category
    return evidence


def _with_visual_selection_evidence(
    exc: BaseException, evidence: dict[str, object]
) -> BaseException:
    """Attach only the already-sanitized visual summary to a fixed-code error."""

    safe = _safe_visual_selection_evidence(evidence)
    if safe:
        exc.__dict__["_visual_selection_evidence"] = safe
    return exc


def _safe_failure(exc: BaseException, *, fallback: str) -> tuple[str, dict[str, object]]:
    """Keep actionable adapter causes without serializing arbitrary exception text."""

    message = str(exc).strip()
    error_code = _SAFE_EXCEPTION_MESSAGES.get(message)
    if error_code is None and _SAFE_ERROR_CODE.fullmatch(message):
        error_code = message
    chain: list[str] = []
    com_hresult: int | None = None
    project_frames: list[dict[str, object]] = []
    visual_selection_evidence: dict[str, object] = {}
    current: BaseException | None = exc
    while current is not None and len(chain) < 5:
        chain.append(type(current).__name__)
        candidate = getattr(current, "hresult", None)
        if not isinstance(candidate, int) and getattr(current, "args", ()):
            candidate = current.args[0]
        if com_hresult is None and isinstance(candidate, int):
            com_hresult = candidate
        if not visual_selection_evidence:
            visual_selection_evidence = _safe_visual_selection_evidence(
                getattr(current, "_visual_selection_evidence", None)
            )
        for frame in traceback.extract_tb(current.__traceback__):
            normalized = frame.filename.replace("\\", "/")
            marker = "/messenger_ai/"
            if marker not in normalized:
                continue
            project_frames.append({
                "function": frame.name,
                "file": f"messenger_ai/{normalized.split(marker, 1)[1]}",
                "line": frame.lineno,
            })
        current = current.__cause__ or current.__context__
    evidence: dict[str, object] = {"exception_types": chain}
    if com_hresult is not None:
        evidence["com_hresult"] = com_hresult
    if project_frames:
        evidence["project_frames"] = project_frames[-8:]
    evidence.update(visual_selection_evidence)
    return error_code or fallback, evidence


class QQVMWorker:
    """One serial UIA execution lane. Instantiate this class in the VM process."""

    def __init__(self, *, accessibility: QQAccessibilityPort, selector_pack: QQSelectorPack,
                 bindings: tuple[QQIdentityBinding, ...],
                 identity_certifier: QQCurrentIdentityCertifier | None = None,
                 candidate_locator: QQCandidateLocator | None = None,
                 selection_actuator: ConversationSelectionActuator | None = None,
                 selection_visual_profile: ConversationRowPaletteProfile | None = None,
                 selection_handoff_signing_key: bytes | None = None,
                 prepare_write_reserve_seconds: float = 5.0,
                 expected_window: tuple[int, int] | None = None,
                 window_validator: Callable[[QQWindow], None] | None = None,
                 run_id: str | None = None) -> None:
        self._accessibility = accessibility
        self._selectors = selector_pack
        self._bindings = {item.binding_id: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("binding_id must be unique")
        self._epoch = uuid4()
        self._lock = threading.Lock()
        self._prepared: dict[UUID, dict[str, object]] = {}
        self._reservation: UUID | None = None
        self._committed: set[UUID] = set()
        self._identity_certifier = identity_certifier
        self._candidate_locator = candidate_locator
        self._selection_actuator = selection_actuator
        self._selection_visual_profile = selection_visual_profile
        self._selection_handoff_signing_key = bytes(
            selection_handoff_signing_key or secrets.token_bytes(32)
        )
        if len(self._selection_handoff_signing_key) < 32:
            raise ValueError("selection handoff signing key is too short")
        self._prepare_write_reserve_seconds = _validated_prepare_write_reserve(
            prepare_write_reserve_seconds
        )
        self._expected_window = expected_window
        self._window_validator = window_validator
        self._reservation_binding_id: str | None = None
        self._last_visual_selection_evidence: dict[str, object] = {}
        self._consumed_selection_handoffs: set[UUID] = set()
        self._trusted_operation_lease: tuple[UUID, str, int, int] | None = None
        self._run_id = run_id
        if expected_window is not None and window_validator is None:
            raise ValueError("a scoped session window requires current-session validation")
        if any(item.authorization_scope == "all_direct_including_temporary" for item in bindings):
            if identity_certifier is None:
                raise ValueError("production direct bindings require a current identity certifier")
            if candidate_locator is None:
                raise ValueError("production direct bindings require a candidate locator")

    def execute(self, command: WorkerCommand) -> WorkerResult:
        """Runs exactly one fixed UI action under the worker's only UI lock."""
        with self._lock:
            if command.deadline is not None and command.deadline <= datetime.now(UTC):
                return self._result(command, WorkerStatus.FAILED_SAFE, "deadline_expired")
            if self._reservation is not None and command.operation_id != self._reservation:
                return self._result(command, WorkerStatus.FAILED_SAFE, "ui_reserved")
            if (self._reservation is not None and command.operation_id == self._reservation and
                    command.binding_id != self._reservation_binding_id):
                return self._result(command, WorkerStatus.FAILED_SAFE, "operation_binding_mismatch")
            if command.requires_binding() and command.binding_id not in self._bindings:
                return self._result(command, WorkerStatus.FAILED_SAFE, "unknown_binding")
            try:
                if command.kind is WorkerKind.HEALTH:
                    return self._health(command)
                if command.kind is WorkerKind.SELECT_ONLY:
                    return self._select_only(command)
                if command.kind is WorkerKind.VERIFY_SELECTION_ONLY:
                    return self._verify_selection_only(command)
                if command.kind is WorkerKind.OBSERVE:
                    return self._observe(command)
                if command.kind is WorkerKind.PREPARE:
                    return self._prepare(command)
                if command.kind is WorkerKind.COMMIT:
                    return self._commit(command)
                if command.kind is WorkerKind.VERIFY:
                    return self._verify(command)
                if command.kind is WorkerKind.ABORT:
                    return self._abort(command)
                if command.kind is WorkerKind.STOP:
                    return self._result(command, WorkerStatus.OK)
            except Exception as exc:  # an action after commit intent is outcome-unknown.
                error_code, evidence = _safe_failure(exc, fallback="worker_action_failed")
                if (
                    command.kind is WorkerKind.PREPARE
                    and command.operation_id is not None
                    and self._reservation == command.operation_id
                ):
                    evidence["cleanup_required"] = True
                if command.kind in {WorkerKind.COMMIT, WorkerKind.VERIFY}:
                    return self._result(command, WorkerStatus.UNCERTAIN, error_code, evidence)
                return self._result(command, WorkerStatus.FAILED_SAFE, error_code, evidence)
            return self._result(command, WorkerStatus.FAILED_SAFE, "unsupported_command")

    def mint_selection_handoff(self, **kwargs) -> SelectionHandoff:
        """Issue a capability authenticated for this worker instance."""

        return mint_selection_handoff(
            **kwargs,
            signing_key=self._selection_handoff_signing_key,
        )

    @staticmethod
    def _ensure_command_live(command: WorkerCommand) -> None:
        now = datetime.now(UTC)
        if command.deadline is not None and now >= command.deadline:
            raise RuntimeError("deadline_expired")
        if (
            command.selection_handoff is not None
            and now >= command.selection_handoff.expires_at
        ):
            raise RuntimeError("deadline_expired")

    def _operation_lease_matches(
        self, command: WorkerCommand, binding: QQIdentityBinding,
    ) -> bool:
        return bool(
            binding.authorization_scope == "all_direct_including_temporary"
            and command.kind
            in {WorkerKind.COMMIT, WorkerKind.VERIFY, WorkerKind.ABORT}
            and command.operation_id is not None
            and self._trusted_operation_lease
            == (
                command.operation_id,
                binding.binding_id,
                command.binding_revision,
                command.conversation_revision,
            )
        )

    def _health(self, command: WorkerCommand) -> WorkerResult:
        try:
            window = self._target_window()
        except Exception as exc:
            error_code, evidence = _safe_failure(exc, fallback="qq_window_unavailable")
            if str(exc) == "QQ window absent or ambiguous":
                error_code = "qq_window_ambiguous"
            return self._result(command, WorkerStatus.UNAVAILABLE, error_code, evidence)
        return self._result(command, WorkerStatus.OK, evidence={"process_id": window.process_id, "window_handle": window.window_handle})

    def _select_only(self, command: WorkerCommand) -> WorkerResult:
        binding = self._bindings[command.binding_id or ""]
        window, _conversation, proof, _current = self._resolve(
            command, binding, current_reader=None
        )
        target_identity = self._selection_identity_evidence(binding, window, proof)
        return self._result(
            command,
            WorkerStatus.OK,
            evidence={
                "selection_confirmed": True,
                "target_identity": target_identity,
            },
        )

    def _verify_selection_only(self, command: WorkerCommand) -> WorkerResult:
        """Confirm an already-selected direct chat without any selection action."""

        binding = self._bindings[command.binding_id or ""]
        with self._stage(command, "target_window"):
            window = self._target_window()
        with self._stage(command, "guest_foreground"):
            ensure_foreground = getattr(
                self._accessibility, "ensure_guest_foreground", None
            )
            if callable(ensure_foreground):
                self._ensure_command_live(command)
                ensure_foreground(window)
                self._ensure_command_live(command)
        read_phase = getattr(self._accessibility, "read_phase", None)
        if callable(read_phase):
            with self._stage(command, "discovery_phase"), read_phase(window):
                conversation = self._locate_conversation(window, binding)
            with self._stage(command, "verification_phase"):
                proof, _current = self._confirm_after_selection(
                    command=command,
                    binding=binding,
                    window=window,
                    conversation=conversation,
                    read_phase=read_phase,
                    current_reader=None,
                )
        else:
            with self._stage(command, "discovery_phase"):
                conversation = self._locate_conversation(window, binding)
            with self._stage(command, "verification_phase"):
                confirm = getattr(
                    self._accessibility, "confirm_conversation_selected", None
                )
                if not callable(confirm):
                    raise UIAUnavailable("selection confirmation unavailable")
                confirm(
                    window,
                    conversation,
                    self._selectors.selector("conversation_item"),
                )
                proof = self._confirm_identity(binding, window, conversation)
        return self._result(
            command,
            WorkerStatus.OK,
            evidence={
                "selection_confirmed": True,
                "target_identity": self._selection_identity_evidence(
                    binding, window, proof
                ),
            },
        )

    def _selection_identity_evidence(
        self,
        binding: QQIdentityBinding,
        window: QQWindow,
        proof: CurrentDirectIdentity | None,
    ) -> dict[str, object]:
        if isinstance(proof, QQSessionObservedDirectIdentity):
            return {
                "binding_id": proof.binding_id,
                "conversation_type": proof.conversation_type,
                "process_id": proof.process_id,
                "window_handle": proof.window_handle,
                "selected_row_runtime_id_hash": proof.selected_row_runtime_id_hash,
                "header_digest": proof.header_digest,
                "group_marker_count": proof.group_marker_count,
            }
        return self._stable_target(binding=binding, window=window, proof=proof)

    def _observe(self, command: WorkerCommand) -> WorkerResult:
        binding = self._bindings[command.binding_id or ""]
        def read_bubbles(window, _conversation, _proof):
            with self._stage(command, "bubbles"):
                return self._accessibility.list_bubbles(
                    window, self._selectors.selector("bubbles")
                )

        window, conversation, _proof, bubbles = self._resolve(
            command, binding, current_reader=read_bubbles
        )
        # The durable, conversation-scoped cursor is assigned by the main process.
        rows = [item.model_dump(mode="json") for item in bubbles]
        complete = all(item.direction.value != "unknown" for item in bubbles)
        return self._result(command, WorkerStatus.OK if complete else WorkerStatus.FAILED_SAFE, None if complete else "direction_unknown", evidence={"target": conversation.model_dump(mode="json"), "bubbles": rows, "complete": complete, "gap": not complete})

    def _prepare(self, command: WorkerCommand) -> WorkerResult:
        if command.operation_id is None or command.text is None or command.segment_ref is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "prepare_fields_missing")
        binding = self._bindings[command.binding_id or ""]

        def read_bubbles(window, _conversation, _proof):
            with self._stage(command, "bubbles"):
                return self._accessibility.list_bubbles(
                    window, self._selectors.selector("bubbles")
                )

        window, conversation, proof, before = self._resolve(
            command, binding, current_reader=read_bubbles
        )
        expected_target = self._stable_target(
            binding=binding, window=window, proof=proof
        )
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=expected_target,
            )
        if self._accessibility.read_composer(window, self._selectors.selector("composer")):
            return self._result(command, WorkerStatus.FAILED_SAFE, "composer_not_empty")
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=expected_target,
            )
        text_hash = __import__("hashlib").sha256(command.text.encode()).hexdigest()
        portable = PreparedVerificationEvidence(
            owner_binding_id=binding.binding_id,
            target_identity=PreparedTargetIdentity.model_validate(expected_target),
            before_bubbles=tuple(
                PreparedBubbleAnchor(
                    direction=item.direction.value,
                    message_key=item.message_key,
                    conversation_internal_id=item.conversation_internal_id,
                    text_hash=item.text_hash,
                )
                for item in before
            ),
            text_hash=text_hash,
            segment_ref=command.segment_ref,
        )
        evidence = {
            "owner_binding_id": binding.binding_id,
            "target_identity": portable.target_identity.model_dump(mode="json"),
            "conversation": conversation.model_dump(mode="json"),
            "before_bubbles": [item.model_dump(mode="json") for item in before],
            "text_hash": text_hash,
            "composer_text": command.text,
            "segment_ref": command.segment_ref,
            "prepared_evidence": portable.model_dump(mode="json"),
        }
        if (
            command.deadline is not None
            and (command.deadline - datetime.now(UTC)).total_seconds()
            <= self._prepare_write_reserve_seconds
        ):
            # Selection, identity proof and bubble reads are read-only.  Do not
            # cross the composer-write boundary unless enough of the parent
            # watchdog remains for write, post-write identity proof and exact
            # readback.  This keeps a slow QQ/UIA snapshot fail-safe instead of
            # turning a preventable pre-write delay into an uncertain draft.
            return self._result(
                command,
                WorkerStatus.FAILED_SAFE,
                "prepare_write_budget_exhausted",
                evidence={"composer_written": False},
            )
        # Establish cleanup ownership before crossing the composer-write
        # boundary. Any partial write or post-write identity failure remains
        # reserved to this exact operation until a proven ABORT succeeds.
        self._prepared[command.operation_id] = evidence
        self._reservation = command.operation_id
        self._reservation_binding_id = command.binding_id
        self._trusted_operation_lease = (
            command.operation_id,
            binding.binding_id,
            command.binding_revision,
            command.conversation_revision,
        )
        self._ensure_command_live(command)
        self._accessibility.write_composer(window, command.text, self._selectors.selector("composer"))
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=expected_target,
            )
        self._ensure_command_live(command)
        actual = self._accessibility.read_composer(window, self._selectors.selector("composer"))
        if actual != command.text:
            return self._result(
                command,
                WorkerStatus.FAILED_SAFE,
                "composer_readback_mismatch",
                evidence={"cleanup_required": True},
            )
        return self._result(
            command,
            WorkerStatus.OK,
            evidence={"prepared_evidence": portable.model_dump(mode="json")},
        )

    def _commit(self, command: WorkerCommand) -> WorkerResult:
        evidence = self._prepared.get(command.operation_id) if command.operation_id else None
        if evidence is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "not_prepared")
        if evidence.get("owner_binding_id") != command.binding_id:
            return self._result(command, WorkerStatus.FAILED_SAFE, "operation_binding_mismatch")
        if command.operation_id in self._committed:
            return self._result(command, WorkerStatus.UNCERTAIN, "commit_already_attempted")
        binding = self._bindings[command.binding_id or ""]
        window, conversation, proof, _ = self._resolve(command, binding)
        current = self._stable_target(binding=binding, window=window, proof=proof)
        if current != evidence["target_identity"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "target_drift")
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=evidence["target_identity"],
            )
        if self._accessibility.read_composer(window, self._selectors.selector("composer")) != evidence["composer_text"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "composer_drift")
        current_bubbles = [item.model_dump(mode="json") for item in self._accessibility.list_bubbles(window, self._selectors.selector("bubbles"))]
        if _semantic_bubbles(current_bubbles) != _semantic_bubbles(evidence["before_bubbles"]):
            return self._result(command, WorkerStatus.FAILED_SAFE, "stale_context")
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=evidence["target_identity"],
            )
        self._ensure_command_live(command)
        self._committed.add(command.operation_id)
        self._accessibility.invoke_send(window, self._selectors.selector("send"))
        return self._result(
            command,
            WorkerStatus.OK,
            evidence={"prepared_evidence": evidence["prepared_evidence"]},
        )

    def _verify(self, command: WorkerCommand) -> WorkerResult:
        evidence = self._prepared.get(command.operation_id) if command.operation_id else None
        if evidence is None and command.prepared_evidence is not None:
            evidence = command.prepared_evidence.model_dump(mode="json")
        if evidence is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "not_prepared")
        if evidence.get("owner_binding_id") != command.binding_id:
            return self._result(command, WorkerStatus.FAILED_SAFE, "operation_binding_mismatch")
        binding = self._bindings[command.binding_id or ""]

        def read_bubbles(window, _conversation, _proof):
            with self._stage(command, "bubbles"):
                return self._accessibility.list_bubbles(
                    window, self._selectors.selector("bubbles")
                )

        window, conversation, proof, after = self._resolve(
            command, binding, current_reader=read_bubbles
        )
        current_target = self._stable_target(
            binding=binding,
            window=window,
            proof=proof,
        )
        if current_target != evidence["target_identity"]:
            return self._result(command, WorkerStatus.UNCERTAIN, "target_drift")
        before = evidence["before_bubbles"]  # type: ignore[index]
        text_hash = str(evidence["text_hash"])
        try:
            new_rows = _new_suffix(before, [item.model_dump(mode="json") for item in after])
        except SnapshotAlignmentError as exc:
            self._reservation = None
            self._reservation_binding_id = None
            self._trusted_operation_lease = None
            return self._result(command, WorkerStatus.UNCERTAIN, str(exc))
        verified = [item for item in after[-len(new_rows):] if item.direction.value == "outbound" and item.text_hash == text_hash] if new_rows else []
        if len(verified) != 1:
            self._reservation = None
            self._reservation_binding_id = None
            self._trusted_operation_lease = None
            return self._result(command, WorkerStatus.UNCERTAIN, "outbound_receipt_not_unique", evidence={"conversation": conversation.model_dump(mode="json"), "after_bubbles": [item.model_dump(mode="json") for item in after]})
        self._reservation = None
        self._reservation_binding_id = None
        self._trusted_operation_lease = None
        return self._result(command, WorkerStatus.OK, evidence={"conversation": conversation.model_dump(mode="json"), "receipt": verified[0].model_dump(mode="json"), "operation_id": str(command.operation_id)})

    def _abort(self, command: WorkerCommand) -> WorkerResult:
        if command.operation_id is None or command.operation_id != self._reservation:
            return self._result(command, WorkerStatus.FAILED_SAFE, "abort_not_owner")
        if command.operation_id in self._committed:
            return self._result(command, WorkerStatus.UNCERTAIN, "committed_cannot_abort")
        evidence = self._prepared.get(command.operation_id)
        if evidence is None or command.binding_id not in self._bindings:
            return self._result(command, WorkerStatus.FAILED_SAFE, "abort_evidence_missing")
        if evidence.get("owner_binding_id") != command.binding_id:
            return self._result(command, WorkerStatus.FAILED_SAFE, "operation_binding_mismatch")
        binding = self._bindings[command.binding_id]
        window, conversation, proof, _ = self._resolve(command, binding)
        if self._stable_target(binding=binding, window=window, proof=proof) != evidence["target_identity"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        current = self._accessibility.read_composer(window, self._selectors.selector("composer"))
        if current not in {"", evidence["composer_text"]}:
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=evidence["target_identity"],
            )
        self._ensure_command_live(command)
        if current:
            clear = getattr(self._accessibility, "clear_composer", None)
            if callable(clear):
                clear(window, str(evidence["composer_text"]), self._selectors.selector("composer"))
            else:
                self._accessibility.write_composer(window, "", self._selectors.selector("composer"))
        if self._accessibility.read_composer(window, self._selectors.selector("composer")) != "":
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        if isinstance(proof, QQSessionObservedDirectIdentity):
            self._revalidate_expected_target(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                expected=evidence["target_identity"],
            )
        self._prepared.pop(command.operation_id, None)
        self._reservation = None
        self._reservation_binding_id = None
        self._trusted_operation_lease = None
        return self._result(command, WorkerStatus.OK)

    def _consume_selection_handoff(
        self, command: WorkerCommand, binding: QQIdentityBinding,
    ) -> bool:
        handoff = command.selection_handoff
        if not isinstance(handoff, SelectionHandoff):
            return False
        try:
            authenticated = verify_selection_handoff_auth(
                handoff, self._selection_handoff_signing_key
            )
        except (TypeError, ValueError):
            return False
        expected_text_sha256 = (
            hashlib.sha256(command.text.encode("utf-8")).hexdigest()
            if command.kind is WorkerKind.PREPARE and command.text is not None
            else None
        )
        expected_segment_ref = (
            command.segment_ref if command.kind is WorkerKind.PREPARE else None
        )
        expected_prepared_evidence_sha256 = (
            hashlib.sha256(
                json.dumps(
                    command.prepared_evidence.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            if command.kind is WorkerKind.VERIFY
            and command.prepared_evidence is not None
            else None
        )
        source_valid = (
            handoff.source == "selection_refresh"
            and handoff.source_kind in {WorkerKind.OBSERVE, WorkerKind.PREPARE}
            and handoff.target_kind is handoff.source_kind
            and command.kind is handoff.target_kind
        ) or (
            handoff.source == "commit_success"
            and handoff.source_kind is WorkerKind.COMMIT
            and handoff.target_kind is WorkerKind.VERIFY
            and command.kind is WorkerKind.VERIFY
            and command.operation_id is not None
        )
        exact_target = (
            authenticated
            and handoff.binding_id == binding.binding_id == command.binding_id
            and handoff.binding_revision == command.binding_revision
            and handoff.conversation_revision == command.conversation_revision
            and handoff.operation_id == command.operation_id
            and handoff.successor_request_id == command.request_id
            and handoff.predecessor_worker_epoch != UUID(int=0)
            and handoff.successor_worker_epoch == self._epoch
            and handoff.target_runtime_id_digest
            == runtime_id_digest(binding.platform_conversation_id)
            and handoff.successor_deadline == command.deadline
            and handoff.text_sha256 == expected_text_sha256
            and handoff.segment_ref == expected_segment_ref
            and handoff.prepared_evidence_sha256
            == expected_prepared_evidence_sha256
            and handoff.expires_at > datetime.now(UTC)
        )
        if (
            not source_valid
            or not exact_target
            or handoff.handoff_id in self._consumed_selection_handoffs
        ):
            return False
        # Spend the authority before reading the live header.  A drift or read
        # failure can never make the same handoff usable on a later request.
        self._consumed_selection_handoffs.add(handoff.handoff_id)
        return True

    def _attest_exact_selected_row(
        self,
        *,
        command: WorkerCommand,
        window: QQWindow,
        conversation: QQConversation,
    ) -> SelectionVisualAttestation:
        self._ensure_command_live(command)
        profile = self._selection_visual_profile
        certify = getattr(
            self._accessibility, "certify_conversation_selected_visual", None
        )
        if profile is None or not callable(certify):
            raise RuntimeError("selection visual profile is unavailable")
        if (
            profile.client_version != self._selectors.client_version
            or profile.selector_pack_version
            != self._selectors.fixture_suite_version
            or profile.environment_fingerprint
            != self._selectors.environment_fingerprint
        ):
            raise RuntimeError("selection visual profile scope drift")
        try:
            raw = certify(
                window,
                conversation,
                self._selectors.selector("conversation_item"),
                profile,
                deadline=command.deadline,
            )
            attestation = (
                raw
                if isinstance(raw, SelectionVisualAttestation)
                else SelectionVisualAttestation.model_validate(raw)
            )
        except Exception as exc:
            if str(exc) == "deadline_expired":
                raise
            raise RuntimeError("selection visual attestation failed") from exc
        if (
            attestation.profile_id != profile.profile_id
            or attestation.client_version != self._selectors.client_version
            or attestation.selector_pack_version
            != self._selectors.fixture_suite_version
            or attestation.environment_fingerprint
            != self._selectors.environment_fingerprint
            or attestation.process_id != window.process_id
            or attestation.window_handle != window.window_handle
            or attestation.target_runtime_id_digest
            != runtime_id_digest(conversation.internal_id)
        ):
            raise RuntimeError("selection visual attestation drift")
        self._ensure_command_live(command)
        return attestation

    def _certify_visual_header_current(
        self,
        *,
        command: WorkerCommand,
        binding: QQIdentityBinding,
        window: QQWindow,
        conversation: QQConversation,
        read_phase: Callable,
        current_reader: Callable[
            [QQWindow, QQConversation, CurrentDirectIdentity | None], object
        ]
        | None,
    ) -> tuple[CurrentDirectIdentity, object | None]:
        try_already_current = getattr(
            self._identity_certifier, "try_certify_already_current", None
        )
        if not callable(try_already_current):
            raise RuntimeError("current identity certifier unavailable")
        before = self._attest_exact_selected_row(
            command=command, window=window, conversation=conversation
        )
        with read_phase(window):
            proof = try_already_current(window, conversation)
            if proof is None:
                raise RuntimeError("selection visual header is unproven")
            if proof.participant_signature != binding.participant_signature:
                raise RuntimeError("profile identity mismatch")
            current = (
                current_reader(window, conversation, proof)
                if current_reader is not None
                else None
            )
        after = self._attest_exact_selected_row(
            command=command, window=window, conversation=conversation
        )
        if (
            before.profile_id != after.profile_id
            or before.process_id != after.process_id
            or before.window_handle != after.window_handle
            or before.target_runtime_id_digest
            != after.target_runtime_id_digest
            or before.row_rect != after.row_rect
        ):
            raise RuntimeError("selection visual attestation drift")
        self._ensure_command_live(command)
        return proof, current

    def _revalidate_expected_target(
        self,
        *,
        command: WorkerCommand,
        binding: QQIdentityBinding,
        window: QQWindow,
        conversation: QQConversation,
        expected: dict[str, object],
    ) -> CurrentDirectIdentity:
        read_phase = getattr(self._accessibility, "read_phase", None)
        if not callable(read_phase):
            raise RuntimeError("selection visual profile is unavailable")
        proof, _current = self._certify_visual_header_current(
            command=command,
            binding=binding,
            window=window,
            conversation=conversation,
            read_phase=read_phase,
            current_reader=None,
        )
        if self._stable_target(binding=binding, window=window, proof=proof) != expected:
            raise RuntimeError("binding proof drift after selection")
        return proof

    def _resolve(self, command: WorkerCommand, binding: QQIdentityBinding,
                 current_reader: Callable[[QQWindow, QQConversation,
                                           CurrentDirectIdentity | None], object]
                 | None = None):
        with self._stage(command, "target_window"):
            window = self._target_window()
        with self._stage(command, "guest_foreground"):
            ensure_foreground = getattr(
                self._accessibility, "ensure_guest_foreground", None
            )
            if callable(ensure_foreground):
                self._ensure_command_live(command)
                ensure_foreground(window)
                self._ensure_command_live(command)

        read_phase = getattr(self._accessibility, "read_phase", None)
        if callable(read_phase):
            with self._stage(command, "discovery_phase"), read_phase(window):
                conversation = self._locate_conversation(window, binding)
            if command.selection_handoff is not None:
                shortcut_authorized = self._consume_selection_handoff(
                    command, binding
                )
                if not shortcut_authorized:
                    raise RuntimeError("selection handoff is invalid")
            else:
                shortcut_authorized = self._operation_lease_matches(
                    command, binding
                )
            if shortcut_authorized:
                with self._stage(command, "preselection_verification_phase"):
                    proof, current = self._certify_visual_header_current(
                        command=command,
                        binding=binding,
                        window=window,
                        conversation=conversation,
                        read_phase=read_phase,
                        current_reader=current_reader,
                    )
                return window, conversation, proof, current
            if (
                command.kind
                in {WorkerKind.COMMIT, WorkerKind.VERIFY, WorkerKind.ABORT}
                and self._reservation is not None
                and binding.authorization_scope
                == "all_direct_including_temporary"
            ):
                raise RuntimeError("operation identity lease is invalid")
            # UI mutations happen only after the discovery snapshot is closed.
            # QQ's Chromium provider may not commit Invoke while the enumerated
            # tree is still retained by the same read phase.
            self._ensure_command_live(command)
            with self._stage(command, "select"):
                selection_changed = self._select_conversation(
                    command, window, binding, conversation
                )
            if selection_changed is True:
                # Invoking a Chromium-backed QQ row can leave this OS process
                # with stale provider state.  End the command before opening a
                # verification snapshot or touching current content/composer;
                # the process supervisor must retry in a fresh worker process.
                raise self._selection_process_refresh_required()
            with self._stage(command, "verification_phase"):
                proof, current = self._confirm_after_selection(
                    command=command, binding=binding, window=window,
                    conversation=conversation, read_phase=read_phase,
                    current_reader=current_reader,
                )
            return window, conversation, proof, current

        if binding.authorization_scope == "all_direct_including_temporary":
            raise RuntimeError("selection visual profile is unavailable")
        with self._stage(command, "discovery_phase"):
            conversation = self._locate_conversation(window, binding)
        self._ensure_command_live(command)
        with self._stage(command, "select"):
            selection_changed = self._select_conversation(
                command, window, binding, conversation
            )
        if selection_changed is True:
            raise self._selection_process_refresh_required()
        with self._stage(command, "verification_phase"):
            confirm = getattr(
                self._accessibility, "confirm_conversation_selected", None
            )
            if callable(confirm):
                confirm(
                    window, conversation,
                    self._selectors.selector("conversation_item"),
                )
            proof = self._confirm_identity(binding, window, conversation)
            current = (
                current_reader(window, conversation, proof)
                if current_reader is not None else None
            )
        return window, conversation, proof, current

    def _locate_conversation(
        self, window: QQWindow, binding: QQIdentityBinding
    ) -> QQConversation:
        conversations = self._accessibility.list_conversations(
            window, self._selectors.selector("conversations")
        )
        if self._identity_certifier is not None:
            if self._candidate_locator is None:
                raise RuntimeError("candidate locator unavailable")
            matches = self._candidate_locator.locate_candidates(binding, conversations)
        else:
            matches = [item for item in conversations if binding.matches(item)]
        if len(matches) != 1:
            raise RuntimeError("binding target absent or ambiguous")
        return matches[0]

    def _select_conversation(
        self,
        command: WorkerCommand,
        window: QQWindow,
        binding: QQIdentityBinding,
        conversation: QQConversation,
    ) -> bool:
        self._last_visual_selection_evidence = {}
        selector = self._selectors.selector("conversation_item")
        if self._selection_actuator is None:
            return self._accessibility.select_conversation(
                window, conversation, selector
            )
        outcome = self._selection_actuator.select(
            window=window,
            binding_id=binding.binding_id,
            conversation=conversation,
            selector=selector,
            deadline=command.deadline,
        )
        self._last_visual_selection_evidence = _safe_visual_selection_evidence(
            outcome.model_dump(mode="python")
        )
        if outcome.status is ConversationSelectionStatus.REJECTED:
            raise _with_visual_selection_evidence(
                UIAUnavailable(
                    outcome.error_code or "visual conversation selection rejected"
                ),
                self._last_visual_selection_evidence,
            )
        return outcome.status is ConversationSelectionStatus.ACTION_ATTEMPTED

    def _selection_process_refresh_required(self) -> BaseException:
        return _with_visual_selection_evidence(
            RuntimeError("selection_process_refresh_required"),
            self._last_visual_selection_evidence,
        )

    # A QQ UIA tree rebuild currently takes about 1.3-1.7 seconds in the guest.
    # Keep enough budget for more than one independent verification snapshot,
    # while remaining well below the 15-second worker request timeout.
    _SELECTION_SETTLE_SECONDS = 5.0
    _SELECTION_SETTLE_RETRY_SECONDS = 0.1
    _SELECTION_CONFIRMATION_UNAVAILABLE = "conversation selection could not be independently confirmed"

    def _confirm_after_selection(
        self, *, command: WorkerCommand, binding: QQIdentityBinding,
        window: QQWindow, conversation: QQConversation, read_phase,
        current_reader: Callable[[QQWindow, QQConversation, CurrentDirectIdentity | None], object] | None,
    ) -> tuple[CurrentDirectIdentity | None, object | None]:
        """Certify only a settled selection, using a fresh UIA phase per retry."""
        if binding.authorization_scope == "all_direct_including_temporary":
            proof, current = self._certify_visual_header_current(
                command=command,
                binding=binding,
                window=window,
                conversation=conversation,
                read_phase=read_phase,
                current_reader=current_reader,
            )
            return proof, current
        now = datetime.now(UTC)
        limit = time.monotonic() + self._SELECTION_SETTLE_SECONDS
        if command.deadline is not None:
            remaining = max(0.0, (command.deadline - now).total_seconds())
            limit = min(limit, time.monotonic() + remaining)
        last_selection_unavailable: UIAUnavailable | None = None
        while True:
            if time.monotonic() >= limit:
                if last_selection_unavailable is not None:
                    raise last_selection_unavailable
                raise RuntimeError("deadline_expired")
            selection_unavailable: UIAUnavailable | None = None
            # The phase was invalidated by Select/Invoke. A retry must build a
            # new snapshot, never reuse UIA controls from it.
            with read_phase(window):
                confirm = getattr(self._accessibility, "confirm_conversation_selected", None)
                if not callable(confirm):
                    raise RuntimeError("phase selection confirmation unavailable")
                try:
                    confirm(window, conversation, self._selectors.selector("conversation_item"))
                except UIAUnavailable as exc:
                    if str(exc) != self._SELECTION_CONFIRMATION_UNAVAILABLE:
                        raise
                    selection_unavailable = exc
                else:
                    # Identity/header/group checks and current reads remain
                    # fail-closed; they are never retryable selection failures.
                    proof = self._confirm_identity(binding, window, conversation)
                    current = (current_reader(window, conversation, proof)
                               if current_reader is not None else None)
                    if (command.deadline is not None
                            and datetime.now(UTC) >= command.deadline):
                        raise RuntimeError("deadline_expired")
                    return proof, current
            assert selection_unavailable is not None
            last_selection_unavailable = selection_unavailable
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise selection_unavailable
            time.sleep(min(self._SELECTION_SETTLE_RETRY_SECONDS, remaining))

    def _confirm_identity(self, binding: QQIdentityBinding, window: QQWindow,
                          conversation: QQConversation) -> CurrentDirectIdentity | None:
        # Re-query after select: a title/name alone is never treated as identity.
        if self._identity_certifier is not None:
            return self._certify(binding, window, conversation)
        confirmed = [
            item for item in self._accessibility.list_conversations(
                window, self._selectors.selector("conversations")
            ) if binding.matches(item)
        ]
        if len(confirmed) != 1:
            raise RuntimeError("binding proof drift after selection")
        return None

    @contextmanager
    def _stage(self, command: WorkerCommand, stage: str):
        if self._run_id is None:
            yield
            return
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        base = {
            "schema": "pmai-qq-worker-stage-event-v1",
            "run_id": self._run_id,
            "worker_epoch": str(self._epoch),
            "request_id": str(command.request_id),
            "kind": command.kind.value,
            "binding_id": command.binding_id,
            "stage": stage,
        }
        print(json.dumps({
            **base, "event": "start", "recorded_at": started_at.isoformat(),
        }, ensure_ascii=False), flush=True)
        try:
            yield
        except Exception as exc:
            print(json.dumps({
                **base,
                "event": "error",
                "recorded_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": max(0, int(
                    (time.perf_counter() - started_clock) * 1000
                )),
                "exception_type": type(exc).__name__,
            }, ensure_ascii=False), flush=True)
            raise
        else:
            print(json.dumps({
                **base,
                "event": "end",
                "recorded_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": max(0, int(
                    (time.perf_counter() - started_clock) * 1000
                )),
            }, ensure_ascii=False), flush=True)

    def _target_window(self) -> QQWindow:
        windows = self._accessibility.find_main_windows(self._selectors.selector("main_window"))
        if self._expected_window is None:
            matches = windows
        else:
            process_id, window_handle = self._expected_window
            matches = [
                item for item in windows
                if item.process_id == process_id and item.window_handle == window_handle
            ]
        if len(matches) != 1:
            if self._expected_window is not None:
                raise RuntimeError("qq_session_window_unavailable")
            raise RuntimeError("QQ window absent or ambiguous")
        window = matches[0]
        if self._window_validator is not None:
            self._window_validator(window)
        return window

    def _certify(self, binding: QQIdentityBinding, window, conversation) -> CurrentDirectIdentity:
        if self._identity_certifier is None:
            raise RuntimeError("current identity certifier unavailable")
        proof = self._identity_certifier.certify_current(window, conversation)
        if proof.process_id != window.process_id or proof.window_handle != window.window_handle:
            raise RuntimeError("identity scope drift")
        if (proof.client_version != self._selectors.client_version or
                proof.selector_pack_version != self._selectors.fixture_suite_version):
            raise RuntimeError("conversation type evidence version drift")
        if proof.participant_signature != binding.participant_signature:
            raise RuntimeError("profile identity mismatch")
        return proof

    def _stable_target(self, *, binding: QQIdentityBinding, window,
                       proof: CurrentDirectIdentity | None) -> dict[str, object]:
        if proof is not None:
            return {"binding_id": binding.binding_id, "participant_signature": proof.participant_signature,
                    "conversation_type": proof.conversation_type, "process_id": proof.process_id,
                    "window_handle": proof.window_handle}
        return {"binding_id": binding.binding_id, "participant_signature": binding.participant_signature,
                "conversation_type": binding.conversation_type, "process_id": window.process_id,
                "window_handle": window.window_handle}

    def _result(self, command: WorkerCommand, status: WorkerStatus, error_code: str | None = None, evidence: dict[str, object] | None = None) -> WorkerResult:
        return WorkerResult(request_id=command.request_id, kind=command.kind, status=status, worker_epoch=self._epoch, operation_id=command.operation_id, binding_id=command.binding_id, binding_revision=command.binding_revision, conversation_revision=command.conversation_revision, error_code=error_code, evidence=evidence or {})


def _semantic_bubbles(rows: object) -> list[tuple[object, ...]]:
    """Compare message identity/content while ignoring volatile capture metadata."""
    if not isinstance(rows, list):
        return []
    return [_bubble_anchor(row) for row in rows if isinstance(row, dict)]


def _bubble_anchor(row: dict[str, object]) -> tuple[object, ...]:
    text_hash = row.get("text_hash")
    if not isinstance(text_hash, str):
        text = row.get("text")
        text_hash = __import__("hashlib").sha256(
            (text if isinstance(text, str) else "").encode()
        ).hexdigest()
    return (
        row.get("direction"),
        text_hash,
        row.get("message_key"),
        row.get("conversation_internal_id"),
    )


class QQVMWorkerProcess:
    """Spawn-only guest process façade; host callers never touch QQ UIA directly."""

    fresh_verify_capable = True

    def __init__(self, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...],
                 session_evidence: tuple[QQSessionObservedDirectIdentity, ...] = (),
                 run_id: str | None = None,
                 visual_selection: VisualSelectionConfig | None = None,
                 visual_api_key: str | None = None,
                 prepare_write_reserve_seconds: float = 5.0) -> None:
        self._selector_pack, self._bindings = selector_pack, bindings
        self._session_evidence = session_evidence
        self._visual_selection = visual_selection
        self._visual_api_key = visual_api_key
        self._prepare_write_reserve_seconds = _validated_prepare_write_reserve(
            prepare_write_reserve_seconds
        )
        self._selection_handoff_signing_key = secrets.token_bytes(32)
        self._parent, child = mp.get_context("spawn").Pipe()
        self._process = mp.get_context("spawn").Process(
            target=_serve,
            args=(
                child,
                selector_pack,
                bindings,
                session_evidence,
                run_id,
                visual_selection,
                visual_api_key,
                self._selection_handoff_signing_key,
                self._prepare_write_reserve_seconds,
            ),
            daemon=True,
        )
        self._request_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._run_id = run_id
        self._started = False
        self._stopping = False
        self._status: dict[str, object] = {
            "run_id": run_id,
            "worker_process_id": None,
            "worker_alive": False,
            "worker_exit_code": None,
            "parent_terminate_reason": None,
            "startup_health": None,
            "last_request": None,
            "last_successful_observe": None,
            "first_terminal_failure": None,
        }

    def start(self) -> None:
        self._process.start()
        self._started = True
        with self._status_lock:
            self._refresh_process_locked()

    def spawn_successor(self) -> QQVMWorkerProcess:
        """Construct an unstarted process with the same certified UI scope."""

        return type(self)(
            self._selector_pack,
            self._bindings,
            session_evidence=self._session_evidence,
            run_id=self._run_id,
            visual_selection=self._visual_selection,
            visual_api_key=self._visual_api_key,
            prepare_write_reserve_seconds=self._prepare_write_reserve_seconds,
        )

    def mint_selection_handoff(self, **kwargs) -> SelectionHandoff:
        """Issue a capability authenticated for this exact child process."""

        return mint_selection_handoff(
            **kwargs,
            signing_key=self._selection_handoff_signing_key,
        )

    def request(self, command: WorkerCommand, timeout_seconds: float) -> WorkerResult:
        # multiprocessing.Connection has no request/response correlation of its
        # own.  Keep each send/poll/recv exchange atomic across the runtime loop,
        # WebUI readers and shutdown.
        with self._request_lock:
            self._ensure_status_state()
            started_at = datetime.now(UTC)
            started_clock = time.perf_counter()
            self._record_request_started(command, started_at)
            try:
                result = self._request_locked(command, timeout_seconds)
            except (BrokenPipeError, EOFError, OSError) as exc:
                elapsed_ms = max(0, int((time.perf_counter() - started_clock) * 1000))
                self._record_request_exception(
                    command, started_at, elapsed_ms, "worker_pipe_failed", exc
                )
                raise
            elapsed_ms = max(0, int((time.perf_counter() - started_clock) * 1000))
            self._record_request_result(command, result, started_at, elapsed_ms)
            return result

    def _request_locked(self, command: WorkerCommand, timeout_seconds: float) -> WorkerResult:
        if not self._process.is_alive():
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNAVAILABLE,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code="worker_not_alive",
            )
        self._parent.send(command.model_dump(mode="json"))
        if not self._parent.poll(timeout_seconds):
            self._process.terminate()
            self._process.join(5)
            with self._status_lock:
                self._status["parent_terminate_reason"] = "request_timeout"
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNCERTAIN,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code="worker_timeout_isolated",
            )
        try:
            result = WorkerResult.model_validate(self._parent.recv())
        except ValidationError as exc:
            with self._status_lock:
                self._status["parent_terminate_reason"] = "response_invalid"
            self._process.terminate()
            self._process.join(5)
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNCERTAIN,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code="worker_response_invalid",
                evidence={"exception_types": [type(exc).__name__]},
            )
        if (
            result.request_id != command.request_id
            or result.kind is not command.kind
            or result.binding_id != command.binding_id
            or result.binding_revision != command.binding_revision
            or result.conversation_revision != command.conversation_revision
            or result.operation_id != command.operation_id
            or (
                result.status is WorkerStatus.OK
                and result.worker_epoch == UUID(int=0)
            )
        ):
            self._process.terminate()
            self._process.join(5)
            with self._status_lock:
                self._status["parent_terminate_reason"] = "response_mismatch"
            return WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNCERTAIN,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code="worker_response_mismatch",
            )
        return result

    def status_snapshot(self) -> dict[str, object]:
        """Return current parent-owned lifecycle evidence without worker IPC."""
        self._ensure_status_state()
        with self._status_lock:
            self._refresh_process_locked()
            if (self._started and not self._stopping
                    and not bool(self._status["worker_alive"])
                    and not (
                        isinstance(self._status["last_request"], dict)
                        and self._status["last_request"].get("status") == "in_progress"
                    )
                    and self._status["first_terminal_failure"] is None):
                self._freeze_terminal_locked(
                    command=None,
                    error_code="worker_not_alive",
                    completed_at=datetime.now(UTC),
                    elapsed_ms=None,
                    exception_types=(),
                )
            return copy.deepcopy(self._status)

    def _ensure_status_state(self) -> None:
        """Support narrow tests that construct this façade with ``__new__``."""
        if hasattr(self, "_status_lock"):
            return
        self._status_lock = threading.Lock()
        self._run_id = None
        self._started = True
        self._stopping = False
        self._status = {
            "run_id": None,
            "worker_process_id": getattr(self._process, "pid", None),
            "worker_alive": bool(self._process.is_alive()),
            "worker_exit_code": getattr(self._process, "exitcode", None),
            "parent_terminate_reason": None,
            "startup_health": None,
            "last_request": None,
            "last_successful_observe": None,
            "first_terminal_failure": None,
        }

    def _refresh_process_locked(self) -> None:
        self._status["worker_process_id"] = getattr(self._process, "pid", None)
        self._status["worker_alive"] = bool(self._process.is_alive())
        self._status["worker_exit_code"] = getattr(self._process, "exitcode", None)

    @staticmethod
    def _request_metadata(command: WorkerCommand) -> dict[str, object]:
        return {
            "request_id": str(command.request_id),
            "kind": command.kind.value,
            "binding_id": command.binding_id,
        }

    @staticmethod
    def _safe_result_diagnostics(evidence: object) -> dict[str, object]:
        """Copy only bounded structural diagnostics from the child response."""
        if not isinstance(evidence, dict):
            return {}
        diagnostics: dict[str, object] = {}
        diagnostics.update(_safe_visual_selection_evidence(evidence))
        hresult = evidence.get("com_hresult")
        if isinstance(hresult, int) and not isinstance(hresult, bool):
            diagnostics["com_hresult"] = hresult
        raw_frames = evidence.get("project_frames")
        frames: list[dict[str, object]] = []
        if isinstance(raw_frames, list):
            for item in raw_frames[-8:]:
                if not isinstance(item, dict):
                    continue
                function = item.get("function")
                filename = item.get("file")
                line = item.get("line")
                if not (
                    isinstance(function, str) and len(function) <= 160
                    and isinstance(filename, str) and len(filename) <= 320
                    and filename.startswith("messenger_ai/") and ".." not in filename
                    and isinstance(line, int) and not isinstance(line, bool)
                    and 1 <= line <= 10_000_000
                ):
                    continue
                frames.append({"function": function, "file": filename, "line": line})
        if frames:
            diagnostics["project_frames"] = frames
        return diagnostics

    def _record_request_started(self, command: WorkerCommand, started_at: datetime) -> None:
        with self._status_lock:
            self._refresh_process_locked()
            self._status["last_request"] = {
                **self._request_metadata(command),
                "started_at": started_at.isoformat(),
                "completed_at": None,
                "elapsed_ms": None,
                "status": "in_progress",
                "error_code": None,
            }

    def _record_request_result(self, command: WorkerCommand, result: WorkerResult,
                               started_at: datetime, elapsed_ms: int) -> None:
        completed_at = datetime.now(UTC)
        with self._status_lock:
            self._refresh_process_locked()
            record = {
                **self._request_metadata(command),
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_ms": elapsed_ms,
                "status": result.status.value,
                "error_code": result.error_code,
                **self._safe_result_diagnostics(result.evidence),
            }
            self._status["last_request"] = record
            if command.kind is WorkerKind.HEALTH:
                self._status["startup_health"] = copy.deepcopy(record)
            if command.kind is WorkerKind.OBSERVE and result.status is WorkerStatus.OK:
                self._status["last_successful_observe"] = copy.deepcopy(record)
            terminal = result.error_code in {
                "worker_not_alive", "worker_timeout_isolated",
                "worker_response_invalid", "worker_response_mismatch"
            }
            if terminal or (not self._stopping and not bool(self._status["worker_alive"])):
                self._freeze_terminal_locked(
                    command=command,
                    error_code=result.error_code or "worker_exited",
                    completed_at=completed_at,
                    elapsed_ms=elapsed_ms,
                    exception_types=(),
                )

    def _record_request_exception(self, command: WorkerCommand, started_at: datetime,
                                  elapsed_ms: int, error_code: str,
                                  exc: BaseException) -> None:
        completed_at = datetime.now(UTC)
        with self._status_lock:
            self._refresh_process_locked()
            self._status["last_request"] = {
                **self._request_metadata(command),
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_ms": elapsed_ms,
                "status": "exception",
                "error_code": error_code,
            }
            self._freeze_terminal_locked(
                command=command,
                error_code=error_code,
                completed_at=completed_at,
                elapsed_ms=elapsed_ms,
                exception_types=(type(exc).__name__,),
            )

    def _freeze_terminal_locked(self, *, command: WorkerCommand | None,
                                error_code: str, completed_at: datetime,
                                elapsed_ms: int | None,
                                exception_types: tuple[str, ...]) -> None:
        if self._status["first_terminal_failure"] is not None:
            return
        record: dict[str, object] = {
            "request_id": str(command.request_id) if command is not None else None,
            "kind": command.kind.value if command is not None else None,
            "binding_id": command.binding_id if command is not None else None,
            "completed_at": completed_at.isoformat(),
            "elapsed_ms": elapsed_ms,
            "error_code": error_code,
            "worker_exit_code": self._status["worker_exit_code"],
            "parent_terminate_reason": self._status["parent_terminate_reason"],
            "exception_types": list(exception_types),
        }
        self._status["first_terminal_failure"] = record
        print(json.dumps({
            "schema": "pmai-qq-worker-terminal-event-v1",
            "run_id": self._run_id,
            "recorded_at": completed_at.isoformat(),
            **record,
        }, ensure_ascii=False), flush=True)

    def stop(self, timeout_seconds: float = 5) -> None:
        with self._request_lock:
            self._ensure_status_state()
            self._stopping = True
            if self._process.is_alive():
                try:
                    self._parent.send(WorkerCommand(kind=WorkerKind.STOP).model_dump(mode="json"))
                except (BrokenPipeError, EOFError, OSError):
                    pass
                self._process.join(timeout_seconds)
                if self._process.is_alive():
                    with self._status_lock:
                        self._status["parent_terminate_reason"] = "stop_timeout"
                    self._process.terminate()
                    self._process.join(timeout_seconds)
                if self._process.is_alive() and hasattr(self._process, "kill"):
                    with self._status_lock:
                        self._status["parent_terminate_reason"] = "stop_kill"
                    self._process.kill()
                    self._process.join(timeout_seconds)
            try:
                self._parent.close()
            except (OSError, ValueError):
                pass
            with self._status_lock:
                self._refresh_process_locked()
                still_alive = self._status["worker_alive"] is True
            if still_alive:
                raise RuntimeError("worker_process_did_not_exit")

    close = stop


def _construct_runtime_worker(
    selector_pack: QQSelectorPack,
    bindings: tuple[QQIdentityBinding, ...],
    session_evidence: tuple[QQSessionObservedDirectIdentity, ...],
    run_id: str | None,
    visual_selection: VisualSelectionConfig | None = None,
    visual_api_key: str | None = None,
    selection_handoff_signing_key: bytes | None = None,
    prepare_write_reserve_seconds: float = 5.0,
) -> QQVMWorker:
    accessibility = WindowsUIAQQAccessibility()
    selection_visual_profile = (
        QQ_VM_ROW_PALETTE_PROFILE
        if (
            selector_pack.client_version
            == QQ_VM_ROW_PALETTE_PROFILE.client_version
            and selector_pack.fixture_suite_version
            == QQ_VM_ROW_PALETTE_PROFILE.selector_pack_version
            and selector_pack.environment_fingerprint
            == QQ_VM_ROW_PALETTE_PROFILE.environment_fingerprint
        )
        else None
    )
    certifier = (QQSessionIdentityCertifier(accessibility=accessibility,
                                            selector_pack=selector_pack,
                                            evidence=session_evidence)
                 if session_evidence else None)
    locator = QQSessionCandidateLocator(session_evidence) if session_evidence else None
    selection_actuator = None
    if visual_selection is not None:
        if not visual_api_key:
            raise ValueError("visual selection requires a DeepSeek API key")
        selection_actuator = ConversationSelectionActuator(
            actions=accessibility,
            provider=DeepSeekVisualSelectionProvider(
                api_key=visual_api_key,
                model=visual_selection.model,
                timeout_seconds=visual_selection.timeout_seconds,
            ),
            labels=visual_selection.labels,
            min_confidence=visual_selection.min_confidence,
        )
    return QQVMWorker(accessibility=accessibility, selector_pack=selector_pack,
                      bindings=bindings, identity_certifier=certifier,
                      candidate_locator=locator,
                      selection_actuator=selection_actuator,
                      selection_visual_profile=selection_visual_profile,
                      selection_handoff_signing_key=selection_handoff_signing_key,
                      prepare_write_reserve_seconds=prepare_write_reserve_seconds,
                      expected_window=certifier.window_scope if certifier else None,
                      window_validator=certifier.validate_window if certifier else None,
                      run_id=run_id)


def _serve(connection, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...],
           session_evidence: tuple[QQSessionObservedDirectIdentity, ...] = (),
           run_id: str | None = None,
           visual_selection: VisualSelectionConfig | None = None,
           visual_api_key: str | None = None,
           selection_handoff_signing_key: bytes | None = None,
           prepare_write_reserve_seconds: float = 5.0) -> None:
    try:
        worker = _construct_runtime_worker(
            selector_pack,
            bindings,
            session_evidence,
            run_id,
            visual_selection,
            visual_api_key,
            selection_handoff_signing_key,
            prepare_write_reserve_seconds,
        )
    except Exception as exc:
        # Stay alive long enough to answer the parent's mandatory HEALTH
        # handshake with the real, redacted initialization cause.
        try:
            command = WorkerCommand.model_validate(connection.recv())
            error_code, evidence = _safe_failure(
                exc, fallback="worker_initialization_failed"
            )
            connection.send(WorkerResult(
                request_id=command.request_id,
                kind=command.kind,
                status=WorkerStatus.UNAVAILABLE,
                worker_epoch=UUID(int=0),
                operation_id=command.operation_id,
                binding_id=command.binding_id,
                binding_revision=command.binding_revision,
                conversation_revision=command.conversation_revision,
                error_code=error_code,
                evidence={"failure_stage": "initialize", **evidence},
            ).model_dump(mode="json"))
        except (EOFError, BrokenPipeError, OSError, ValueError):
            pass
        return
    try:
        while True:
            command = WorkerCommand.model_validate(connection.recv())
            result = worker.execute(command)
            connection.send(result.model_dump(mode="json"))
            if command.kind is WorkerKind.STOP:
                return
    finally:
        selection_actuator = getattr(worker, "_selection_actuator", None)
        close = getattr(selection_actuator, "close", None)
        if callable(close):
            close()


def _new_suffix(before: list[object], after: list[dict[str, object]]) -> list[dict[str, object]]:
    """Find only an ordered post-snapshot tail; never subtract unstable UI keys."""
    expected = [_bubble_anchor(row) for row in before if isinstance(row, dict)]
    actual = [_bubble_anchor(row) for row in after]
    if not expected:
        raise SnapshotAlignmentError("message_anchor_gap")
    return after[unique_suffix_start(expected, actual):]
