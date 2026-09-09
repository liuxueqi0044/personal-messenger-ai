"""Verify-only three-stage Q3 WGC avatar orchestration."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.qq.live_driver.wgc_avatar import (
    WgcAvatarError,
    assess_wgc_application,
    build_wgc_request,
    invoke_discovery_helper,
    invoke_wgc_sidecar,
    parse_discovery_report,
    parse_wgc_capture_report,
    require_discovery_unchanged,
)
from messenger_ai.observability import WindowsDPAPISecretStore
from scripts.qq_q3_profile_assess import (
    get_live_scope,
    load_pending_application,
    save_application,
    validate_application_scope,
)

DEFAULT_SECRET_NAME = "q3.profile-identity.hmac"


def _default_discovery_command() -> list[str]:
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


def _default_sidecar_command() -> list[str]:
    sidecar = (
        PROJECT_ROOT
        / "scripts"
        / "qq_wgc_avatar_sidecar_managed"
        / "bin"
        / "Release"
        / "net10.0-windows10.0.26100.0"
        / "QQ.WgcAvatar.dll"
    )
    return ["dotnet", str(sidecar)]


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ Q3 WGC avatar assessment")
    parser.add_argument(
        "--discovery-command", nargs="+", default=_default_discovery_command()
    )
    parser.add_argument(
        "--sidecar-command", nargs="+", default=_default_sidecar_command()
    )
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=PROJECT_ROOT / "profiles" / "qq" / "qq-9.9.26-local-certified.json",
    )
    parser.add_argument(
        "--vault", type=Path, default=PROJECT_ROOT / "state" / "secrets"
    )
    args = parser.parse_args(argv)
    try:
        application = load_pending_application(args.application)
        scope = get_live_scope(args.profile)
        environment = scope["environment_fingerprint"]
        selector = scope["selector_pack_version"]
        expected_header = application["right_region_evidence"]["active_header_digest"]
        validate_application_scope(
            application,
            environment_fingerprint=environment,
            selector_pack_version=selector,
            expected_header_digest=expected_header,
        )

        # Stage 1: Terra UIA discovery. It receives only the expected header.
        before_report = invoke_discovery_helper(args.discovery_command, expected_header)
        before = parse_discovery_report(
            before_report, expected_header_digest=expected_header
        )
        if (
            before.process_id != scope["process_id"]
            or before.window_handle != scope["window_handle"]
        ):
            raise WgcAvatarError("SCOPE_MISMATCH")
        request = build_wgc_request(before)

        key = WindowsDPAPISecretStore(args.vault).get_or_create_hmac_key(
            DEFAULT_SECRET_NAME
        )
        # Stage 2: native WGC sidecar. Its stdout is a capture report only.
        capture_report = invoke_wgc_sidecar(args.sidecar_command, request, key)
        parsed = parse_wgc_capture_report(
            capture_report,
            process_id=before.process_id,
            window_handle=before.window_handle,
            expected_header_digest=before.active_header_digest,
            expected_structure_digest=before.structure_digest,
            expected_candidate_count=before.candidate_count,
        )

        # Stage 3: Terra discovery again; no application update until stable.
        after_report = invoke_discovery_helper(args.discovery_command, expected_header)
        after = parse_discovery_report(
            after_report, expected_header_digest=expected_header
        )
        require_discovery_unchanged(before, after)

        parsed = {
            **parsed,
            "environment_fingerprint": environment,
            "selector_pack_version": selector,
        }
        updated = assess_wgc_application(application, parsed)
        save_application(updated, args.application)
        _emit(
            {
                "succeeded": True,
                "application_updated": True,
                "status": updated["status"],
                "capture_api": "WindowsGraphicsCapture",
                "candidate_row_count": parsed["candidate_row_count"],
                "stable_match_count": parsed["stable_match_count"],
                "active_header_digest": parsed["active_header_digest"],
                "structure_digest": parsed["structure_digest"],
                "evidence_hash": updated["identity_evidence_hash"],
                "reasons": updated["binding_evidence_assessment"]["reason_codes"],
            }
        )
        return 0
    except WgcAvatarError as exc:
        _emit({"succeeded": False, "error_code": exc.code})
        return 2
    except Exception:  # noqa: BLE001 - verify-only must fail closed
        _emit({"succeeded": False, "error_code": "INTERNAL_ERROR"})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["DEFAULT_SECRET_NAME", "main"]
