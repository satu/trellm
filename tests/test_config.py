"""Tests for config module."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from trellm.config import Config, load_config, ProjectConfig, TrelloConfig, ClaudeConfig
from trellm.__main__ import compare_configs, configs_equal, parse_project


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
