"""Tool definitions for the ATLAS introspection server.

Four surfaces, one per thing an operator actually wants to poke at:

* **conversations** — read-only. Chat history for the configured user.
* **mcp.json** — read/write on disk, backed up on every write.
* **workspaces** — read/write through the ATLAS API.
* **custom prompts** — the per-user prompt library (API) and the on-disk
  system prompt files in ``prompts/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import atlas_client as atlas
from . import mcp_config
from .atlas_client import AtlasError
from .config import get_config
from .mcp_config import McpConfigError

mcp = FastMCP(
    "atlas-introspect",
    instructions=(
        "Introspect and configure the running ATLAS instance: browse chat "
        "history, read and edit config/mcp.json, manage workspaces and custom "
        "prompts, and tail the daemon journal. Conversation tools are "
        "read-only. mcp.json edits need an ATLAS restart to take effect."
    ),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _guard_writes() -> None:
    if get_config().read_only:
        raise ToolError("Server is running --read-only; mutating tools are disabled.")


def _clip(text: str, limit: int | None = None) -> str:
    """Truncate at the configured cap, saying so rather than trailing off."""
    cap = limit if limit is not None else get_config().max_output_chars
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + f"\n\n... [truncated: {len(text) - cap} more characters]"


def _clip_value(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _clip(value, limit)
    return value


async def _call(coro):
    """Turn an AtlasError into a ToolError so the model sees the reason."""
    try:
        return await coro
    except AtlasError as exc:
        raise ToolError(str(exc)) from exc


def _prompt_file(name: str) -> Path:
    """Resolve a prompt filename inside prompts_dir, refusing traversal."""
    prompts_dir = get_config().prompts_dir.resolve()
    candidate = (prompts_dir / name).resolve()
    if candidate.parent != prompts_dir:
        raise ToolError(f"'{name}' is not a file directly inside {prompts_dir}")
    if candidate.suffix != ".md":
        raise ToolError("Prompt files are markdown; the name must end in .md")
    return candidate


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


@mcp.tool
async def atlas_status() -> dict[str, Any]:
    """Check that ATLAS is up and report what this server is pointed at.

    Start here when another tool fails: it distinguishes "ATLAS is down" from
    "the feature is disabled" from "this server is aimed at the wrong URL".
    """
    cfg = get_config()
    info: dict[str, Any] = {
        "base_url": cfg.base_url,
        "acting_as_user": cfg.user_email,
        "mcp_json": str(cfg.mcp_json_path),
        "prompts_dir": str(cfg.prompts_dir),
        "read_only": cfg.read_only,
    }
    try:
        info["health"] = await atlas.get("/api/health")
    except AtlasError as exc:
        info["health"] = {"error": str(exc)}
        return info

    try:
        config = await atlas.get("/api/config")
    except AtlasError as exc:
        info["config"] = {"error": str(exc)}
        return info

    if isinstance(config, dict):
        tools = config.get("tools") or config.get("mcp_servers") or []
        info["config"] = {
            "app_name": config.get("app_name"),
            "model_count": len(config.get("models") or []),
            "server_count": len(tools) if isinstance(tools, (list, dict)) else None,
            "feature_flags": {
                key: value
                for key, value in config.items()
                if key.startswith("feature") or key.endswith("_enabled")
            },
        }
    return info


# --------------------------------------------------------------------------
# conversations (read-only)
# --------------------------------------------------------------------------


@mcp.tool
async def list_conversations(
    limit: int = 25,
    offset: int = 0,
    tag: Optional[str] = None,
) -> dict[str, Any]:
    """List saved conversations, newest first.

    Returns id, title, model, timestamps, message count, and a short preview.
    Use `get_conversation` for the messages themselves.

    Args:
        limit: How many to return (1-200).
        offset: Skip this many for paging.
        tag: Only conversations carrying this tag name.
    """
    data = await _call(atlas.get("/api/conversations", limit=limit, offset=offset, tag=tag))
    conversations = data.get("conversations", []) if isinstance(data, dict) else []
    return {"count": len(conversations), "conversations": conversations}


@mcp.tool
async def search_conversations(query: str, limit: int = 20) -> dict[str, Any]:
    """Search conversations by title and message content.

    Args:
        query: Text to look for.
        limit: Maximum matches (1-100).
    """
    data = await _call(atlas.get("/api/conversations/search", q=query, limit=limit))
    conversations = data.get("conversations", []) if isinstance(data, dict) else []
    return {"query": query, "count": len(conversations), "conversations": conversations}


@mcp.tool
async def get_conversation(
    conversation_id: str,
    max_message_chars: int = 4000,
    include_metadata: bool = False,
) -> dict[str, Any]:
    """Read one full conversation, including its messages.

    Long messages are clipped per-message so a single large tool result cannot
    swamp the answer; raise `max_message_chars` when you need the full text of
    a specific turn.

    Args:
        conversation_id: The conversation's UUID, from `list_conversations`.
        max_message_chars: Per-message clip length.
        include_metadata: Keep each message's raw metadata_json blob (tool
            calls, files, agent steps). Verbose — off by default.
    """
    data = await _call(atlas.get(f"/api/conversations/{conversation_id}"))
    if not isinstance(data, dict):
        raise ToolError(f"Unexpected response for conversation {conversation_id}")

    messages = data.get("messages") or []
    trimmed = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = {
            key: _clip_value(value, max_message_chars)
            for key, value in message.items()
            if include_metadata or key != "metadata_json"
        }
        trimmed.append(item)
    data["messages"] = trimmed
    data["message_count_returned"] = len(trimmed)
    return data


@mcp.tool
async def list_conversation_tags() -> dict[str, Any]:
    """List the tags this user has applied to conversations."""
    data = await _call(atlas.get("/api/conversations/tags/list"))
    return data if isinstance(data, dict) else {"tags": data}


# --------------------------------------------------------------------------
# mcp.json
# --------------------------------------------------------------------------


@mcp.tool
async def get_mcp_config(server: Optional[str] = None, lint: bool = True) -> dict[str, Any]:
    """Read config/mcp.json — the whole file, or one server entry.

    Args:
        server: Return only this server's entry.
        lint: Include warnings about entries likely to fail silently
            (relative command paths, port 8080, transport/field mismatch).
    """
    try:
        data = mcp_config.load()
    except McpConfigError as exc:
        raise ToolError(str(exc)) from exc

    if server is not None:
        if server not in data:
            raise ToolError(f"No server '{server}' in mcp.json. Present: {', '.join(sorted(data))}")
        entry = {server: data[server]}
    else:
        entry = data

    result: dict[str, Any] = {
        "path": str(get_config().mcp_json_path),
        "server_count": len(entry),
        "config": entry,
    }
    if lint:
        warnings: list[str] = []
        for name, item in entry.items():
            warnings.extend(mcp_config.lint(name, item))
        result["warnings"] = warnings
    return result


@mcp.tool
async def upsert_mcp_server(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Add or replace one server entry in config/mcp.json.

    The entry is replaced wholesale, not merged — read the current entry with
    `get_mcp_config` first and send it back with your change applied. The rest
    of the file is untouched, and the previous file is backed up.

    A stdio entry looks like::

        {"command": ["/abs/path/.venv/bin/entrypoint", "--stdio"],
         "cwd": "/abs/path", "transport": "stdio",
         "groups": ["users"], "description": "what it does"}

    Args:
        name: Server key, e.g. "basic_fns".
        config: The full entry object.
    """
    _guard_writes()
    if not isinstance(config, dict):
        raise ToolError("config must be a JSON object")
    try:
        data = mcp_config.load()
        existed = name in data
        data[name] = config
        result = mcp_config.save(data)
    except McpConfigError as exc:
        raise ToolError(str(exc)) from exc

    result["action"] = "replaced" if existed else "added"
    result["server"] = name
    result["warnings"] = mcp_config.lint(name, config)
    return result


