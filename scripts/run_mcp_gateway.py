"""Offline M12 contract check; no messenger client or socket is started."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from messenger_ai.mcp_gateway import CallerIdentity, MCPGateway


class AuditHub:
    def call(
        self, operation: str, payload: Mapping[str, Any], caller: CallerIdentity
    ) -> Mapping[str, Any]:
        del payload, caller
        return {"operation": operation, "status": "ok"}


def main() -> int:
    gateway = MCPGateway(AuditHub())
    caller = CallerIdentity(caller_id="audit-caller", scopes=("messenger.read",))
    response = gateway.invoke("messenger.health", {}, caller)
    assert response.ok
    names = {item["name"] for item in gateway.tool_schemas()}
    assert "messenger.send_message" not in names
    assert all(
        item["inputSchema"]["additionalProperties"] is False
        for item in gateway.tool_schemas()
    )
    print("M12 MCP Gateway audit: PASS (minimal registry, no send tool)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
