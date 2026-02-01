"""Tests for maintenance module."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trellm.config import ClaudeConfig, MaintenanceConfig, ProjectConfig, TrelloConfig
from trellm.maintenance import (
    MaintenanceResult,
    _update_maintenance_card,
    build_maintenance_prompt,
    run_maintenance,
    should_run_maintenance,
)
from trellm.state import StateManager
from trellm.trello import TrelloCard, TrelloClient


class TestShouldRunMaintenance:
    """Tests for should_run_maintenance function.

    Maintenance runs when we've completed at least N tickets since last maintenance.
    This is checked BEFORE processing a new ticket:
    - Tickets 1-10 complete, counter=10
    - Ticket 11 arrives, should_run_maintenance(10) returns True
    - Maintenance runs, counter resets to 0
    - Ticket 11 processes, counter becomes 1
    """

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

    def test_maintenance_runs_when_threshold_reached(self):
        """Test that maintenance runs when we've completed at least N tickets."""
        config = MaintenanceConfig(enabled=True, interval=10)

        # Should run when we've completed 10 or more tickets
        assert should_run_maintenance(10, config)
        assert should_run_maintenance(11, config)  # Over threshold also triggers
        assert should_run_maintenance(20, config)
        assert should_run_maintenance(100, config)

    def test_maintenance_skips_below_threshold(self):
        """Test that maintenance doesn't run below the threshold."""
        config = MaintenanceConfig(enabled=True, interval=10)

        # Should not run when we haven't completed enough tickets
        assert not should_run_maintenance(0, config)
        assert not should_run_maintenance(1, config)
        assert not should_run_maintenance(5, config)
        assert not should_run_maintenance(9, config)

    def test_maintenance_skips_zero(self):
        """Test that maintenance doesn't run at ticket count 0."""
        config = MaintenanceConfig(enabled=True, interval=10)
        assert not should_run_maintenance(0, config)

    def test_maintenance_custom_interval(self):
        """Test maintenance with custom interval."""
        config = MaintenanceConfig(enabled=True, interval=5)

        # Should run at 5 or above
        assert should_run_maintenance(5, config)
        assert should_run_maintenance(6, config)
        assert should_run_maintenance(10, config)
        # Should not run below 5
        assert not should_run_maintenance(3, config)
        assert not should_run_maintenance(4, config)


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
        # Should output to Trello, not file
        assert "DO NOT create any files" in prompt
        assert "Trello card" in prompt

    def test_prompt_does_not_write_files(self):
        """Test that prompt explicitly tells Claude not to create files."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=None,
            interval=10,
        )

        # Should NOT mention creating local files
        assert ".claude/maintenance-log.md" not in prompt
        # Should emphasize no file creation
        assert "DO NOT create" in prompt or "DO NOT modify" in prompt

    def test_prompt_uses_last_maintenance_for_git_history(self):
        """Test that git history instruction references last_maintenance timestamp."""
        timestamp = "2026-01-15T10:00:00Z"
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=timestamp,
            interval=10,
        )

        # Should reference commits since last maintenance, not interval
        assert f"all commits since {timestamp}" in prompt
        # Should NOT use the old interval-based instruction
        assert "last 10 commits" not in prompt

    def test_prompt_uses_never_for_git_history_when_no_last_maintenance(self):
        """Test that git history says 'never' when no prior maintenance."""
        prompt = build_maintenance_prompt(
            project="proj",
            ticket_count=10,
            last_maintenance=None,
            interval=10,
        )

        # Should reference "never" for first-time maintenance
        assert "all commits since never" in prompt


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
        """Test that maintenance compacts first then resumes with compacted session."""
        # Create separate mocks for compact and maintenance calls
        compact_proc = AsyncMock()
        compact_proc.returncode = 0
        compact_proc.communicate = AsyncMock(
            return_value=(
                b'{"session_id":"compacted-session-id"}\n',
                b"",
            )
        )

        maintenance_proc = AsyncMock()
        maintenance_proc.returncode = 0
        maintenance_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Done","session_id":"s1"}\n',
                b"",
            )
        )

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call is compact, second is maintenance
            if call_count == 1:
                return compact_proc
            return maintenance_proc

        with patch(
            "asyncio.create_subprocess_exec", side_effect=mock_subprocess
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

            # Should have been called twice: compact then maintenance
            assert mock_exec.call_count == 2

            # First call should be compact with original session
            compact_call_args = mock_exec.call_args_list[0][0]
            assert "/compact" in compact_call_args
            assert "--resume" in compact_call_args
            assert "existing-session-id" in compact_call_args

            # Second call should be maintenance with compacted session
            maintenance_call_args = mock_exec.call_args_list[1][0]
            assert "--resume" in maintenance_call_args
            assert "compacted-session-id" in maintenance_call_args

    @pytest.mark.asyncio
    async def test_run_maintenance_continues_when_compact_fails(self, tmp_path):
        """Test that maintenance continues with original session when compact fails."""
        # Create mock for compact that fails
        compact_proc = AsyncMock()
        compact_proc.returncode = 1  # Non-zero = failure
        compact_proc.communicate = AsyncMock(
            return_value=(b"", b"Compact failed\n")
        )

        maintenance_proc = AsyncMock()
        maintenance_proc.returncode = 0
        maintenance_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Done","session_id":"s1"}\n',
                b"",
            )
        )

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return compact_proc
            return maintenance_proc

        with patch(
            "asyncio.create_subprocess_exec", side_effect=mock_subprocess
        ) as mock_exec:
            result = await run_maintenance(
                project="testproject",
                working_dir=str(tmp_path),
                session_id="existing-session-id",
                claude_config=ClaudeConfig(binary="claude", timeout=60),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

            # Maintenance should still succeed despite compact failure
            assert result.success

            # Should have been called twice
            assert mock_exec.call_count == 2

            # Second call should use original session ID (compact failed)
            maintenance_call_args = mock_exec.call_args_list[1][0]
            assert "--resume" in maintenance_call_args
            assert "existing-session-id" in maintenance_call_args

    @pytest.mark.asyncio
    async def test_run_maintenance_no_compact_without_session(self, tmp_path):
        """Test that maintenance skips compaction when there's no existing session."""
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
                session_id=None,  # No session = no compact
                claude_config=ClaudeConfig(binary="claude", timeout=60),
                maintenance_config=MaintenanceConfig(enabled=True, interval=10),
                ticket_count=10,
                last_maintenance=None,
            )

            # Should only be called once (maintenance, no compact)
            assert mock_exec.call_count == 1

            # Should not have --resume
            call_args = mock_exec.call_args[0]
            assert "--resume" not in call_args


