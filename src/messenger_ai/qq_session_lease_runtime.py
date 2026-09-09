"""Production read-only scope composition for a manual QQ session lease."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from messenger_ai.adapters.qq.live_driver import (
    CertifiedQQProfile,
    CurrentSessionInspectionError,
    CurrentSessionSnapshot,
    FrontHalfAssessment,
    LiveSessionScope,
    QQReadOnlyFrontHalf,
    invoke_current_session_helper,
    parse_current_session_report,
)


class QQSessionScopeError(RuntimeError):
    """A fixed-code failure that does not reveal file paths or QQ UI data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_APPLICATION_BYTES = 1_048_576
ProbeReader = Callable[[], Mapping[str, Any] | str | bytes]
SessionInspector = Callable[[Sequence[str], str], CurrentSessionSnapshot]


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise QQSessionScopeError(code)
    return value


def _load_pending_application(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise QQSessionScopeError("APPLICATION_READ_FAILED") from None
    if not payload or len(payload) > _MAX_APPLICATION_BYTES:
        raise QQSessionScopeError("APPLICATION_SCHEMA_INVALID")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise QQSessionScopeError("APPLICATION_SCHEMA_INVALID") from None
    if not isinstance(value, dict):
        raise QQSessionScopeError("APPLICATION_SCHEMA_INVALID")
    if (
        value.get("schema_version") != "qq-q3-binding-application-v1"
        or value.get("platform") != "qq"
        or value.get("status") != "pending_human_binding"
        or value.get("binding_created") is not False
        or value.get("automatic_eligible") is not False
        or value.get("local_contact_id") is not None
    ):
        raise QQSessionScopeError("APPLICATION_NOT_PENDING")
    confirmation = value.get("human_confirmation")
    if not isinstance(confirmation, Mapping) or (
        confirmation.get("action") != "bind"
        or confirmation.get("approved") is not True
        or confirmation.get("required") is not True
        or confirmation.get("scope") != "conversation_selection_only"
    ):
        raise QQSessionScopeError("APPLICATION_CONFIRMATION_INVALID")
    _digest(value.get("environment_fingerprint"), "APPLICATION_ENVIRONMENT_INVALID")
    selector = value.get("selector_pack_version")
    if not isinstance(selector, str) or not re.fullmatch(r"q1:[0-9a-f]{64}", selector):
        raise QQSessionScopeError("APPLICATION_SELECTOR_INVALID")
    right = value.get("right_region_evidence")
    if not isinstance(right, Mapping):
        raise QQSessionScopeError("APPLICATION_HEADER_INVALID")
    _digest(right.get("active_header_digest"), "APPLICATION_HEADER_INVALID")
    _digest(right.get("structure_digest"), "APPLICATION_STRUCTURE_INVALID")
    return value, hashlib.sha256(payload).hexdigest()


def _default_inspector(
    command: Sequence[str], expected_header_digest: str
) -> CurrentSessionSnapshot:
    report = invoke_current_session_helper(command, expected_header_digest)
    return parse_current_session_report(
        report, expected_header_digest=expected_header_digest
    )


class QQManualSessionScopeReader:
    """Revalidates Q0/Q1, the pending application, and the active chat header.

    The reader is deliberately stateless.  The lease manager compares complete
    snapshots across prepare, confirm, and status calls and invalidates on any
    process, HWND, environment, selector, header, structure, or file drift.
    """

    def __init__(
        self,
        *,
        profile: CertifiedQQProfile,
        application_path: str | Path,
        helper_command: Sequence[str],
        read_probe: ProbeReader,
        inspect_session: SessionInspector = _default_inspector,
        fixture_suite_version: str = "qq-uia-readonly-v2",
        allow_application_selector_rebaseline: bool = False,
    ) -> None:
        if not helper_command or any(
            not isinstance(item, str) or not item for item in helper_command
        ):
            raise ValueError("helper_command is required")
        if not fixture_suite_version.strip():
            raise ValueError("fixture_suite_version is required")
        self._profile = profile
        self._application_path = Path(application_path)
        self._helper_command = tuple(helper_command)
        self._read_probe = read_probe
        self._inspect_session = inspect_session
        self._allow_application_selector_rebaseline = (
            allow_application_selector_rebaseline
        )
        self._front_half = QQReadOnlyFrontHalf(
            profile=profile,
            fixture_suite_version=fixture_suite_version,
        )

    def __call__(self) -> LiveSessionScope:
        try:
            application, application_digest = _load_pending_application(
                self._application_path
            )
            assessment = self._read_ready_assessment()
            environment = assessment.environment.fingerprint.digest
            selector = assessment.selector_pack_version
            if application["environment_fingerprint"] != environment:
                raise QQSessionScopeError("APPLICATION_ENVIRONMENT_MISMATCH")
            if (
                application["selector_pack_version"] != selector
                and not self._allow_application_selector_rebaseline
            ):
                raise QQSessionScopeError("APPLICATION_SELECTOR_MISMATCH")
            right = application["right_region_evidence"]
            expected_header = _digest(
                right["active_header_digest"], "APPLICATION_HEADER_INVALID"
            )
            inspected = self._inspect_stable_session(expected_header)
            if not isinstance(inspected, CurrentSessionSnapshot):
                raise QQSessionScopeError("SESSION_INSPECTION_INVALID")
            if (
                inspected.process_id != assessment.runtime.process_id
                or inspected.window_handle != assessment.runtime.window_handle
            ):
                raise QQSessionScopeError("SESSION_SCOPE_DRIFT")
            return LiveSessionScope(
                process_id=inspected.process_id,
                process_started_at=inspected.process_started_at,
                window_handle=inspected.window_handle,
                header_digest=inspected.active_header_digest,
                structure_digest=inspected.structure_digest,
                environment_fingerprint=environment,
                selector_pack_version=selector,
                pending_application_digest=application_digest,
            )
        except QQSessionScopeError:
            raise
        except Exception:  # noqa: BLE001 - public boundary returns fixed codes only
            # No exception text or source data crosses the lease-service boundary.
            raise QQSessionScopeError("LIVE_SCOPE_FAILED") from None

    def _read_ready_assessment(self) -> FrontHalfAssessment:
        """Retry one transient Windows presentation sample without mutating QQ."""

        for _attempt in range(2):
            assessment = self._front_half.assess_probe(self._read_probe())
            if assessment.observation_ready:
                return assessment
        raise QQSessionScopeError("LIVE_SCOPE_NOT_READY")

    def _inspect_stable_session(self, expected_header: str) -> CurrentSessionSnapshot:
        """Retry only the known transient maximize-state sample once."""

        for attempt in range(2):
            try:
                return self._inspect_session(self._helper_command, expected_header)
            except CurrentSessionInspectionError as exc:
                if exc.code != "WINDOW_STATE_NOT_CERTIFIED" or attempt == 1:
                    raise
        raise QQSessionScopeError("SESSION_INSPECTION_INVALID")


__all__ = ["QQManualSessionScopeReader", "QQSessionScopeError"]
