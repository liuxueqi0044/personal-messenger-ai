"""M12: restricted management tools for a local Codex MCP connection."""

from .gateway import SPECS, MCPGateway
from .models import CallerIdentity, GatewayErrorCode, ToolPermission

__all__ = [
    "SPECS",
    "CallerIdentity",
    "GatewayErrorCode",
    "MCPGateway",
    "ToolPermission",
]
