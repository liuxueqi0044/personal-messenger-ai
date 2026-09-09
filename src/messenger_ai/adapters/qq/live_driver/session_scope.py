"""Read-only inspection of the currently visible QQ conversation scope.

The companion helper emits only process/window metadata and digests.  It does
not emit the header text, message text, QQ number, coordinates, or UIA names.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CurrentSessionInspectionError(RuntimeError):
    """A fixed-code failure that never includes QQ UI content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CurrentSessionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: int = Field(gt=0)
    process_started_at: datetime
    window_handle: int = Field(gt=0)
    is_maximized: bool
    is_foreground: bool
    active_header_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    structure_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("process_started_at")
    @classmethod
    def normalize_process_start(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("process_started_at must be timezone-aware")
        return value.astimezone(UTC)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_FIELDS = {
    "probe_version",
    "mode",
    "succeeded",
    "status",
    "process_id",
    "window_handle",
    "process_started_at",
    "is_maximized",
    "is_foreground",
    "header_candidate_count",
    "active_header_digest",
    "structure_digest",
    "privacy",
}
_PRIVACY = {
    "exact_hwnd": True,
    "emitted_chat_text": False,
    "emitted_control_names": False,
    "emitted_contact_identifier": False,
    "desktop_capture_supported": False,
    "navigation_performed": False,
    "foreground_changed": False,
    "write_actions_supported": False,
    "mouse_input_used": False,
    "keyboard_input_used": False,
    "clipboard_used": False,
    "composer_or_send_accessed": False,
}


def _fail(code: str) -> None:
    raise CurrentSessionInspectionError(code)


def _parse_json(output: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(output, Mapping):
        return output
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8")
        except UnicodeDecodeError:
            _fail("HELPER_OUTPUT_INVALID")
    if not isinstance(output, str) or len(output) > 64_000:
        _fail("HELPER_OUTPUT_INVALID")
    try:
        value = json.loads(output.lstrip("\ufeff"))
    except json.JSONDecodeError:
        _fail("HELPER_OUTPUT_INVALID")
    if not isinstance(value, Mapping):
        _fail("HELPER_OUTPUT_INVALID")
    return value


def parse_current_session_report(
    output: str | bytes | Mapping[str, Any], *, expected_header_digest: str
) -> CurrentSessionSnapshot:
    """Validate and project the helper's redacted current-session report."""

    if not isinstance(expected_header_digest, str) or not _HASH.fullmatch(
        expected_header_digest
    ):
        _fail("HELPER_INPUT_INVALID")
    report = _parse_json(output)
    if set(report) != _FIELDS:
        _fail("INSPECTION_SCHEMA_INVALID")
    if report["probe_version"] != "qq-uia-current-session-v1":
        _fail("INSPECTION_VERSION_MISMATCH")
    if report["mode"] != "current_session_inspection":
        _fail("INSPECTION_MODE_MISMATCH")
    if (
        report["succeeded"] is not True
        or report["status"] != "CURRENT_SESSION_INSPECTED"
    ):
        _fail("INSPECTION_FAILED")
    if report["header_candidate_count"] != 1:
        _fail("ACTIVE_HEADER_AMBIGUOUS")
    if report["is_maximized"] is not True or not isinstance(
        report["is_foreground"], bool
    ):
        _fail("WINDOW_STATE_NOT_CERTIFIED")
    privacy = report["privacy"]
    if not isinstance(privacy, Mapping) or set(privacy) != set(_PRIVACY):
        _fail("PRIVACY_CONTRACT_FAILED")
    if any(privacy[key] is not expected for key, expected in _PRIVACY.items()):
        _fail("PRIVACY_CONTRACT_FAILED")
    header = report["active_header_digest"]
    if not isinstance(header, str) or not _HASH.fullmatch(header):
        _fail("INVALID_DIGEST")
    if header != expected_header_digest:
        _fail("HEADER_MISMATCH")
    try:
        return CurrentSessionSnapshot(
            process_id=report["process_id"],
            process_started_at=report["process_started_at"],
            window_handle=report["window_handle"],
            is_maximized=True,
            is_foreground=report["is_foreground"],
            active_header_digest=header,
            structure_digest=report["structure_digest"],
        )
    except (TypeError, ValueError):
        _fail("INSPECTION_DATA_INVALID")


Runner = Callable[..., subprocess.CompletedProcess[str]]


def invoke_current_session_helper(
    command: Sequence[str],
    expected_header_digest: str,
    *,
    runner: Runner = subprocess.run,
) -> Mapping[str, Any]:
    """Invoke the exact-HWND metadata-only helper without changing QQ state."""

    if (
        not command
        or any(not isinstance(part, str) or not part for part in command)
        or not isinstance(expected_header_digest, str)
        or not _HASH.fullmatch(expected_header_digest)
    ):
        _fail("HELPER_INPUT_INVALID")
    try:
        completed = runner(
            [
                *command,
                "--inspect-current-session",
                "--session-inspection-authorized",
            ],
            input=expected_header_digest + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _fail("HELPER_TIMEOUT")
    except (OSError, TypeError, ValueError):
        _fail("HELPER_INVOKE_FAILED")
    report = _parse_json(getattr(completed, "stdout", ""))
    if getattr(completed, "returncode", 1) != 0:
        status = report.get("status")
        if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
            _fail(status)
        _fail("HELPER_FAILED")
    return report


__all__ = [
    "CurrentSessionInspectionError",
    "CurrentSessionSnapshot",
    "invoke_current_session_helper",
    "parse_current_session_report",
]
