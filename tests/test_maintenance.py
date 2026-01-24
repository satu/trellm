"""Tests for maintenance module."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trellm.config import ClaudeConfig, MaintenanceConfig, ProjectConfig
from trellm.maintenance import (
    MaintenanceResult,
    build_maintenance_prompt,
    run_maintenance,
    should_run_maintenance,
)
from trellm.state import StateManager


class TestShouldRunMaintenance:
    """Tests for should_run_maintenance function."""

    def test_maintenance_disabled(self):
        """Test that maintenance doesn't run when disabled."""
        config = MaintenanceConfig(enabled=False, interval=10)
        assert not should_run_maintenance(10, config)
        assert not should_run_maintenance(20, config)
        assert not should_run_maintenance(100, config)

    def test_maintenance_no_config(self):
        """Test that maintenance doesn't run when config is None."""
        assert not should_run_maintenance(10, None)
        assert not should_run_maintenance(100, None)

    def test_maintenance_runs_on_interval(self):
        """Test that maintenance runs on interval boundaries."""
        config = MaintenanceConfig(enabled=True, interval=10)

        # Should run at multiples of 10
        assert should_run_maintenance(10, config)
        assert should_run_maintenance(20, config)
        assert should_run_maintenance(100, config)

    def test_maintenance_skips_non_interval(self):
        """Test that maintenance doesn't run on non-interval counts."""
        config = MaintenanceConfig(enabled=True, interval=10)

        # Should not run at non-multiples of 10
        assert not should_run_maintenance(1, config)
        assert not should_run_maintenance(5, config)
        assert not should_run_maintenance(11, config)
        assert not should_run_maintenance(99, config)

    def test_maintenance_skips_zero(self):
        """Test that maintenance doesn't run at ticket count 0."""
        config = MaintenanceConfig(enabled=True, interval=10)
        assert not should_run_maintenance(0, config)

    def test_maintenance_custom_interval(self):
        """Test maintenance with custom interval."""
        config = MaintenanceConfig(enabled=True, interval=5)

        assert should_run_maintenance(5, config)
        assert should_run_maintenance(10, config)
        assert should_run_maintenance(15, config)
        assert not should_run_maintenance(3, config)
        assert not should_run_maintenance(7, config)


class TestBuildMaintenancePrompt:
    """Tests for build_maintenance_prompt function."""

    def test_prompt_contains_project_name(self):
        """Test that prompt includes project name."""
        prompt = build_maintenance_prompt(
            project="myproject",
            ticket_count=10,
            last_maintenance=None,
            interval=10,
        )
        assert "myproject" in prompt

    def test_prompt_contains_ticket_count(self):
        """Test that prompt includes ticket count."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=50,
            last_maintenance=None,
            interval=10,
        )
        assert "50" in prompt

    def test_prompt_contains_interval(self):
        """Test that prompt includes interval."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=None,
            interval=15,
        )
        assert "15" in prompt

    def test_prompt_contains_last_maintenance(self):
        """Test that prompt includes last maintenance timestamp."""
        timestamp = "2026-01-20T10:00:00Z"
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=timestamp,
            interval=10,
        )
        assert timestamp in prompt

    def test_prompt_handles_no_last_maintenance(self):
        """Test that prompt handles None last_maintenance."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=None,
            interval=10,
        )
        assert "never" in prompt

    def test_prompt_contains_maintenance_tasks(self):
        """Test that prompt includes all maintenance task sections."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=None,
            interval=10,
        )

        # Check for main sections
        assert "CLAUDE.md" in prompt
        assert "Compaction Prompt" in prompt
        assert "Documentation Freshness" in prompt
        assert "Maintenance Log" in prompt
        assert ".claude/maintenance-log.md" in prompt


class TestMaintenanceResult:
    """Tests for MaintenanceResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful result."""
        result = MaintenanceResult(
            success=True,
            summary="Maintenance completed successfully",
            session_id="session-123",
        )
        assert result.success is True
        assert result.summary == "Maintenance completed successfully"
        assert result.session_id == "session-123"

    def test_failed_result(self):
        """Test creating a failed result."""
        result = MaintenanceResult(
            success=False,
            summary="Maintenance failed: timeout",
        )
        assert result.success is False
        assert result.summary == "Maintenance failed: timeout"
        assert result.session_id is None


