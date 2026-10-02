"""Strict N2 acquisition adapter and verifier with injected local UI sources.

No QQ access, enrollment, model call or thread-based timeout is hidden here.
The synchronous capture adapter belongs inside the supervised guest process.
Async sources must finish cancellation by isolating any in-flight UI process;
they must never retain COM controls across profile capture or return late input.
"""
from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError, field_validator, model_validator

from messenger_ai.adapters.qq.live_driver.profile_identity import (
    ProfileIdentityError, parse_guest_foreground_profile_report,
)
from messenger_ai.adapters.qq.vm_driver.profile_identity import (
    ProfileCaptureError, _capture_current_profile,
)
from .contracts import ContactIdentityMode, ContactTarget, NavigationFrame, NavigationModel
from .identity import (
    ActiveChatVerificationResult, CurrentChatEvidence, CurrentChatWitness,
    Digest, ProfileIdentityExpectation, ScopeId, _aware, verify_current_chat,
)


class ProfileAcquisitionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProfileAcquisitionWitness(NavigationModel):
    captured_at: datetime
    captured_monotonic_ns: int = Field(ge=0, strict=True)
    header_digest: Digest | None
    selected_row_candidate_count: int = Field(ge=0, le=10000, strict=True)
    # V2 fence only: SHA256(UTF8(".".join(decimal RuntimeId components))).
    # This deliberately does not use the legacy Python registered-row encoding.
    selected_row_runtime_id_hash: Digest | None
    selected_row_selection_source: Literal["selected_class", "selection_pattern"] | None
    active_chat_structure_digest: Digest | None

    _captured_aware = field_validator("captured_at")(_aware)


class ProfileAcquisition(NavigationModel):
    version: Literal["qq_profile_acquisition_v2"]
    process_started_at_100ns: int = Field(gt=0, strict=True)
    profile_window_handle: int = Field(gt=0, strict=True)
    profile_process_id: int = Field(gt=0, strict=True)
    profile_window_candidate_count: int = Field(ge=0, le=10000, strict=True)
    profile_window_was_new: bool = Field(strict=True)
    profile_captured_at: datetime
    profile_captured_monotonic_ns: int = Field(ge=0, strict=True)
    profile_window_closed: bool = Field(strict=True)
    original_chat_restored: bool = Field(strict=True)
    foreground_restored: bool = Field(strict=True)
    before: ProfileAcquisitionWitness
    after: ProfileAcquisitionWitness

    _profile_aware = field_validator("profile_captured_at")(_aware)

    @model_validator(mode="after")
    def _capture_order(self) -> ProfileAcquisition:
        if not (self.before.captured_at <= self.profile_captured_at <= self.after.captured_at
                and self.before.captured_monotonic_ns <= self.profile_captured_monotonic_ns
                <= self.after.captured_monotonic_ns):
            raise ValueError("profile acquisition has inconsistent capture order")
        return self


class CapturedProfileProjection(NavigationModel):
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    environment_fingerprint: Digest
    selector_pack_version: ScopeId
    active_header_digest: Digest
    right_region_structure_digest: Digest
    profile_structure_digest: Digest
    profile_id_hmac: Digest
    candidate_count: Literal[1]


class ValidatedProfileAcquisition(NavigationModel):
    """Validated data may still lack a selected marker; it is not a lease."""
    profile: CapturedProfileProjection
    acquisition: ProfileAcquisition


def _project_acquisition(raw: Mapping[str, object], projection: Mapping[str, object]) -> ValidatedProfileAcquisition:
    try:
        result = ValidatedProfileAcquisition(
            profile=CapturedProfileProjection.model_validate(projection),
            acquisition=ProfileAcquisition.model_validate(raw.get("acquisition")),
        )
    except (ValidationError, TypeError, ValueError):
        # Pydantic validation errors include input values; never propagate them.
        raise ProfileAcquisitionError("identity_profile_acquisition_invalid") from None
    acquisition = result.acquisition
    if (acquisition.profile_process_id != result.profile.process_id
            or acquisition.profile_window_handle == result.profile.window_handle):
        raise ProfileAcquisitionError("identity_profile_window_mismatch")
    return result


