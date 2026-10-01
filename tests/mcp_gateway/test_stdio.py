from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from messenger_ai.mcp_gateway import SPECS, MCPGateway, stdio
from messenger_ai.mcp_gateway.models import GatewayErrorCode


def test_sdk_probe_requires_fastmcp_even_when_top_level_package_exists(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)

    assert not stdio.sdk_available()
    with pytest.raises(RuntimeError, match="MCP_SDK_UNAVAILABLE") as error:
        stdio.require_sdk()
    assert "mcp>=1.2,<2" in str(error.value)


@pytest.mark.parametrize(
    "failure",
    [ModuleNotFoundError("mcp is not installed"), ImportError("FastMCP is missing")],
)
def test_unavailable_sdk_fails_closed_without_protocol_stdout(
    monkeypatch, capsys, failure
):
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "mcp.server.fastmcp":
            raise failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    # No real Hub or caller is needed: SDK failure must precede their use.
    gateway = MCPGateway(None)

    def unexpected_caller():
        pytest.fail("SDK failure must not resolve a caller")

    assert not stdio.sdk_available()
    with pytest.raises(RuntimeError, match="MCP_SDK_UNAVAILABLE"):
        stdio.build_fastmcp(gateway, unexpected_caller)
    assert stdio.run_stdio(gateway, unexpected_caller) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("MCP_SDK_UNAVAILABLE:")
    assert "mcp>=1.2,<2" in captured.err


def test_sdk_probe_does_not_hide_unrelated_errors(monkeypatch):
    original_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "mcp.server.fastmcp":
            raise ValueError("unexpected SDK initialization failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ValueError, match="unexpected SDK initialization failure"):
        stdio.sdk_available()


def test_run_stdio_explicitly_selects_only_stdio(monkeypatch):
    calls = []
    server = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(stdio, "build_fastmcp", lambda *_args: server)

    assert stdio.run_stdio(MCPGateway(None), lambda: None) == 0
    assert calls == [{"transport": "stdio"}]


@pytest.mark.asyncio
async def test_real_stdio_protocol_with_synthetic_read_only_hub():
    if not stdio.sdk_available():
        pytest.skip("The optional MCP 1.x FastMCP SDK is not available")

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Run the actual transport in a subprocess, with no desktop, storage,
    # credentials, or live Hub. Imports work even without an editable install.
    source = Path(__file__).parents[2] / "src"
    program = f"""
import sys
sys.path.insert(0, {str(source)!r})
from messenger_ai.mcp_gateway import CallerIdentity, MCPGateway
from messenger_ai.mcp_gateway.stdio import run_stdio

class SyntheticHub:
    def call(self, operation, payload, caller):
        assert operation in ("health", "get_capabilities")
        return {{"operation": operation, "status": "ok"}}

caller = CallerIdentity(caller_id="protocol-test", scopes=("messenger.read",))
raise SystemExit(run_stdio(MCPGateway(SyntheticHub()), lambda: caller))
"""
    parameters = StdioServerParameters(command=sys.executable, args=["-c", program])
    with anyio.fail_after(20):
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "personal-messenger-ai"
            inventory = await session.list_tools()
            assert {tool.name for tool in inventory.tools} == {
                spec.name for spec in SPECS
            }
            # Preserve the existing FastMCP arguments envelope.
            for name in ("messenger.health", "messenger.get_capabilities"):
                result = await session.call_tool(name, {"arguments": {}})
                assert not result.isError
                response = json.loads(result.content[0].text)
                assert response["ok"] is True
                assert response["data"]["operation"] == name.split(".")[1]
            for name, arguments, expected in (
                (
                    "messenger.health",
                    {"extra": True},
                    GatewayErrorCode.INVALID_ARGUMENT,
                ),
                (
                    "messenger.pause",
                    {"scope": "global"},
                    GatewayErrorCode.FORBIDDEN,
                ),
            ):
                result = await session.call_tool(name, {"arguments": arguments})
                response = json.loads(result.content[0].text)
                assert response["ok"] is False
                assert response["error"]["code"] == expected.value
