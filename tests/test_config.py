"""Tests for config module."""

import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from trellm.config import (
    BrowserConfig,
    Config,
    load_config,
    ProjectConfig,
    TrelloConfig,
    ClaudeConfig,
)
from trellm.__main__ import (
    compare_configs,
    configs_equal,
    parse_project,
    _processing_cards,
)


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_env_vars(self, monkeypatch):
        """Test loading config from environment variables."""
        monkeypatch.setenv("TRELLO_API_KEY", "test-key")
        monkeypatch.setenv("TRELLO_API_TOKEN", "test-token")
        monkeypatch.setenv("TRELLO_BOARD_ID", "test-board")
        monkeypatch.setenv("TRELLO_TODO_LIST_ID", "test-list")

        config = load_config("/nonexistent/path")

        assert config.trello.api_key == "test-key"
        assert config.trello.api_token == "test-token"
        assert config.trello.board_id == "test-board"
        assert config.trello.todo_list_id == "test-list"

    def test_load_from_file(self, tmp_path):
        """Test loading config from YAML file."""
        config_data = {
            "trello": {
                "api_key": "file-key",
                "api_token": "file-token",
                "board_id": "file-board",
                "todo_list_id": "file-list",
            },
            "polling": {
                "interval_seconds": 10,
            },
            "claude": {
                "binary": "/usr/bin/claude",
                "projects": {
                    "myproject": {
                        "working_dir": "~/src/myproject",
                        "session_id": "abc123",
                        "compact_prompt": "Preserve API patterns",
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert config.trello.api_key == "file-key"
        assert config.poll_interval == 10
        assert config.claude.binary == "/usr/bin/claude"
        assert "myproject" in config.claude.projects
        assert config.claude.projects["myproject"].session_id == "abc123"
        assert config.claude.projects["myproject"].compact_prompt == "Preserve API patterns"

    def test_env_vars_override_file(self, tmp_path, monkeypatch):
        """Test that environment variables override file values."""
        config_data = {
            "trello": {
                "api_key": "file-key",
                "api_token": "file-token",
                "board_id": "file-board",
                "todo_list_id": "file-list",
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        monkeypatch.setenv("TRELLO_API_KEY", "env-key")

        config = load_config(str(config_file))

        # Env var should override
        assert config.trello.api_key == "env-key"
        # File value should be used
        assert config.trello.api_token == "file-token"

    def test_default_values(self):
        """Test default configuration values."""
        config = load_config("/nonexistent/path")

        assert config.poll_interval == 5
        assert config.state_file == "~/.trellm/state.json"
        assert config.claude.binary == "claude"
        assert config.claude.timeout == 1200


class TestConfig:
    """Tests for Config class methods."""

    def test_get_working_dir(self):
        """Test get_working_dir method."""
        from trellm.config import TrelloConfig, ClaudeConfig

        config = Config(
            trello=TrelloConfig(
                api_key="",
                api_token="",
                board_id="",
                todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "myproject": ProjectConfig(working_dir="~/src/myproject"),
                }
            ),
        )

        assert config.get_working_dir("myproject") == "~/src/myproject"
        assert config.get_working_dir("unknown") is None

    def test_get_initial_session_id(self):
        """Test get_initial_session_id method."""
        from trellm.config import TrelloConfig, ClaudeConfig

        config = Config(
            trello=TrelloConfig(
                api_key="",
                api_token="",
                board_id="",
                todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "myproject": ProjectConfig(
                        working_dir="~/src/myproject",
                        session_id="abc123",
                    ),
                }
            ),
        )

        assert config.get_initial_session_id("myproject") == "abc123"
        assert config.get_initial_session_id("unknown") is None

    def test_get_compact_prompt(self):
        """Test get_compact_prompt method."""
        from trellm.config import TrelloConfig, ClaudeConfig

        config = Config(
            trello=TrelloConfig(
                api_key="",
                api_token="",
                board_id="",
                todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "myproject": ProjectConfig(
                        working_dir="~/src/myproject",
                        compact_prompt="Preserve API patterns and test conventions",
                    ),
                    "nocompact": ProjectConfig(
                        working_dir="~/src/nocompact",
                    ),
                }
            ),
        )

        assert config.get_compact_prompt("myproject") == "Preserve API patterns and test conventions"
        assert config.get_compact_prompt("nocompact") is None
        assert config.get_compact_prompt("unknown") is None


class TestResolveProject:
    """Tests for Config.resolve_project method."""

    def _make_config(self) -> Config:
        return Config(
            trello=TrelloConfig(
                api_key="",
                api_token="",
                board_id="",
                todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "smugcoin": ProjectConfig(
                        working_dir="~/src/smugcoin",
                        aliases=["smg"],
                    ),
                    "myproject": ProjectConfig(
                        working_dir="~/src/myproject",
                        aliases=["mp", "myp"],
                    ),
                    "noalias": ProjectConfig(
                        working_dir="~/src/noalias",
                    ),
                }
            ),
        )

    def test_resolve_direct_name(self):
        """Test resolving a canonical project name."""
        config = self._make_config()
        assert config.resolve_project("smugcoin") == "smugcoin"
        assert config.resolve_project("myproject") == "myproject"
        assert config.resolve_project("noalias") == "noalias"

    def test_resolve_alias(self):
        """Test resolving a project alias."""
        config = self._make_config()
        assert config.resolve_project("smg") == "smugcoin"
        assert config.resolve_project("mp") == "myproject"
        assert config.resolve_project("myp") == "myproject"

    def test_resolve_unknown(self):
        """Test resolving an unknown name returns None."""
        config = self._make_config()
        assert config.resolve_project("unknown") is None
        assert config.resolve_project("") is None

    def test_direct_name_takes_priority_over_alias(self):
        """Test that direct project name match takes priority over alias."""
        config = Config(
            trello=TrelloConfig(
                api_key="", api_token="", board_id="", todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "smg": ProjectConfig(working_dir="~/src/smg"),
                    "smugcoin": ProjectConfig(
                        working_dir="~/src/smugcoin",
                        aliases=["smg"],
                    ),
                }
            ),
        )
        # Direct name match should win over alias
        assert config.resolve_project("smg") == "smg"


class TestGetAllProjectNames:
    """Tests for Config.get_all_project_names method."""

    def test_projects_without_aliases(self):
        """Test with projects that have no aliases."""
        config = Config(
            trello=TrelloConfig(
                api_key="", api_token="", board_id="", todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "proj1": ProjectConfig(working_dir="~/src/proj1"),
                    "proj2": ProjectConfig(working_dir="~/src/proj2"),
                }
            ),
        )
        assert config.get_all_project_names() == {"proj1", "proj2"}

    def test_projects_with_aliases(self):
        """Test with projects that have aliases."""
        config = Config(
            trello=TrelloConfig(
                api_key="", api_token="", board_id="", todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects={
                    "smugcoin": ProjectConfig(
                        working_dir="~/src/smugcoin",
                        aliases=["smg"],
                    ),
                    "myproject": ProjectConfig(
                        working_dir="~/src/myproject",
                        aliases=["mp", "myp"],
                    ),
                }
            ),
        )
        assert config.get_all_project_names() == {
            "smugcoin", "smg", "myproject", "mp", "myp",
        }


