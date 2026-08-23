from __future__ import annotations

from app.grok_oauth.constants import GROK_MCP_PATH, GROK_OAUTH_SCOPE

__all__ = [
    "GROK_MCP_PATH",
    "GROK_OAUTH_SCOPE",
    "GrokOAuthProvider",
    "grok_mcp_resource",
    "grok_oauth_issuer",
]


def __getattr__(name: str) -> object:
    if name in {"GrokOAuthProvider", "grok_mcp_resource", "grok_oauth_issuer"}:
        from app.grok_oauth import provider

        return getattr(provider, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