def parse_profile_acquisition_report(
    raw: Mapping[str, object], *, pid: int, hwnd: int, environment_fingerprint: str,
    selector_pack_version: str, expected_header_digest: str,
    expected_right_region_structure_digest: str,
) -> ValidatedProfileAcquisition:
    """Apply the unchanged legacy privacy/recovery parser, then a closed schema."""
    if not isinstance(raw, Mapping):
        raise ProfileAcquisitionError("identity_profile_report_invalid")
    try:
        projection = parse_guest_foreground_profile_report(
            raw, window_handle=hwnd, expected_process_id=pid,
            environment_fingerprint=environment_fingerprint,
            selector_pack_version=selector_pack_version,
            expected_header_digest=expected_header_digest,
            expected_right_region_structure_digest=expected_right_region_structure_digest,
        )
    except (ProfileIdentityError, RecursionError):
        raise ProfileAcquisitionError("identity_profile_report_invalid") from None
    return _project_acquisition(raw, projection)


def capture_profile_acquisition(helper: str, **kwargs: Any) -> ValidatedProfileAcquisition:
    """Same capture arguments/budget as capture_current_profile, with strict V2 data.

    Run this synchronous call in the supervised guest process, never an
    uncancellable background thread.  No raw helper report leaves the adapter.
    """
    if "validated_projector" in kwargs:
        raise ProfileAcquisitionError("identity_profile_capture_input_invalid")
    return _capture_current_profile(helper, validated_projector=_project_acquisition, **kwargs)


class ProfileVerificationContext(NavigationModel):
    process_started_at_100ns: int = Field(gt=0, strict=True)
    observation_epoch: ScopeId


class LocalChatWitnessSource(Protocol):
    async def snapshot(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime,
    ) -> CurrentChatWitness:
        """Fresh complete value snapshot; selected token uses the V2 dot encoding.

        Close its COM phase before returning. Group, tail and composer booleans
        come from actual local reads, and scope includes current pause/control
        admission by the desktop owner. Names/messages/draft text never return.
        """
        ...


class ProfileAcquisitionSource(Protocol):
    async def capture(
        self, target: ContactTarget, frame: NavigationFrame,
        expectation: ProfileIdentityExpectation, *, deadline_at: datetime,
    ) -> ValidatedProfileAcquisition:
        """Admit exact live scope immediately before isolated helper execution.

        Use capture_profile_acquisition, the registered key/selector environment,
        and the same host QPC clock as local witnesses. Enforce the remaining
        deadline by terminating and reaping the isolated UI process on failure;
        cancellation must finish that isolation before releasing desktop control.
        """
        ...


class ProfileVerificationAttempt(NavigationModel):
    result: ActiveChatVerificationResult
    # Retain only a strict redacted acquisition when subsequent reads/time fail.
    acquisition: ValidatedProfileAcquisition | None = None


_TARGET_SCOPE = ("account_id", "conversation_id", "binding_id", "binding_revision")
_FRAME_SCOPE = (
    "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
    "control_revision", "binding_id", "binding_revision", "process_id", "window_handle",
)
_MAX_PROOF_AGE_SECONDS = 10.0


def _remaining_proof_life(captured_at: datetime, captured_ns: int, *, now: datetime, tick: int) -> float:
    if captured_at > now or captured_ns > tick:
        raise ProfileAcquisitionError("identity_capture_order_invalid")
    remaining = min(
        _MAX_PROOF_AGE_SECONDS - (now - captured_at).total_seconds(),
        _MAX_PROOF_AGE_SECONDS - (tick - captured_ns) / 1e9,
    )
    if remaining <= 0:
        raise ProfileAcquisitionError("identity_evidence_stale")
    return remaining


