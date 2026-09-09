"""Q2 read-only adapter for the QQ current-chat capture helper.

The helper's message text is intentionally kept in memory only long enough to
construct :class:`VisibleConversationSnapshot`.  Callers should pass that
snapshot to ``ReadonlyMessageObserver``; timestamps are unknown on this first
baseline, so every message remains pending and cannot become an auto-reply.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .identity import IdentityEvidenceSet
from .observation import (
    ExactHwndCaptureCapabilities,
    VisibleConversationSnapshot,
    VisibleDirection,
    VisibleMessage,
)


class UiaCaptureError(RuntimeError):
    """A malformed, unsafe, or failed current-chat capture."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH = re.compile(r"^[0-9a-f]{64}$")
_PRIVACY = {
    "exact_hwnd": True,
    "desktop_capture_supported": False,
    "changed_window_state": False,
    "write_actions_supported": False,
    "emitted_chat_text": True,
}


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise UiaCaptureError("MISSING_FIELD")
    return value[key]


def _hash(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise UiaCaptureError("INVALID_DIGEST")
    return value


def _parse_report(output: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(output, Mapping):
        value = output
    else:
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UiaCaptureError("INVALID_OUTPUT") from exc
        if not isinstance(output, str):
            raise UiaCaptureError("INVALID_OUTPUT")
        try:
            value = json.loads(output.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise UiaCaptureError("INVALID_OUTPUT") from exc
    if not isinstance(value, Mapping):
        raise UiaCaptureError("INVALID_SCHEMA")
    return value


def _validate_common(report: Mapping[str, Any]) -> None:
    if _required(report, "probe_version") != "qq-uia-current-chat-v1":
        raise UiaCaptureError("VERSION_MISMATCH")
    if _required(report, "succeeded") is not True:
        raise UiaCaptureError("HELPER_FAILED")
    if _required(report, "mode") != "current_chat_capture":
        raise UiaCaptureError("MODE_MISMATCH")
    privacy = _required(report, "privacy")
    if not isinstance(privacy, Mapping) or any(
        privacy.get(key) is not expected for key, expected in _PRIVACY.items()
    ):
        raise UiaCaptureError("PRIVACY_CONTRACT_FAILED")


def parse_current_chat_report(
    output: str | bytes | Mapping[str, Any],
    *,
    requested_window_handle: int | None = None,
    expected_header_digest: str | None = None,
    environment_fingerprint: str | None = None,
    selector_pack_version: str | None = None,
) -> dict[str, Any]:
    """Return a validated in-memory report without exposing its message text."""

    report = _parse_report(output)
    _validate_common(report)
    handle = _required(report, "window_handle")
    process_id = _required(report, "process_id")
    if (
        isinstance(handle, bool)
        or not isinstance(handle, int)
        or handle <= 0
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise UiaCaptureError("INVALID_WINDOW_SCOPE")
    if requested_window_handle is not None and handle != requested_window_handle:
        raise UiaCaptureError("HWND_MISMATCH")
    header = _hash(_required(report, "active_header_digest"))
    right_digest = _hash(_required(report, "right_region_structure_digest"))
    if expected_header_digest is not None and header != expected_header_digest:
        raise UiaCaptureError("HEADER_MISMATCH")
    if environment_fingerprint is not None and not _HASH.fullmatch(
        environment_fingerprint
    ):
        raise UiaCaptureError("ENVIRONMENT_MISMATCH")
    if selector_pack_version is not None and not selector_pack_version.strip():
        raise UiaCaptureError("SELECTOR_MISMATCH")
    captured_at_value = _required(report, "captured_at")
    if not isinstance(captured_at_value, str):
        raise UiaCaptureError("INVALID_CAPTURE_TIME")
    try:
        captured_at = datetime.fromisoformat(captured_at_value)
    except ValueError as exc:
        raise UiaCaptureError("INVALID_CAPTURE_TIME") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise UiaCaptureError("INVALID_CAPTURE_TIME")
    captured_at = captured_at.astimezone(UTC)

    messages = _required(report, "messages")
    if not isinstance(messages, list):
        raise UiaCaptureError("INVALID_MESSAGES")
    # Return only validated fields; callers cannot accidentally persist helper
    # metadata or unknown fields that might contain raw UI text.
    safe_messages: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise UiaCaptureError("INVALID_MESSAGE")
        direction = _required(item, "direction")
        if direction not in {"inbound", "outbound", "unknown"}:
            raise UiaCaptureError("INVALID_DIRECTION")
        text = _required(item, "text")
        watermark = _required(item, "message_watermark")
        source_hash = _hash(_required(item, "source_evidence_hash"))
        if not isinstance(text, str) or len(text) > 100_000:
            raise UiaCaptureError("INVALID_MESSAGE")
        if (
            not isinstance(watermark, str)
            or not watermark.strip()
            or len(watermark) > 512
        ):
            raise UiaCaptureError("INVALID_MESSAGE")
        safe_messages.append(
            {
                "direction": direction,
                "text": text,
                "message_watermark": watermark,
                "source_evidence_hash": source_hash,
                "observer_confidence": item.get("observer_confidence", 0.0),
                "direction_confidence": item.get("direction_confidence", 0.0),
            }
        )
    minimized = _required(report, "is_minimized")
    if not isinstance(minimized, bool):
        raise UiaCaptureError("INVALID_WINDOW_SCOPE")
    return {
        "process_id": process_id,
        "window_handle": handle,
        "environment_fingerprint": environment_fingerprint,
        "selector_pack_version": selector_pack_version,
        "active_header_digest": header,
        "right_region_structure_digest": right_digest,
        "captured_at": captured_at,
        "is_minimized": minimized,
        "messages": safe_messages,
    }


def snapshot_from_report(
    report: str | bytes | Mapping[str, Any],
    *,
    identity_evidence: IdentityEvidenceSet,
    expected_header_digest: str,
    clock: Callable[[], datetime] | None = None,
    ttl: timedelta = timedelta(seconds=5),
) -> VisibleConversationSnapshot:
    """Build a Q2 snapshot with unknown times, guaranteeing pending output."""

    if ttl <= timedelta(0):
        raise UiaCaptureError("INVALID_TTL")
    parsed = parse_current_chat_report(
        report,
        requested_window_handle=identity_evidence.window_handle,
        expected_header_digest=expected_header_digest,
        environment_fingerprint=identity_evidence.environment_fingerprint,
        selector_pack_version=identity_evidence.selector_pack_version,
    )
    if parsed["is_minimized"]:
        raise UiaCaptureError("WINDOW_MINIMIZED")
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None or now.utcoffset() is None:
        raise UiaCaptureError("CLOCK_NOT_TIMEZONE_AWARE")
    now = now.astimezone(UTC)
    captured_at = parsed["captured_at"]
    if captured_at > now + timedelta(seconds=30):
        raise UiaCaptureError("FUTURE_CAPTURE_TIME")
    visible: list[VisibleMessage] = []
    for item in parsed["messages"]:
        try:
            visible.append(
                VisibleMessage(
                    message_watermark=item["message_watermark"],
                    source_evidence_hash=item["source_evidence_hash"],
                    direction=VisibleDirection(item["direction"]),
                    text=item["text"],
                    observed_at=None,
                    observer_confidence=item["observer_confidence"],
                    direction_confidence=item["direction_confidence"],
                    time_confidence=0.0,
                )
            )
        except (TypeError, ValueError) as exc:
            raise UiaCaptureError("INVALID_MESSAGE") from exc
    try:
        return VisibleConversationSnapshot(
            process_id=parsed["process_id"],
            window_handle=parsed["window_handle"],
            captured_at=captured_at,
            expires_at=captured_at + ttl,
            is_minimized=parsed["is_minimized"],
            identity_evidence=identity_evidence,
            conversation_confidence=0.0,
            messages=tuple(visible),
        )
    except (TypeError, ValueError) as exc:
        raise UiaCaptureError("SNAPSHOT_INVALID") from exc


Runner = Callable[..., subprocess.CompletedProcess[str]]


class QQCurrentChatCapturePort:
    """Exact-HWND, read-only capture port implemented with the C# helper."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        identity_evidence: IdentityEvidenceSet,
        header_digest: str,
        runner: Runner = subprocess.run,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not command:
            raise UiaCaptureError("HELPER_COMMAND_INVALID")
        if not _HASH.fullmatch(header_digest):
            raise UiaCaptureError("HEADER_MISMATCH")
        self._command = list(command)
        self._evidence = identity_evidence
        self._header_digest = header_digest
        self._runner = runner
        self._clock = clock

    def capture_capabilities(self) -> ExactHwndCaptureCapabilities:
        return ExactHwndCaptureCapabilities()

    def capture_visible_messages(
        self, window_handle: int
    ) -> VisibleConversationSnapshot:
        if window_handle != self._evidence.window_handle:
            raise UiaCaptureError("HWND_MISMATCH")
        command = [*self._command, "--capture-current-chat", "--capture-authorized"]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise UiaCaptureError("HELPER_INVOKE_FAILED") from exc
        if getattr(completed, "returncode", 1) != 0:
            raise UiaCaptureError("HELPER_FAILED")
        return snapshot_from_report(
            getattr(completed, "stdout", ""),
            identity_evidence=self._evidence,
            expected_header_digest=self._header_digest,
            clock=self._clock,
        )


__all__ = [
    "QQCurrentChatCapturePort",
    "UiaCaptureError",
    "parse_current_chat_report",
    "snapshot_from_report",
]