class TestLoadConfigWithAliases:
    """Tests for loading aliases from config file."""

    def test_load_aliases_from_file(self, tmp_path):
        """Test loading project aliases from YAML config."""
        config_data = {
            "trello": {
                "api_key": "key",
                "api_token": "token",
                "board_id": "board",
                "todo_list_id": "list",
            },
            "claude": {
                "projects": {
                    "smugcoin": {
                        "working_dir": "~/src/smugcoin",
                        "aliases": ["smg", "sc"],
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert "smugcoin" in config.claude.projects
        assert config.claude.projects["smugcoin"].aliases == ["smg", "sc"]
        assert config.resolve_project("smg") == "smugcoin"
        assert config.resolve_project("sc") == "smugcoin"

    def test_load_no_aliases_defaults_empty(self, tmp_path):
        """Test that missing aliases defaults to empty list."""
        config_data = {
            "trello": {
                "api_key": "key",
                "api_token": "token",
                "board_id": "board",
                "todo_list_id": "list",
            },
            "claude": {
                "projects": {
                    "myproject": {
                        "working_dir": "~/src/myproject",
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert config.claude.projects["myproject"].aliases == []


class TestConfigComparison:
    """Tests for config comparison functions."""

    def _make_config(self, **overrides) -> Config:
        """Create a base config with optional overrides."""
        base = {
            "trello": TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
                ready_to_try_list_id="ready",
            ),
            "claude": ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(working_dir="~/src/proj1")},
            ),
            "poll_interval": 5,
            "state_file": "~/.trellm/state.json",
        }
        base.update(overrides)
        return Config(**base)

    def test_configs_equal_same(self):
        """Test that identical configs are equal."""
        config1 = self._make_config()
        config2 = self._make_config()

        assert configs_equal(config1, config2)
        assert compare_configs(config1, config2) == []

    def test_configs_equal_poll_interval_change(self):
        """Test detecting poll_interval change."""
        config1 = self._make_config(poll_interval=5)
        config2 = self._make_config(poll_interval=10)

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert len(changes) == 1
        assert "poll_interval: 5 → 10" in changes[0]

    def test_configs_equal_claude_binary_change(self):
        """Test detecting claude binary change."""
        config1 = self._make_config()
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="/usr/bin/claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(working_dir="~/src/proj1")},
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("claude.binary" in c for c in changes)

    def test_configs_equal_yolo_change(self):
        """Test detecting yolo flag change."""
        config1 = self._make_config()
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=True,
                projects={"proj1": ProjectConfig(working_dir="~/src/proj1")},
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("claude.yolo" in c for c in changes)

    def test_configs_equal_project_added(self):
        """Test detecting added project."""
        config1 = self._make_config()
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={
                    "proj1": ProjectConfig(working_dir="~/src/proj1"),
                    "proj2": ProjectConfig(working_dir="~/src/proj2"),
                },
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("Added project: proj2" in c for c in changes)

    def test_configs_equal_project_removed(self):
        """Test detecting removed project."""
        config1 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={
                    "proj1": ProjectConfig(working_dir="~/src/proj1"),
                    "proj2": ProjectConfig(working_dir="~/src/proj2"),
                },
            )
        )
        config2 = self._make_config()

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("Removed project: proj2" in c for c in changes)

    def test_configs_equal_project_working_dir_changed(self):
        """Test detecting project working_dir change."""
        config1 = self._make_config()
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(working_dir="~/src/proj1-new")},
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("proj1.working_dir" in c for c in changes)

    def test_configs_equal_project_compact_prompt_changed(self):
        """Test detecting project compact_prompt change."""
        config1 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(
                    working_dir="~/src/proj1",
                    compact_prompt="Old prompt",
                )},
            )
        )
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(
                    working_dir="~/src/proj1",
                    compact_prompt="New prompt",
                )},
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("proj1.compact_prompt" in c for c in changes)

    def test_configs_equal_aliases_changed(self):
        """Test detecting project aliases change."""
        config1 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(
                    working_dir="~/src/proj1",
                    aliases=["p1"],
                )},
            )
        )
        config2 = self._make_config(
            claude=ClaudeConfig(
                binary="claude",
                timeout=600,
                yolo=False,
                projects={"proj1": ProjectConfig(
                    working_dir="~/src/proj1",
                    aliases=["p1", "pr1"],
                )},
            )
        )

        assert not configs_equal(config1, config2)
        changes = compare_configs(config1, config2)
        assert any("proj1.aliases" in c for c in changes)