def _local_scope_preflight(
    witness: CurrentChatWitness, target: ContactTarget, frame: NavigationFrame,
    context: ProfileVerificationContext,
) -> None:
    if not isinstance(witness, CurrentChatWitness):
        raise ProfileAcquisitionError("identity_profile_evidence_invalid")
    try:
        # A constructed/copied model can bypass its field validators. In
        # particular, malformed hashes must never look like another contact.
        CurrentChatWitness.model_validate(witness.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise ProfileAcquisitionError("identity_profile_evidence_invalid") from None
    if any(getattr(witness, key) != getattr(target, key) for key in _TARGET_SCOPE):
        raise ProfileAcquisitionError("identity_target_mismatch")
    if any(getattr(witness, key) != getattr(frame, key) for key in _FRAME_SCOPE):
        raise ProfileAcquisitionError("identity_scope_mismatch")
    if witness.process_started_at_100ns != context.process_started_at_100ns:
        raise ProfileAcquisitionError("identity_process_lifetime_mismatch")
    if witness.observation_epoch != context.observation_epoch:
        raise ProfileAcquisitionError("identity_observation_mismatch")


def _local_chat_preflight(witness: CurrentChatWitness) -> None:
    if (witness.surface_kind not in {"chat", "unknown"}
            or any(value is not None and value != 1 for value in (
                witness.header_candidate_count, witness.message_candidate_count, witness.composer_candidate_count))
            or witness.selected_row_candidate_count != 1 or witness.selected_row_runtime_id_hash is None
            or witness.selected_row_selection_source is None or witness.header_digest is None
            or witness.active_chat_structure_digest is None):
        raise ProfileAcquisitionError("identity_chat_correlation_unproven")
    if witness.conversation_type != "direct" or witness.group_marker_count != 0:
        raise ProfileAcquisitionError("identity_conversation_not_direct")
    if not witness.group_marker_probe_complete:
        raise ProfileAcquisitionError("identity_group_probe_incomplete")
    if witness.latest_tail is not True:
        raise ProfileAcquisitionError("identity_message_tail_unproven")
    if witness.composer_empty is not True:
        raise ProfileAcquisitionError("identity_composer_not_empty")


def _local_non_chat_preflight(witness: CurrentChatWitness) -> None:
    # This negative surface proof is useful only before navigation starts. A
    # missing/partial chat, ambiguous selection or unknown metadata is not it.
    if (witness.surface_kind != "non_chat"
            or (witness.header_candidate_count, witness.message_candidate_count,
                witness.composer_candidate_count) != (0, 0, 0)
            or witness.header_digest is not None or witness.active_chat_structure_digest is not None
            or witness.conversation_type != "unknown"
            or witness.latest_tail is not None or witness.composer_empty is not None
            or witness.selected_row_candidate_count not in (0, 1)
            or (witness.selected_row_candidate_count == 0 and (
                witness.selected_row_runtime_id_hash is not None or witness.selected_row_selection_source is not None))
            or (witness.selected_row_candidate_count == 1 and (
                witness.selected_row_runtime_id_hash is None or witness.selected_row_selection_source is None))):
        raise ProfileAcquisitionError("identity_chat_correlation_unproven")
    if witness.group_marker_count != 0:
        raise ProfileAcquisitionError("identity_conversation_not_direct")
    if not witness.group_marker_probe_complete:
        raise ProfileAcquisitionError("identity_group_probe_incomplete")


def _local_other_title_preflight(witness: CurrentChatWitness, trusted_headers: set[str]) -> None:
    # Initial N1 only: an independently read other title, with no message region
    # and no composer, may be left by navigation. It remains an unknown surface,
    # not a certified chat, and cannot enter full verification or N4.
    if (witness.surface_kind != "unknown"
            or (witness.header_candidate_count, witness.message_candidate_count,
                witness.composer_candidate_count) != (1, 0, 0)
            or witness.header_digest is None or witness.header_digest in trusted_headers
            or witness.selected_row_candidate_count != 1 or witness.selected_row_runtime_id_hash is None
            or witness.selected_row_selection_source is None or witness.active_chat_structure_digest is not None
            or witness.conversation_type != "unknown" or witness.latest_tail is not None
            or witness.composer_empty is not None):
        raise ProfileAcquisitionError("identity_chat_correlation_unproven")
    if witness.group_marker_count != 0:
        raise ProfileAcquisitionError("identity_conversation_not_direct")
    if not witness.group_marker_probe_complete:
        raise ProfileAcquisitionError("identity_group_probe_incomplete")


def _local_preflight(
    witness: CurrentChatWitness, target: ContactTarget, frame: NavigationFrame,
    context: ProfileVerificationContext,
) -> None:
    _local_scope_preflight(witness, target, frame, context)
    _local_chat_preflight(witness)


def assemble_current_chat_evidence(
    target: ContactTarget, frame: NavigationFrame, expectation: ProfileIdentityExpectation,
    captured: ValidatedProfileAcquisition, before: CurrentChatWitness, after: CurrentChatWitness,
    context: ProfileVerificationContext,
) -> CurrentChatEvidence:
    """Join four fresh selected witnesses; never compare unlike header hashes."""
    if not isinstance(captured, ValidatedProfileAcquisition):
        raise ProfileAcquisitionError("identity_profile_acquisition_invalid")
    _local_preflight(before, target, frame, context)
    _local_preflight(after, target, frame, context)
    profile, acquisition = captured.profile, captured.acquisition
    if (profile.process_id != frame.process_id or profile.window_handle != frame.window_handle
            or acquisition.process_started_at_100ns != context.process_started_at_100ns):
        raise ProfileAcquisitionError("identity_profile_scope_mismatch")
    if (profile.environment_fingerprint != expectation.environment_fingerprint
            or profile.selector_pack_version != expectation.selector_pack_version):
        raise ProfileAcquisitionError("identity_evidence_version_mismatch")
    if not (before.captured_at <= acquisition.before.captured_at
            <= acquisition.after.captured_at <= after.captured_at
            and before.captured_monotonic_ns <= acquisition.before.captured_monotonic_ns
            <= acquisition.after.captured_monotonic_ns <= after.captured_monotonic_ns):
        raise ProfileAcquisitionError("identity_profile_capture_order_invalid")
    for inner in (acquisition.before, acquisition.after):
        if (inner.selected_row_candidate_count != 1 or inner.selected_row_selection_source is None
                or inner.selected_row_runtime_id_hash is None or inner.header_digest is None
                or inner.active_chat_structure_digest is None):
            raise ProfileAcquisitionError("identity_chat_correlation_unproven")
        if (inner.selected_row_runtime_id_hash != before.selected_row_runtime_id_hash
                or inner.selected_row_runtime_id_hash != after.selected_row_runtime_id_hash):
            raise ProfileAcquisitionError("identity_chat_drift")
    if (acquisition.before.header_digest != profile.active_header_digest
            or acquisition.after.header_digest != profile.active_header_digest
            or acquisition.before.active_chat_structure_digest != profile.right_region_structure_digest
            or acquisition.after.active_chat_structure_digest != profile.right_region_structure_digest):
        raise ProfileAcquisitionError("identity_chat_drift")
    # Local and helper headers/structures have separate algorithms. Their own
    # pairs must close; the common fresh selected token links the four reads.
    acquisition_digest = hashlib.sha256(captured.model_dump_json().encode("utf-8")).hexdigest()
    return CurrentChatEvidence(
        evidence_ref=acquisition_digest, frame_id=frame.frame_id,
        source="normal_ui_profile", identity_evidence_type="explicit_labeled_qq_id",
        client_version=expectation.client_version, selector_pack_version=profile.selector_pack_version,
        environment_fingerprint=profile.environment_fingerprint, hmac_key_id=expectation.hmac_key_id,
        profile_id_hmac=profile.profile_id_hmac, profile_candidate_count=profile.candidate_count,
        profile_window_candidate_count=acquisition.profile_window_candidate_count,
        profile_window_was_new=acquisition.profile_window_was_new,
        profile_window_handle=acquisition.profile_window_handle,
        profile_process_id=acquisition.profile_process_id,
        profile_captured_at=acquisition.profile_captured_at,
        profile_captured_monotonic_ns=acquisition.profile_captured_monotonic_ns,
        correlation_method="profile_from_current_header_with_selected_row_fence",
        # This is established by successful legacy mode parsing, unique new
        # window metadata, and the four-way selected/header fence above.
        profile_opened_from_current_header=True,
        profile_window_closed=acquisition.profile_window_closed,
        original_chat_restored=acquisition.original_chat_restored,
        foreground_restored=acquisition.foreground_restored,
        before=before, after=after,
    )


class ProfileCurrentChatVerifier:
    """Production-facing async port; sources own actual process isolation."""

    def __init__(
        self, *, expectation_lookup: Callable[[ContactTarget], ProfileIdentityExpectation | None],
        context_source: Callable[[ContactTarget, NavigationFrame], ProfileVerificationContext],
        local_witness_source: LocalChatWitnessSource, profile_source: ProfileAcquisitionSource,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.expectation_lookup = expectation_lookup
        self.context_source = context_source
        self.local_witness_source = local_witness_source
        self.profile_source = profile_source
        self.clock, self.monotonic_ns = clock, monotonic_ns

    async def verify(self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime) -> ActiveChatVerificationResult:
        return (await self.verify_with_evidence(target, frame, deadline_at=deadline_at)).result

    async def verify_if_current(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime,
    ) -> ActiveChatVerificationResult:
        """Initial navigation optimization only; a non-current surface grants no lease.

        A locally proven non-chat surface, or another unique title with neither
        a message region nor composer, may start navigation. A chat needs a
        complete fresh witness before comparing its header hash with trusted
        labels; missing or ambiguous chat metadata never suffices. A match needs
        full profile identity proof, including for same-name impostors. After
        navigation opens a candidate, callers must always use ``verify``.
        """
        return (await self._verify_attempt(
            target, frame, deadline_at=deadline_at, initial_if_current=True,
        )).result

    async def verify_with_evidence(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime,
    ) -> ProfileVerificationAttempt:
        return await self._verify_attempt(target, frame, deadline_at=deadline_at)

    async def _verify_attempt(
        self, target: ContactTarget, frame: NavigationFrame, *, deadline_at: datetime,
        initial_if_current: bool = False,
    ) -> ProfileVerificationAttempt:
        captured = None
        try:
            _aware(deadline_at)
            start, start_ns = self.clock(), self.monotonic_ns()
            _aware(start)
            if type(start_ns) is not int or start_ns < 0:
                raise ProfileAcquisitionError("identity_capture_order_invalid")
            end_ns = start_ns + int((deadline_at - start).total_seconds() * 1e9)
            last_ns = start_ns

            def live_state() -> tuple[datetime, int, datetime]:
                nonlocal last_ns
                now, tick = self.clock(), self.monotonic_ns()
                _aware(now)
                if type(tick) is not int or tick < last_ns:
                    raise ProfileAcquisitionError("identity_capture_order_invalid")
                last_ns = tick
                remaining = min((deadline_at - now).total_seconds(), (end_ns - tick) / 1e9)
                if not math.isfinite(remaining) or remaining <= 0:
                    raise ProfileAcquisitionError("identity_deadline_exhausted")
                return now, tick, min(deadline_at, now + timedelta(seconds=remaining))

            def live_deadline() -> datetime:
                return live_state()[2]

            live_deadline()
            if target.identity_mode != ContactIdentityMode.PERSISTENT:
                raise ProfileAcquisitionError("identity_mode_unproven")
            expectation = self.expectation_lookup(target)
            if expectation is None:
                raise ProfileAcquisitionError("identity_profile_expectation_missing")
            if any(getattr(expectation, key) != getattr(target, key) for key in _TARGET_SCOPE):
                raise ProfileAcquisitionError("identity_target_mismatch")
            context = self.context_source(target, frame)
            before = await self.local_witness_source.snapshot(target, frame, deadline_at=live_deadline())
            live_deadline()
            _local_scope_preflight(before, target, frame, context)
            trusted_headers = {hashlib.sha256(label.encode("utf-8")).hexdigest()
                               for label in target.trusted_queries} if initial_if_current else set()
            non_chat = initial_if_current and before.surface_kind == "non_chat"
            other_title = (initial_if_current and before.surface_kind == "unknown"
                           and (before.header_candidate_count, before.message_candidate_count,
                                before.composer_candidate_count) == (1, 0, 0))
            if non_chat:
                _local_non_chat_preflight(before)
            elif other_title:
                _local_other_title_preflight(before, trusted_headers)
            else:
                _local_chat_preflight(before)
            if before.captured_at < frame.captured_at:
                raise ProfileAcquisitionError("identity_capture_order_invalid")
            if initial_if_current:
                # The await may have changed admission or the independent
                # binding. Check again even when no profile will be opened.
                if context != self.context_source(target, frame) or expectation != self.expectation_lookup(target):
                    raise ProfileAcquisitionError("identity_scope_mismatch")
            now, tick, _ = live_state()
            _remaining_proof_life(before.captured_at, before.captured_monotonic_ns,
                                  now=now, tick=tick)
            if initial_if_current and (non_chat or other_title or before.header_digest not in trusted_headers):
                return ProfileVerificationAttempt(result=ActiveChatVerificationResult(
                    verified=False, error_code="active_chat_not_current",
                ))
            candidate = await self.profile_source.capture(target, frame, expectation, deadline_at=live_deadline())
            if not isinstance(candidate, ValidatedProfileAcquisition):
                raise ProfileAcquisitionError("identity_profile_acquisition_invalid")
            captured = candidate
            live_deadline()
            after = await self.local_witness_source.snapshot(target, frame, deadline_at=live_deadline())
            live_deadline()
            if context != self.context_source(target, frame) or expectation != self.expectation_lookup(target):
                raise ProfileAcquisitionError("identity_scope_mismatch")
            evidence = assemble_current_chat_evidence(target, frame, expectation, captured, before, after, context)
            now, tick, effective_deadline = live_state()
            # Neither restoration nor an outer post-read renews the actual
            # labeled-ID read. Both clocks bound its original proof lifetime.
            proof_life = _remaining_proof_life(
                captured.acquisition.profile_captured_at,
                captured.acquisition.profile_captured_monotonic_ns, now=now, tick=tick,
            )
            result = verify_current_chat(
                target, frame, expectation, evidence, now=now, deadline_at=effective_deadline,
                expected_process_started_at_100ns=context.process_started_at_100ns,
                expected_observation_epoch=context.observation_epoch, now_monotonic_ns=tick,
                ttl_seconds=proof_life,
            )
            return ProfileVerificationAttempt(result=result, acquisition=captured)
        except ProfileAcquisitionError as exc:
            error_code = exc.code
        except ProfileCaptureError:
            error_code = "identity_profile_capture_failed"
        except TimeoutError:
            error_code = "identity_deadline_exhausted"
        except (ValidationError, ValueError, TypeError):
            error_code = "identity_profile_evidence_invalid"
        except Exception:
            # Trusted source failures may contain native paths/control text.
            # Fail closed without propagating them; cancellation still escapes.
            error_code = "identity_profile_evidence_unavailable"
        return ProfileVerificationAttempt(
            result=ActiveChatVerificationResult(verified=False, error_code=error_code),
            acquisition=captured,
        )


__all__ = [
    "ProfileAcquisition", "ProfileAcquisitionWitness", "ValidatedProfileAcquisition",
    "ProfileAcquisitionError", "ProfileCurrentChatVerifier", "ProfileVerificationContext",
    "ProfileVerificationAttempt", "LocalChatWitnessSource", "ProfileAcquisitionSource",
    "parse_profile_acquisition_report", "capture_profile_acquisition", "assemble_current_chat_evidence",
]
