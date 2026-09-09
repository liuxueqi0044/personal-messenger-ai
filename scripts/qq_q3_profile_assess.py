"""Verify-only Q3 profile evidence assessment CLI.

The DPAPI-backed key is supplied to the helper on stdin as base64 text.  It is
never placed in argv, logs, JSON output, or a pending application.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.qq.live_driver.profile_identity import (
    ProfileIdentityError,
    assess_pending_application,
    build_identity_evidence,
    parse_profile_report,
)
from messenger_ai.observability import WindowsDPAPISecretStore


class Q3CliError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Runner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_SECRET_NAME = "q3.profile-identity.hmac"
_SAFE_HELPER_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def get_live_scope(profile_path: Path) -> dict[str, Any]:
    """Run the existing Q0/Q1 read-only assessment for the certified profile."""

    try:
        from messenger_ai.adapters.qq.live_driver import (
            CertifiedQQProfile,
            QQReadOnlyFrontHalf,
        )
        from scripts.qq_live_readonly_assess import _run_probe

        profile = CertifiedQQProfile.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
        assessment = QQReadOnlyFrontHalf(
            profile=profile, fixture_suite_version="qq-uia-readonly-v2"
        ).assess_probe(_run_probe(5000))
    except Exception as exc:
        raise Q3CliError("LIVE_SCOPE_FAILED") from exc
    if not assessment.observation_ready:
        raise Q3CliError("LIVE_SCOPE_NOT_READY")
    return {
        "process_id": assessment.runtime.process_id,
        "window_handle": assessment.runtime.window_handle,
        "environment_fingerprint": assessment.environment.fingerprint.digest,
        "selector_pack_version": assessment.selector_pack_version,
    }


def invoke_profile_helper(
    command: Sequence[str],
    expected_header_digest: str,
    key: bytes,
    *,
    runner: Runner = subprocess.run,
) -> Mapping[str, Any]:
    if not command or not isinstance(key, bytes) or len(key) < 32:
        raise Q3CliError("HELPER_INPUT_INVALID")
    if any(not isinstance(part, str) or not part for part in command):
        raise Q3CliError("HELPER_COMMAND_INVALID")
    helper_command = [
        *command,
        "--capture-current-identity",
        "--identity-authorized",
    ]
    try:
        completed = runner(
            helper_command,
            input=expected_header_digest
            + "\n"
            + base64.b64encode(key).decode("ascii")
            + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Q3CliError("HELPER_INVOKE_FAILED") from exc
    stdout = getattr(completed, "stdout", "")
    if not isinstance(stdout, str):
        raise Q3CliError("HELPER_OUTPUT_INVALID")
    try:
        value = json.loads(stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise Q3CliError("HELPER_OUTPUT_INVALID") from exc
    if not isinstance(value, Mapping):
        raise Q3CliError("HELPER_OUTPUT_INVALID")
    if getattr(completed, "returncode", 1) != 0:
        status = value.get("status")
        if isinstance(status, str) and _SAFE_HELPER_STATUS.fullmatch(status):
            raise Q3CliError(status)
        raise Q3CliError("HELPER_FAILED")
    return value


def save_application(application: Mapping[str, Any], destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(application, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        raise Q3CliError("APPLICATION_SAVE_FAILED") from exc


def load_pending_application(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Q3CliError("APPLICATION_READ_FAILED") from exc
    if not isinstance(value, dict):
        raise Q3CliError("APPLICATION_SCHEMA_INVALID")
    if value.get("schema_version") != "qq-q3-binding-application-v1":
        raise Q3CliError("APPLICATION_SCHEMA_INVALID")
    if value.get("status") != "pending_human_binding":
        raise Q3CliError("APPLICATION_NOT_PENDING")
    if value.get("binding_created") is not False:
        raise Q3CliError("ACTIVE_BINDING_FORBIDDEN")
    if value.get("automatic_eligible") is not False:
        raise Q3CliError("AUTOMATIC_ELIGIBILITY_FORBIDDEN")
    if value.get("local_contact_id") not in (None,):
        raise Q3CliError("LOCAL_CONTACT_FORBIDDEN")
    return value


def validate_application_scope(
    application: Mapping[str, Any],
    *,
    environment_fingerprint: str,
    selector_pack_version: str,
    expected_header_digest: str,
) -> None:
    if application.get("environment_fingerprint") != environment_fingerprint:
        raise Q3CliError("APPLICATION_ENVIRONMENT_MISMATCH")
    if application.get("selector_pack_version") != selector_pack_version:
        raise Q3CliError("APPLICATION_SELECTOR_MISMATCH")
    right = application.get("right_region_evidence")
    if not isinstance(right, Mapping):
        raise Q3CliError("APPLICATION_HEADER_MISSING")
    if right.get("active_header_digest") != expected_header_digest:
        raise Q3CliError("APPLICATION_HEADER_MISMATCH")


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


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ Q3 profile verify-only assessment")
    parser.add_argument("--helper-command", nargs="+", default=_default_command())
    default_vault = Path(__file__).resolve().parents[1] / "state" / "secrets"
    parser.add_argument("--vault", type=Path, default=default_vault)
    parser.add_argument("--application", type=Path, required=True)
    default_profile = (
        Path(__file__).resolve().parents[1]
        / "profiles"
        / "qq"
        / "qq-9.9.26-local-certified.json"
    )
    parser.add_argument("--profile", type=Path, default=default_profile)
    args = parser.parse_args(argv)
    try:
        application = load_pending_application(args.application)
        scope = get_live_scope(args.profile)
        environment_fingerprint = scope["environment_fingerprint"]
        selector_pack_version = scope["selector_pack_version"]
        expected_header_digest = application["right_region_evidence"][
            "active_header_digest"
        ]
        validate_application_scope(
            application,
            environment_fingerprint=environment_fingerprint,
            selector_pack_version=selector_pack_version,
            expected_header_digest=expected_header_digest,
        )
        store = WindowsDPAPISecretStore(args.vault)
        key = store.get_or_create_hmac_key(DEFAULT_SECRET_NAME)
        report = invoke_profile_helper(args.helper_command, expected_header_digest, key)
        parsed = parse_profile_report(
            report,
            window_handle=scope["window_handle"],
            expected_process_id=scope["process_id"],
            environment_fingerprint=environment_fingerprint,
            selector_pack_version=selector_pack_version,
            expected_header_digest=expected_header_digest,
        )
        identity_evidence = build_identity_evidence(parsed, application)
        updated = assess_pending_application(
            application,
            parsed,
            identity_evidence=identity_evidence,
        )
        updated["status"] = "pending_human_binding_confirmation"
        updated["binding_created"] = False
        updated["automatic_eligible"] = False
        save_application(updated, args.application)
        _emit(
            {
                "succeeded": True,
                "status": updated["status"],
                "application_updated": True,
                "candidate_count": parsed["candidate_count"],
                "active_header_digest": parsed["active_header_digest"],
                "right_region_structure_digest": parsed[
                    "right_region_structure_digest"
                ],
                "evidence_hash": updated["identity_evidence_hash"],
                "reasons": updated["binding_evidence_assessment"]["reason_codes"],
            }
        )
        return 0
    except (ProfileIdentityError, Q3CliError) as exc:
        _emit({"succeeded": False, "error_code": exc.code})
        return 2
    except Exception:  # noqa: BLE001 - verification must fail closed
        _emit({"succeeded": False, "error_code": "INTERNAL_ERROR"})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_SECRET_NAME",
    "Q3CliError",
    "invoke_profile_helper",
    "main",
    "save_application",
]
