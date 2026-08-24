"""Tool-layer behaviour that does not need a running ATLAS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from atlas_introspect_mcp import server
from test_mcp_config import SAMPLE, configure


@pytest.fixture
def cfg(tmp_path: Path):
    cfg = configure(tmp_path)
    cfg.mcp_json_path.write_text(json.dumps(SAMPLE, indent=2), encoding="utf-8")
    (cfg.prompts_dir / "system_prompt.md").write_text("original", encoding="utf-8")
    return cfg


async def test_every_tool_is_registered():
    names = await server.tool_names()
    for expected in (
        "list_conversations",
        "get_conversation",
        "get_mcp_config",
        "upsert_mcp_server",
        "list_workspaces",
        "list_custom_prompts",
        "write_prompt_file",
    ):
        assert expected in names


async def test_get_mcp_config_single_server(cfg):
    result = await server.get_mcp_config(server="basic_fns")
    assert list(result["config"]) == ["basic_fns"]
    assert result["warnings"] == []


async def test_get_mcp_config_unknown_server_lists_what_exists(cfg):
    with pytest.raises(ToolError, match="basic_fns"):
        await server.get_mcp_config(server="nope")


async def test_upsert_adds_and_reports_warnings(cfg):
    result = await server.upsert_mcp_server("new_one", {"command": ["relative", "--stdio"]})
    assert result["action"] == "added"
    assert any("systemd's PATH" in w for w in result["warnings"])
    assert "new_one" in json.loads(cfg.mcp_json_path.read_text())


async def test_upsert_replaces_only_the_named_entry(cfg):
    await server.upsert_mcp_server("basic_fns", {"command": ["/abs/bin/other"], "transport": "stdio"})
    data = json.loads(cfg.mcp_json_path.read_text())
    assert data["basic_fns"]["command"] == ["/abs/bin/other"]
    assert "cwd" not in data["basic_fns"]  # wholesale replacement, not a merge


async def test_delete_returns_what_it_removed(cfg):
    result = await server.delete_mcp_server("basic_fns")
    assert result["removed_entry"] == SAMPLE["basic_fns"]
    assert json.loads(cfg.mcp_json_path.read_text()) == {}


async def test_replace_rejects_bad_json_without_touching_the_file(cfg):
    with pytest.raises(ToolError, match="not valid JSON"):
        await server.replace_mcp_config("{oops")
    assert json.loads(cfg.mcp_json_path.read_text()) == SAMPLE


async def test_prompt_file_round_trip(cfg):
    listing = await server.list_prompt_files()
    assert [f["name"] for f in listing["files"]] == ["system_prompt.md"]

    written = await server.write_prompt_file("system_prompt.md", "replaced")
    assert (cfg.prompts_dir / "system_prompt.md").read_text() == "replaced"
    assert Path(written["backup"]).read_text() == "original"

    read = await server.read_prompt_file("system_prompt.md")
    assert read["content"] == "replaced"


async def test_prompt_file_refuses_traversal(cfg):
    with pytest.raises(ToolError, match="not a file directly inside"):
        await server.read_prompt_file("../config/mcp.json")


async def test_prompt_file_refuses_non_markdown(cfg):
    with pytest.raises(ToolError, match="markdown"):
        await server.read_prompt_file("secrets.env")


async def test_read_only_blocks_mutating_tools(tmp_path):
    cfg = configure(tmp_path, read_only=True)
    cfg.mcp_json_path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    with pytest.raises(ToolError, match="read-only"):
        await server.upsert_mcp_server("x", {"command": ["/abs/x"]})
    with pytest.raises(ToolError, match="read-only"):
        await server.write_prompt_file("system_prompt.md", "nope")


async def test_clip_marks_truncation(tmp_path):
    configure(tmp_path)
    clipped = server._clip("x" * 50, limit=10)
    assert clipped.startswith("x" * 10)
    assert "40 more characters" in clipped
