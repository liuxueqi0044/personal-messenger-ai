"""Protocol layer for the isolated Windows Graphics Capture sidecar.

Terra's UIA helper discovers redacted avatar rows. A separate native sidecar
receives only that geometry and computes the keyed pixel fingerprint.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .avatar_identity import (
    AvatarIdentityError,
    assess_pending_application,
    build_avatar_evidence,
    parse_avatar_report,
)


class WgcAvatarError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
DISCOVERY_SCHEMA = "qq-uia-current-avatar-discovery-v1"
DISCOVERY_MODE = "current_chat_avatar_discovery"
DISCOVERY_STATUS = "AVATAR_CANDIDATES_DISCOVERED"
CAPTURE_API = "WindowsGraphicsCapture"
_FORBIDDEN = frozenset(
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


@dataclass(frozen=True)
class AvatarCandidateRect:
    normalized_x: float
    normalized_y: float
    normalized_width: float
    normalized_height: float


@dataclass(frozen=True)
class DiscoverySnapshot:
    process_id: int
    window_handle: int
    is_maximized: bool
    is_foreground: bool
    is_background: bool
    active_header_digest: str
    candidate_count: int
    structure_digest: str
    candidates: tuple[AvatarCandidateRect, ...]


@dataclass(frozen=True)
class WgcAvatarRequest:
    process_id: int
    window_handle: int
    expected_header_digest: str
    structure_digest: str
    candidates: tuple[AvatarCandidateRect, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": "qq-wgc-avatar-v1",
            "operation": "capture_current_avatar",
            "process_id": self.process_id,
            "window_handle": self.window_handle,
            "expected_header_digest": self.expected_header_digest,
            "structure_digest": self.structure_digest,
            "candidates": [
                {
                    "normalized_x": candidate.normalized_x,
                    "normalized_y": candidate.normalized_y,
                    "normalized_width": candidate.normalized_width,
                    "normalized_height": candidate.normalized_height,
                }
                for candidate in self.candidates
            ],
            "capture_api": CAPTURE_API,
            "desktop_capture": False,
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _fail(code: str) -> None:
    raise WgcAvatarError(code)


def _parse_json(value: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        parsed = value
    else:
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WgcAvatarError("INVALID_OUTPUT") from exc
        if not isinstance(value, str):
            _fail("INVALID_OUTPUT")
        try:
            parsed = json.loads(value.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise WgcAvatarError("INVALID_OUTPUT") from exc
    if not isinstance(parsed, Mapping):
        _fail("INVALID_SCHEMA")
    if _contains_forbidden(parsed):
        _fail("PRIVACY_CONTRACT_FAILED")
    return parsed


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _FORBIDDEN or _contains_forbidden(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden(item) for item in value)
    return False


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("INVALID_DIGEST")
    return value


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("SCOPE_MISMATCH")
    return value


def _validate_discovery_privacy(report: Mapping[str, Any]) -> None:
    privacy = report.get("privacy")
    expected = {
        "exact_hwnd": True,
        "desktop_capture": False,
        "image_bytes_emitted": False,
        "emitted_chat_text": False,
        "emitted_control_names": False,
        "emitted_runtime_ids": False,
        "absolute_screen_coordinates_emitted": False,
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
        _fail("PRIVACY_CONTRACT_FAILED")


def _parse_candidate(value: Any) -> AvatarCandidateRect:
    keys = {
        "normalized_x",
        "normalized_y",
        "normalized_width",
        "normalized_height",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("CANDIDATE_SCHEMA_INVALID")
    ordered_keys = (
        "normalized_x",
        "normalized_y",
        "normalized_width",
        "normalized_height",
    )
    numbers = [value[key] for key in ordered_keys]
    if any(
        isinstance(number, bool) or not isinstance(number, (int, float))
        for number in numbers
    ):
        _fail("CANDIDATE_GEOMETRY_INVALID")
    x, y, width, height = (float(value[key]) for key in ordered_keys)
    if (
        not all(0 <= number <= 1 for number in (x, y, width, height))
        or width <= 0
        or height <= 0
        or x + width > 1
        or y + height > 1
    ):
        _fail("CANDIDATE_GEOMETRY_INVALID")
    return AvatarCandidateRect(x, y, width, height)


def parse_discovery_report(
    output: str | bytes | Mapping[str, Any], *, expected_header_digest: str
) -> DiscoverySnapshot:
    report = _parse_json(output)
    required = {
        "probe_version",
        "mode",
        "succeeded",
        "status",
        "process_id",
        "window_handle",
        "is_maximized",
        "is_foreground",
        "is_background",
        "active_header_digest",
        "candidate_count",
        "structure_digest",
        "candidates",
        "privacy",
    }
    if set(report) != required:
        _fail("DISCOVERY_SCHEMA_INVALID")
    if report["probe_version"] != DISCOVERY_SCHEMA:
        _fail("DISCOVERY_VERSION_MISMATCH")
    if report["mode"] != DISCOVERY_MODE or report["status"] != DISCOVERY_STATUS:
        _fail("DISCOVERY_STATUS_MISMATCH")
    if report["succeeded"] is not True:
        _fail("DISCOVERY_FAILED")
    process_id = _positive_int(report["process_id"])
    window_handle = _positive_int(report["window_handle"])
    if report["is_maximized"] is not True:
        _fail("WINDOW_NOT_MAXIMIZED")
    if report["is_foreground"] is not False or report["is_background"] is not True:
        _fail("QQ_FOREGROUND")
    active_header_digest = _digest(report["active_header_digest"])
    if active_header_digest != _digest(expected_header_digest):
        _fail("HEADER_MISMATCH")
    candidate_count = report["candidate_count"]
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
        _fail("CANDIDATE_COUNT_INVALID")
    if not 2 <= candidate_count <= 256:
        _fail("CANDIDATE_COUNT_INVALID")
    raw_candidates = report["candidates"]
    if not isinstance(raw_candidates, list) or len(raw_candidates) != candidate_count:
        _fail("CANDIDATE_COUNT_INVALID")
    candidates = tuple(_parse_candidate(candidate) for candidate in raw_candidates)
    _validate_discovery_privacy(report)
    return DiscoverySnapshot(
        process_id=process_id,
        window_handle=window_handle,
        is_maximized=True,
        is_foreground=False,
        is_background=True,
        active_header_digest=active_header_digest,
        candidate_count=candidate_count,
        structure_digest=_digest(report["structure_digest"]),
        candidates=candidates,
    )


def require_discovery_unchanged(
    before: DiscoverySnapshot, after: DiscoverySnapshot
) -> None:
    if before != after:
        _fail("DISCOVERY_CHANGED")


def build_wgc_request(snapshot: DiscoverySnapshot) -> WgcAvatarRequest:
    return WgcAvatarRequest(
        process_id=snapshot.process_id,
        window_handle=snapshot.window_handle,
        expected_header_digest=snapshot.active_header_digest,
        structure_digest=snapshot.structure_digest,
        candidates=snapshot.candidates,
    )


def _run_json_command(
    command: Sequence[str], stdin: str, *, runner: Runner, timeout: int = 10
) -> Mapping[str, Any]:
    if not command or any(not isinstance(part, str) or not part for part in command):
        _fail("HELPER_COMMAND_INVALID")
    try:
        completed = runner(
            list(command),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WgcAvatarError("HELPER_TIMEOUT") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise WgcAvatarError("HELPER_INVOKE_FAILED") from exc
    stdout = getattr(completed, "stdout", "")
    if not isinstance(stdout, str):
        _fail("HELPER_OUTPUT_INVALID")
    parsed = _parse_json(stdout)
    if getattr(completed, "returncode", 1) != 0:
        status = parsed.get("status")
        if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
            raise WgcAvatarError(status)
        _fail("HELPER_FAILED")
    return parsed


def invoke_discovery_helper(
    command: Sequence[str],
    expected_header_digest: str,
    *,
    runner: Runner = subprocess.run,
) -> Mapping[str, Any]:
    if not _HASH.fullmatch(expected_header_digest):
        _fail("HELPER_INPUT_INVALID")
    return _run_json_command(
        [*command, "--discover-current-avatar", "--avatar-discovery-authorized"],
        expected_header_digest + "\n",
        runner=runner,
        timeout=60,
    )


def serialize_wgc_stdin(request: WgcAvatarRequest, key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        _fail("HELPER_INPUT_INVALID")
    return (
        base64.b64encode(key).decode("ascii")
        + "\n"
        + json.dumps(
            request.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )


def invoke_wgc_sidecar(
    command: Sequence[str],
    request: WgcAvatarRequest,
    key: bytes,
    *,
    runner: Runner = subprocess.run,
) -> Mapping[str, Any]:
    return _run_json_command(
        command, serialize_wgc_stdin(request, key), runner=runner, timeout=15
    )


def parse_wgc_capture_report(
    output: str | bytes | Mapping[str, Any],
    *,
    process_id: int,
    window_handle: int,
    expected_header_digest: str,
    expected_structure_digest: str,
    expected_candidate_count: int,
) -> dict[str, Any]:
    report = _parse_json(output)
    if report.get("capture_api") != CAPTURE_API:
        _fail("CAPTURE_API_MISMATCH")
    try:
        parsed = parse_avatar_report(
            report,
            window_handle=window_handle,
            process_id=process_id,
            expected_header_digest=expected_header_digest,
        )
    except AvatarIdentityError as exc:
        raise WgcAvatarError(exc.code) from exc
    if parsed["structure_digest"] != expected_structure_digest:
        _fail("STRUCTURE_DIGEST_MISMATCH")
    if parsed["candidate_row_count"] != expected_candidate_count:
        _fail("CANDIDATE_COUNT_MISMATCH")
    if not 2 <= parsed["stable_match_count"] <= expected_candidate_count:
        _fail("AVATAR_ROWS_NOT_STABLE")
    return parsed


def assess_wgc_application(
    application: Mapping[str, Any], parsed: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        evidence = build_avatar_evidence(parsed, application)
        updated = assess_pending_application(
            application, parsed, identity_evidence=evidence
        )
    except AvatarIdentityError as exc:
        raise WgcAvatarError(exc.code) from exc
    updated["status"] = "pending_human_binding_confirmation"
    updated["binding_created"] = False
    updated["automatic_eligible"] = False
    return updated


__all__ = [
    "CAPTURE_API",
    "DISCOVERY_MODE",
    "DISCOVERY_SCHEMA",
    "DISCOVERY_STATUS",
    "AvatarCandidateRect",
    "DiscoverySnapshot",
    "WgcAvatarError",
    "WgcAvatarRequest",
    "assess_wgc_application",
    "build_wgc_request",
    "invoke_discovery_helper",
    "invoke_wgc_sidecar",
    "parse_discovery_report",
    "parse_wgc_capture_report",
    "require_discovery_unchanged",
    "serialize_wgc_stdin",
]
