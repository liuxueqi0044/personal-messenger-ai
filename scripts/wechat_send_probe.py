from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import platform
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.wechat.sending import WechatReadOnlyCapabilityProbe
from messenger_ai.domain import Platform
from messenger_ai.execution_guard import (
    ActionInterceptor,
    CapabilityRegistry,
    CircuitBreaker,
    DesktopState,
    EmergencyStop,
    EnvironmentFingerprinter,
    ExecutionGuard,
    PlatformMutex,
    SnapshotContentionMonitor,
)


def _powershell_process_records() -> list[dict]:
    script = """
$items = @(Get-Process -Name Weixin -ErrorAction SilentlyContinue | ForEach-Object {
  $version = 'unknown'
  try { $version = $_.MainModule.FileVersionInfo.ProductVersion } catch {}
  [pscustomobject]@{
    process_id = [int]$_.Id
    main_window_handle = [int64]$_.MainWindowHandle
    product_version = [string]$version
  }
})
$items | ConvertTo-Json -Compress
""".strip()
    completed = subprocess.run(
        [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    raw = completed.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = [parsed]
    return sorted(parsed, key=lambda item: int(item["process_id"]))


def _sha256_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _system_dpi_scale() -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        return max(dpi, 96) / 96
    except (AttributeError, OSError, ValueError):
        return 1.0


class WindowsWechatEnvironmentSource:
    def read_environment(self, requested_platform: Platform) -> dict:
        if requested_platform is not Platform.WECHAT:
            raise ValueError("this probe only supports WeChat")
        records = _powershell_process_records()
        visible = next(
            (item for item in records if int(item["main_window_handle"]) > 0), None
        )
        chosen = visible or (records[0] if records else None)
        client_version = (
            str(chosen["product_version"]) if chosen is not None else "not-running"
        )
        process_id = int(chosen["process_id"]) if chosen is not None else None
        window_handle = (
            int(chosen["main_window_handle"])
            if chosen is not None and int(chosen["main_window_handle"]) > 0
            else None
        )
        metadata = [
            {
                "process_id": int(item["process_id"]),
                "main_window_handle": int(item["main_window_handle"]),
                "product_version": str(item["product_version"]),
            }
            for item in records
        ]
        return {
            "client_version": client_version,
            "windows_version": platform.version(),
            "dpi_scale": _system_dpi_scale(),
            "theme": "not-probed",
            "window_mode": "window-bound" if window_handle else "no-main-window",
            "window_signature": _sha256_json(
                [item["main_window_handle"] for item in metadata]
            ),
            "process_signature": _sha256_json(metadata),
            "process_id": process_id,
            "window_handle": window_handle,
        }


class WindowsDesktopStateReader:
    def read_state(self) -> DesktopState:
        point = wintypes.POINT()
        pointer = None
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            pointer = (point.x, point.y)
        foreground = int(ctypes.windll.user32.GetForegroundWindow()) or None
        focus = int(ctypes.windll.user32.GetFocus()) or None
        clipboard_revision = int(ctypes.windll.user32.GetClipboardSequenceNumber())
        return DesktopState(
            foreground_window=foreground,
            keyboard_focus=focus,
            pointer_position=pointer,
            clipboard_revision=clipboard_revision,
            window_state_digest=_sha256_json(
                {"foreground": foreground, "focus": focus}
            ),
        )


class WindowsReadOnlyProbeDriver:
    """Metadata-only inspection. No composer, invoke, message, or input APIs exist here."""

    async def query_process(self, token) -> dict:
        token.raise_if_cancelled()
        records = _powershell_process_records()
        versions = sorted(
            {
                str(item["product_version"])
                for item in records
                if item["product_version"]
            }
        )
        return {
            "executable": "Weixin.exe",
            "process_count": len(records),
            "process_ids": [int(item["process_id"]) for item in records],
            "product_versions": versions,
            "main_window_handles": [
                int(item["main_window_handle"]) for item in records
            ],
        }

    async def query_window(self, process: dict, token) -> dict:
        token.raise_if_cancelled()
        handles = [value for value in process.get("main_window_handles", []) if value]
        return {
            "handle": handles[0] if handles else None,
            "readable": bool(handles),
            "requires_foreground": False,
            "ordinary_window_message_send_tested": False,
            "reason": (
                "non-zero top-level window observed"
                if handles
                else "all observed Weixin MainWindowHandle values are zero"
            ),
        }

    async def inspect_uia(self, window: dict, token) -> dict:
        token.raise_if_cancelled()
        return {
            "available": False,
            "semantic_send_chain_proven": False,
            "real_send_attempted": False,
            "reason": "no audited UIA send fixture is installed for this client build",
        }

    async def inspect_msaa(self, window: dict, token) -> dict:
        token.raise_if_cancelled()
        return {
            "available": False,
            "semantic_send_chain_proven": False,
            "real_send_attempted": False,
            "reason": "no audited MSAA send fixture is installed for this client build",
        }


async def run_probe() -> dict:
    source = WindowsWechatEnvironmentSource()
    guard = ExecutionGuard(
        registry=CapabilityRegistry(),
        fingerprinter=EnvironmentFingerprinter(source),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(WindowsDesktopStateReader()),
        platform_mutex=PlatformMutex(),
        circuit_breaker=CircuitBreaker(),
        emergency_stop=EmergencyStop(),
    )
    decision = await WechatReadOnlyCapabilityProbe(
        guard=guard,
        driver=WindowsReadOnlyProbeDriver(),
        capability_version="wechat-send-4.1.12.55-read-only-2026-09-08",
        fixture_suite_version="read-only-no-send-v1",
    ).run()
    return decision.model_dump(mode="json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only, fail-closed WeChat background-send capability probe."
    )
    parser.parse_args()
    if sys.platform != "win32":
        print(json.dumps({"status": "error", "reason": "Windows is required"}))
        return 2
    try:
        report = asyncio.run(run_probe())
    except Exception as exc:  # noqa: BLE001 - command line probe must fail closed
        print(
            json.dumps(
                {
                    "status": "failed_closed",
                    "error_code": "CAPABILITY_UNSUPPORTED",
                    "reason": f"read-only probe failed: {type(exc).__name__}",
                    "real_send_attempted": False,
                },
                indent=2,
            )
        )
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