@mcp.tool
async def delete_mcp_server(name: str) -> dict[str, Any]:
    """Remove a server entry from config/mcp.json (previous file is backed up).

    Args:
        name: Server key to remove.
    """
    _guard_writes()
    try:
        data = mcp_config.load()
        if name not in data:
            raise ToolError(f"No server '{name}' in mcp.json. Present: {', '.join(sorted(data))}")
        removed = data.pop(name)
        result = mcp_config.save(data)
    except McpConfigError as exc:
        raise ToolError(str(exc)) from exc

    result["action"] = "deleted"
    result["server"] = name
    result["removed_entry"] = removed
    return result


@mcp.tool
async def replace_mcp_config(content: str) -> dict[str, Any]:
    """Overwrite config/mcp.json with a complete JSON document.

    For restructuring several entries at once. Rejected unless `content` parses
    as an object of objects; the previous file is backed up either way. Prefer
    `upsert_mcp_server` for a single-server change.

    Args:
        content: The full replacement JSON text.
    """
    _guard_writes()
    try:
        data = mcp_config.parse(content, source="content")
        result = mcp_config.save(data)
    except McpConfigError as exc:
        raise ToolError(str(exc)) from exc

    warnings: list[str] = []
    for name, entry in data.items():
        warnings.extend(mcp_config.lint(name, entry))
    result["action"] = "replaced_whole_file"
    result["warnings"] = warnings
    return result


