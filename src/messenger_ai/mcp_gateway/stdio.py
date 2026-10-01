"""Optional official-SDK STDIO entry point; it never opens a listening socket."""

from __future__ import annotations

import sys
from collections.abc import Callable

from .gateway import SPECS, MCPGateway
from .models import CallerIdentity, GatewayErrorCode


def sdk_available() -> bool:
    """Check the FastMCP API we use, not just the top-level SDK package."""
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except ImportError:
        return False
    return True


def require_sdk() -> None:
    if not sdk_available():
        raise RuntimeError(
            f"{GatewayErrorCode.SDK_UNAVAILABLE.value}: "
            "install optional dependency 'mcp>=1.2,<2' to run STDIO"
        )


def build_fastmcp(gateway: MCPGateway, caller_factory: Callable[[], CallerIdentity]):
    """Register every registry tool with the optional official SDK."""
    require_sdk()
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("personal-messenger-ai")
    for spec in SPECS:

        def make_handler(tool_name: str):
            def handler(arguments: dict) -> dict:
                return gateway.invoke(
                    tool_name, arguments, caller_factory()
                ).model_dump(mode="json")

            handler.__name__ = tool_name.replace(".", "_")
            return handler

        server.tool(name=spec.name)(make_handler(spec.name))
    return server


def run_stdio(gateway: MCPGateway, caller_factory: Callable[[], CallerIdentity]) -> int:
    """Start the supported transport only when the official SDK is installed.

    Registry tests use :class:`MCPGateway` directly, so development never needs
    a transport dependency.  This guard prevents silently exposing a homemade
    protocol under the MCP name.
    """
    try:
        server = build_fastmcp(gateway, caller_factory)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


def validate_http_bind(host: str, *, enabled: bool = False) -> str:
    """HTTP is opt-in and loopback-only; STDIO remains the default transport."""
    if not enabled:
        raise ValueError("HTTP transport is disabled by default")
    if host != "127.0.0.1":
        raise ValueError("MCP HTTP may bind only to 127.0.0.1")
    return host