class TestParseProject:
    """Tests for parse_project function."""

    def test_parse_simple_project_name(self):
        """Test parsing project name from simple card title."""
        assert parse_project("myproject implement feature") == "myproject"

    def test_parse_project_with_colon(self):
        """Test parsing project name with colon separator."""
        assert parse_project("myproject: implement feature") == "myproject"

    def test_parse_project_uppercase(self):
        """Test that project name is lowercased."""
        assert parse_project("MyProject fix bug") == "myproject"

    def test_parse_project_empty_name(self):
        """Test parsing empty card name returns 'unknown'."""
        assert parse_project("") == "unknown"

    def test_parse_project_single_word(self):
        """Test parsing single word card name."""
        assert parse_project("trellm") == "trellm"

    def test_parse_project_colon_only(self):
        """Test that trailing colon is stripped."""
        assert parse_project("trellm:") == "trellm"


class TestBrowserConfig:
    """Tests for the BrowserConfig dataclass and the global / per-project
    `browser` block (M2 of the patchright-mcp integration).

    Why this exists: trellm needs an opt-in switch for whether each Claude
    subprocess is launched with `--mcp-config` pointing at patchright-mcp-lite.
    Spawning the MCP server when it isn't built yet (or when the project
    doesn't need a browser) would only waste resources, so the flag stays
    off by default. We support a global default plus a per-project override
    so the rollout can be done one project at a time.

    The single accessor is `Config.is_browser_enabled(project)` — callers
    must not reach into the dataclasses directly.
    """

    def test_browser_config_defaults_disabled(self):
        """A bare BrowserConfig() must default to enabled=False so adding
        the field doesn't accidentally turn the flag on for existing setups."""
        assert BrowserConfig().enabled is False

    def test_global_browser_config_loaded_from_yaml(self, tmp_path):
        config_data = {
            "trello": {
                "api_key": "k", "api_token": "t",
                "board_id": "b", "todo_list_id": "l",
            },
            "claude": {
                "browser": {"enabled": True},
                "projects": {"p1": {"working_dir": "~/src/p1"}},
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))
        assert config.claude.browser is not None
        assert config.claude.browser.enabled is True

    def test_project_browser_config_loaded_from_yaml(self, tmp_path):
        config_data = {
            "trello": {
                "api_key": "k", "api_token": "t",
                "board_id": "b", "todo_list_id": "l",
            },
            "claude": {
                "projects": {
                    "p1": {
                        "working_dir": "~/src/p1",
                        "browser": {"enabled": True},
                    }
                },
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))
        proj = config.claude.projects["p1"]
        assert proj.browser is not None
        assert proj.browser.enabled is True

    def test_missing_browser_block_yields_none(self, tmp_path):
        """No `browser:` block in yaml leaves the field as None at both
        levels — distinguishes 'not set' from 'set to false'."""
        config_data = {
            "trello": {
                "api_key": "k", "api_token": "t",
                "board_id": "b", "todo_list_id": "l",
            },
            "claude": {
                "projects": {"p1": {"working_dir": "~/src/p1"}},
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))
        assert config.claude.browser is None
        assert config.claude.projects["p1"].browser is None


