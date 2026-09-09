"""Verify-only CLI for the QQ current-chat read-only capture helper."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class CaptureAssessmentError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH = re.compile(r"^[0-9a-f]{64}$")


def _parse(output: str | bytes) -> Mapping[str, Any]:
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CaptureAssessmentError("INVALID_OUTPUT") from exc
    try:
        value = json.loads(output.lstrip("\ufeff"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CaptureAssessmentError("INVALID_OUTPUT") from exc
    if not isinstance(value, Mapping):
        raise CaptureAssessmentError("INVALID_SCHEMA")
    return value


def verify_report(output: str | bytes) -> dict[str, Any]:
    """Produce counts/digests/directions only; never return message text."""

    report = _parse(output)
    if report.get("probe_version") != "qq-uia-current-chat-v1":
        raise CaptureAssessmentError("VERSION_MISMATCH")
    if report.get("mode") != "current_chat_capture":
        raise CaptureAssessmentError("MODE_MISMATCH")
    if report.get("succeeded") is not True:
        raise CaptureAssessmentError("HELPER_FAILED")
    if report.get("is_minimized") is not False:
        raise CaptureAssessmentError("WINDOW_MINIMIZED")
    captured_at_value = report.get("captured_at")
    if not isinstance(captured_at_value, str):
        raise CaptureAssessmentError("INVALID_CAPTURE_TIME")
    try:
        captured_at = datetime.fromisoformat(captured_at_value)
    except ValueError as exc:
        raise CaptureAssessmentError("INVALID_CAPTURE_TIME") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise CaptureAssessmentError("INVALID_CAPTURE_TIME")
    if captured_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=30):
        raise CaptureAssessmentError("FUTURE_CAPTURE_TIME")
    for key in ("active_header_digest", "right_region_structure_digest"):
        value = report.get(key)
        if not isinstance(value, str) or not _HASH.fullmatch(value):
            raise CaptureAssessmentError("INVALID_DIGEST")
    privacy = report.get("privacy")
    expected = {
        "exact_hwnd": True,
        "desktop_capture_supported": False,
        "changed_window_state": False,
        "write_actions_supported": False,
        "emitted_chat_text": True,
    }
    if not isinstance(privacy, Mapping) or any(
        privacy.get(key) is not value for key, value in expected.items()
    ):
        raise CaptureAssessmentError("PRIVACY_CONTRACT_FAILED")
    messages = report.get("messages")
    if not isinstance(messages, list):
        raise CaptureAssessmentError("INVALID_MESSAGES")
    directions: dict[str, int] = {"inbound": 0, "outbound": 0, "unknown": 0}
    source_digests: list[str] = []
    issues: list[str] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise CaptureAssessmentError("INVALID_MESSAGE")
        direction = item.get("direction")
        if direction not in directions:
            raise CaptureAssessmentError("INVALID_DIRECTION")
        directions[direction] += 1
        digest = item.get("source_evidence_hash")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            issues.append("missing_source_evidence_hash")
        else:
            source_digests.append(digest)
        if not isinstance(item.get("text"), str):
            raise CaptureAssessmentError("INVALID_MESSAGE")
        if item.get("observed_at") is not None:
            issues.append("observed_time_ignored")
    return {
        "succeeded": True,
        "verify_only": True,
        "message_count": len(messages),
        "directions": directions,
        "digests": {
            "active_header_digest": report["active_header_digest"],
            "right_region_structure_digest": report["right_region_structure_digest"],
            "source_evidence_hashes": source_digests,
        },
        "issues": issues,
        "pending_time_baseline": True,
    }


def _default_command() -> list[str]:
    helper = Path(__file__).with_name("qq_uia_probe_helper") / "QQ.UiaProbe.csproj"
    return [
        "dotnet",
        "run",
        "--project",
        str(helper),
        "--configuration",
        "Release",
        "--verbosity",
        "quiet",
        "--",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ current-chat verify-only capture")
    parser.add_argument("--helper-command", nargs="+", default=_default_command())
    args = parser.parse_args(argv)
    try:
        completed = subprocess.run(
            [*args.helper_command, "--capture-current-chat", "--capture-authorized"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise CaptureAssessmentError("HELPER_FAILED")
        result = verify_report(completed.stdout)
    except CaptureAssessmentError as exc:
        print(json.dumps({"succeeded": False, "error_code": exc.code}))
        return 2
    except Exception:  # noqa: BLE001 - verify-only gate fails closed
        print(json.dumps({"succeeded": False, "error_code": "INTERNAL_ERROR"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["CaptureAssessmentError", "main", "verify_report"]
