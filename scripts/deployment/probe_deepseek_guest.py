"""Guest-only, one-request DeepSeek Responses smoke probe.

The default mode performs no secret-store access and no network call.  ``--run``
is the only mode that reads the fixed ``deepseek.api_key`` DPAPI alias and
issues one synthetic planner request.  This probe never reads QQ state, sends
QQ messages, activates RulePacks, or starts the runtime daemon.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from messenger_ai.llm.deepseek import DeepSeekResponsesProvider
from messenger_ai.llm.models import (
    ContactProjection,
    InboundItem,
    ReplyPlanRequest,
    RuleProjection,
)
from messenger_ai.observability import WindowsDPAPISecretStore

MODEL = "deepseek-v4-flash"
MAX_OUTPUT_TOKENS = 1024
TIMEOUT_SECONDS = 30.0


def safe_error_message(message: str, secret: str | None = None) -> str:
    """Keep only a bounded diagnostic without credentials or token-like values."""

    text = str(message)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = re.sub(r"\b[0-9a-fA-F]{32,}\b", "[REDACTED]", text)
    return text[:500]


def synthetic_request() -> ReplyPlanRequest:
    """Build a fixed, non-personal planner request with no QQ data."""

    return ReplyPlanRequest(
        request_id="guest-probe-0001",
        account_id="synthetic-account",
        contact=ContactProjection(
            contact_id="synthetic-contact",
            conversation_id="synthetic-conversation",
            relationship_stage="new",
        ),
        rules=RuleProjection(
            rulepack_id="synthetic-probe-rules",
            rule_version="probe-1",
            source_hash="synthetic-source-hash",
            system_safety=("Return a short safe greeting only.",),
            language="en-US",
            preferred_length="concise",
        ),
        inbound=(
            InboundItem(
                message_key="synthetic-message-0001",
                text="Hello from the synthetic provider smoke probe.",
            ),
        ),
        context_fingerprint="synthetic-context-fingerprint",
        created_at=datetime.now(timezone.utc),
        model_hint=MODEL,
    )


class MaxOutputTransport:
    """Transport wrapper that hard-limits one Responses request."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    def create(self, **kwargs: Any) -> Any:
        if self.calls:
            raise RuntimeError("single request limit exceeded")
        self.calls += 1
        bounded = dict(kwargs)
        bounded["max_output_tokens"] = MAX_OUTPUT_TOKENS
        target = getattr(self.inner, "responses", self.inner)
        return target.create(**bounded)

    def close(self) -> Any:
        close = getattr(self.inner, "close", None)
        return close() if close is not None else None


def verify_guest_context() -> None:
    """Require the dedicated non-admin qqbot VirtualBox guest for ``--run``."""

    if os.name != "nt":
        raise RuntimeError("guest_guard")
    probe = (
        "$id=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p=New-Object Security.Principal.WindowsPrincipal($id);"
        "$cs=Get-CimInstance Win32_ComputerSystem;"
        "[ordered]@{identity=$id.Name;is_admin=$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
        "computer=$cs.Name;manufacturer=$cs.Manufacturer;model=$cs.Model}|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", probe],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        info = json.loads(completed.stdout)
    except Exception as exc:
        raise RuntimeError("guest_guard") from exc
    identity = str(info.get("identity", "")).lower()
    manufacturer = str(info.get("manufacturer", "")).lower()
    model = str(info.get("model", "")).lower()
    if (
        str(info.get("computer", "")).upper() != "PMAI-QQVM"
        or os.environ.get("COMPUTERNAME", "").upper() != "PMAI-QQVM"
        or not identity.endswith(r"\qqbot")
        or bool(info.get("is_admin"))
        or not any(value in manufacturer for value in ("oracle", "innotek"))
        or "virtualbox" not in model
    ):
        raise RuntimeError("guest_guard")


class FakeTransport:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_response() -> dict[str, Any]:
    return {
        "output_text": json.dumps(
            {
                "action": "ignore",
                "reply_text": "",
                "reply_segments": [],
                "risk_level": "low",
                "selection_reason": "synthetic smoke response",
                "variation_seed": "probe-1",
            }
        ),
        "usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
    }


