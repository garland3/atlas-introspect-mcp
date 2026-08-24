"""Read and edit ATLAS's ``config/mcp.json``.

ATLAS has no API for this file, so it is edited on disk. Two properties matter
more than convenience here: a write is never partial (temp file + atomic
rename), and the previous contents are always recoverable (timestamped backup
next to the file, matching the `mcp.json.bak-YYYYMMDD` convention already in
that directory).

Edits take effect on the next ATLAS start — the servers are spawned at boot.
Every mutating helper says so in its result rather than leaving the caller to
wonder why nothing changed.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_config

RESTART_HINT = (
    "Change is on disk but not live: ATLAS spawns MCP servers at startup. "
    "Run `systemctl --user restart atlas` (or ./redeploy.sh --no-pull) to apply."
)


class McpConfigError(RuntimeError):
    """The file is missing, unparseable, or the edit would produce garbage."""


def load() -> dict[str, Any]:
    path = get_config().mcp_json_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise McpConfigError(f"mcp.json not found at {path}") from exc
    return parse(text, source=str(path))


def parse(text: str, *, source: str = "input") -> dict[str, Any]:
    """Parse and shape-check an mcp.json document."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigError(f"{source} must be a JSON object mapping server name -> config")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise McpConfigError(f"server '{name}' must be a JSON object, got {type(entry).__name__}")
    return data


def save(data: dict[str, Any]) -> dict[str, Any]:
    """Write the whole document atomically, after backing up the old one.

    Returns a dict describing what happened (path, backup, restart hint) so the
    tool layer can hand it straight back to the model.
    """
    cfg = get_config()
    if cfg.read_only:
        raise McpConfigError("Server is running --read-only; refusing to write mcp.json")

    path = cfg.mcp_json_path
    backup = _backup(path)
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    # Same directory so the rename stays on one filesystem and is atomic.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mcp.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    return {
        "path": str(path),
        "backup": str(backup) if backup else None,
        "servers": sorted(data),
        "note": RESTART_HINT,
    }


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def lint(name: str, entry: dict[str, Any]) -> list[str]:
    """Return human-readable warnings for one server entry.

    These are warnings, not errors — mcp.json accepts extra keys and ATLAS
    tolerates omissions. They encode the mistakes that fail *silently* on this
    host: a relative command that resolves against systemd's PATH, an HTTP
    server on the port k3s Traefik hijacks, a transport that does not match the
    fields actually present.
    """
    warnings: list[str] = []
    transport = entry.get("transport")
    command = entry.get("command")
    url = entry.get("url")

    if command is None and url is None:
        warnings.append(f"'{name}': needs either 'command' (stdio) or 'url' (http)")

    if command is not None:
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            warnings.append(f"'{name}': 'command' must be a list of strings")
        elif command and not command[0].startswith("/"):
            warnings.append(
                f"'{name}': command[0] '{command[0]}' is relative — it is looked up on "
                "systemd's PATH, not yours. Use an absolute path to the venv entrypoint."
            )
        if transport not in (None, "stdio"):
            warnings.append(f"'{name}': has 'command' but transport is '{transport}'")

    if url is not None:
        if transport != "http":
            warnings.append(f"'{name}': has 'url' but transport is '{transport}' (expected 'http')")
        if ":8080" in str(url):
            warnings.append(
                f"'{name}': port 8080 is hijacked by k3s Traefik on this host — every request "
                "returns an empty 500. Use 8100+."
            )

    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd and not cwd.startswith("/"):
        warnings.append(f"'{name}': relative cwd '{cwd}' resolves against the ATLAS project root")

    if not entry.get("groups"):
        warnings.append(f"'{name}': no 'groups' — users may not see the server")
    if not entry.get("description"):
        warnings.append(f"'{name}': no 'description' — the model gets no hint about the server")

    return warnings
