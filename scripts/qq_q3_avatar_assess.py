"""Verify-only Q3 avatar identity assessment for a pending QQ application."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.qq.live_driver.avatar_identity import (
    AvatarIdentityError,
    assess_pending_application,
    build_avatar_evidence,
    parse_avatar_report,
)
from messenger_ai.observability import WindowsDPAPISecretStore
from scripts.qq_q3_profile_assess import (
    get_live_scope,
    load_pending_application,
    save_application,
    validate_application_scope,
)


class Q3AvatarCliError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Runner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_SECRET_NAME = "q3.profile-identity.hmac"
_SAFE_HELPER_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def invoke_avatar_helper(
    command: Sequence[str],
    expected_header_digest: str,
    key: bytes,
    *,
    runner: Runner = subprocess.run,
) -> Mapping[str, Any]:
    """Invoke the authorized avatar helper with header/key on stdin only."""

    if not command or not isinstance(key, bytes) or len(key) < 32:
        raise Q3AvatarCliError("HELPER_INPUT_INVALID")
    if any(not isinstance(part, str) or not part for part in command):
        raise Q3AvatarCliError("HELPER_COMMAND_INVALID")
    helper_command = [
        *command,
        "--capture-current-avatar",
        "--avatar-authorized",
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
        raise Q3AvatarCliError("HELPER_INVOKE_FAILED") from exc
    stdout = getattr(completed, "stdout", "")
    if not isinstance(stdout, str):
        raise Q3AvatarCliError("HELPER_OUTPUT_INVALID")
    try:
        value = json.loads(stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise Q3AvatarCliError("HELPER_OUTPUT_INVALID") from exc
    if not isinstance(value, Mapping):
        raise Q3AvatarCliError("HELPER_OUTPUT_INVALID")
    if getattr(completed, "returncode", 1) != 0:
        status = value.get("status")
        if isinstance(status, str) and _SAFE_HELPER_STATUS.fullmatch(status):
            raise Q3AvatarCliError(status)
        raise Q3AvatarCliError("HELPER_FAILED")
    return value


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
    # This projection is intentionally assembled from parser-safe fields only.
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QQ Q3 avatar verify-only identity assessment"
    )
    parser.add_argument("--helper-command", nargs="+", default=_default_command())
    parser.add_argument(
        "--vault",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "state" / "secrets",
    )
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "profiles"
        / "qq"
        / "qq-9.9.26-local-certified.json",
    )
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
        report = invoke_avatar_helper(args.helper_command, expected_header_digest, key)
        parsed = parse_avatar_report(
            report,
            window_handle=scope["window_handle"],
            process_id=scope["process_id"],
            expected_header_digest=expected_header_digest,
        )
        # Add live scope only after helper self-evidence has passed.  It cannot
        # be taken from helper JSON, which prevents version/scope drift.
        parsed = {
            **parsed,
            "environment_fingerprint": environment_fingerprint,
            "selector_pack_version": selector_pack_version,
        }
        identity_evidence = build_avatar_evidence(parsed, application)
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
                "candidate_row_count": parsed["candidate_row_count"],
                "stable_match_count": parsed["stable_match_count"],
                "active_header_digest": parsed["active_header_digest"],
                "structure_digest": parsed["structure_digest"],
                "evidence_hash": updated["identity_evidence_hash"],
                "reasons": updated["binding_evidence_assessment"]["reason_codes"],
            }
        )
        return 0
    except (AvatarIdentityError, Q3AvatarCliError) as exc:
        _emit({"succeeded": False, "error_code": exc.code})
        return 2
    except Exception:  # noqa: BLE001 - verification must fail closed
        _emit({"succeeded": False, "error_code": "INTERNAL_ERROR"})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_SECRET_NAME",
    "Q3AvatarCliError",
    "invoke_avatar_helper",
    "main",
]