# --------------------------------------------------------------------------
# workspaces
# --------------------------------------------------------------------------


@mcp.tool
async def list_workspaces() -> dict[str, Any]:
    """List this user's workspaces (saved bundles of prompt/tool/RAG selections)."""
    data = await _call(atlas.get("/api/workspaces"))
    workspaces = data.get("workspaces", []) if isinstance(data, dict) else []
    return {"count": len(workspaces), "workspaces": workspaces}


@mcp.tool
async def create_workspace(
    name: str,
    description: Optional[str] = None,
    active_prompt_key: Optional[str] = None,
    selected_tools: Optional[list[str]] = None,
    selected_prompts: Optional[list[str]] = None,
    selected_data_sources: Optional[list[str]] = None,
    rag_enabled: bool = False,
) -> dict[str, Any]:
    """Create a workspace.

    The selection fields are exactly what ATLAS restores when the workspace is
    applied; unknown keys are rejected by the API, so use these arguments
    rather than a free-form config blob.

    Args:
        name: Display name.
        description: Optional one-liner.
        active_prompt_key: Key of the system prompt to activate.
        selected_tools: Tool identifiers, e.g. "basic_fns_bash".
        selected_prompts: Prompt-library entries to include.
        selected_data_sources: RAG data source ids.
        rag_enabled: Whether RAG is on in this workspace.
    """
    _guard_writes()
    body = {
        "name": name,
        "description": description,
        "config": {
            "active_prompt_key": active_prompt_key,
            "selected_tools": selected_tools or [],
            "selected_prompts": selected_prompts or [],
            "selected_data_sources": selected_data_sources or [],
            "rag_enabled": rag_enabled,
        },
    }
    return await _call(atlas.post("/api/workspaces", body))


