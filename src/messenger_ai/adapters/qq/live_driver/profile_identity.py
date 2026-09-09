"""Q3 profile-identity evidence consumption and pending-application updates.

Only a helper-produced HMAC of the QQ profile identifier is accepted.  Raw
QQ identifiers, display names, and ordinary (unsalted) digests never enter a
returned object or an error message.  This module deliberately does not call
``IdentityBindingRegistry``: a human must still create the active binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .identity import IdentityEvidenceSet, assess_binding_evidence


class ProfileIdentityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROBE_VERSION = "qq-uia-current-identity-v1"
_MODE = "current_chat_identity"
_STATUS = "STABLE_IDENTITY_CAPTURED"
_EVIDENCE_TYPE = "explicit_labeled_qq_id"
_FORBIDDEN_FIELDS = frozenset(
    {
        "profile_id",
        "raw_profile_id",
        "qq_number",
        "qq_id",
        "hmac_key",
        "raw_hmac_key",
    }
)


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise ProfileIdentityError("MISSING_FIELD")
    return value[key]


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ProfileIdentityError("INVALID_DIGEST")
    return value


def _parse(output: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(output, Mapping):
        value = output
    else:
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProfileIdentityError("INVALID_OUTPUT") from exc
        if not isinstance(output, str):
            raise ProfileIdentityError("INVALID_OUTPUT")
        try:
            value = json.loads(output.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileIdentityError("INVALID_OUTPUT") from exc
    if not isinstance(value, Mapping):
        raise ProfileIdentityError("INVALID_SCHEMA")
    if _contains_forbidden_field(value):
        raise ProfileIdentityError("PRIVACY_CONTRACT_FAILED")
    return value


def _contains_forbidden_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _FORBIDDEN_FIELDS or _contains_forbidden_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _validate_privacy(report: Mapping[str, Any]) -> None:
    privacy = _required(report, "privacy")
    expected = {
        "exact_hwnd": True,
        "raw_profile_id_emitted": False,
        "hmac_key_from_stdin_only": True,
        "emitted_chat_text": False,
        "emitted_control_names": False,
        "desktop_capture_supported": False,
        "mouse_input_used": False,
        "keyboard_input_used": False,
        "clipboard_used": False,
        "foreground_requested": False,
        "composer_send_attempted": False,
        "composer_or_send_accessed": False,
        "write_actions_supported": False,
        "hmac_key_emitted": False,
        "transient_navigation_performed": True,
    }
    if not isinstance(privacy, Mapping) or any(
        privacy.get(key) is not expected_value
        for key, expected_value in expected.items()
    ):
        raise ProfileIdentityError("PRIVACY_CONTRACT_FAILED")


def _validate_recovery(report: Mapping[str, Any]) -> None:
    recovery = _required(report, "recovery")
    if not isinstance(recovery, Mapping):
        raise ProfileIdentityError("RECOVERY_CONTRACT_FAILED")
    if (
        recovery.get("attempted") is not True
        or recovery.get("original_view_restored") is not True
        or recovery.get("foreground_changed") is not False
    ):
        raise ProfileIdentityError("RECOVERY_CONTRACT_FAILED")


def parse_profile_report(
    output: str | bytes | Mapping[str, Any],
    *,
    window_handle: int,
    environment_fingerprint: str,
    selector_pack_version: str,
    expected_header_digest: str,
    expected_process_id: int | None = None,
) -> dict[str, Any]:
    """Validate helper evidence and return only redacted profile data."""

    report = _parse(output)
    if _required(report, "probe_version") != _PROBE_VERSION:
        raise ProfileIdentityError("VERSION_MISMATCH")
    if _required(report, "mode") != _MODE:
        raise ProfileIdentityError("MODE_MISMATCH")
    if _required(report, "succeeded") is not True:
        raise ProfileIdentityError("HELPER_FAILED")
    if _required(report, "status") != _STATUS:
        raise ProfileIdentityError("STATUS_MISMATCH")
    actual_window = _required(report, "window_handle")
    process_id = _required(report, "process_id")
    if (
        isinstance(actual_window, bool)
        or not isinstance(actual_window, int)
        or actual_window != window_handle
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
        or (expected_process_id is not None and process_id != expected_process_id)
    ):
        raise ProfileIdentityError("HWND_OR_PROCESS_MISMATCH")
    if _required(report, "is_maximized") is not True:
        raise ProfileIdentityError("WINDOW_NOT_MAXIMIZED")
    foreground_before = _required(report, "is_foreground_before")
    foreground_after = _required(report, "is_foreground_after")
    if foreground_before is not False or foreground_after is not False:
        raise ProfileIdentityError("QQ_FOREGROUND")
    _validate_privacy(report)
    _validate_recovery(report)
    expected_header_digest = _digest(expected_header_digest)
    active_header_digest = _digest(_required(report, "active_header_digest"))
    if active_header_digest != expected_header_digest:
        raise ProfileIdentityError("HEADER_MISMATCH")
    right_digest = _digest(_required(report, "right_region_structure_digest"))
    if not isinstance(environment_fingerprint, str) or not _HASH.fullmatch(
        environment_fingerprint
    ):
        raise ProfileIdentityError("ENVIRONMENT_MISMATCH")
    if not isinstance(selector_pack_version, str) or not selector_pack_version.strip():
        raise ProfileIdentityError("SELECTOR_MISMATCH")

    header_count = _required(report, "header_candidate_count")
    if (
        isinstance(header_count, bool)
        or not isinstance(header_count, int)
        or header_count != 1
    ):
        raise ProfileIdentityError("HEADER_NOT_UNIQUE")
    candidate_count = _required(report, "identity_candidate_count")
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
        raise ProfileIdentityError("PROFILE_CANDIDATE_NOT_UNIQUE")
    if candidate_count != 1:
        raise ProfileIdentityError("PROFILE_CANDIDATE_NOT_UNIQUE")
    profile_id_hmac = _digest(_required(report, "profile_id_hmac"))
    if _required(report, "identity_evidence_type") != _EVIDENCE_TYPE:
        raise ProfileIdentityError("EVIDENCE_TYPE_MISMATCH")
    profile_structure_digest = _digest(_required(report, "profile_structure_digest"))
    # Deliberately project no candidate fields besides the HMAC and digests.
    return {
        "process_id": process_id,
        "window_handle": actual_window,
        "environment_fingerprint": environment_fingerprint,
        "selector_pack_version": selector_pack_version,
        "active_header_digest": active_header_digest,
        "right_region_structure_digest": right_digest,
        "profile_structure_digest": profile_structure_digest,
        "profile_id_hmac": profile_id_hmac,
        "candidate_count": candidate_count,
    }


def build_identity_evidence(
    parsed: Mapping[str, Any], application: Mapping[str, Any]
) -> IdentityEvidenceSet:
    """Project helper/application digests into opaque Q3 evidence only."""

    profile_id_hmac = _digest(parsed.get("profile_id_hmac"))
    environment = application.get("environment_fingerprint")
    selector = application.get("selector_pack_version")
    if not isinstance(environment, str) or not _HASH.fullmatch(environment):
        raise ProfileIdentityError("ENVIRONMENT_MISMATCH")
    if not isinstance(selector, str) or not selector.strip():
        raise ProfileIdentityError("SELECTOR_MISMATCH")
    if parsed.get("environment_fingerprint") != environment:
        raise ProfileIdentityError("ENVIRONMENT_MISMATCH")
    if parsed.get("selector_pack_version") != selector:
        raise ProfileIdentityError("SELECTOR_MISMATCH")
    window_handle = parsed.get("window_handle")
    if not isinstance(window_handle, int) or window_handle <= 0:
        raise ProfileIdentityError("HWND_OR_PROCESS_MISMATCH")
    opaque = lambda label: hashlib.sha256(
        f"qq-q3:{label}:{profile_id_hmac}".encode("ascii")
    ).hexdigest()
    right_digest = _digest(parsed.get("right_region_structure_digest"))
    return IdentityEvidenceSet(
        window_handle=window_handle,
        platform_conversation_id=f"qq-conversation-hmac:{opaque('conversation')}",
        conversation_type="direct",
        structure_path=("main_window", "conversation_view", "current_profile"),
        participant_signature=f"qq-participant-hmac:{opaque('participant')}",
        profile_id=f"hmac-sha256:{profile_id_hmac}",
        recent_message_anchor=f"qq-right-region:{right_digest}",
        environment_fingerprint=environment,
        selector_pack_version=selector,
        confidence=0.99,
        display_name="",
    )


def assess_pending_application(
    application: Mapping[str, Any],
    parsed: Mapping[str, Any],
    *,
    identity_evidence: IdentityEvidenceSet,
    minimum_confidence: float = 0.95,
) -> dict[str, Any]:
    """Attach Q3 evidence assessment without creating a contact or binding."""

    if application.get("status") != "pending_human_binding":
        raise ProfileIdentityError("APPLICATION_NOT_PENDING")
    if application.get("binding_created") is not False:
        raise ProfileIdentityError("ACTIVE_BINDING_FORBIDDEN")
    if application.get("automatic_eligible") is not False:
        raise ProfileIdentityError("AUTOMATIC_ELIGIBILITY_FORBIDDEN")
    profile_id_hmac = _digest(parsed.get("profile_id_hmac"))
    if parsed.get("window_handle") != identity_evidence.window_handle:
        raise ProfileIdentityError("HWND_OR_PROCESS_MISMATCH")
    if (
        parsed.get("environment_fingerprint")
        != identity_evidence.environment_fingerprint
    ):
        raise ProfileIdentityError("ENVIRONMENT_MISMATCH")
    if parsed.get("selector_pack_version") != identity_evidence.selector_pack_version:
        raise ProfileIdentityError("SELECTOR_MISMATCH")
    enriched = identity_evidence.model_copy(
        update={"profile_id": f"hmac-sha256:{profile_id_hmac}"}
    )
    assessment = assess_binding_evidence(
        enriched, minimum_confidence=minimum_confidence
    )
    updated = dict(application)
    updated["binding_evidence_assessment"] = {
        "bindable": assessment.bindable,
        "automatic_eligible": False,
        "reason_codes": [reason.value for reason in assessment.reasons],
    }
    updated["profile_id"] = f"hmac-sha256:{profile_id_hmac}"
    updated["identity_evidence"] = enriched.model_dump(mode="json")
    updated["identity_evidence_hash"] = enriched.evidence_hash
    updated["evidence_assessed_at"] = datetime.now(UTC).isoformat()
    updated["binding_created"] = False
    updated["automatic_eligible"] = False
    return updated


__all__ = [
    "ProfileIdentityError",
    "assess_pending_application",
    "build_identity_evidence",
    "parse_profile_report",
]
