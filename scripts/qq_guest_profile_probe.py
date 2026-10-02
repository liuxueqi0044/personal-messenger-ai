"""Guest profile probe CLI; capture never creates keys, bindings, or leases."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import subprocess

from messenger_ai.adapters.qq.vm_driver.profile_identity import (
    ProfileCaptureError, capture_current_profile, run_profile_helper,
)
from messenger_ai.observability import WindowsDPAPISecretStore


GuestProfileProbeError = ProfileCaptureError


def _run(command: list[str], *, stage: str, stdin: str | None = None) -> dict:
    """Compatibility entry point; production capture uses the bounded runner."""
    return run_profile_helper(command, stage=stage, timeout=60,
                              stdin=stdin.encode("utf-8") if stdin is not None else None,
                              runner=subprocess.run)


def capture(helper: str, *, pid: int, hwnd: int, vault: Path,
            environment_fingerprint: str, selector_pack_version: str,
            key_name: str = "qq.identity.hmac", deadline: datetime | None = None,
            timeout_seconds: float = 90) -> dict:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ProfileCaptureError("CAPTURE_INPUT_INVALID")
    return capture_current_profile(
        helper, pid=pid, hwnd=hwnd, vault=vault, key_name=key_name,
        environment_fingerprint=environment_fingerprint,
        selector_pack_version=selector_pack_version,
        deadline=deadline if deadline is not None else datetime.now(UTC) + timedelta(seconds=timeout_seconds),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--hwnd", type=int)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--environment-fingerprint")
    parser.add_argument("--selector-pack-version")
    parser.add_argument("--key-name", default="qq.identity.hmac")
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--initialize-key", action="store_true",
                        help="Initialize the identity key only; do not inspect or open QQ UI.")
    args = parser.parse_args()
    if not args.initialize_key and any(value is None for value in (
        args.helper, args.pid, args.hwnd, args.environment_fingerprint, args.selector_pack_version,
    )):
        parser.error("capture requires --helper, --pid, --hwnd, --environment-fingerprint and --selector-pack-version")
    try:
        if args.initialize_key:
            WindowsDPAPISecretStore(args.vault).get_or_create_hmac_key(args.key_name)
            print(json.dumps({"succeeded": True, "status": "IDENTITY_KEY_READY"}, sort_keys=True))
            return 0
        result = capture(
            args.helper, pid=args.pid, hwnd=args.hwnd, vault=args.vault,
            environment_fingerprint=args.environment_fingerprint,
            selector_pack_version=args.selector_pack_version,
            key_name=args.key_name, timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ProfileCaptureError, OSError, ValueError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, ProfileCaptureError) else type(exc).__name__.upper()
        payload: dict[str, object] = {"succeeded": False, "status": code}
        if isinstance(exc, ProfileCaptureError) and exc.diagnostic:
            payload["diagnostic"] = exc.diagnostic
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
