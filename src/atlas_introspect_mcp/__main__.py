"""Entrypoint: parse configuration, then serve over stdio (or HTTP).

Defaults assume the common local layout — ATLAS on loopback 8090 with its
checkout at ``~/ATLAS-GROUP/atlas-ui-3``. Every default can be overridden by
flag or environment variable, so one install can point at a dev instance too.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ServerConfig, set_config
from .server import mcp, tool_names

DEFAULT_ATLAS_HOME = Path.home() / "ATLAS-GROUP" / "atlas-ui-3"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    atlas_home = _env_path("ATLAS_HOME", DEFAULT_ATLAS_HOME)

    parser = argparse.ArgumentParser(
        prog="atlas-introspect-mcp",
        description="MCP server for introspecting a running ATLAS instance.",
    )
    parser.add_argument(
        "--atlas-url",
        default=os.environ.get("ATLAS_URL", "http://127.0.0.1:8090"),
        help="ATLAS origin (loopback). Default: http://127.0.0.1:8090",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("ATLAS_USER", "test@test.com"),
        help=(
            "Email sent as the auth header; every query is scoped to this user. "
            "Default: test@test.com (the DEBUG_MODE test user)."
        ),
    )
    parser.add_argument(
        "--auth-header",
        default=os.environ.get("ATLAS_AUTH_HEADER", "X-User-Email"),
        help="Header ATLAS reads the user identity from. Default: X-User-Email",
    )
    parser.add_argument(
        "--proxy-secret-header",
        default=os.environ.get("ATLAS_PROXY_SECRET_HEADER", "X-Proxy-Secret"),
        help=(
            "Header carrying the ATLAS proxy secret. Must match PROXY_SECRET_HEADER. "
            "The secret itself is read from ATLAS_PROXY_SECRET in the environment, "
            "never from argv, which is world-readable in ps. Default: X-Proxy-Secret"
        ),
    )
    parser.add_argument(
        "--atlas-home",
        type=Path,
        default=atlas_home,
        help=f"ATLAS checkout, used to locate config/ and prompts/. Default: {atlas_home}",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=_env_path("ATLAS_CONFIG_DIR", atlas_home / "config"),
        help="Directory holding mcp.json (overrides --atlas-home)",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=_env_path("ATLAS_PROMPTS_DIR", atlas_home / "prompts"),
        help="Directory holding the on-disk *.md system prompts",
    )
    parser.add_argument(
        "--timeout", type=float, default=_env_float("ATLAS_TIMEOUT", 30.0), help="HTTP timeout, seconds"
    )
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=_env_int("ATLAS_MAX_OUTPUT", 100_000),
        help="Cap on a single tool's text payload",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=_env_bool("ATLAS_INTROSPECT_READ_ONLY", False),
        help="Refuse every mutating tool (view-only introspection)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("ATLAS_INTROSPECT_TRANSPORT", "stdio"),
        help="Transport. Default: stdio (spawned by ATLAS)",
    )
    parser.add_argument("--stdio", action="store_true", help="Shorthand for --transport stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (loopback only)")
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("ATLAS_INTROSPECT_PORT", 8101),
        help="HTTP bind port. Never 8080 on this host — k3s Traefik hijacks it.",
    )
    parser.add_argument("--list-tools", action="store_true", help="Print tool names and exit")

    args = parser.parse_args()

    # --config-dir / --prompts-dir keep their own defaults, so a bare
    # --atlas-home has to be propagated into both.
    config_dir = args.config_dir
    prompts_dir = args.prompts_dir
    if args.atlas_home != atlas_home:
        if "--config-dir" not in sys.argv:
            config_dir = args.atlas_home / "config"
        if "--prompts-dir" not in sys.argv:
            prompts_dir = args.atlas_home / "prompts"

    set_config(
        ServerConfig(
            base_url=args.atlas_url.rstrip("/"),
            user_email=args.user,
            auth_header=args.auth_header,
            proxy_secret=os.environ.get("ATLAS_PROXY_SECRET") or None,
            proxy_secret_header=args.proxy_secret_header,
            config_dir=Path(config_dir).expanduser().resolve(),
            prompts_dir=Path(prompts_dir).expanduser().resolve(),
            timeout=args.timeout,
            max_output_chars=args.max_output_chars,
            read_only=args.read_only,
        )
    )

    if args.list_tools:
        import asyncio

        print("\n".join(asyncio.run(tool_names())))
        return

    transport = "stdio" if args.stdio else args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        if args.port == 8080:
            parser.error("port 8080 is hijacked by k3s Traefik on this host; use 8100+")
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
