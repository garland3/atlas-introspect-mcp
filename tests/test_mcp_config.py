"""File-editing behaviour: backup, atomicity, validation, read-only refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_introspect_mcp import mcp_config
from atlas_introspect_mcp.config import ServerConfig, set_config
from atlas_introspect_mcp.mcp_config import McpConfigError

SAMPLE = {
    "basic_fns": {
        "command": ["/abs/.venv/bin/basic-fns-mcp", "--stdio"],
        "cwd": "/abs",
        "transport": "stdio",
        "groups": ["users"],
        "description": "files and shell",
    }
}


def configure(tmp_path: Path, *, read_only: bool = False) -> ServerConfig:
    config_dir = tmp_path / "config"
    prompts_dir = tmp_path / "prompts"
    config_dir.mkdir(exist_ok=True)
    prompts_dir.mkdir(exist_ok=True)
    cfg = ServerConfig(
        base_url="http://127.0.0.1:8090",
        user_email="test@test.com",
        auth_header="X-User-Email",
        config_dir=config_dir,
        prompts_dir=prompts_dir,
        timeout=5.0,
        max_output_chars=1000,
        read_only=read_only,
    )
    set_config(cfg)
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> ServerConfig:
    cfg = configure(tmp_path)
    cfg.mcp_json_path.write_text(json.dumps(SAMPLE, indent=2), encoding="utf-8")
    return cfg


def test_load_round_trips(cfg: ServerConfig):
    assert mcp_config.load() == SAMPLE


def test_missing_file_names_the_path(tmp_path: Path):
    cfg = configure(tmp_path)
    with pytest.raises(McpConfigError, match=str(cfg.mcp_json_path)):
        mcp_config.load()


def test_parse_rejects_non_object_document():
    with pytest.raises(McpConfigError, match="object mapping"):
        mcp_config.parse("[1, 2]")


def test_parse_rejects_non_object_entry():
    with pytest.raises(McpConfigError, match="server 'x'"):
        mcp_config.parse('{"x": "not an object"}')


def test_save_backs_up_previous_contents(cfg: ServerConfig):
    updated = dict(SAMPLE)
    updated["extra"] = {"command": ["/abs/bin/x"], "transport": "stdio", "groups": ["users"]}
    result = mcp_config.save(updated)

    assert json.loads(cfg.mcp_json_path.read_text()) == updated
    assert json.loads(Path(result["backup"]).read_text()) == SAMPLE
    assert "restart atlas" in result["note"]


def test_save_leaves_no_temp_files_behind(cfg: ServerConfig):
    mcp_config.save(SAMPLE)
    leftovers = [p.name for p in cfg.config_dir.iterdir() if p.name.startswith(".mcp.json.")]
    assert leftovers == []


def test_read_only_refuses_to_write(tmp_path: Path):
    cfg = configure(tmp_path, read_only=True)
    cfg.mcp_json_path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    with pytest.raises(McpConfigError, match="read-only"):
        mcp_config.save({})
    assert json.loads(cfg.mcp_json_path.read_text()) == SAMPLE


class TestLint:
    def test_clean_entry_has_no_warnings(self):
        assert mcp_config.lint("basic_fns", SAMPLE["basic_fns"]) == []

    def test_relative_command_is_flagged(self):
        warnings = mcp_config.lint("x", {"command": ["python", "main.py"], "groups": ["u"], "description": "d"})
        assert any("systemd's PATH" in w for w in warnings)

    def test_port_8080_is_flagged(self):
        warnings = mcp_config.lint(
            "x",
            {"url": "http://127.0.0.1:8080/mcp", "transport": "http", "groups": ["u"], "description": "d"},
        )
        assert any("8080" in w for w in warnings)

    def test_missing_transport_and_command_is_flagged(self):
        warnings = mcp_config.lint("x", {})
        assert any("needs either" in w for w in warnings)

    def test_url_without_http_transport_is_flagged(self):
        warnings = mcp_config.lint(
            "x", {"url": "http://127.0.0.1:8101/mcp", "transport": "stdio", "groups": ["u"], "description": "d"}
        )
        assert any("expected 'http'" in w for w in warnings)