class TestRunMaintenance:
    """Tests for run_maintenance function."""

    @pytest.mark.asyncio
    async def test_run_maintenance_success(self, tmp_path):
        """Test successful maintenance run."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Maintenance completed","session_id":"maint-session-123"}\n',
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_maintenance(
                project="testproject",
                working_dir=str(tmp_path),
                session_id="existing-session",
                claude_config=ClaudeConfig(binary="claude", timeout=60),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

        assert result.success is True
        assert result.session_id == "maint-session-123"

    @pytest.mark.asyncio
    async def test_run_maintenance_failure(self, tmp_path):
        """Test maintenance run that fails."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Error: command failed")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_maintenance(
                project="testproject",
                working_dir=str(tmp_path),
                session_id=None,
                claude_config=ClaudeConfig(binary="claude", timeout=60),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

        assert result.success is False
        assert "failed" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_run_maintenance_timeout(self, tmp_path):
        """Test maintenance run that times out."""
        import asyncio

        async def mock_communicate():
            await asyncio.sleep(10)  # Will timeout
            return (b"", b"")

        mock_proc = AsyncMock()
        mock_proc.communicate = mock_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await run_maintenance(
                    project="testproject",
                    working_dir=str(tmp_path),
                    session_id=None,
                    claude_config=ClaudeConfig(binary="claude", timeout=60),
                    maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                    ticket_count=10,
                    last_maintenance=None,
                )

        assert result.success is False
        assert "timed out" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_run_maintenance_with_yolo_flag(self, tmp_path):
        """Test that yolo flag is passed to subprocess."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Done","session_id":"s1"}\n',
                b"",
            )
        )

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await run_maintenance(
                project="testproject",
                working_dir=str(tmp_path),
                session_id=None,
                claude_config=ClaudeConfig(binary="claude", timeout=60, yolo=True),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

            # Check that --dangerously-skip-permissions was passed
            call_args = mock_exec.call_args
            assert "--dangerously-skip-permissions" in call_args[0]

    @pytest.mark.asyncio
    async def test_run_maintenance_resumes_session(self, tmp_path):
        """Test that maintenance resumes existing session."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Done","session_id":"s1"}\n',
                b"",
            )
        )

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await run_maintenance(
                project="testproject",
                working_dir=str(tmp_path),
                session_id="existing-session-id",
                claude_config=ClaudeConfig(binary="claude", timeout=60),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

            # Check that --resume was passed with session ID
            call_args = mock_exec.call_args
            assert "--resume" in call_args[0]
            assert "existing-session-id" in call_args[0]


class TestStateManagerMaintenance:
    """Tests for StateManager maintenance tracking methods."""

    def test_get_ticket_count_initial(self, tmp_path):
        """Test getting ticket count when not set."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert manager.get_ticket_count("project1") == 0

    def test_increment_ticket_count(self, tmp_path):
        """Test incrementing ticket count."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        count = manager.increment_ticket_count("project1")
        assert count == 1
        assert manager.get_ticket_count("project1") == 1

        count = manager.increment_ticket_count("project1")
        assert count == 2
        assert manager.get_ticket_count("project1") == 2

    def test_ticket_count_persistence(self, tmp_path):
        """Test that ticket count is persisted."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.increment_ticket_count("project1")
        manager1.increment_ticket_count("project1")
        manager1.increment_ticket_count("project1")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_ticket_count("project1") == 3

    def test_ticket_count_per_project(self, tmp_path):
        """Test that ticket count is tracked per project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.increment_ticket_count("project1")
        manager.increment_ticket_count("project1")
        manager.increment_ticket_count("project2")

        assert manager.get_ticket_count("project1") == 2
        assert manager.get_ticket_count("project2") == 1

    def test_get_last_maintenance_initial(self, tmp_path):
        """Test getting last maintenance when not set."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert manager.get_last_maintenance("project1") is None

    def test_set_last_maintenance(self, tmp_path):
        """Test setting last maintenance timestamp."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.set_last_maintenance("project1")

        last_maint = manager.get_last_maintenance("project1")
        assert last_maint is not None
        # Should be a valid ISO timestamp
        datetime.fromisoformat(last_maint.replace("Z", "+00:00"))

    def test_last_maintenance_persistence(self, tmp_path):
        """Test that last maintenance is persisted."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.set_last_maintenance("project1")
        expected = manager1.get_last_maintenance("project1")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_last_maintenance("project1") == expected

    def test_last_maintenance_per_project(self, tmp_path):
        """Test that last maintenance is tracked per project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.set_last_maintenance("project1")

        assert manager.get_last_maintenance("project1") is not None
        assert manager.get_last_maintenance("project2") is None


class TestConfigMaintenance:
    """Tests for maintenance config loading."""

    def test_load_maintenance_config(self, tmp_path):
        """Test loading maintenance config from YAML."""
        import yaml
        from trellm.config import load_config

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
                        "maintenance": {
                            "enabled": True,
                            "interval": 15,
                        },
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert "myproject" in config.claude.projects
        proj_config = config.claude.projects["myproject"]
        assert proj_config.maintenance is not None
        assert proj_config.maintenance.enabled is True
        assert proj_config.maintenance.interval == 15

    def test_load_maintenance_config_defaults(self, tmp_path):
        """Test that maintenance config uses defaults when not fully specified."""
        import yaml
        from trellm.config import load_config

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
                        "maintenance": {
                            "enabled": True,
                            # No interval specified
                        },
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        proj_config = config.claude.projects["myproject"]
        assert proj_config.maintenance is not None
        assert proj_config.maintenance.interval == 10  # Default value

    def test_load_no_maintenance_config(self, tmp_path):
        """Test that projects without maintenance config have None."""
        import yaml
        from trellm.config import load_config

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
                        # No maintenance section
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        proj_config = config.claude.projects["myproject"]
        assert proj_config.maintenance is None

    def test_get_maintenance_config_method(self, tmp_path):
        """Test Config.get_maintenance_config method."""
        import yaml
        from trellm.config import load_config

        config_data = {
            "trello": {
                "api_key": "key",
                "api_token": "token",
                "board_id": "board",
                "todo_list_id": "list",
            },
            "claude": {
                "projects": {
                    "with_maint": {
                        "working_dir": "~/src/p1",
                        "maintenance": {
                            "enabled": True,
                            "interval": 20,
                        },
                    },
                    "without_maint": {
                        "working_dir": "~/src/p2",
                    },
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        # Project with maintenance
        maint_config = config.get_maintenance_config("with_maint")
        assert maint_config is not None
        assert maint_config.enabled is True
        assert maint_config.interval == 20

        # Project without maintenance
        assert config.get_maintenance_config("without_maint") is None

        # Unknown project
        assert config.get_maintenance_config("unknown") is None
