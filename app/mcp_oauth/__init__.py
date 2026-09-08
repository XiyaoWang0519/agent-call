from __future__ import annotations

from app.mcp_oauth.constants import MCP_OAUTH_SCOPE, OAUTH_MCP_PATH

__all__ = [
    "OAUTH_MCP_PATH",
    "MCP_OAUTH_SCOPE",
    "MCPOAuthProvider",
    "oauth_mcp_resource",
    "mcp_oauth_issuer",
]


def __getattr__(name: str) -> object:
    if name in {"MCPOAuthProvider", "oauth_mcp_resource", "mcp_oauth_issuer"}:
        from app.mcp_oauth import provider

        return getattr(provider, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
