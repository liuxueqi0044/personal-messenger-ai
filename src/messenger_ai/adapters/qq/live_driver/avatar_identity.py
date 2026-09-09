"""PII-minimized QQ avatar identity evidence consumption.

The helper computes a keyed fingerprint of the visible avatar locally.  This
module accepts that fingerprint and structural evidence only; it never
accepts image bytes, OCR, names, or a contact id and it never creates an
active binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .identity import IdentityEvidenceSet, assess_binding_evidence


class AvatarIdentityError(RuntimeError):
    """A safe, stable error code with no helper payload attached."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROBE_VERSION = "qq-uia-current-avatar-v1"
_MODE = "current_chat_avatar_capture"
_CAPTURE_APIS = frozenset({"PrintWindow", "WindowsGraphicsCapture", "ExactHwndBitBlt"})
_SUCCESS_STATUSES = frozenset(
    {
        "AVATAR_CAPTURED",
        "CURRENT_AVATAR_CAPTURED",
        "CURRENT_CHAT_AVATAR_CAPTURED",
        "STABLE_AVATAR_CAPTURED",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "raw_image",
        "raw_pixels",
        "pixel_bytes",
        "image_bytes",
        "image_data",
        "bitmap",
        "screenshot",
        "chat_text",
        "message_text",
        "display_name",
        "nickname",
        "profile_id",
        "qq_number",
        "qq_id",
        "hmac_key",
    }
)


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise AvatarIdentityError("MISSING_FIELD")
    return value[key]


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise AvatarIdentityError("INVALID_DIGEST")
    return value


