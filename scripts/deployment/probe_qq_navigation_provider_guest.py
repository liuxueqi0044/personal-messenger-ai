"""One synthetic image request; no QQ state, desktop input or runtime startup."""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from messenger_ai.adapters.qq.navigation.contracts import (
    ContactIdentityMode, ContactTarget, NavigationAction, NavigationFrame,
    NavigationRect, NavigationRegion, NavigationRegionKind, NavigationRequest,
)
from messenger_ai.adapters.qq.navigation.provider import ResponsesVisionNavigator
from messenger_ai.adapters.qq.vm_driver.transport import _encode_bgra_png
from messenger_ai.observability import WindowsDPAPISecretStore


def synthetic_frame() -> NavigationFrame:
    """Three synthetic numeric labels in deliberately reordered visual rows."""
    width, height = 400, 280
    pixels = bytearray(b"\xff\xff\xff\xff" * (width * height))
    glyphs = {
        "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
        "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
        "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    }
    def fill(left: int, top: int, right: int, bottom: int, color: bytes):
        for y in range(top, bottom):
            for x in range(left, right):
                offset = (y * width + x) * 4
                pixels[offset:offset + 4] = color
    for row, label in enumerate(("111", "333", "222")):
        top = 35 + row * 80
        fill(15, top, 385, top + 60, b"\xee\xee\xee\xff")
        for index, char in enumerate(label):
            for gy, line in enumerate(glyphs[char]):
                for gx, bit in enumerate(line):
                    if bit == "1":
                        x = 40 + index * 35 + gx * 5
                        y = top + 12 + gy * 5
                        fill(x, y, x + 5, y + 5, b"\x20\x20\x20\xff")
    return NavigationFrame(
        frame_id="synthetic-reorder-image", run_id="synthetic-run", session_epoch="synthetic-session",
        surface_epoch="synthetic-surface", worker_epoch="synthetic-worker", desktop_lease_id="synthetic-lease",
        control_revision=0, binding_id="synthetic-binding", binding_revision=1, process_id=1, window_handle=1,
        captured_at=datetime.now(UTC), screen_width=width, screen_height=height,
        crop_origin_x=0, crop_origin_y=0, crop_width=width, crop_height=height, dpi_scale=1.0,
        allowed_regions=tuple(NavigationRegion(kind=NavigationRegionKind.CANDIDATE,
            bbox=NavigationRect(left=15, top=35 + row * 80, right=385, bottom=95 + row * 80)) for row in range(3)),
        privacy_mask_applied=True, png_bytes=_encode_bgra_png(width, height, bytes(pixels)),
    )


async def run_probe(vault: Path) -> dict:
    if (os.name != "nt" or os.environ.get("COMPUTERNAME") != "PMAI-QQVM" or
            os.environ.get("USERNAME", "").lower() != "qqbot" or vault != Path(r"C:\PMAI\secrets")):
        raise RuntimeError("CERTIFIED_GUEST_REQUIRED")
    from openai import AsyncOpenAI
    key = WindowsDPAPISecretStore(vault).get_secret("deepseek.api_key").decode("utf-8")
    client = AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=15, max_retries=0)
    key = ""
    class OneRequest:
        calls = 0
        failure = None
        async def create(self, **kwargs):
            if self.calls:
                raise RuntimeError("PROBE_REQUEST_LIMIT")
            self.calls += 1
            bounded = {**kwargs, "max_output_tokens": 512, "reasoning": {"effort": "none"}}
            try:
                return await client.responses.create(**bounded)
            except Exception as exc:
                self.failure = {"exception_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
                body = getattr(exc, "body", None)
                if isinstance(body, dict):
                    error = body.get("error", body)
                    if isinstance(error, dict):
                        message = error.get("message")
                        if isinstance(message, str):
                            message = message.replace(client.api_key, "[REDACTED]")
                            message = re.sub(r"(?i)(?:bearer\s+|sk-)[A-Za-z0-9._~+/=-]+", "[REDACTED]", message)
                            self.failure["message"] = message[:300]
                        for field in ("code", "type", "param"):
                            value = error.get(field)
                            if value is None or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_. -]{1,96}", value)):
                                self.failure[field] = value
                raise
    transport = OneRequest()
    navigator = ResponsesVisionNavigator(model="deepseek-flash", endpoint="https://api.deepseek.com", transport=transport,
        schema_dialect="flat_primitive", max_output_tokens=512, reasoning_effort="none")
    frame = synthetic_frame()
    target = ContactTarget(account_id="synthetic-account", conversation_id="synthetic-conversation",
        binding_id="synthetic-binding", binding_revision=1, display_name="222", identity_mode=ContactIdentityMode.PERSISTENT)
    try:
        result = await navigator.decide(NavigationRequest(target=target, frame=frame,
            deadline_at=datetime.now(UTC) + timedelta(seconds=15), allowed_actions=(NavigationAction.CLICK_CANDIDATE, NavigationAction.UNABLE)))
        box = result.decision.bbox if result.decision else None
        correct = (result.decision is not None and result.decision.action == NavigationAction.CLICK_CANDIDATE
            and result.decision.observed_label == "222" and box is not None and box.top >= 195 and box.bottom <= 255)
        return {"schema": "qq-navigation-provider-synthetic-v2", "succeeded": correct, "requests": transport.calls,
            "model": "deepseek-flash", "latency_ms": result.latency_ms,
            "error_category": result.error.category if result.error else None,
            "decision_action": result.decision.action if result.decision else None,
            "correct_visual_row": correct, "real_qq_data_used": False, "desktop_actions": 0,
            "transport_failure": transport.failure}
    finally:
        await client.close()


if __name__ == "__main__":
    import sys
    Path(sys.argv[1]).write_text(json.dumps(asyncio.run(run_probe(Path(r"C:\PMAI\secrets")))), encoding="utf-8")