class TestIsBrowserEnabled:
    """Tests for Config.is_browser_enabled(project).

    Resolution order: per-project override > global > False. Mirrors the
    pattern of get_maintenance_config but returns a plain bool because
    callers only need the on/off bit when deciding whether to append
    `--mcp-config` to the claude subprocess command.
    """

    def _make_config(self, *, global_browser=None, project_browsers=None):
        projects = {}
        for name, browser in (project_browsers or {}).items():
            projects[name] = ProjectConfig(
                working_dir=f"~/src/{name}",
                browser=browser,
            )
        return Config(
            trello=TrelloConfig(
                api_key="", api_token="", board_id="", todo_list_id="",
            ),
            claude=ClaudeConfig(
                projects=projects,
                browser=global_browser,
            ),
        )

    def test_default_returns_false(self):
        """No browser config anywhere → False (regression safety)."""
        config = self._make_config(project_browsers={"p1": None})
        assert config.is_browser_enabled("p1") is False

    def test_global_enabled_propagates_to_project(self):
        config = self._make_config(
            global_browser=BrowserConfig(enabled=True),
            project_browsers={"p1": None},
        )
        assert config.is_browser_enabled("p1") is True

    def test_project_enabled_overrides_global_disabled(self):
        config = self._make_config(
            global_browser=BrowserConfig(enabled=False),
            project_browsers={"p1": BrowserConfig(enabled=True)},
        )
        assert config.is_browser_enabled("p1") is True

    def test_project_disabled_overrides_global_enabled(self):
        """Lets us blacklist one project from a global rollout."""
        config = self._make_config(
            global_browser=BrowserConfig(enabled=True),
            project_browsers={"p1": BrowserConfig(enabled=False)},
        )
        assert config.is_browser_enabled("p1") is False

    def test_unknown_project_falls_back_to_global(self):
        """If the project isn't in config (e.g. card with unknown prefix),
        the global setting still applies."""
        config = self._make_config(
            global_browser=BrowserConfig(enabled=True),
            project_browsers={},
        )
        assert config.is_browser_enabled("unknown") is True

    def test_unknown_project_with_no_global_returns_false(self):
        config = self._make_config(
            global_browser=None,
            project_browsers={},
        )
        assert config.is_browser_enabled("unknown") is False