def _parse(output: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(output, Mapping):
        value = output
    else:
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AvatarIdentityError("INVALID_OUTPUT") from exc
        if not isinstance(output, str):
            raise AvatarIdentityError("INVALID_OUTPUT")
        try:
            value = json.loads(output.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise AvatarIdentityError("INVALID_OUTPUT") from exc
    if not isinstance(value, Mapping):
        raise AvatarIdentityError("INVALID_SCHEMA")
    if _contains_forbidden_field(value):
        raise AvatarIdentityError("PRIVACY_CONTRACT_FAILED")
    return value


def _contains_forbidden_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _FORBIDDEN_FIELDS or _contains_forbidden_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _count(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AvatarIdentityError("AVATAR_ROWS_NOT_STABLE")
    return value


def _validate_privacy(report: Mapping[str, Any]) -> None:
    privacy = _required(report, "privacy")
    expected = {
        "exact_hwnd": True,
        "desktop_capture": False,
        "image_bytes_emitted": False,
        "emitted_chat_text": False,
        "emitted_control_names": False,
        "navigation_performed": False,
        "foreground_changed": False,
        "write_actions_supported": False,
        "mouse_input_used": False,
        "keyboard_input_used": False,
        "clipboard_used": False,
        "foreground_requested": False,
        "composer_or_send_accessed": False,
    }
    if not isinstance(privacy, Mapping) or any(
        privacy.get(key) is not expected_value
        for key, expected_value in expected.items()
    ):
        raise AvatarIdentityError("PRIVACY_CONTRACT_FAILED")


def parse_avatar_report(
    output: str | bytes | Mapping[str, Any],
    *,
    window_handle: int,
    process_id: int,
    expected_header_digest: str,
) -> dict[str, Any]:
    """Validate a successful helper report and project only safe evidence."""

    report = _parse(output)
    if _required(report, "probe_version") != _PROBE_VERSION:
        raise AvatarIdentityError("VERSION_MISMATCH")
    if _required(report, "mode") != _MODE:
        raise AvatarIdentityError("MODE_MISMATCH")
    if _required(report, "succeeded") is not True:
        raise AvatarIdentityError("HELPER_FAILED")
    status = _required(report, "status")
    if status not in _SUCCESS_STATUSES:
        raise AvatarIdentityError("STATUS_MISMATCH")
    actual_pid = _required(report, "process_id")
    actual_hwnd = _required(report, "window_handle")
    if (
        isinstance(actual_pid, bool)
        or not isinstance(actual_pid, int)
        or actual_pid <= 0
        or actual_pid != process_id
        or isinstance(actual_hwnd, bool)
        or not isinstance(actual_hwnd, int)
        or actual_hwnd != window_handle
    ):
        raise AvatarIdentityError("HWND_OR_PROCESS_MISMATCH")
    if _required(report, "is_maximized") is not True:
        raise AvatarIdentityError("WINDOW_NOT_MAXIMIZED")
    if (
        _required(report, "is_foreground_before") is not False
        or _required(report, "is_foreground_after") is not False
    ):
        raise AvatarIdentityError("QQ_FOREGROUND")
    _validate_privacy(report)
    expected_header_digest = _digest(expected_header_digest)
    active_header_digest = _digest(_required(report, "active_header_digest"))
    if active_header_digest != expected_header_digest:
        raise AvatarIdentityError("HEADER_MISMATCH")
    candidate_rows = _count(_required(report, "candidate_row_count"), minimum=2)
    stable_matches = _count(_required(report, "stable_match_count"), minimum=2)
    if stable_matches > candidate_rows:
        raise AvatarIdentityError("AVATAR_ROWS_NOT_STABLE")
    avatar_hmac = _digest(_required(report, "avatar_hmac"))
    structure_digest = _digest(_required(report, "structure_digest"))
    if _required(report, "capture_api") not in _CAPTURE_APIS:
        raise AvatarIdentityError("CAPTURE_API_MISMATCH")
    return {
        "process_id": actual_pid,
        "window_handle": actual_hwnd,
        "active_header_digest": active_header_digest,
        "candidate_row_count": candidate_rows,
        "stable_match_count": stable_matches,
        "avatar_hmac": avatar_hmac,
        "structure_digest": structure_digest,
    }


def build_avatar_evidence(
    parsed: Mapping[str, Any], application: Mapping[str, Any]
) -> IdentityEvidenceSet:
    """Build evidence with a keyed pixel fingerprint as its stable signal."""

    avatar_hmac = _digest(parsed.get("avatar_hmac"))
    environment = application.get("environment_fingerprint")
    selector = application.get("selector_pack_version")
    if not isinstance(environment, str) or not _HASH.fullmatch(environment):
        raise AvatarIdentityError("ENVIRONMENT_MISMATCH")
    if not isinstance(selector, str) or not selector.strip():
        raise AvatarIdentityError("SELECTOR_MISMATCH")
    hwnd = parsed.get("window_handle")
    if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
        raise AvatarIdentityError("HWND_OR_PROCESS_MISMATCH")
    opaque = lambda label: hashlib.sha256(
        f"qq-q3-avatar:{label}:{avatar_hmac}".encode("ascii")
    ).hexdigest()
    return IdentityEvidenceSet(
        window_handle=hwnd,
        platform_conversation_id=f"qq-conversation-hmac:{opaque('conversation')}",
        conversation_type="direct",
        structure_path=("main_window", "conversation_view", "avatar_identity"),
        participant_signature=f"qq-participant-hmac:{opaque('participant')}",
        profile_id=(
            application.get("profile_id")
            if isinstance(application.get("profile_id"), str)
            and application.get("profile_id", "").startswith("hmac-sha256:")
            else None
        ),
        avatar_hash=avatar_hmac,
        recent_message_anchor=f"qq-right-region:{_digest(parsed.get('structure_digest'))}",
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
    """Attach avatar evidence while keeping the application human-gated."""

    if application.get("status") != "pending_human_binding":
        raise AvatarIdentityError("APPLICATION_NOT_PENDING")
    if application.get("binding_created") is not False:
        raise AvatarIdentityError("ACTIVE_BINDING_FORBIDDEN")
    if application.get("automatic_eligible") is not False:
        raise AvatarIdentityError("AUTOMATIC_ELIGIBILITY_FORBIDDEN")
    if parsed.get("window_handle") != identity_evidence.window_handle:
        raise AvatarIdentityError("HWND_OR_PROCESS_MISMATCH")
    assessment = assess_binding_evidence(
        identity_evidence, minimum_confidence=minimum_confidence
    )
    updated = dict(application)
    updated["binding_evidence_assessment"] = {
        "bindable": assessment.bindable,
        "automatic_eligible": False,
        "reason_codes": [reason.value for reason in assessment.reasons],
    }
    updated["identity_evidence"] = identity_evidence.model_dump(mode="json")
    updated["identity_evidence_hash"] = identity_evidence.evidence_hash
    updated["evidence_assessed_at"] = datetime.now(UTC).isoformat()
    updated["binding_created"] = False
    updated["automatic_eligible"] = False
    return updated


__all__ = [
    "AvatarIdentityError",
    "assess_pending_application",
    "build_avatar_evidence",
    "parse_avatar_report",
]
