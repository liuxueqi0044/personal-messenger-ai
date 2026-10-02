"""Deadline-bound normal-UI profile capture; no binding or lease authority.

Only the explicit initialization CLI may create a key. Production capture reads
an existing DPAPI key, sends it over stdin, and returns the existing strict
profile parser's redacted projection. Opening/closing the profile is a UI action:
call outside any cached UIA read phase and retain desktop ownership on failure.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from messenger_ai.adapters.qq.live_driver.profile_identity import (
    ProfileIdentityError, parse_guest_foreground_profile_report,
)
from messenger_ai.observability.secrets import (
    SecretNotFoundError, SecretStoreError, WindowsDPAPISecretStore,
)


MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GUEST = {"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"}
_HELPER_FAILURES = frozenset({
    "GUEST_HEADER_ENVIRONMENT_NOT_CERTIFIED", "ACTIVE_HEADER_AMBIGUOUS",
    "WINDOW_STATE_NOT_CERTIFIED", "GUEST_FOREGROUND_ENVIRONMENT_NOT_CERTIFIED",
    "QQ_FOREGROUND", "CHAT_SHELL_NOT_CERTIFIED", "TREE_TRUNCATED", "IDENTITY_STDIN_INVALID",
    "ACTIVE_HEADER_DIGEST_MISMATCH", "HEADER_INVOKE_UNAVAILABLE",
    "PROFILE_WINDOW_PRECONDITION_FAILED", "HEADER_INVOKE_FAILED",
    "PROFILE_VIEW_NOT_SEPARATE_WINDOW", "PROFILE_WINDOW_AMBIGUOUS",
    "PROFILE_UIA_ROOT_UNAVAILABLE", "RESTORATION_FAILED", "FOREGROUND_CHANGED",
    "QQ_WINDOW_NOT_RESTORED", "ORIGINAL_CONVERSATION_NOT_RESTORED", "PENDING_NO_STABLE_SIGNAL",
})


class ProfileCaptureError(RuntimeError):
    """Fixed public code and bounded content-free diagnostics only."""

    def __init__(self, code: str, diagnostic: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic or {}


class _OutputLimit(Exception):
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata


def _bounded_run(command: list[str], *, input: bytes | None, timeout: float, **_kwargs):
    """Drain pipes concurrently with capped memory; kill on either output limit."""
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    exceeded = threading.Event()
    failed = threading.Event()
    captures: dict[str, dict[str, Any]] = {
        name: {"buffer": bytearray(), "count": 0, "hash": hashlib.sha256()}
        for name in ("stdout", "stderr")
    }

    def stop() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def read(name: str, pipe, limit: int) -> None:
        state = captures[name]
        try:
            while chunk := pipe.read1(4096):
                state["count"] += len(chunk)
                state["hash"].update(chunk)
                available = max(0, limit - len(state["buffer"]))
                state["buffer"].extend(chunk[:available])
                if state["count"] > limit:
                    exceeded.set()
                    stop()
                    break
        except OSError:
            failed.set()
            stop()
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=read, args=("stdout", process.stdout, MAX_STDOUT_BYTES), daemon=True),
        threading.Thread(target=read, args=("stderr", process.stderr, MAX_STDERR_BYTES), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        if input is not None:
            try:
                process.stdin.write(input)
                process.stdin.close()
            except BrokenPipeError:
                pass
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        process.wait(timeout=remaining)
        for reader in readers:
            reader.join(max(0, timeout - (time.monotonic() - started)))
        if any(reader.is_alive() for reader in readers):
            raise subprocess.TimeoutExpired(command, timeout)
        if exceeded.is_set():
            metadata: dict[str, object] = {"exit_code": process.returncode, "output_limit_exceeded": True}
            for name, state in captures.items():
                metadata[f"{name}_bytes"] = state["count"]
                metadata[f"{name}_sha256"] = state["hash"].hexdigest()
            raise _OutputLimit(metadata)
        if failed.is_set():
            raise OSError("helper pipe read failed")
        return subprocess.CompletedProcess(command, process.returncode,
            bytes(captures["stdout"]["buffer"]), bytes(captures["stderr"]["buffer"]))
    finally:
        if process.poll() is None:
            stop()
        # Reap the exact launched helper. No UI cleanup or retry is inferred.
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for reader in readers:
            reader.join(timeout=0.1)
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass


def _output_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    raise ProfileCaptureError("HELPER_INVALID_OUTPUT")


def run_profile_helper(
    command: list[str], *, stage: str, timeout: float, stdin: bytes | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, object]:
    """One bounded helper call; also used by the legacy CLI's private wrapper."""
    if stage not in {"header", "profile"} or not math.isfinite(timeout) or timeout <= 0:
        raise ProfileCaptureError("CAPTURE_DEADLINE_EXHAUSTED")
    try:
        completed = (runner or _bounded_run)(
            command, input=stdin, capture_output=True, text=False, timeout=timeout,
            check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except _OutputLimit as exc:
        raise ProfileCaptureError("HELPER_OUTPUT_LIMIT", {"stage": stage, **exc.metadata}) from None
    except subprocess.TimeoutExpired:
        raise ProfileCaptureError("HELPER_TIMEOUT", {"stage": stage}) from None
    except OSError:
        raise ProfileCaptureError("HELPER_START_FAILED", {"stage": stage}) from None
    stdout, stderr = _output_bytes(completed.stdout), _output_bytes(completed.stderr)
    diagnostic = {
        "stage": stage, "exit_code": completed.returncode,
        "stdout_bytes": len(stdout), "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }
    if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
        raise ProfileCaptureError("HELPER_OUTPUT_LIMIT", diagnostic)
    try:
        value = json.loads(stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ProfileCaptureError("HELPER_INVALID_OUTPUT", diagnostic) from None
    if not isinstance(value, dict):
        raise ProfileCaptureError("HELPER_INVALID_OUTPUT", diagnostic)
    if completed.returncode != 0 or value.get("succeeded") is not True:
        status = value.get("status")
        safe = status if isinstance(status, str) and status in _HELPER_FAILURES else "HELPER_FAILED"
        raise ProfileCaptureError(safe, diagnostic)
    return value


def _validate_header(header: dict[str, object], *, pid: int, hwnd: int) -> tuple[str, str]:
    if (header.get("probe_version") != "qq-uia-guest-header-v1"
            or header.get("mode") != "guest_foreground_header_inspect"
            or header.get("status") != "HEADER_CAPTURED"
            or type(header.get("header_candidate_count")) is not int
            or header.get("header_candidate_count") != 1
            or type(header.get("process_id")) is not int or header.get("process_id") != pid
            or type(header.get("window_handle")) is not int or header.get("window_handle") != hwnd):
        raise ProfileCaptureError("HEADER_SCOPE_MISMATCH")
    guest = header.get("guest_environment")
    if not isinstance(guest, dict) or guest != _GUEST or guest.get("certified") is not True:
        raise ProfileCaptureError("GUEST_ENVIRONMENT_MISMATCH")
    privacy = header.get("privacy")
    if not isinstance(privacy, dict) or any(privacy.get(key) is not expected for key, expected in {
        "exact_hwnd": True, "emitted_control_names": False, "emitted_chat_text": False,
        "mouse_input_used": False, "keyboard_input_used": False, "clipboard_used": False,
        "transient_navigation_performed": False, "composer_or_send_accessed": False,
    }.items()):
        raise ProfileCaptureError("HEADER_PRIVACY_CONTRACT_FAILED")
    header_digest, right_digest = header.get("active_header_digest"), header.get("right_region_structure_digest")
    if not isinstance(header_digest, str) or not _HASH.fullmatch(header_digest):
        raise ProfileCaptureError("HEADER_DIGEST_INVALID")
    if not isinstance(right_digest, str) or not _HASH.fullmatch(right_digest):
        raise ProfileCaptureError("RIGHT_REGION_DIGEST_INVALID")
    return header_digest, right_digest


def _capture_current_profile(
    helper: str, *, pid: int, hwnd: int, vault: Path, environment_fingerprint: str,
    selector_pack_version: str, deadline: datetime, key_name: str = "qq.identity.hmac",
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    secret_store: Any = None,
    validated_projector: Callable[[dict[str, object], dict[str, object]], Any] | None = None,
) -> Any:
    """Capture one current profile within one UTC and monotonic total budget.

    Injected runner/clock/store are testing seams, not provider capabilities.
    Failure after invoking the profile may leave transient UI; this function
    never guesses cleanup, retries navigation, creates keys, or changes binding.
    """
    if (not isinstance(helper, str) or not helper.strip() or "\x00" in helper
            or type(pid) is not int or pid <= 0 or type(hwnd) is not int or hwnd <= 0
            or not isinstance(environment_fingerprint, str) or not _HASH.fullmatch(environment_fingerprint)
            or not isinstance(selector_pack_version, str) or not selector_pack_version.strip()
            or len(selector_pack_version) > 128 or any(ord(c) < 32 for c in selector_pack_version)
            or not isinstance(key_name, str) or not _KEY_NAME.fullmatch(key_name)
            or not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None):
        raise ProfileCaptureError("CAPTURE_INPUT_INVALID")
    initial_now = clock()
    started = monotonic()
    if initial_now.tzinfo is None or initial_now.utcoffset() is None or not math.isfinite(started):
        raise ProfileCaptureError("CAPTURE_CLOCK_INVALID")
    monotonic_deadline = started + (deadline - initial_now).total_seconds()

    def remaining() -> float:
        current, tick = clock(), monotonic()
        if (current.tzinfo is None or current.utcoffset() is None
                or not math.isfinite(tick) or tick < started):
            raise ProfileCaptureError("CAPTURE_CLOCK_INVALID")
        value = min((deadline - current).total_seconds(), monotonic_deadline - tick)
        if value <= 0:
            raise ProfileCaptureError("CAPTURE_DEADLINE_EXHAUSTED")
        return value

    remaining()
    try:
        if secret_store is None:
            # The DPAPI constructor creates its root. Avoid creating a missing
            # vault during a supposedly read-only production capture.
            if not Path(vault).is_dir():
                raise ProfileCaptureError("IDENTITY_KEY_UNAVAILABLE")
            secret_store = WindowsDPAPISecretStore(vault)
        key = secret_store.get_secret(key_name)
    except SecretNotFoundError:
        raise ProfileCaptureError("IDENTITY_KEY_MISSING") from None
    except (SecretStoreError, OSError):
        raise ProfileCaptureError("IDENTITY_KEY_UNAVAILABLE") from None
    if not isinstance(key, bytes) or not 32 <= len(key) <= 128:
        raise ProfileCaptureError("IDENTITY_KEY_INVALID")
    target = [helper, "--target-qq-pid", str(pid), "--target-qq-hwnd", str(hwnd)]
    try:
        header = run_profile_helper(
            target + ["--inspect-guest-current-header", "--guest-header-authorized"],
            stage="header", timeout=remaining(), runner=runner,
        )
        remaining()
        header_digest, right_digest = _validate_header(header, pid=pid, hwnd=hwnd)
        payload = header_digest.encode("ascii") + b"\n" + base64.b64encode(key) + b"\n"
        raw = run_profile_helper(
            target + ["--capture-current-identity-guest-foreground", "--guest-identity-authorized"],
            stage="profile", timeout=remaining(), stdin=payload, runner=runner,
        )
        remaining()
        try:
            result = parse_guest_foreground_profile_report(
                raw, window_handle=hwnd, expected_process_id=pid,
                environment_fingerprint=environment_fingerprint,
                selector_pack_version=selector_pack_version,
                expected_header_digest=header_digest, expected_right_region_structure_digest=right_digest,
            )
        except ProfileIdentityError as exc:
            raise ProfileCaptureError(exc.code, {"stage": "profile"}) from None
        except RecursionError:
            raise ProfileCaptureError("HELPER_INVALID_OUTPUT", {"stage": "profile"}) from None
        if validated_projector is not None:
            result = validated_projector(raw, result)
        remaining()
        return result
    finally:
        # Drop references; Python immutable byte strings cannot promise zeroing.
        key = b""
        payload = b""


def capture_current_profile(
    helper: str, *, pid: int, hwnd: int, vault: Path, environment_fingerprint: str,
    selector_pack_version: str, deadline: datetime, key_name: str = "qq.identity.hmac",
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    secret_store: Any = None,
) -> dict[str, object]:
    """Return only the legacy strict projection; never expose a raw report."""
    return _capture_current_profile(
        helper, pid=pid, hwnd=hwnd, vault=vault,
        environment_fingerprint=environment_fingerprint, selector_pack_version=selector_pack_version,
        deadline=deadline, key_name=key_name, runner=runner, clock=clock,
        monotonic=monotonic, secret_store=secret_store,
    )


__all__ = ["ProfileCaptureError", "capture_current_profile"]