class TestPatchrightMcpConfigJson:
    """Tests for Config.patchright_mcp_config_json().

    This is the JSON string trellm passes to `claude --mcp-config <json>`.
    It must be valid JSON, declare a single `patchright` MCP server, point
    at the locally-built patchright-mcp-lite, and feed it the CDP endpoint
    and the browser-restart command that scripts/start-browser.sh provides.
    """

    def _bare_config(self, **overrides):
        claude_kwargs = {"projects": {}}
        claude_kwargs.update(overrides)
        return Config(
            trello=TrelloConfig(
                api_key="", api_token="", board_id="", todo_list_id="",
            ),
            claude=ClaudeConfig(**claude_kwargs),
        )

    def test_returns_valid_json(self):
        config = self._bare_config()
        parsed = json.loads(config.patchright_mcp_config_json())
        assert "mcpServers" in parsed

    def test_declares_patchright_server_with_node_command(self):
        config = self._bare_config()
        parsed = json.loads(config.patchright_mcp_config_json())
        server = parsed["mcpServers"]["patchright"]
        assert server["command"] == "node"
        # Path arg must point at the patchright-mcp-lite dist entry.
        joined_args = " ".join(server["args"])
        assert "patchright-mcp-lite" in joined_args
        assert joined_args.endswith("dist/index.js")

    def test_passes_cdp_endpoint_in_env(self):
        """patchright-mcp-lite reads CDP_ENDPOINT to know where to attach."""
        config = self._bare_config()
        parsed = json.loads(config.patchright_mcp_config_json())
        env = parsed["mcpServers"]["patchright"]["env"]
        assert env["CDP_ENDPOINT"] == "http://localhost:9222"

    def test_passes_browser_restart_command_in_env(self):
        """patchright-mcp-lite reads BROWSER_RESTART_CMD to recover when
        the CDP endpoint is unreachable (see connection.ts)."""
        config = self._bare_config()
        parsed = json.loads(config.patchright_mcp_config_json())
        env = parsed["mcpServers"]["patchright"]["env"]
        assert env["BROWSER_RESTART_CMD"].endswith("start-browser.sh start")

    def test_path_is_configurable_via_browser_config(self):
        """An override on BrowserConfig.patchright_path lets us point at a
        non-default checkout (e.g. for CI smoke tests)."""
        custom_path = "/tmp/custom-patchright/dist/index.js"
        config = self._bare_config(
            browser=BrowserConfig(enabled=True, patchright_path=custom_path),
        )
        parsed = json.loads(config.patchright_mcp_config_json())
        server = parsed["mcpServers"]["patchright"]
        assert custom_path in server["args"]


class TestProcessingCardsTracking:
    """Tests for _processing_cards set that tracks in-flight cards."""

    def test_processing_cards_is_set(self):
        """Test that _processing_cards is a set type."""
        assert isinstance(_processing_cards, set)

    def test_processing_cards_add_remove(self):
        """Test adding and removing cards from processing set."""
        test_card_id = "test_card_123"

        # Ensure clean state
        _processing_cards.discard(test_card_id)

        # Add card
        _processing_cards.add(test_card_id)
        assert test_card_id in _processing_cards

        # Remove card
        _processing_cards.discard(test_card_id)
        assert test_card_id not in _processing_cards