class TestStateManagerMaintenance:
    """Tests for StateManager maintenance tracking methods."""

    def test_get_ticket_count_initial(self, tmp_path):
        """Test getting ticket count when not set."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert manager.get_ticket_count("project1") == 0

    def test_add_processed_ticket(self, tmp_path):
        """Test adding processed tickets."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        count = manager.add_processed_ticket("project1", "card-1")
        assert count == 1
        assert manager.get_ticket_count("project1") == 1

        count = manager.add_processed_ticket("project1", "card-2")
        assert count == 2
        assert manager.get_ticket_count("project1") == 2

    def test_add_processed_ticket_unique_only(self, tmp_path):
        """Test that same ticket processed multiple times counts as one."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add same ticket multiple times
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-1")

        # Should only count as 1
        assert manager.get_ticket_count("project1") == 1

    def test_add_processed_ticket_mixed_unique_and_duplicates(self, tmp_path):
        """Test with mix of unique tickets and duplicates."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Process 3 unique tickets, some multiple times
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        manager.add_processed_ticket("project1", "card-1")  # Duplicate
        manager.add_processed_ticket("project1", "card-3")
        manager.add_processed_ticket("project1", "card-2")  # Duplicate

        # Should count 3 unique tickets
        assert manager.get_ticket_count("project1") == 3

    def test_ticket_count_persistence(self, tmp_path):
        """Test that ticket count is persisted."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.add_processed_ticket("project1", "card-1")
        manager1.add_processed_ticket("project1", "card-2")
        manager1.add_processed_ticket("project1", "card-3")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_ticket_count("project1") == 3

    def test_ticket_count_per_project(self, tmp_path):
        """Test that ticket count is tracked per project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        manager.add_processed_ticket("project2", "card-3")

        assert manager.get_ticket_count("project1") == 2
        assert manager.get_ticket_count("project2") == 1

    def test_backwards_compatibility_with_old_ticket_count(self, tmp_path):
        """Test that old ticket_count format is still read correctly."""
        state_file = tmp_path / "state.json"

        # Write state in old format
        import json
        old_state = {
            "sessions": {
                "project1": {
                    "session_id": "s1",
                    "ticket_count": 5,
                }
            },
            "processed": {},
            "stats": {"global": {}, "by_project": {}, "by_date": {}, "ticket_history": []},
        }
        state_file.write_text(json.dumps(old_state))

        manager = StateManager(str(state_file))
        # Should read old ticket_count when processed_ticket_ids is empty
        assert manager.get_ticket_count("project1") == 5

    def test_migration_from_old_format(self, tmp_path):
        """Test that adding a ticket migrates from old format to new format."""
        state_file = tmp_path / "state.json"

        # Write state in old format
        import json
        old_state = {
            "sessions": {
                "project1": {
                    "session_id": "s1",
                    "ticket_count": 5,
                }
            },
            "processed": {},
            "stats": {"global": {}, "by_project": {}, "by_date": {}, "ticket_history": []},
        }
        state_file.write_text(json.dumps(old_state))

        manager = StateManager(str(state_file))
        # Add a new ticket - this triggers migration
        manager.add_processed_ticket("project1", "card-new")

        # Should now use new format (old ticket_count is lost, only new ticket counted)
        assert manager.get_ticket_count("project1") == 1

        # Verify old ticket_count is removed
        new_state = json.loads(state_file.read_text())
        assert "ticket_count" not in new_state["sessions"]["project1"]
        assert "processed_ticket_ids" in new_state["sessions"]["project1"]

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

    def test_reset_ticket_count(self, tmp_path):
        """Test resetting ticket count after maintenance."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add tickets to simulate completed work
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        manager.add_processed_ticket("project1", "card-3")
        assert manager.get_ticket_count("project1") == 3

        # Reset after maintenance
        manager.reset_ticket_count("project1")
        assert manager.get_ticket_count("project1") == 0

    def test_reset_ticket_count_per_project(self, tmp_path):
        """Test that reset only affects the specified project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        manager.add_processed_ticket("project2", "card-3")
        manager.add_processed_ticket("project2", "card-4")

        # Reset project1 only
        manager.reset_ticket_count("project1")

        assert manager.get_ticket_count("project1") == 0
        assert manager.get_ticket_count("project2") == 2

    def test_reset_ticket_count_persistence(self, tmp_path):
        """Test that reset is persisted."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.add_processed_ticket("project1", "card-1")
        manager1.add_processed_ticket("project1", "card-2")
        manager1.reset_ticket_count("project1")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_ticket_count("project1") == 0

    def test_reset_clears_ticket_ids_for_new_cycle(self, tmp_path):
        """Test that reset clears IDs so same tickets can be counted in next cycle."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # First maintenance cycle
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        assert manager.get_ticket_count("project1") == 2

        # Maintenance runs, reset
        manager.reset_ticket_count("project1")
        assert manager.get_ticket_count("project1") == 0

        # Second cycle - same tickets should count again
        manager.add_processed_ticket("project1", "card-1")
        manager.add_processed_ticket("project1", "card-2")
        assert manager.get_ticket_count("project1") == 2


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

    def test_global_maintenance_config(self, tmp_path):
        """Test loading global maintenance config from YAML."""
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
                "maintenance": {
                    "enabled": True,
                    "interval": 15,
                },
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

        # Global maintenance should be set
        assert config.claude.maintenance is not None
        assert config.claude.maintenance.enabled is True
        assert config.claude.maintenance.interval == 15

    def test_global_maintenance_applies_to_projects(self, tmp_path):
        """Test that global maintenance applies to projects without per-project config."""
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
                "maintenance": {
                    "enabled": True,
                    "interval": 10,
                },
                "projects": {
                    "project_no_config": {
                        "working_dir": "~/src/project1",
                        # No per-project maintenance config
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        # Should use global config
        maint_config = config.get_maintenance_config("project_no_config")
        assert maint_config is not None
        assert maint_config.enabled is True
        assert maint_config.interval == 10

    def test_per_project_overrides_global(self, tmp_path):
        """Test that per-project maintenance config overrides global config."""
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
                "maintenance": {
                    "enabled": True,
                    "interval": 10,
                },
                "projects": {
                    "global_project": {
                        "working_dir": "~/src/global",
                        # Uses global maintenance
                    },
                    "custom_project": {
                        "working_dir": "~/src/custom",
                        "maintenance": {
                            "enabled": True,
                            "interval": 25,  # Custom interval
                        },
                    },
                    "disabled_project": {
                        "working_dir": "~/src/disabled",
                        "maintenance": {
                            "enabled": False,  # Explicitly disabled
                        },
                    },
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        # Project using global config
        global_maint = config.get_maintenance_config("global_project")
        assert global_maint is not None
        assert global_maint.enabled is True
        assert global_maint.interval == 10

        # Project with custom interval
        custom_maint = config.get_maintenance_config("custom_project")
        assert custom_maint is not None
        assert custom_maint.enabled is True
        assert custom_maint.interval == 25

        # Project explicitly disabled
        disabled_maint = config.get_maintenance_config("disabled_project")
        assert disabled_maint is not None
        assert disabled_maint.enabled is False

    def test_no_global_no_project_maintenance(self, tmp_path):
        """Test that without global or per-project config, get_maintenance_config returns None."""
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
                    }
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        # No global, no per-project config
        assert config.claude.maintenance is None
        assert config.get_maintenance_config("myproject") is None


class TestMaintenanceTrelloCard:
    """Tests for Trello card creation/update in maintenance."""

    @pytest.mark.asyncio
    async def test_update_maintenance_card_creates_new(self):
        """Test that a new card is created when none exists."""
        mock_client = AsyncMock(spec=TrelloClient)
        mock_client.find_card_by_name = AsyncMock(return_value=None)
        mock_client.create_card = AsyncMock(
            return_value=TrelloCard(
                id="new-card-id",
                name="testproject regular maintenance",
                description="summary",
                url="https://trello.com/c/abc123",
                last_activity="2026-01-24T00:00:00Z",
            )
        )

        await _update_maintenance_card(
            trello_client=mock_client,
            icebox_list_id="icebox-list-123",
            project="testproject",
            summary="Test maintenance summary",
            prefix="[test] ",
        )

        mock_client.find_card_by_name.assert_called_once_with(
            list_id="icebox-list-123",
            name="testproject regular maintenance",
        )
        mock_client.create_card.assert_called_once_with(
            list_id="icebox-list-123",
            name="testproject regular maintenance",
            description="Test maintenance summary",
        )

    @pytest.mark.asyncio
    async def test_update_maintenance_card_updates_existing(self):
        """Test that existing card is updated when found."""
        existing_card = TrelloCard(
            id="existing-card-id",
            name="testproject regular maintenance",
            description="old summary",
            url="https://trello.com/c/xyz789",
            last_activity="2026-01-20T00:00:00Z",
        )
        mock_client = AsyncMock(spec=TrelloClient)
        mock_client.find_card_by_name = AsyncMock(return_value=existing_card)
        mock_client.update_card_description = AsyncMock()

        await _update_maintenance_card(
            trello_client=mock_client,
            icebox_list_id="icebox-list-123",
            project="testproject",
            summary="New maintenance summary",
            prefix="[test] ",
        )

        mock_client.find_card_by_name.assert_called_once()
        mock_client.update_card_description.assert_called_once_with(
            card_id="existing-card-id",
            description="New maintenance summary",
        )
        # Should not create new card
        mock_client.create_card.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_maintenance_card_handles_error(self):
        """Test that errors in card update are handled gracefully."""
        mock_client = AsyncMock(spec=TrelloClient)
        mock_client.find_card_by_name = AsyncMock(
            side_effect=Exception("API error")
        )

        # Should not raise
        await _update_maintenance_card(
            trello_client=mock_client,
            icebox_list_id="icebox-list-123",
            project="testproject",
            summary="Test summary",
            prefix="[test] ",
        )

    @pytest.mark.asyncio
    async def test_run_maintenance_with_trello_client(self, tmp_path):
        """Test that run_maintenance creates Trello card when configured."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Maintenance findings","session_id":"s1"}\n',
                b"",
            )
        )

        mock_trello = AsyncMock(spec=TrelloClient)
        mock_trello.find_card_by_name = AsyncMock(return_value=None)
        mock_trello.create_card = AsyncMock(
            return_value=TrelloCard(
                id="card-123",
                name="testproject regular maintenance",
                description="Maintenance findings",
                url="https://trello.com/c/abc",
                last_activity="2026-01-24T00:00:00Z",
            )
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
                trello_client=mock_trello,
                icebox_list_id="icebox-list-456",
            )

        assert result.success is True
        # Should have called Trello to create card
        mock_trello.find_card_by_name.assert_called_once()
        mock_trello.create_card.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_maintenance_without_trello_client(self, tmp_path):
        """Test that run_maintenance works without Trello client."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","result":"Done","session_id":"s1"}\n',
                b"",
            )
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
                # No trello_client or icebox_list_id
            )

        assert result.success is True


