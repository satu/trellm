"""Tests for config module."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from trellm.config import Config, load_config, ProjectConfig


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
        assert config.claude.timeout == 600


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
