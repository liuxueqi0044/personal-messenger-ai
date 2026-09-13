"""Guest-only exact-HWND profile evidence probe; never creates a binding."""
from __future__ import annotations

import argparse, base64, hashlib, json, re, subprocess
from pathlib import Path

from messenger_ai.adapters.qq.live_driver.profile_identity import (
    ProfileIdentityError, parse_guest_foreground_profile_report,
)
from messenger_ai.observability import WindowsDPAPISecretStore


class GuestProfileProbeError(RuntimeError):
    def __init__(self, code: str, diagnostic: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic or {}


def _diagnostic(stage: str, completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    stdout = (completed.stdout or "").encode("utf-8")
    stderr = (completed.stderr or "").encode("utf-8")
    return {
        "stage": stage,
        "exit_code": completed.returncode,
        "stdout_bytes": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def _run(command: list[str], *, stage: str, stdin: str | None = None) -> dict:
    try:
        completed = subprocess.run(command, input=stdin, capture_output=True, text=True,
                                   timeout=60, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise GuestProfileProbeError("HELPER_TIMEOUT", {"stage": stage}) from exc
    except OSError as exc:
        raise GuestProfileProbeError("HELPER_START_FAILED", {"stage": stage}) from exc
    diagnostic = _diagnostic(stage, completed)
    try:
        value = json.loads(completed.stdout.lstrip("\ufeff"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise GuestProfileProbeError("HELPER_INVALID_OUTPUT", diagnostic) from exc
    if not isinstance(value, dict):
        raise GuestProfileProbeError("HELPER_INVALID_OUTPUT", diagnostic)
    if completed.returncode != 0 or value.get("succeeded") is not True:
        status = value.get("status")
        safe = status if isinstance(status, str) and status.replace("_", "").isalnum() else "HELPER_FAILED"
        raise GuestProfileProbeError(safe, diagnostic)
    return value


def capture(helper: str, *, pid: int, hwnd: int, vault: Path,
            environment_fingerprint: str, selector_pack_version: str) -> dict:
    target = [helper, "--target-qq-pid", str(pid), "--target-qq-hwnd", str(hwnd)]
    header = _run(target + ["--inspect-guest-current-header", "--guest-header-authorized"], stage="header")
    if (header.get("probe_version") != "qq-uia-guest-header-v1" or
        header.get("mode") != "guest_foreground_header_inspect" or
        header.get("status") != "HEADER_CAPTURED" or
        header.get("header_candidate_count") != 1 or
        header.get("process_id") != pid or header.get("window_handle") != hwnd):
        raise GuestProfileProbeError("HEADER_SCOPE_MISMATCH")
    if header.get("guest_environment") != {"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"}:
        raise GuestProfileProbeError("GUEST_ENVIRONMENT_MISMATCH")
    privacy = header.get("privacy")
    if not isinstance(privacy, dict) or any(privacy.get(key) is not expected for key, expected in {
        "exact_hwnd": True, "emitted_control_names": False, "emitted_chat_text": False,
        "mouse_input_used": False, "keyboard_input_used": False, "clipboard_used": False,
        "transient_navigation_performed": False, "composer_or_send_accessed": False}.items()):
        raise GuestProfileProbeError("HEADER_PRIVACY_CONTRACT_FAILED")
    digest = header.get("active_header_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise GuestProfileProbeError("HEADER_DIGEST_INVALID")
    right_digest = header.get("right_region_structure_digest")
    if not isinstance(right_digest, str) or re.fullmatch(r"[0-9a-f]{64}", right_digest) is None:
        raise GuestProfileProbeError("RIGHT_REGION_DIGEST_INVALID")
    key = WindowsDPAPISecretStore(vault).get_or_create_hmac_key("qq.identity.hmac")
    try:
        raw = _run(target + ["--capture-current-identity-guest-foreground", "--guest-identity-authorized"], stage="profile",
                   stdin=digest + "\n" + base64.b64encode(key).decode("ascii") + "\n")
    finally:
        key = b""
    return parse_guest_foreground_profile_report(raw, window_handle=hwnd,
        expected_process_id=pid, environment_fingerprint=environment_fingerprint,
        selector_pack_version=selector_pack_version, expected_header_digest=digest,
        expected_right_region_structure_digest=right_digest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True); parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--hwnd", type=int, required=True); parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--environment-fingerprint", required=True); parser.add_argument("--selector-pack-version", required=True)
    args = parser.parse_args()
    try:
        result = capture(args.helper, pid=args.pid, hwnd=args.hwnd, vault=args.vault,
                         environment_fingerprint=args.environment_fingerprint,
                         selector_pack_version=args.selector_pack_version)
        print(json.dumps(result, sort_keys=True)); return 0
    except (GuestProfileProbeError, ProfileIdentityError, subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
        code = str(exc) if isinstance(exc, (GuestProfileProbeError, ProfileIdentityError)) else type(exc).__name__.upper()
        payload: dict[str, object] = {"succeeded": False, "status": code}
        if isinstance(exc, GuestProfileProbeError) and exc.diagnostic:
            payload["diagnostic"] = exc.diagnostic
        print(json.dumps(payload, sort_keys=True)); return 2


if __name__ == "__main__": raise SystemExit(main())
