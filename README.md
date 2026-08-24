# atlas-introspect-mcp

An MCP server that lets [ATLAS](https://github.com/sandialabs/atlas-ui-3) look
at — and edit — itself:

| surface | what you get | how |
|---|---|---|
| **conversations** | list, search, read full chat history | ATLAS REST API (read-only) |
| **`config/mcp.json`** | read, add/replace/delete a server, replace the file | on disk, atomic + backed up |
| **workspaces** | list, create, update, delete | ATLAS REST API |
| **custom prompts** | the per-user prompt library | ATLAS REST API |
| **prompt files** | the on-disk `prompts/*.md` defaults | on disk, backed up |

## Why it goes through the HTTP API

Chat history, workspaces, and prompts all live in one DuckDB file
(`data/chat_history.db`). DuckDB is single-writer and the ATLAS process holds
that write lock for its entire lifetime — a second process opening the file,
**even read-only**, fails with a lock conflict. So this server asks ATLAS over
loopback HTTP instead. That also means per-user scoping is enforced by ATLAS
rather than reimplemented here.

Only `mcp.json` and `prompts/*.md` are touched directly; ATLAS has no API for
either.

Being a separate process is what makes calling back into ATLAS safe: ATLAS
awaits this server's stdio reply while its own event loop stays free to serve
the request. An in-process tool doing the same thing would deadlock.

## Identity

Every query is scoped to one user, sent as `X-User-Email`. With
`DEBUG_MODE=true` ATLAS honours that header; with it off, the header is what
the auth proxy would have supplied, so the same flag works either way. The
default is `test@test.com` — the test user ATLAS falls back to in debug mode,
and therefore the owner of the conversations created through the UI on this
host.

## Install

```bash
git clone https://github.com/garland3/atlas-introspect-mcp
cd atlas-introspect-mcp
uv venv .venv
uv pip install -e ".[dev]" --python .venv/bin/python
```

Standalone check (with ATLAS running):

```bash
.venv/bin/atlas-introspect-mcp --list-tools
.venv/bin/python -c "
import asyncio; from fastmcp import Client
from fastmcp.client.transports import StdioTransport
async def m():
    async with Client(StdioTransport(command='.venv/bin/atlas-introspect-mcp', args=['--stdio'])) as c:
        print(await c.call_tool('atlas_status', {}))
asyncio.run(m())"
```

## Register with ATLAS

Add to your ATLAS `config/mcp.json` (absolute paths — the command is looked up
on the service manager's `PATH`, not your shell's), then restart ATLAS:

```json
"atlas_introspect": {
  "command": [
    "/abs/path/to/atlas-introspect-mcp/.venv/bin/atlas-introspect-mcp",
    "--stdio",
    "--atlas-url", "http://127.0.0.1:8090",
    "--user", "test@test.com"
  ],
  "cwd": "/abs/path/to/atlas-introspect-mcp",
  "transport": "stdio",
  "groups": ["users"],
  "description": "Introspect and configure this ATLAS instance: browse chat history, read/edit config/mcp.json, manage workspaces and custom prompts.",
  "short_description": "ATLAS self-introspection",
  "compliance_level": "Public"
}
```

Add `"--read-only"` to the command for a view-only build; every mutating tool
then refuses instead of writing.

Ports: stdio needs none. `--transport http` defaults to **8101**. It refuses
port 8080 outright — on a host running k3s, klipper-lb DNATs 8080 to Traefik
*including on 127.0.0.1*, so a server bound there receives nothing and every
request comes back as an empty 500 with no line in your log.

## Configuration

Every flag has an environment-variable equivalent.

| flag | env var | default |
|---|---|---|
| `--atlas-url` | `ATLAS_URL` | `http://127.0.0.1:8090` |
| `--user` | `ATLAS_USER` | `test@test.com` |
| `--auth-header` | `ATLAS_AUTH_HEADER` | `X-User-Email` |
| `--atlas-home` | `ATLAS_HOME` | `~/ATLAS-GROUP/atlas-ui-3` |
| `--config-dir` | `ATLAS_CONFIG_DIR` | `<atlas-home>/config` |
| `--prompts-dir` | `ATLAS_PROMPTS_DIR` | `<atlas-home>/prompts` |
| `--timeout` | `ATLAS_TIMEOUT` | `30` seconds |
| `--max-output-chars` | `ATLAS_MAX_OUTPUT` | `100000` |
| `--read-only` | `ATLAS_INTROSPECT_READ_ONLY` | off |
| `--transport` | `ATLAS_INTROSPECT_TRANSPORT` | `stdio` |
| `--port` | `ATLAS_INTROSPECT_PORT` | `8101` (http transport only) |

## Tools

**Conversations (read-only)** — `list_conversations`, `search_conversations`,
`get_conversation`, `list_conversation_tags`

**`config/mcp.json`** — `get_mcp_config`, `upsert_mcp_server`,
`delete_mcp_server`, `replace_mcp_config`

**Workspaces** — `list_workspaces`, `create_workspace`, `update_workspace`,
`delete_workspace`

**Custom prompts** — `list_custom_prompts`, `get_custom_prompt`,
`create_custom_prompt`, `update_custom_prompt`, `delete_custom_prompt`

**On-disk prompt files** — `list_prompt_files`, `read_prompt_file`,
`write_prompt_file`

**Health** — `atlas_status`

## Things that will bite

- **`mcp.json` edits are not live.** ATLAS spawns stdio servers at startup.
  Every write tool says so in its result; apply with
  `systemctl --user restart atlas` or `./redeploy.sh --no-pull`.
- **Editing your own entry works, but only takes effect on restart** — at which
  point a broken entry means this server is gone and you cannot fix it from
  inside ATLAS. Backups land next to the file as `mcp.json.bak-<stamp>`.
- **`upsert_mcp_server` replaces an entry wholesale**, it does not merge. Read
  it first, change it, send the whole thing back. Same for a workspace `config`
  and a prompt's `content`.
- **Conversation tools are read-only by design.** There are delete endpoints in
  ATLAS; they are deliberately not exposed here.
- **`write_prompt_file` changes every chat in the instance.** It backs up first
  and, like `mcp.json`, applies on the next restart.

## Tests

```bash
.venv/bin/pytest
```

They cover the file-editing and validation logic (atomic write, backup,
traversal refusal, lint warnings, read-only mode) against a temp directory —
no running ATLAS needed.
