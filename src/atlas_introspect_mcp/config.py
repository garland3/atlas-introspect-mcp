"""Runtime configuration for the ATLAS introspection server.

One module-level ``ServerConfig`` is set once at startup by ``__main__`` and
read by the tools. Keeping it here (rather than threading it through every
tool signature) mirrors the sibling MCP servers in this folder.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerConfig:
    """Where ATLAS lives and how much output the tools may return."""

    base_url: str
    """ATLAS origin, e.g. ``http://127.0.0.1:8090`` (loopback only)."""

    user_email: str
    """Sent as the auth header. Every ATLAS query is scoped to this user."""

    auth_header: str
    """Header name ATLAS reads the user identity from."""

    config_dir: Path
    """ATLAS ``config/`` directory — holds ``mcp.json``."""

    prompts_dir: Path
    """ATLAS ``prompts/`` directory — the on-disk system prompt files."""

    timeout: float
    """HTTP timeout in seconds for calls into ATLAS."""

    max_output_chars: int
    """Cap on a single tool's textual payload before it is truncated."""

    read_only: bool
    """When true, every mutating tool refuses instead of writing."""

    proxy_secret: str | None = None
    """Shared secret proving this client is a trusted front end.

    ATLAS enables the proxy-secret gate by default outside DEBUG_MODE and
    rejects any request carrying neither a browser session nor this header,
    so a loopback client like this one must present it. Read from the
    environment rather than argv: process arguments are world-readable in
    ``ps``. Defaults to None so a DEBUG_MODE instance is unaffected.
    """

    proxy_secret_header: str = "X-Proxy-Secret"
    """Header carrying ``proxy_secret``. Must match ATLAS's PROXY_SECRET_HEADER."""

    @property
    def mcp_json_path(self) -> Path:
        return self.config_dir / "mcp.json"


_config: ServerConfig | None = None


def set_config(config: ServerConfig) -> None:
    global _config
    _config = config


def get_config() -> ServerConfig:
    if _config is None:
        raise RuntimeError("Server config not initialized")
    return _config
