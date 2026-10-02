"""Pure, target-bound verification of trusted normal-UI profile observations.

The collector, never the vision model, supplies these observations.  Matching
hashes cannot prove how UI was read: the collector must actually invoke the
current header, read the unique new profile window, and restore that chat.  A
fresh selected-row token fences this one acquisition; it is not a registered
locator or durable contact identity.  Leases are local verification results,
not signed cross-process capabilities or permission to send.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import (
    ContactIdentityMode, ContactTarget, NavigationFrame, NavigationModel,
)


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
ScopeId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
BusinessId = Annotated[str, Field(min_length=1, max_length=256, strict=True)]
CorrelationMethod = Literal["profile_from_current_header_with_selected_row_fence"]
MAX_LEASE_TTL_SECONDS = 15.0
MAX_EVIDENCE_AGE_SECONDS = 15.0


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("identity timestamps must be timezone-aware")
    return value


class ProfileIdentityExpectation(NavigationModel):
    """Approved profile anchor alongside an existing business binding.

    Creating this object does not enroll an observed identity.  The trusted
    binding registry supplies it; no existing participant signature is changed.
    """

    account_id: BusinessId
    conversation_id: BusinessId
    binding_id: BusinessId
    binding_revision: int = Field(ge=1, strict=True)
    expected_profile_hmac: Digest
    hmac_key_id: ScopeId
    client_version: ScopeId
    selector_pack_version: ScopeId
    environment_fingerprint: Digest


class CurrentChatWitness(NavigationModel):
    """A fresh before/after observation, without names, messages or draft text.

    The selected-row token is taken from the currently selected row, not looked
    up from an old binding.  Its selection marker must be independently read;
    a header hash or model decision is not a selected-row witness.
    """

    account_id: BusinessId
    conversation_id: BusinessId
    binding_id: BusinessId
    binding_revision: int = Field(ge=1, strict=True)
    run_id: ScopeId
    session_epoch: ScopeId
    surface_epoch: ScopeId
    worker_epoch: ScopeId
    observation_epoch: ScopeId
    desktop_lease_id: ScopeId
    control_revision: int = Field(ge=0, strict=True)
    process_id: int = Field(gt=0, strict=True)
    process_started_at_100ns: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    captured_at: datetime
    captured_monotonic_ns: int = Field(ge=0, strict=True)
    # Local UI classification is not contact identity. Older producers keep
    # unknown and must still provide the complete original chat proof. Only
    # explicit zero counts may establish a non-chat starting surface.
    surface_kind: Literal["chat", "non_chat", "unknown"] = "unknown"
    header_candidate_count: int | None = Field(default=None, ge=0, le=10000, strict=True)
    message_candidate_count: int | None = Field(default=None, ge=0, le=10000, strict=True)
    composer_candidate_count: int | None = Field(default=None, ge=0, le=10000, strict=True)
    header_digest: Digest | None = None
    selected_row_runtime_id_hash: Digest | None = None
    selected_row_candidate_count: int = Field(ge=0, le=10000, strict=True)
    selected_row_selection_source: Literal["selection_pattern", "selected_class"] | None = None
    active_chat_structure_digest: Digest | None = None
    conversation_type: Literal["direct", "group", "unknown"]
    group_marker_probe_complete: bool = Field(strict=True)
    group_marker_count: int = Field(ge=0, le=10000, strict=True)
    latest_tail: bool | None = Field(strict=True)
    composer_empty: bool | None = Field(strict=True)

    _captured_aware = field_validator("captured_at")(_aware)


class CurrentChatEvidence(NavigationModel):
    """One completed profile acquisition bracketed by independent witnesses.

    The profile timestamp denotes the actual fresh labeled-ID read.  Success
    flags describe exact-window recovery, not an attempted cleanup.  The
    collector must discard old UIA controls across this transient UI action.
    """

    schema_version: Literal["qq_current_chat_profile_v2"] = "qq_current_chat_profile_v2"
    evidence_ref: ScopeId
    frame_id: ScopeId
    source: Literal["normal_ui_profile"]
    identity_evidence_type: Literal["explicit_labeled_qq_id"]
    client_version: ScopeId
    selector_pack_version: ScopeId
    environment_fingerprint: Digest
    hmac_key_id: ScopeId
    profile_id_hmac: Digest | None = None
    profile_candidate_count: int = Field(ge=0, le=10000, strict=True)
    profile_window_candidate_count: int = Field(ge=0, le=10000, strict=True)
    profile_window_was_new: bool = Field(strict=True)
    profile_window_handle: int = Field(gt=0, strict=True)
    profile_process_id: int = Field(gt=0, strict=True)
    profile_captured_at: datetime
    profile_captured_monotonic_ns: int = Field(ge=0, strict=True)
    correlation_method: CorrelationMethod | None = None
    profile_opened_from_current_header: bool = Field(strict=True)
    profile_window_closed: bool = Field(strict=True)
    original_chat_restored: bool = Field(strict=True)
    foreground_restored: bool = Field(strict=True)
    before: CurrentChatWitness
    after: CurrentChatWitness

    _captured_aware = field_validator("profile_captured_at")(_aware)


class ActiveChatLease(NavigationModel):
    """Short-lived local proof; a consumer must still check live scope/expiry.

    Both clocks bound validity.  Monotonic values are meaningful only in the
    producing worker epoch; a new worker must obtain its own fresh proof.
    """

    schema_version: Literal["qq_active_chat_lease_v2"] = "qq_active_chat_lease_v2"
    lease_id: Digest
    account_id: BusinessId
    conversation_id: BusinessId
    binding_id: BusinessId
    binding_revision: int = Field(ge=1, strict=True)
    run_id: ScopeId
    session_epoch: ScopeId
    surface_epoch: ScopeId
    worker_epoch: ScopeId
    observation_epoch: ScopeId
    desktop_lease_id: ScopeId
    control_revision: int = Field(ge=0, strict=True)
    process_id: int = Field(gt=0, strict=True)
    process_started_at_100ns: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    frame_id: ScopeId
    evidence_ref: ScopeId
    evidence_digest: Digest
    verification_method: CorrelationMethod
    issued_at: datetime
    expires_at: datetime
    issued_monotonic_ns: int = Field(ge=0, strict=True)
    expires_monotonic_ns: int = Field(gt=0, strict=True)

    _aware_times = field_validator("issued_at", "expires_at")(_aware)

    @model_validator(mode="after")
    def _short_lifetime(self) -> ActiveChatLease:
        seconds = (self.expires_at - self.issued_at).total_seconds()
        mono_seconds = (self.expires_monotonic_ns - self.issued_monotonic_ns) / 1e9
        if not (0 < seconds <= MAX_LEASE_TTL_SECONDS and
                0 < mono_seconds <= MAX_LEASE_TTL_SECONDS):
            raise ValueError("active chat lease must have a short positive lifetime")
        return self

    def is_fresh(self, *, now: datetime, now_monotonic_ns: int) -> bool:
        """Temporal check only; this does not replace matching the live scope."""
        _aware(now)
        if isinstance(now_monotonic_ns, bool) or not isinstance(now_monotonic_ns, int):
            return False
        return (self.issued_at <= now < self.expires_at and
                self.issued_monotonic_ns <= now_monotonic_ns < self.expires_monotonic_ns)


class ActiveChatVerificationResult(NavigationModel):
    verified: bool = Field(strict=True)
    lease: ActiveChatLease | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$", strict=True)

    @model_validator(mode="after")
    def _one_outcome(self) -> ActiveChatVerificationResult:
        if self.verified:
            if self.lease is None or self.error_code is not None:
                raise ValueError("verified identity requires a lease and no error")
        elif self.lease is not None or self.error_code is None:
            raise ValueError("rejected identity requires an error and no lease")
        return self


_TARGET_FIELDS = ("account_id", "conversation_id", "binding_id", "binding_revision")
_FRAME_FIELDS = (
    "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
    "control_revision", "binding_id", "binding_revision", "process_id", "window_handle",
)
_VERSION_FIELDS = ("client_version", "selector_pack_version", "environment_fingerprint")
_CORRELATION_FIELDS = (
    "header_digest", "selected_row_runtime_id_hash", "active_chat_structure_digest",
)


def _reject(code: str) -> ActiveChatVerificationResult:
    return ActiveChatVerificationResult(verified=False, error_code=code)


def _bounded_seconds(value: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("identity freshness policy must be numeric")
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("identity freshness policy exceeds its bound")
    return float(value)


def verify_current_chat(
    target: ContactTarget,
    frame: NavigationFrame,
    expectation: ProfileIdentityExpectation,
    evidence: CurrentChatEvidence,
    *,
    now: datetime,
    deadline_at: datetime,
    expected_process_started_at_100ns: int,
    expected_observation_epoch: str,
    now_monotonic_ns: int,
    ttl_seconds: float = 10.0,
    max_evidence_age_seconds: float = 10.0,
) -> ActiveChatVerificationResult:
    """Validate trusted observations without I/O, mutation, clock reads or UI.

    Expected process lifetime/observation epoch come from the current trusted
    worker, not from the evidence being checked.  This intentionally offers no
    session-bound/header-only fallback and never enrolls a profile identity.
    """
    _aware(now)
    _aware(deadline_at)
    ttl = _bounded_seconds(ttl_seconds, MAX_LEASE_TTL_SECONDS)
    max_age = _bounded_seconds(max_evidence_age_seconds, MAX_EVIDENCE_AGE_SECONDS)
    if (isinstance(now_monotonic_ns, bool) or not isinstance(now_monotonic_ns, int)
            or now_monotonic_ns < 0):
        raise ValueError("current monotonic time must be a nonnegative integer")
    if (isinstance(expected_process_started_at_100ns, bool)
            or not isinstance(expected_process_started_at_100ns, int)
            or expected_process_started_at_100ns <= 0
            or not isinstance(expected_observation_epoch, str)
            or not expected_observation_epoch.strip()):
        raise ValueError("expected worker lifetime and observation must be explicit")
    if target.identity_mode != ContactIdentityMode.PERSISTENT:
        return _reject("identity_mode_unproven")
    if any(getattr(target, key) != getattr(expectation, key) for key in _TARGET_FIELDS):
        return _reject("identity_target_mismatch")
    if (frame.binding_id != target.binding_id or frame.binding_revision != target.binding_revision
            or evidence.frame_id != frame.frame_id):
        return _reject("identity_frame_mismatch")
    for witness in (evidence.before, evidence.after):
        if any(getattr(witness, key) != getattr(target, key) for key in _TARGET_FIELDS):
            return _reject("identity_target_mismatch")
        if any(getattr(witness, key) != getattr(frame, key) for key in _FRAME_FIELDS):
            return _reject("identity_scope_mismatch")
        if witness.process_started_at_100ns != expected_process_started_at_100ns:
            return _reject("identity_process_lifetime_mismatch")
        if witness.observation_epoch != expected_observation_epoch:
            return _reject("identity_observation_mismatch")
    if any(getattr(evidence, key) != getattr(expectation, key) for key in _VERSION_FIELDS):
        return _reject("identity_evidence_version_mismatch")
    if evidence.hmac_key_id != expectation.hmac_key_id:
        return _reject("identity_profile_key_mismatch")
    if deadline_at <= now:
        return _reject("identity_deadline_exhausted")
    before, after = evidence.before, evidence.after
    if not (frame.captured_at <= before.captured_at <= evidence.profile_captured_at
            <= after.captured_at <= now):
        return _reject("identity_capture_order_invalid")
    if not (before.captured_monotonic_ns <= evidence.profile_captured_monotonic_ns
            <= after.captured_monotonic_ns <= now_monotonic_ns):
        return _reject("identity_capture_order_invalid")
    if ((now - after.captured_at).total_seconds() >= max_age or
            (now_monotonic_ns - after.captured_monotonic_ns) / 1e9 >= max_age):
        return _reject("identity_evidence_stale")
    if evidence.profile_candidate_count != 1 or evidence.profile_id_hmac is None:
        return _reject("identity_profile_unproven")
    if not hmac.compare_digest(evidence.profile_id_hmac, expectation.expected_profile_hmac):
        return _reject("identity_profile_mismatch")
    if (evidence.correlation_method is None or not evidence.profile_opened_from_current_header
            or evidence.profile_window_candidate_count != 1 or not evidence.profile_window_was_new
            or evidence.profile_process_id != frame.process_id
            or evidence.profile_window_handle == frame.window_handle):
        return _reject("identity_chat_correlation_unproven")
    for witness in (before, after):
        if (witness.surface_kind not in {"chat", "unknown"}
                or any(value is not None and (type(value) is not int or value != 1) for value in (
                    witness.header_candidate_count, witness.message_candidate_count, witness.composer_candidate_count))
                or any(getattr(witness, key) is None for key in _CORRELATION_FIELDS)
                or witness.selected_row_candidate_count != 1
                or witness.selected_row_selection_source is None):
            return _reject("identity_chat_correlation_unproven")
    if any(getattr(before, key) != getattr(after, key) for key in _CORRELATION_FIELDS):
        return _reject("identity_chat_drift")
    if not (evidence.profile_window_closed and evidence.original_chat_restored
            and evidence.foreground_restored):
        return _reject("identity_profile_recovery_failed")
    for witness in (before, after):
        if witness.conversation_type != "direct" or witness.group_marker_count != 0:
            return _reject("identity_conversation_not_direct")
        if not witness.group_marker_probe_complete:
            return _reject("identity_group_probe_incomplete")
        if witness.latest_tail is not True:
            return _reject("identity_message_tail_unproven")
        if witness.composer_empty is not True:
            return _reject("identity_composer_not_empty")
    remaining = min(
        ttl,
        (deadline_at - now).total_seconds(),
        max_age - (now - after.captured_at).total_seconds(),
        max_age - (now_monotonic_ns - after.captured_monotonic_ns) / 1e9,
    )
    # Use whole microseconds so both serialized clocks have identical bounds.
    remaining_us = int(remaining * 1_000_000)
    if remaining_us <= 0:
        return _reject("identity_deadline_exhausted")
    evidence_digest = hashlib.sha256(evidence.model_dump_json().encode("utf-8")).hexdigest()
    values = {key: getattr(after, key) for key in (*_TARGET_FIELDS, *_FRAME_FIELDS)}
    values.update(
        process_started_at_100ns=expected_process_started_at_100ns,
        observation_epoch=expected_observation_epoch,
        frame_id=frame.frame_id,
        evidence_ref=evidence.evidence_ref,
        evidence_digest=evidence_digest,
        verification_method=evidence.correlation_method,
        issued_at=now,
        expires_at=now + timedelta(microseconds=remaining_us),
        issued_monotonic_ns=now_monotonic_ns,
        expires_monotonic_ns=now_monotonic_ns + remaining_us * 1000,
    )
    lease_id = hashlib.sha256(
        f"qq_active_chat_lease_v2|{evidence_digest}|{now.isoformat()}|{now_monotonic_ns}|{remaining_us}".encode("ascii")
    ).hexdigest()
    return ActiveChatVerificationResult(verified=True, lease=ActiveChatLease(lease_id=lease_id, **values))


__all__ = [
    "ActiveChatLease", "ActiveChatVerificationResult", "CurrentChatEvidence",
    "CurrentChatWitness", "ProfileIdentityExpectation", "verify_current_chat",
]