@mcp.tool
async def update_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Update a workspace's name, description, or selections.

    `config` replaces the whole selection snapshot when given — read the
    current one with `list_workspaces` first. Allowed keys: active_prompt_key,
    selected_tools, selected_prompts, selected_data_sources, rag_enabled.
    Anything else is rejected by ATLAS.

    Args:
        workspace_id: The workspace UUID.
        name: New name, if changing.
        description: New description, if changing.
        config: Replacement selection snapshot, if changing.
    """
    _guard_writes()
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if config is not None:
        body["config"] = config
    if not body:
        raise ToolError("Nothing to update: pass name, description, or config.")
    return await _call(atlas.put(f"/api/workspaces/{workspace_id}", body))


@mcp.tool
async def delete_workspace(workspace_id: str) -> dict[str, Any]:
    """Delete a workspace. Not recoverable — confirm with the user first.

    Args:
        workspace_id: The workspace UUID.
    """
    _guard_writes()
    result = await _call(atlas.delete(f"/api/workspaces/{workspace_id}"))
    return {"deleted": workspace_id, "response": result}


# --------------------------------------------------------------------------
# custom prompts (per-user library)
# --------------------------------------------------------------------------


@mcp.tool
async def list_custom_prompts(include_content: bool = False) -> dict[str, Any]:
    """List this user's custom prompt library.

    Args:
        include_content: Include each prompt's full text. Off by default so a
            large library stays browsable.
    """
    data = await _call(atlas.get("/api/user-prompts"))
    prompts = data.get("prompts", []) if isinstance(data, dict) else []
    if not include_content:
        prompts = [
            {key: value for key, value in prompt.items() if key != "content"}
            | {"content_chars": len(prompt.get("content") or "")}
            for prompt in prompts
            if isinstance(prompt, dict)
        ]
    return {"count": len(prompts), "prompts": prompts}


@mcp.tool
async def get_custom_prompt(prompt_id: str) -> dict[str, Any]:
    """Read one custom prompt in full, by id.

    Args:
        prompt_id: The prompt UUID, from `list_custom_prompts`.
    """
    data = await _call(atlas.get("/api/user-prompts"))
    prompts = data.get("prompts", []) if isinstance(data, dict) else []
    for prompt in prompts:
        if isinstance(prompt, dict) and prompt.get("id") == prompt_id:
            return prompt
    raise ToolError(f"No prompt with id '{prompt_id}' for user {get_config().user_email}")


@mcp.tool
async def create_custom_prompt(title: str, content: str) -> dict[str, Any]:
    """Add a prompt to this user's library.

    Args:
        title: Short label (<=200 chars).
        content: The system prompt text (<=50000 chars).
    """
    _guard_writes()
    return await _call(atlas.post("/api/user-prompts", {"title": title, "content": content}))


@mcp.tool
async def update_custom_prompt(
    prompt_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> dict[str, Any]:
    """Edit a custom prompt's title or content.

    `content` replaces the whole text — read it with `get_custom_prompt` first
    if you are amending rather than rewriting.

    Args:
        prompt_id: The prompt UUID.
        title: New title, if changing.
        content: New full text, if changing.
    """
    _guard_writes()
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content"] = content
    if not body:
        raise ToolError("Nothing to update: pass title or content.")
    return await _call(atlas.put(f"/api/user-prompts/{prompt_id}", body))


@mcp.tool
async def delete_custom_prompt(prompt_id: str) -> dict[str, Any]:
    """Delete a custom prompt. Not recoverable — confirm with the user first.

    Args:
        prompt_id: The prompt UUID.
    """
    _guard_writes()
    result = await _call(atlas.delete(f"/api/user-prompts/{prompt_id}"))
    return {"deleted": prompt_id, "response": result}


# --------------------------------------------------------------------------
# on-disk prompt files (the default system prompts)
# --------------------------------------------------------------------------


@mcp.tool
async def list_prompt_files() -> dict[str, Any]:
    """List the markdown prompt files ATLAS loads from disk.

    These are the *defaults* (system prompt, agent reason/observe/summary
    prompts), separate from the per-user library above. Editing one changes
    behaviour for every chat, and takes effect on the next ATLAS restart.
    """
    prompts_dir = get_config().prompts_dir
    if not prompts_dir.is_dir():
        raise ToolError(f"Prompts directory not found: {prompts_dir}")
    files = [
        {"name": path.name, "bytes": path.stat().st_size}
        for path in sorted(prompts_dir.glob("*.md"))
    ]
    return {"dir": str(prompts_dir), "count": len(files), "files": files}


@mcp.tool
async def read_prompt_file(name: str) -> dict[str, Any]:
    """Read one on-disk prompt file.

    Args:
        name: Filename from `list_prompt_files`, e.g. "system_prompt.md".
    """
    path = _prompt_file(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ToolError(f"No prompt file '{name}' in {path.parent}") from exc
    return {"name": name, "path": str(path), "chars": len(text), "content": _clip(text)}


@mcp.tool
async def write_prompt_file(name: str, content: str) -> dict[str, Any]:
    """Overwrite an on-disk prompt file, backing up the previous version.

    Affects every chat in this instance and applies on the next ATLAS restart.
    Confirm with the user before changing `system_prompt.md`.

    Args:
        name: Filename ending in .md, inside the prompts directory.
        content: Full replacement text.
    """
    _guard_writes()
    path = _prompt_file(name)
    backup = None
    if path.exists():
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    path.write_text(content, encoding="utf-8")
    return {
        "name": name,
        "path": str(path),
        "chars": len(content),
        "backup": str(backup) if backup else None,
        "note": "Applies on the next ATLAS restart (`systemctl --user restart atlas`).",
    }


# --------------------------------------------------------------------------
# journal (read-only)
# --------------------------------------------------------------------------

# Clamp for the journal tail. journalctl has its own upper bound, but a model
# asking for a million lines would still try to drag them all through stdio
# before we clipped them, so cap the fetch too.
_JOURNAL_MAX_LINES = 10000


def _clamp_lines(lines: int) -> int:
    """Keep a requested line count inside a sane window."""
    if lines < 1:
        return 1
    if lines > _JOURNAL_MAX_LINES:
        return _JOURNAL_MAX_LINES
    return lines


def _journalctl_argv(lines: int, unit: str) -> list[str]:
    """Build the journalctl invocation for the given user unit."""
    return ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager"]


def _journal_env() -> dict[str, str]:
    """Environment for ``journalctl --user`` from a spawned subprocess.

    ATLAS is a systemd --user service, so it (and the MCP children it spawns)
    already inherits ``XDG_RUNTIME_DIR`` and ``DBUS_SESSION_BUS_ADDRESS``.
    Other launch paths -- a bare shell, a test, an exec from outside the
    session -- may not, and ``journalctl --user`` then fails with "No journal
    files were found" / "Failed to determine bus address". Fall back to the
    per-uid runtime dir the same way ``redeploy.sh`` does.
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    runtime = env["XDG_RUNTIME_DIR"]
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(f"{runtime}/bus"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


@mcp.tool
async def tail_atlas_journal(lines: int = 150, unit: str = "atlas") -> dict[str, Any]:
    """Return the last N lines of the ATLAS daemon journal.

    Reads the systemd --user journal for the unit via ``journalctl --user``.
    This is a snapshot, not a live follow -- an MCP tool returns once. Use it
    when a tool call failed and you want to see what ATLAS logged, or to check
    whether the daemon restarted recently.

    Args:
        lines: How many trailing lines to fetch (clamped to 1-10000). Default 150.
        unit: systemd --user unit name. Default "atlas" (the prod daemon on
            :8090). Point at e.g. "osii-mcp" to read a sibling server's log.
    """
    import asyncio

    count = _clamp_lines(lines)
    argv = _journalctl_argv(count, unit)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_journal_env(),
        )
    except FileNotFoundError as exc:
        raise ToolError(f"journalctl not found on PATH: {exc}") from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ToolError(
            f"journalctl exited {proc.returncode} for unit '{unit}': "
            f"{stderr.decode(errors='replace').strip() or '(no stderr)'}"
        )
    text = stdout.decode(errors="replace")
    return {
        "unit": unit,
        "lines_requested": count,
        "lines_returned": len(text.splitlines()),
        "journal": _clip(text),
    }


async def tool_names() -> list[str]:
    """Names of every registered tool — used by the tests and --list-tools."""
    return sorted(tool.name for tool in await mcp.list_tools())