class TestTrelloConfigIceBox:
    """Tests for icebox_list_id in TrelloConfig."""

    def test_load_icebox_list_id(self, tmp_path):
        """Test loading icebox_list_id from config."""
        import yaml
        from trellm.config import load_config

        config_data = {
            "trello": {
                "api_key": "key",
                "api_token": "token",
                "board_id": "board",
                "todo_list_id": "todo",
                "icebox_list_id": "icebox-123",
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert config.trello.icebox_list_id == "icebox-123"

    def test_icebox_list_id_optional(self, tmp_path):
        """Test that icebox_list_id is optional."""
        import yaml
        from trellm.config import load_config

        config_data = {
            "trello": {
                "api_key": "key",
                "api_token": "token",
                "board_id": "board",
                "todo_list_id": "todo",
                # No icebox_list_id
            },
        }

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))

        assert config.trello.icebox_list_id is None


class TestTrelloClientMethods:
    """Tests for new TrelloClient methods."""

    @pytest.mark.asyncio
    async def test_find_card_by_name_found(self):
        """Test finding a card by name when it exists."""
        config = TrelloConfig(
            api_key="key",
            api_token="token",
            board_id="board",
            todo_list_id="todo",
        )
        client = TrelloClient(config)

        mock_response = [
            {"id": "card1", "name": "Other Card", "desc": "", "url": "url1", "dateLastActivity": "2026-01-01"},
            {"id": "card2", "name": "Target Card", "desc": "desc", "url": "url2", "dateLastActivity": "2026-01-02"},
        ]

        with patch.object(client, "_request", return_value=mock_response):
            result = await client.find_card_by_name("list-123", "target card")

        assert result is not None
        assert result.id == "card2"
        assert result.name == "Target Card"

    @pytest.mark.asyncio
    async def test_find_card_by_name_not_found(self):
        """Test finding a card by name when it doesn't exist."""
        config = TrelloConfig(
            api_key="key",
            api_token="token",
            board_id="board",
            todo_list_id="todo",
        )
        client = TrelloClient(config)

        mock_response = [
            {"id": "card1", "name": "Other Card", "desc": "", "url": "url1", "dateLastActivity": "2026-01-01"},
        ]

        with patch.object(client, "_request", return_value=mock_response):
            result = await client.find_card_by_name("list-123", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_create_card(self):
        """Test creating a new card."""
        config = TrelloConfig(
            api_key="key",
            api_token="token",
            board_id="board",
            todo_list_id="todo",
        )
        client = TrelloClient(config)

        mock_response = {
            "id": "new-card-id",
            "name": "New Card",
            "desc": "Description",
            "url": "https://trello.com/c/abc",
            "dateLastActivity": "2026-01-24",
        }

        with patch.object(client, "_request", return_value=mock_response) as mock_req:
            result = await client.create_card("list-123", "New Card", "Description")

            mock_req.assert_called_once_with(
                "POST",
                "/cards",
                params={
                    "idList": "list-123",
                    "name": "New Card",
                    "desc": "Description",
                },
            )

        assert result.id == "new-card-id"
        assert result.name == "New Card"
        assert result.description == "Description"

    @pytest.mark.asyncio
    async def test_update_card_description(self):
        """Test updating a card's description."""
        config = TrelloConfig(
            api_key="key",
            api_token="token",
            board_id="board",
            todo_list_id="todo",
        )
        client = TrelloClient(config)

        with patch.object(client, "_request", return_value={}) as mock_req:
            await client.update_card_description("card-123", "New description")

            mock_req.assert_called_once_with(
                "PUT",
                "/cards/card-123",
                json_data={"desc": "New description"},
            )