async def fake_self_test() -> dict[str, Any]:
    request = synthetic_request()
    success_transport = FakeTransport(fake_response())
    success_limited = MaxOutputTransport(success_transport)
    success_provider = DeepSeekResponsesProvider(
        api_key="synthetic-key", model=MODEL, transport=success_limited
    )
    success = await success_provider.plan_reply(request)
    if success_transport.calls[0].get("max_output_tokens") != MAX_OUTPUT_TOKENS:
        raise AssertionError("output token bound was not injected")
    if success.plan is None or success.error is not None:
        raise AssertionError("fake success did not parse the strict plan")
    try:
        success_limited.create(test=True)
    except RuntimeError as exc:
        if str(exc) != "single request limit exceeded":
            raise
    else:
        raise AssertionError("transport allowed a second request")
    if len(success_transport.calls) != 1:
        raise AssertionError("second request reached the wrapped transport")

    synthetic_secret = "sk-synthetic-secret-1234567890"
    redacted = safe_error_message(
        f"Bearer {synthetic_secret} body={synthetic_secret} hex=" + "a" * 64,
        synthetic_secret,
    )
    if synthetic_secret in redacted or "a" * 64 in redacted or len(redacted) > 500:
        raise AssertionError("safe diagnostic leaked a credential-like value")

    error_transport = FakeTransport(error=RuntimeError("synthetic transport error"))
    error_provider = DeepSeekResponsesProvider(
        api_key="synthetic-key", model=MODEL, transport=MaxOutputTransport(error_transport)
    )
    error = await error_provider.plan_reply(request)
    if error.error is None or error.error.category != "unknown":
        raise AssertionError("fake error was not classified safely")

    async def rejected_guard_probe() -> dict[str, Any]:
        # Exercise the guard boundary without touching the host identity APIs.
        return await run_probe(
            Path("synthetic-vault"),
            guest_guard=lambda: (_ for _ in ()).throw(RuntimeError("guest_guard")),
        )

    guard_result = await rejected_guard_probe()
    if guard_result.get("provider_error_category") != "guest_guard" or guard_result.get("api_call_attempted"):
        raise AssertionError("fake guest guard did not block before secret/API access")

    return {
        "schema": "pmai-v5-deepseek-guest-probe-self-test-1",
        "success": {
            "api_calls": len(success_transport.calls),
            "schema_valid": success.plan is not None,
            "max_output_tokens": success_transport.calls[0]["max_output_tokens"],
            "usage_total_tokens": success.usage.total_tokens,
        },
        "error": {
            "api_calls": len(error_transport.calls),
            "provider_error_category": error.error.category if error.error else "unknown",
        },
        "guard": {
            "api_calls": int(bool(guard_result.get("api_call_attempted"))),
            "provider_error_category": guard_result["provider_error_category"],
        },
        "default_mode_calls": 0,
        "secrets_emitted": False,
        "safe_diagnostic_test": True,
        "qq_accessed": False,
    }


async def run_probe(vault: Path, *, guest_guard: Any = verify_guest_context) -> dict[str, Any]:
    started = time.perf_counter()
    base: dict[str, Any] = {
        "schema": "pmai-v5-deepseek-guest-probe-1",
        "mode": "run",
        "model": MODEL,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "api_call_attempted": False,
        "qq_accessed": False,
        "rulepack_activated": False,
        "raw_output_emitted": False,
    }
    try:
        guest_guard()
    except Exception:
        base.update({"provider_error_category": "guest_guard", "schema_valid": False})
        base["latency_ms"] = max(0, round((time.perf_counter() - started) * 1000))
        return base
    try:
        key = WindowsDPAPISecretStore(vault).get_secret("deepseek.api_key")
    except Exception:
        base.update({"provider_error_category": "secret_store", "schema_valid": False})
        base["latency_ms"] = max(0, round((time.perf_counter() - started) * 1000))
        return base

    provider = None
    limited: MaxOutputTransport | None = None
    try:
        provider = DeepSeekResponsesProvider(
            api_key=key.decode("utf-8"), model=MODEL, timeout_seconds=TIMEOUT_SECONDS
        )
        limited = MaxOutputTransport(provider.transport)
        provider.transport = limited
        result = await provider.plan_reply(synthetic_request())
        base.update(
            {
                "api_call_attempted": bool(limited.calls),
                "schema_valid": result.plan is not None and result.error is None,
                "provider_error_category": result.error.category if result.error else None,
                "provider_status_code": (
                    result.error.status_code
                    if result.error and isinstance(result.error.status_code, int)
                    and 100 <= result.error.status_code <= 599
                    else None
                ),
                "provider_error_message_safe": (
                    safe_error_message(result.error.message, key.decode("utf-8"))
                    if result.error
                    else None
                ),
                "latency_ms": result.latency_ms,
                "usage": result.usage.model_dump(),
            }
        )
        return base
    except Exception:
        base.update(
            {
                "api_call_attempted": bool(limited and limited.calls),
                "provider_error_category": "unknown",
                "schema_valid": False,
            }
        )
        base["latency_ms"] = max(0, round((time.perf_counter() - started) * 1000))
        return base
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Guest-only synthetic DeepSeek provider smoke probe")
    parser.add_argument("--run", action="store_true", help="read guest DPAPI alias and issue one synthetic API request")
    parser.add_argument("--self-test", action="store_true", help="run fake transport boundary checks; never contacts the API")
    parser.add_argument("--vault", type=Path, help="guest DPAPI vault path; only used with --run")
    args = parser.parse_args()
    if args.run and args.self_test:
        parser.error("choose --run or --self-test")
    if args.run and args.vault is None:
        parser.error("--vault is required with --run")
    if not args.run and not args.self_test:
        print(json.dumps({"schema": "pmai-v5-deepseek-guest-probe-1", "mode": "default", "api_calls": 0, "secrets_read": False, "qq_accessed": False}, separators=(",", ":")))
        return 0
    result = asyncio.run(run_probe(args.vault)) if args.run else asyncio.run(fake_self_test())
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    if args.self_test:
        return 0
    return 0 if result.get("schema_valid") is True and result.get("provider_error_category") is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
