"""Tests for __main__ module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from trellm.__main__ import (
    parse_project,
    is_stats_command,
    is_maintenance_command,
    is_reset_session_command,
    is_abort_command,
    is_restart_command,
    handle_stats_command,
    handle_maintenance_command,
    handle_reset_session_command,
    handle_abort_command,
    handle_restart_command,
    RestartRequested,
)
from trellm.config import (
    Config,
    TrelloConfig,
    ClaudeConfig,
    ProjectConfig,
    MaintenanceConfig,
)
from trellm.maintenance import MaintenanceResult
from trellm.trello import TrelloCard


class TestParseProject:
    """Tests for parse_project function."""

    def test_parse_simple_project(self):
        """Test parsing simple project name."""
        assert parse_project("myproject Add new feature") == "myproject"

    def test_parse_project_with_colon(self):
        """Test parsing project name with colon."""
        assert parse_project("myproject: Add new feature") == "myproject"

    def test_parse_project_uppercase(self):
        """Test that project names are lowercased."""
        assert parse_project("MyProject: Add feature") == "myproject"

    def test_parse_empty_name(self):
        """Test parsing empty card name."""
        assert parse_project("") == "unknown"


class TestIsStatsCommand:
    """Tests for is_stats_command function."""

    def test_stats_command_basic(self):
        """Test basic /stats command detection."""
        assert is_stats_command("project /stats")
        assert is_stats_command("trellm /stats")

    def test_stats_command_with_colon(self):
        """Test /stats with project colon format."""
        assert is_stats_command("project: /stats")

    def test_stats_command_case_insensitive(self):
        """Test /stats is case insensitive."""
        assert is_stats_command("project /STATS")
        assert is_stats_command("project /Stats")

    def test_not_stats_command(self):
        """Test regular cards are not detected as /stats."""
        assert not is_stats_command("project Add stats feature")
        assert not is_stats_command("trellm Fix bug")
        assert not is_stats_command("project / stats")  # space breaks command

    def test_stats_not_after_project_name(self):
        """Test /stats must appear immediately after project name."""
        # /stats appearing later in the card name should NOT match
        assert not is_stats_command("trellm problem with the /stats command")
        assert not is_stats_command("project fix /stats display")
        assert not is_stats_command("myapp bug in /stats feature")

    def test_stats_with_valid_projects_filter(self):
        """Test /stats with valid_projects filter."""
        valid = {"trellm", "myapp"}
        # Should match when project is in valid set
        assert is_stats_command("trellm /stats", valid)
        assert is_stats_command("myapp /stats", valid)
        # Should NOT match when project is not in valid set
        assert not is_stats_command("otherproject /stats", valid)
        assert not is_stats_command("unknown /stats", valid)

    def test_stats_single_word_not_matched(self):
        """Test that single word cards are not matched."""
        assert not is_stats_command("/stats")
        assert not is_stats_command("project")

    def test_stats_with_alias_in_valid_projects(self):
        """Test /stats with aliases included in valid_projects set."""
        # Aliases should be included in the valid set via get_all_project_names()
        valid = {"smugcoin", "smg", "myapp"}
        assert is_stats_command("smg /stats", valid)
        assert is_stats_command("smugcoin /stats", valid)


class TestHandleStatsCommand:
    """Tests for handle_stats_command function."""

    @pytest.mark.asyncio
    async def test_handle_stats_basic(self, tmp_path):
        """Test handling a basic /stats command."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))

        # Record some test data
        state.record_cost(
            card_id="test-card",
            project="testproject",
            total_cost="$5.00",
        )

        # Create mock card and trello client
        card = TrelloCard(
            id="stats-card-123",
            name="testproject /stats",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_stats_command(
            card=card,
            trello=trello,
            state=state,
        )

        assert result is True
        trello.add_comment.assert_called_once()
        trello.move_to_ready.assert_called_once_with("stats-card-123")

        # Check comment contains stats
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/stats command processed" in comment_arg
        assert "$5.00" in comment_arg

    @pytest.mark.asyncio
    async def test_handle_stats_error(self, tmp_path):
        """Test handling /stats when trello call fails."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))

        card = TrelloCard(
            id="stats-card-123",
            name="testproject /stats",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock(side_effect=Exception("API error"))

        result = await handle_stats_command(
            card=card,
            trello=trello,
            state=state,
        )

        assert result is False


class TestIsMaintenanceCommand:
    """Tests for is_maintenance_command function."""

    def test_maintenance_command_basic(self):
        """Test basic /maintenance command detection."""
        assert is_maintenance_command("project /maintenance")
        assert is_maintenance_command("trellm /maintenance")
        assert is_maintenance_command("sus /maintenance")

    def test_maintenance_command_with_colon(self):
        """Test /maintenance with project colon format."""
        assert is_maintenance_command("project: /maintenance")

    def test_maintenance_command_case_insensitive(self):
        """Test /maintenance is case insensitive."""
        assert is_maintenance_command("project /MAINTENANCE")
        assert is_maintenance_command("project /Maintenance")

    def test_not_maintenance_command(self):
        """Test regular cards are not detected as /maintenance."""
        assert not is_maintenance_command("project Add maintenance feature")
        assert not is_maintenance_command("trellm Fix bug")
        assert not is_maintenance_command("project / maintenance")  # space breaks command

    def test_maintenance_not_after_project_name(self):
        """Test /maintenance must appear immediately after project name."""
        # /maintenance appearing later in the card name should NOT match
        assert not is_maintenance_command("trellm problem with the /maintenance command")
        assert not is_maintenance_command("project fix /maintenance display")
        assert not is_maintenance_command("myapp bug in /maintenance feature")

    def test_maintenance_with_valid_projects_filter(self):
        """Test /maintenance with valid_projects filter."""
        valid = {"trellm", "myapp", "sus"}
        # Should match when project is in valid set
        assert is_maintenance_command("trellm /maintenance", valid)
        assert is_maintenance_command("myapp /maintenance", valid)
        assert is_maintenance_command("sus /maintenance", valid)
        # Should NOT match when project is not in valid set
        assert not is_maintenance_command("otherproject /maintenance", valid)
        assert not is_maintenance_command("unknown /maintenance", valid)

    def test_maintenance_single_word_not_matched(self):
        """Test that single word cards are not matched."""
        assert not is_maintenance_command("/maintenance")
        assert not is_maintenance_command("project")

    def test_maintenance_with_alias_in_valid_projects(self):
        """Test /maintenance with aliases included in valid_projects set."""
        valid = {"smugcoin", "smg", "myapp"}
        assert is_maintenance_command("smg /maintenance", valid)
        assert is_maintenance_command("smugcoin /maintenance", valid)


class TestHandleMaintenanceCommand:
    """Tests for handle_maintenance_command function."""

    def _create_test_config(self, project: str = "testproject") -> Config:
        """Create a test configuration."""
        return Config(
            trello=TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
                ready_to_try_list_id="ready",
                icebox_list_id="icebox",
            ),
            claude=ClaudeConfig(
                binary="claude",
                timeout=60,
                projects={
                    project: ProjectConfig(
                        working_dir="/tmp/testproject",
                        maintenance=MaintenanceConfig(enabled=True, interval=10),
                    )
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_handle_maintenance_unknown_project(self, tmp_path):
        """Test handling /maintenance for unknown project."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        config = self._create_test_config("otherproject")

        card = TrelloCard(
            id="maint-card-123",
            name="unknownproject /maintenance",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_maintenance_command(
            card=card,
            trello=trello,
            state=state,
            config=config,
        )

        assert result is True  # Card handled, just with error
        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "not found in configuration" in comment_arg
        trello.move_to_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_maintenance_no_maintenance_config(self, tmp_path):
        """Test handling /maintenance when maintenance not configured."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))

        # Config without maintenance
        config = Config(
            trello=TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
            ),
            claude=ClaudeConfig(
                binary="claude",
                timeout=60,
                projects={
                    "testproject": ProjectConfig(
                        working_dir="/tmp/testproject",
                        # No maintenance config
                    )
                },
            ),
        )

        card = TrelloCard(
            id="maint-card-123",
            name="testproject /maintenance",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_maintenance_command(
            card=card,
            trello=trello,
            state=state,
            config=config,
        )

        assert result is True
        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "not configured" in comment_arg

    @pytest.mark.asyncio
    async def test_handle_maintenance_success(self, tmp_path):
        """Test successful /maintenance command."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        config = self._create_test_config("testproject")

        # Add some tickets to verify reset
        state.add_processed_ticket("testproject", "card-1")
        state.add_processed_ticket("testproject", "card-2")
        assert state.get_ticket_count("testproject") == 2

        card = TrelloCard(
            id="maint-card-123",
            name="testproject /maintenance",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        mock_result = MaintenanceResult(
            success=True,
            summary="Maintenance completed successfully",
            session_id="new-session-123",
        )

        with patch("trellm.__main__.run_maintenance", return_value=mock_result) as mock_run:
            result = await handle_maintenance_command(
                card=card,
                trello=trello,
                state=state,
                config=config,
            )

            mock_run.assert_called_once()

        assert result is True
        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/maintenance command completed" in comment_arg
        assert "testproject" in comment_arg
        trello.move_to_ready.assert_called_once()

        # Verify state was updated
        assert state.get_ticket_count("testproject") == 0  # Reset
        assert state.get_last_maintenance("testproject") is not None
        assert state.get_session("testproject") == "new-session-123"

    @pytest.mark.asyncio
    async def test_handle_maintenance_failure(self, tmp_path):
        """Test failed /maintenance command."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        config = self._create_test_config("testproject")

        card = TrelloCard(
            id="maint-card-123",
            name="testproject /maintenance",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        mock_result = MaintenanceResult(
            success=False,
            summary="Maintenance timed out",
        )

        with patch("trellm.__main__.run_maintenance", return_value=mock_result):
            result = await handle_maintenance_command(
                card=card,
                trello=trello,
                state=state,
                config=config,
            )

        assert result is True  # Card was handled
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/maintenance command failed" in comment_arg
        assert "testproject" in comment_arg
        trello.move_to_ready.assert_called_once()

        # Verify state was NOT updated (no reset on failure)
        assert state.get_last_maintenance("testproject") is None

    @pytest.mark.asyncio
    async def test_handle_maintenance_with_alias(self, tmp_path):
        """Test /maintenance command using a project alias."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))

        # Config with alias
        config = Config(
            trello=TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
                ready_to_try_list_id="ready",
                icebox_list_id="icebox",
            ),
            claude=ClaudeConfig(
                binary="claude",
                timeout=60,
                projects={
                    "smugcoin": ProjectConfig(
                        working_dir="/tmp/smugcoin",
                        aliases=["smg"],
                        maintenance=MaintenanceConfig(enabled=True, interval=10),
                    )
                },
            ),
        )

        # Card uses alias "smg" instead of canonical "smugcoin"
        card = TrelloCard(
            id="maint-alias-123",
            name="smg /maintenance",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        mock_result = MaintenanceResult(
            success=True,
            summary="Maintenance completed via alias",
            session_id="alias-session-123",
        )

        with patch("trellm.__main__.run_maintenance", return_value=mock_result) as mock_run:
            result = await handle_maintenance_command(
                card=card,
                trello=trello,
                state=state,
                config=config,
            )

            mock_run.assert_called_once()
            # Verify it was called with the canonical project name
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["project"] == "smugcoin"

        assert result is True
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/maintenance command completed" in comment_arg
        assert "smugcoin" in comment_arg


class TestIsResetSessionCommand:
    """Tests for is_reset_session_command function."""

    def test_reset_session_command_basic(self):
        """Test basic /reset-session command detection."""
        assert is_reset_session_command("project /reset-session")
        assert is_reset_session_command("trellm /reset-session")
        assert is_reset_session_command("jcapp /reset-session")

    def test_reset_session_command_with_colon(self):
        """Test /reset-session with project colon format."""
        assert is_reset_session_command("project: /reset-session")

    def test_reset_session_command_case_insensitive(self):
        """Test /reset-session is case insensitive."""
        assert is_reset_session_command("project /RESET-SESSION")
        assert is_reset_session_command("project /Reset-Session")

    def test_not_reset_session_command(self):
        """Test regular cards are not detected as /reset-session."""
        assert not is_reset_session_command("project Add reset feature")
        assert not is_reset_session_command("trellm Fix bug")
        assert not is_reset_session_command("project / reset-session")  # space breaks

    def test_reset_session_not_after_project_name(self):
        """Test /reset-session must appear immediately after project name."""
        assert not is_reset_session_command("trellm problem with /reset-session")
        assert not is_reset_session_command("project fix /reset-session issue")

    def test_reset_session_with_valid_projects_filter(self):
        """Test /reset-session with valid_projects filter."""
        valid = {"trellm", "jcapp"}
        assert is_reset_session_command("trellm /reset-session", valid)
        assert is_reset_session_command("jcapp /reset-session", valid)
        assert not is_reset_session_command("other /reset-session", valid)

    def test_reset_session_single_word_not_matched(self):
        """Test that single word cards are not matched."""
        assert not is_reset_session_command("/reset-session")
        assert not is_reset_session_command("project")

    def test_reset_session_with_alias_in_valid_projects(self):
        """Test /reset-session with aliases in valid_projects set."""
        valid = {"smugcoin", "smg", "jcapp"}
        assert is_reset_session_command("smg /reset-session", valid)
        assert is_reset_session_command("smugcoin /reset-session", valid)


class TestHandleResetSessionCommand:
    """Tests for handle_reset_session_command function."""

    def _create_test_config(self, project: str = "testproject", session_id: str = None) -> Config:
        """Create a test configuration."""
        return Config(
            trello=TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
                ready_to_try_list_id="ready",
            ),
            claude=ClaudeConfig(
                binary="claude",
                timeout=60,
                projects={
                    project: ProjectConfig(
                        working_dir="/tmp/testproject",
                        session_id=session_id,
                    )
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_handle_reset_session_clears_state(self, tmp_path):
        """Test /reset-session clears session from state."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        state.set_session("testproject", "old-session-123")
        config = self._create_test_config("testproject")

        card = TrelloCard(
            id="reset-card-123",
            name="testproject /reset-session",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_reset_session_command(
            card=card, trello=trello, state=state, config=config,
        )

        assert result is True
        assert state.get_session("testproject") is None
        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/reset-session completed" in comment_arg
        assert "Cleared session ID from state" in comment_arg
        trello.move_to_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_reset_session_no_existing_session(self, tmp_path):
        """Test /reset-session when no session exists in state."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        config = self._create_test_config("testproject")

        card = TrelloCard(
            id="reset-card-123",
            name="testproject /reset-session",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_reset_session_command(
            card=card, trello=trello, state=state, config=config,
        )

        assert result is True
        comment_arg = trello.add_comment.call_args[0][1]
        assert "No session ID was set in state" in comment_arg

    @pytest.mark.asyncio
    async def test_handle_reset_session_warns_about_config_session(self, tmp_path):
        """Test /reset-session warns if session_id also in config."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        state.set_session("testproject", "state-session-123")
        config = self._create_test_config("testproject", session_id="config-session-456")

        card = TrelloCard(
            id="reset-card-123",
            name="testproject /reset-session",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_reset_session_command(
            card=card, trello=trello, state=state, config=config,
        )

        assert result is True
        comment_arg = trello.add_comment.call_args[0][1]
        assert "config-session-456" in comment_arg
        assert "Remove it from the config" in comment_arg

    @pytest.mark.asyncio
    async def test_handle_reset_session_unknown_project(self, tmp_path):
        """Test /reset-session for unknown project."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        config = self._create_test_config("otherproject")

        card = TrelloCard(
            id="reset-card-123",
            name="unknownproject /reset-session",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_reset_session_command(
            card=card, trello=trello, state=state, config=config,
        )

        assert result is True
        comment_arg = trello.add_comment.call_args[0][1]
        assert "not found in configuration" in comment_arg

    @pytest.mark.asyncio
    async def test_handle_reset_session_with_alias(self, tmp_path):
        """Test /reset-session command using a project alias."""
        from trellm.state import StateManager

        state_file = tmp_path / "state.json"
        state = StateManager(str(state_file))
        state.set_session("smugcoin", "old-session-xyz")

        config = Config(
            trello=TrelloConfig(
                api_key="key",
                api_token="token",
                board_id="board",
                todo_list_id="todo",
                ready_to_try_list_id="ready",
            ),
            claude=ClaudeConfig(
                binary="claude",
                timeout=60,
                projects={
                    "smugcoin": ProjectConfig(
                        working_dir="/tmp/smugcoin",
                        aliases=["smg"],
                    )
                },
            ),
        )

        card = TrelloCard(
            id="reset-alias-123",
            name="smg /reset-session",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_reset_session_command(
            card=card, trello=trello, state=state, config=config,
        )

        assert result is True
        assert state.get_session("smugcoin") is None
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/reset-session completed" in comment_arg
        assert "smugcoin" in comment_arg


class TestIsAbortCommand:
    """Tests for is_abort_command function."""

    def test_abort_command_basic(self):
        """Test basic /abort command detection."""
        assert is_abort_command("trellm /abort")

    def test_abort_command_with_colon(self):
        """Test /abort with colon format."""
        assert is_abort_command("trellm: /abort")

    def test_abort_command_case_insensitive(self):
        """Test /abort is case insensitive."""
        assert is_abort_command("trellm /ABORT")
        assert is_abort_command("Trellm /abort")
        assert is_abort_command("TRELLM /Abort")

    def test_not_abort_command(self):
        """Test regular cards are not detected as /abort."""
        assert not is_abort_command("trellm Fix bug")
        assert not is_abort_command("trellm Add abort feature")
        assert not is_abort_command("trellm / abort")  # space breaks command

    def test_abort_requires_trellm_prefix(self):
        """Test /abort only works with 'trellm' as prefix, not other projects."""
        assert not is_abort_command("myproject /abort")
        assert not is_abort_command("smugcoin /abort")

    def test_abort_not_after_project_name(self):
        """Test /abort must appear immediately after trellm."""
        assert not is_abort_command("trellm problem with /abort")
        assert not is_abort_command("trellm implement /abort")

    def test_abort_single_word_not_matched(self):
        """Test that single word cards are not matched."""
        assert not is_abort_command("/abort")
        assert not is_abort_command("trellm")


class TestHandleAbortCommand:
    """Tests for handle_abort_command function."""

    @pytest.mark.asyncio
    async def test_handle_abort_no_tasks_no_cards(self):
        """Test /abort when there's nothing to abort."""
        card = TrelloCard(
            id="abort-card-123",
            name="trellm /abort",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_abort_command(
            card=card,
            trello=trello,
            running_tasks=set(),
            processing_cards=set(),
        )

        assert result is True
        # Abort card gets a confirmation comment and is moved
        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/abort" in comment_arg
        trello.move_to_ready.assert_called_once_with("abort-card-123")

    @pytest.mark.asyncio
    async def test_handle_abort_moves_todo_cards(self):
        """Test /abort moves TODO cards to READY TO TRY with comments."""
        abort_card = TrelloCard(
            id="abort-card-123",
            name="trellm /abort",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        todo_card1 = TrelloCard(
            id="todo-card-1",
            name="myproject Fix bug",
            url="https://trello.com/c/test1",
            description="",
            last_activity="2026-01-24T09:00:00Z",
        )
        todo_card2 = TrelloCard(
            id="todo-card-2",
            name="otherproject Add feature",
            url="https://trello.com/c/test2",
            description="",
            last_activity="2026-01-24T09:00:00Z",
        )

        trello = MagicMock()
        # get_todo_cards returns the abort card plus other TODO cards
        trello.get_todo_cards = AsyncMock(
            return_value=[abort_card, todo_card1, todo_card2]
        )
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        result = await handle_abort_command(
            card=abort_card,
            trello=trello,
            running_tasks=set(),
            processing_cards=set(),
        )

        assert result is True
        # 2 TODO cards get abort comments + 1 abort card confirmation = 3 comments
        assert trello.add_comment.call_count == 3
        # 2 TODO cards + 1 abort card = 3 moves
        assert trello.move_to_ready.call_count == 3

        # Check that TODO cards got abort comments
        comment_calls = trello.add_comment.call_args_list
        todo_comments = [c for c in comment_calls if c[0][0] != "abort-card-123"]
        for call in todo_comments:
            assert "aborted" in call[0][1].lower()

    @pytest.mark.asyncio
    async def test_handle_abort_cancels_running_tasks(self):
        """Test /abort cancels running asyncio tasks."""
        abort_card = TrelloCard(
            id="abort-card-123",
            name="trellm /abort",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        # Create mock tasks
        task1 = MagicMock()
        task1.cancel = MagicMock()
        task1.cancelled = MagicMock(return_value=False)
        task2 = MagicMock()
        task2.cancel = MagicMock()
        task2.cancelled = MagicMock(return_value=False)

        running_tasks = {task1, task2}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            result = await handle_abort_command(
                card=abort_card,
                trello=trello,
                running_tasks=running_tasks,
                processing_cards=set(),
            )

        assert result is True
        task1.cancel.assert_called_once()
        task2.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_abort_clears_processing_cards(self):
        """Test /abort clears the processing cards set."""
        abort_card = TrelloCard(
            id="abort-card-123",
            name="trellm /abort",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        processing_cards = {"card-a", "card-b"}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            result = await handle_abort_command(
                card=abort_card,
                trello=trello,
                running_tasks=set(),
                processing_cards=processing_cards,
            )

        assert result is True
        assert len(processing_cards) == 0

    @pytest.mark.asyncio
    async def test_handle_abort_summary_counts(self):
        """Test /abort confirmation comment includes correct counts."""
        abort_card = TrelloCard(
            id="abort-card-123",
            name="trellm /abort",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        todo_card = TrelloCard(
            id="todo-card-1",
            name="myproject Fix bug",
            url="https://trello.com/c/test1",
            description="",
            last_activity="2026-01-24T09:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[abort_card, todo_card])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        task1 = MagicMock()
        task1.cancel = MagicMock()
        task1.cancelled = MagicMock(return_value=False)
        running_tasks = {task1}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            result = await handle_abort_command(
                card=abort_card,
                trello=trello,
                running_tasks=running_tasks,
                processing_cards=set(),
            )

        assert result is True
        # Find the confirmation comment on the abort card
        abort_comments = [
            c for c in trello.add_comment.call_args_list
            if c[0][0] == "abort-card-123"
        ]
        assert len(abort_comments) == 1
        confirmation = abort_comments[0][0][1]
        assert "1" in confirmation  # 1 task cancelled
        assert "1" in confirmation  # 1 card moved


class TestIsRestartCommand:
    """Tests for is_restart_command function."""

    def test_restart_command_basic(self):
        """Test basic /restart command detection."""
        assert is_restart_command("trellm /restart")

    def test_restart_command_with_colon(self):
        """Test /restart with colon format."""
        assert is_restart_command("trellm: /restart")

    def test_restart_command_case_insensitive(self):
        """Test /restart is case insensitive."""
        assert is_restart_command("trellm /RESTART")
        assert is_restart_command("Trellm /restart")
        assert is_restart_command("TRELLM /Restart")

    def test_not_restart_command(self):
        """Test regular cards are not detected as /restart."""
        assert not is_restart_command("trellm Fix bug")
        assert not is_restart_command("trellm Add restart feature")
        assert not is_restart_command("trellm / restart")  # space breaks command

    def test_restart_requires_trellm_prefix(self):
        """Test /restart only works with 'trellm' as prefix."""
        assert not is_restart_command("myproject /restart")
        assert not is_restart_command("smugcoin /restart")

    def test_restart_not_after_project_name(self):
        """Test /restart must appear immediately after trellm."""
        assert not is_restart_command("trellm problem with /restart")
        assert not is_restart_command("trellm implement /restart")

    def test_restart_single_word_not_matched(self):
        """Test that single word cards are not matched."""
        assert not is_restart_command("/restart")
        assert not is_restart_command("trellm")


class TestHandleRestartCommand:
    """Tests for handle_restart_command function."""

    @pytest.mark.asyncio
    async def test_handle_restart_raises_restart_requested(self):
        """Test /restart raises RestartRequested after handling."""
        card = TrelloCard(
            id="restart-card-123",
            name="trellm /restart",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()
        trello.close = AsyncMock()

        with pytest.raises(RestartRequested):
            await handle_restart_command(
                card=card,
                trello=trello,
                running_tasks=set(),
                processing_cards=set(),
            )

    @pytest.mark.asyncio
    async def test_handle_restart_posts_comment_before_restart(self):
        """Test /restart posts confirmation comment."""
        card = TrelloCard(
            id="restart-card-123",
            name="trellm /restart",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()
        trello.close = AsyncMock()

        with pytest.raises(RestartRequested):
            await handle_restart_command(
                card=card,
                trello=trello,
                running_tasks=set(),
                processing_cards=set(),
            )

        trello.add_comment.assert_called_once()
        comment_arg = trello.add_comment.call_args[0][1]
        assert "/restart" in comment_arg
        trello.move_to_ready.assert_called_once_with("restart-card-123")

    @pytest.mark.asyncio
    async def test_handle_restart_cancels_running_tasks(self):
        """Test /restart cancels running asyncio tasks."""
        card = TrelloCard(
            id="restart-card-123",
            name="trellm /restart",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()
        trello.close = AsyncMock()

        task1 = MagicMock()
        task1.cancel = MagicMock()
        task1.cancelled = MagicMock(return_value=False)
        task2 = MagicMock()
        task2.cancel = MagicMock()
        task2.cancelled = MagicMock(return_value=False)

        running_tasks = {task1, task2}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            with pytest.raises(RestartRequested):
                await handle_restart_command(
                    card=card,
                    trello=trello,
                    running_tasks=running_tasks,
                    processing_cards=set(),
                )

        task1.cancel.assert_called_once()
        task2.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_restart_clears_processing_cards(self):
        """Test /restart clears the processing cards set."""
        card = TrelloCard(
            id="restart-card-123",
            name="trellm /restart",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()
        trello.close = AsyncMock()

        processing_cards = {"card-a", "card-b"}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            with pytest.raises(RestartRequested):
                await handle_restart_command(
                    card=card,
                    trello=trello,
                    running_tasks=set(),
                    processing_cards=processing_cards,
                )

        assert len(processing_cards) == 0

    @pytest.mark.asyncio
    async def test_handle_restart_summary_includes_counts(self):
        """Test /restart confirmation includes task/card counts."""
        card = TrelloCard(
            id="restart-card-123",
            name="trellm /restart",
            url="https://trello.com/c/test",
            description="",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.get_todo_cards = AsyncMock(return_value=[])
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()
        trello.close = AsyncMock()

        task1 = MagicMock()
        task1.cancel = MagicMock()
        task1.cancelled = MagicMock(return_value=False)
        running_tasks = {task1}

        with patch("asyncio.gather", new_callable=AsyncMock, return_value=[]):
            with pytest.raises(RestartRequested):
                await handle_restart_command(
                    card=card,
                    trello=trello,
                    running_tasks=running_tasks,
                    processing_cards={"card-a"},
                )

        comment_arg = trello.add_comment.call_args[0][1]
        assert "1" in comment_arg  # 1 task cancelled


class TestGlobalRateLimitPause:
    """Tests for the global rate-limit pause that prevents busy-looping
    when Claude reports an org/monthly usage limit."""

    def setup_method(self) -> None:
        # Reset the module-level pause between tests so they're independent.
        from trellm import __main__ as main_mod
        main_mod._rate_limit_pause_until = 0.0

    def test_initially_not_rate_limited(self):
        """Fresh process is not rate-limited."""
        from trellm.__main__ import is_globally_rate_limited
        assert is_globally_rate_limited() is False

    def test_pause_globally_blocks(self):
        """After pause_globally(60), is_globally_rate_limited returns True."""
        from trellm.__main__ import (
            is_globally_rate_limited,
            pause_globally,
        )
        pause_globally(60)
        assert is_globally_rate_limited() is True

    def test_pause_globally_clears_after_window(self):
        """A pause set in the past is no longer active."""
        import time
        from trellm import __main__ as main_mod
        from trellm.__main__ import is_globally_rate_limited

        # Simulate a pause that already expired
        main_mod._rate_limit_pause_until = time.time() - 1
        assert is_globally_rate_limited() is False

    def test_pause_globally_does_not_shorten_existing_pause(self):
        """If a longer pause is already active, a shorter one mustn't override it."""
        import time
        from trellm import __main__ as main_mod
        from trellm.__main__ import pause_globally

        long_until = time.time() + 7200  # 2h
        main_mod._rate_limit_pause_until = long_until

        pause_globally(60)  # only 1 minute

        # Existing longer pause should be preserved
        assert main_mod._rate_limit_pause_until == long_until

    def test_seconds_until_resume_returns_remaining(self):
        """seconds_until_resume returns roughly the remaining duration."""
        from trellm.__main__ import pause_globally, seconds_until_resume
        pause_globally(120)
        remaining = seconds_until_resume()
        assert 100 < remaining <= 120

    def test_seconds_until_resume_is_zero_when_not_paused(self):
        """seconds_until_resume returns 0 when not paused."""
        from trellm.__main__ import seconds_until_resume
        assert seconds_until_resume() == 0


class TestProcessCardForProjectMonthlyLimit:
    """Tests for the /process_card_for_project/ behaviour when Claude raises
    MonthlyLimitError — must trigger the global pause without retrying."""

    def setup_method(self) -> None:
        from trellm import __main__ as main_mod
        main_mod._rate_limit_pause_until = 0.0

    def _make_config(self) -> Config:
        return Config(
            trello=TrelloConfig(
                api_key="key", api_token="token", board_id="board",
                todo_list_id="todo", ready_to_try_list_id="ready",
            ),
            claude=ClaudeConfig(
                binary="claude", timeout=60,
                projects={
                    "testproject": ProjectConfig(working_dir="/tmp/testproject"),
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_monthly_limit_triggers_global_pause(self, tmp_path):
        """When ClaudeRunner raises MonthlyLimitError, the polling loop
        must enter a global pause so it stops retrying the same card."""
        from trellm.state import StateManager
        from trellm.claude import MonthlyLimitError
        from trellm.__main__ import (
            process_card_for_project,
            is_globally_rate_limited,
        )

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()

        card = TrelloCard(
            id="abc123",
            name="testproject do thing",
            description="",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        # No reset_seconds — exercises the default-pause fallback
        claude.run = AsyncMock(side_effect=MonthlyLimitError("hit limit"))

        result = await process_card_for_project(
            card=card,
            project="testproject",
            trello=trello,
            state=state,
            claude=claude,
            config=config,
        )

        # Card was not processed (None returned), but the global pause is set
        assert result is None
        assert is_globally_rate_limited() is True

    @pytest.mark.asyncio
    async def test_monthly_limit_does_not_mark_card_processed(self, tmp_path):
        """The card must NOT be marked processed — once the pause clears,
        we want to try again (the limit may have reset)."""
        from trellm.state import StateManager
        from trellm.claude import MonthlyLimitError
        from trellm.__main__ import process_card_for_project

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()

        card = TrelloCard(
            id="abc123",
            name="testproject do thing",
            description="",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-24T10:00:00Z",
        )

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(side_effect=MonthlyLimitError("hit limit"))

        await process_card_for_project(
            card=card,
            project="testproject",
            trello=trello,
            state=state,
            claude=claude,
            config=config,
        )

        assert state.is_processed("abc123") is False
        # Should not have been moved to ready either
        trello.move_to_ready.assert_not_called()


class TestCardRetryState:
    """Tests for per-card retry tracking with exponential backoff.

    Background (card ZCwyx8wO): unrecognized failures (timeouts and other
    non-usage-limit errors) busy-loop the polling loop because each spawn
    removes the card from `_processing_cards` in a finally block. A
    per-card backoff prevents this for failures that exit <1m after start
    — the indicator that the run isn't doing meaningful work."""

    def test_initial_state_is_zero(self):
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        assert s.error_count == 0
        assert s.timeout_count == 0
        assert s.fast_failure_streak == 0
        assert s.backoff_until == 0.0

    def test_is_in_backoff_initially_false(self):
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        assert s.is_in_backoff(now=1000.0) is False
        assert s.seconds_until_resume(now=1000.0) == 0

    def test_fast_failure_increments_streak_and_sets_backoff(self):
        """A failure within the fast-fail threshold (default 60s) should
        increment the streak AND schedule a backoff window."""
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        s.record_failure(duration_seconds=10.0, now=1000.0)
        assert s.fast_failure_streak == 1
        assert s.error_count == 1
        assert s.backoff_until > 1000.0

    def test_slow_failure_increments_error_count_but_no_backoff(self):
        """Failures >= 60s after start aren't busy-loops by definition.
        We still record the error for visibility but DON'T pause."""
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        s.record_failure(duration_seconds=120.0, now=1000.0)
        assert s.error_count == 1
        assert s.fast_failure_streak == 0
        assert s.backoff_until == 0.0

    def test_slow_failure_resets_fast_streak(self):
        """A slow failure breaks a fast-failure streak — the card got
        somewhere this time, so the next fast failure starts from 30s
        again, not whatever the previous streak was scheduling."""
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        # Pretend we'd already had 3 fast failures
        s.fast_failure_streak = 3
        s.record_failure(duration_seconds=120.0, now=1000.0)
        assert s.fast_failure_streak == 0

    def test_exponential_backoff_schedule(self):
        """Backoff follows 30 * 2**(streak-1), capped at 1800 (30min).
        User constraint: 'in any case the retry timeout should be no
        more than 30 mins'."""
        from trellm.__main__ import CardRetryState
        expected_backoff_seconds = [30, 60, 120, 240, 480, 960, 1800, 1800, 1800]
        s = CardRetryState()
        for i, expected in enumerate(expected_backoff_seconds, start=1):
            s.record_failure(duration_seconds=10.0, now=1000.0)
            actual = s.backoff_until - 1000.0
            assert actual == expected, (
                f"streak {i}: expected {expected}s, got {actual}s"
            )

    def test_backoff_caps_at_30_minutes(self):
        """User explicitly capped backoff at 30min — even after many
        consecutive fast failures, never exceed 1800s."""
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        # Force a very high streak directly
        s.fast_failure_streak = 100
        s.record_failure(duration_seconds=10.0, now=1000.0)
        assert s.backoff_until - 1000.0 == 1800

    def test_timeout_failure_increments_timeout_count(self):
        """Timeouts are categorized separately for dashboard display."""
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        s.record_failure(duration_seconds=1200.0, is_timeout=True, now=1000.0)
        assert s.timeout_count == 1
        assert s.error_count == 0  # mutually exclusive — not double-counted

    def test_is_in_backoff_during_and_after_window(self):
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        s.record_failure(duration_seconds=10.0, now=1000.0)
        # 30s backoff: still active at 1020, gone at 1050
        assert s.is_in_backoff(now=1020.0) is True
        assert s.is_in_backoff(now=1050.0) is False

    def test_seconds_until_resume_returns_remaining(self):
        from trellm.__main__ import CardRetryState
        s = CardRetryState()
        s.record_failure(duration_seconds=10.0, now=1000.0)
        # 30s backoff: 20s remaining at t=1010
        assert s.seconds_until_resume(now=1010.0) == 20
        # 0 once expired
        assert s.seconds_until_resume(now=1100.0) == 0


class TestProcessCardFailureRecording:
    """Tests for process_card_for_project recording failures in
    _card_retry_state. Three concerns: (1) generic RuntimeError increments
    error_count; (2) 'timed out after' string sets is_timeout; (3) success
    clears the retry entry."""

    def setup_method(self) -> None:
        from trellm import __main__ as main_mod
        main_mod._rate_limit_pause_until = 0.0
        main_mod._card_retry_state.clear()

    def _make_config(self) -> Config:
        return Config(
            trello=TrelloConfig(
                api_key="key", api_token="token", board_id="board",
                todo_list_id="todo", ready_to_try_list_id="ready",
            ),
            claude=ClaudeConfig(
                binary="claude", timeout=60,
                projects={
                    "testproject": ProjectConfig(working_dir="/tmp/testproject"),
                },
            ),
        )

    def _make_card(self) -> TrelloCard:
        return TrelloCard(
            id="card-fail-1",
            name="testproject do thing",
            description="",
            url="https://trello.com/c/card-fail-1",
            last_activity="2026-05-13T10:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_generic_runtime_error_records_error(self, tmp_path):
        """A RuntimeError from claude.run() must register as an error in
        _card_retry_state so the polling loop can apply backoff."""
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project, _card_retry_state

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(side_effect=RuntimeError("Claude Code failed: boom"))

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert card.id in _card_retry_state
        assert _card_retry_state[card.id].error_count == 1
        assert _card_retry_state[card.id].timeout_count == 0

    @pytest.mark.asyncio
    async def test_timeout_runtime_error_records_timeout(self, tmp_path):
        """A RuntimeError whose message contains 'timed out after' must
        increment timeout_count, not error_count."""
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project, _card_retry_state

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(
            side_effect=RuntimeError("Claude Code timed out after 1200s")
        )

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert _card_retry_state[card.id].timeout_count == 1
        assert _card_retry_state[card.id].error_count == 0

    @pytest.mark.asyncio
    async def test_success_clears_retry_state(self, tmp_path):
        """A successful run must clear the card's retry entry — the next
        failure starts a fresh streak at 30s, not whatever the previous
        streak was."""
        from trellm.claude import ClaudeResult
        from trellm.state import StateManager
        from trellm.__main__ import (
            process_card_for_project,
            _card_retry_state,
            CardRetryState,
        )

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        # Pre-populate as if there had been failures
        _card_retry_state[card.id] = CardRetryState(error_count=3, fast_failure_streak=3)

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(
            return_value=ClaudeResult(
                success=True, session_id="sess-1", summary="", output="",
            )
        )

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert card.id not in _card_retry_state

    @pytest.mark.asyncio
    async def test_should_skip_card_for_backoff_returns_true_during_backoff(self):
        """Helper used by the polling loop: True when the card is in
        backoff and shouldn't be re-spawned this tick."""
        import time as time_mod
        from trellm.__main__ import (
            CardRetryState,
            _card_retry_state,
            should_skip_card_for_backoff,
        )

        # Pre-seed a backoff that's still active
        state = CardRetryState()
        state.backoff_until = time_mod.time() + 60
        _card_retry_state["card-busy"] = state

        assert should_skip_card_for_backoff("card-busy") is True

    @pytest.mark.asyncio
    async def test_should_skip_card_for_backoff_returns_false_after_expiry(self):
        """Once the backoff window expires, the polling loop should
        re-spawn the card."""
        import time as time_mod
        from trellm.__main__ import (
            CardRetryState,
            _card_retry_state,
            should_skip_card_for_backoff,
        )

        state = CardRetryState()
        state.backoff_until = time_mod.time() - 1
        _card_retry_state["card-expired"] = state

        assert should_skip_card_for_backoff("card-expired") is False

    @pytest.mark.asyncio
    async def test_should_skip_card_for_backoff_returns_false_for_unknown_card(self):
        """A card that has never failed has no entry — must not skip."""
        from trellm.__main__ import should_skip_card_for_backoff
        assert should_skip_card_for_backoff("never-seen") is False

    @pytest.mark.asyncio
    async def test_monthly_limit_does_not_record_retry_state(self, tmp_path):
        """Org/monthly limit hits trigger the global pause — they shouldn't
        ALSO record a per-card failure (would double-penalize the card)."""
        from trellm.claude import MonthlyLimitError
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project, _card_retry_state

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(side_effect=MonthlyLimitError("hit limit"))

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert card.id not in _card_retry_state


class TestFindPendingSiblingForProject:
    """Tests for find_pending_sibling_for_project — the picker-side helper
    that defers a card if a sibling for the same project is already in
    flight or in active retry backoff.

    Background (card 1jZZ6lOB): when cards A and B for the same project
    are both in TODO and A times out, the project lock serializes them
    but the original picker spawned tasks for both in the same poll
    cycle. After A's task failed, B's queued task immediately picked
    up the lock — ping-pong, wasting compaction tokens on session
    context that the retry of A actually needed. The fix: refuse to
    pick a sibling while the just-failed card is still in backoff (or
    while its task is still alive)."""

    def setup_method(self) -> None:
        from trellm import __main__ as main_mod
        main_mod._card_retry_state.clear()
        main_mod._processing_cards.clear()

    def _card(self, card_id: str, project: str = "testproject") -> TrelloCard:
        return TrelloCard(
            id=card_id,
            name=f"{project} do thing",
            description="",
            url=f"https://trello.com/c/{card_id}",
            last_activity="2026-01-24T10:00:00Z",
        )

    def test_no_siblings_returns_none(self):
        from trellm.__main__ import find_pending_sibling_for_project
        a = self._card("a")
        result = find_pending_sibling_for_project(
            this_card_id=a.id,
            project="testproject",
            cards=[a],
            card_projects={a.id: "testproject"},
        )
        assert result is None

    def test_sibling_for_different_project_returns_none(self):
        """A card for project Y currently processing shouldn't block a
        sibling-check for project X."""
        from trellm.__main__ import find_pending_sibling_for_project
        from trellm import __main__ as main_mod
        a = self._card("a", project="alpha")
        b = self._card("b", project="beta")
        main_mod._processing_cards.add(b.id)
        result = find_pending_sibling_for_project(
            this_card_id=a.id,
            project="alpha",
            cards=[a, b],
            card_projects={a.id: "alpha", b.id: "beta"},
        )
        assert result is None

    def test_sibling_in_processing_returns_sibling_id(self):
        """A different card for the same project, currently being
        processed, must be returned — this is the 'in-flight' case."""
        from trellm.__main__ import find_pending_sibling_for_project
        from trellm import __main__ as main_mod
        a = self._card("a")
        b = self._card("b")
        main_mod._processing_cards.add(a.id)
        result = find_pending_sibling_for_project(
            this_card_id=b.id,
            project="testproject",
            cards=[a, b],
            card_projects={a.id: "testproject", b.id: "testproject"},
        )
        assert result == "a"

    def test_sibling_in_backoff_returns_sibling_id(self):
        """A sibling card in active backoff (recently failed, retry
        pending) must block the picker — this is the 'sticky' case."""
        import time as time_mod
        from trellm.__main__ import (
            CardRetryState,
            _card_retry_state,
            find_pending_sibling_for_project,
        )
        a = self._card("a")
        b = self._card("b")
        retry = CardRetryState()
        retry.backoff_until = time_mod.time() + 60
        _card_retry_state[a.id] = retry
        result = find_pending_sibling_for_project(
            this_card_id=b.id,
            project="testproject",
            cards=[a, b],
            card_projects={a.id: "testproject", b.id: "testproject"},
        )
        assert result == "a"

    def test_sibling_with_expired_backoff_does_not_block(self):
        """Once a sibling's backoff window expires, it no longer blocks
        — the picker can pick either card freely on the next cycle."""
        import time as time_mod
        from trellm.__main__ import (
            CardRetryState,
            _card_retry_state,
            find_pending_sibling_for_project,
        )
        a = self._card("a")
        b = self._card("b")
        retry = CardRetryState()
        retry.backoff_until = time_mod.time() - 1
        _card_retry_state[a.id] = retry
        result = find_pending_sibling_for_project(
            this_card_id=b.id,
            project="testproject",
            cards=[a, b],
            card_projects={a.id: "testproject", b.id: "testproject"},
        )
        assert result is None

    def test_self_is_not_a_sibling(self):
        """The card being checked must never be reported as its own
        sibling, even if it's in processing/backoff itself."""
        from trellm.__main__ import find_pending_sibling_for_project
        from trellm import __main__ as main_mod
        a = self._card("a")
        main_mod._processing_cards.add(a.id)
        result = find_pending_sibling_for_project(
            this_card_id=a.id,
            project="testproject",
            cards=[a],
            card_projects={a.id: "testproject"},
        )
        assert result is None

    def test_returns_some_matching_sibling(self):
        """When multiple siblings could block, return any of them — the
        picker only needs to know *some* sibling is active."""
        from trellm.__main__ import find_pending_sibling_for_project
        from trellm import __main__ as main_mod
        a = self._card("a")
        b = self._card("b")
        c = self._card("c")
        main_mod._processing_cards.add(a.id)
        main_mod._processing_cards.add(b.id)
        result = find_pending_sibling_for_project(
            this_card_id=c.id,
            project="testproject",
            cards=[a, b, c],
            card_projects={
                a.id: "testproject",
                b.id: "testproject",
                c.id: "testproject",
            },
        )
        assert result in {"a", "b"}


class TestProcessCardFailurePostsRetryComment:
    """When a card fails (timeout or generic error) and will be retried,
    the harness must leave a "Claude:" comment on the card so the next
    run has context — what happened on the previous run and a nudge to
    investigate root cause rather than blindly re-running. Card 6U11EfUz.
    """

    def setup_method(self) -> None:
        from trellm import __main__ as main_mod
        main_mod._rate_limit_pause_until = 0.0
        main_mod._card_retry_state.clear()
        main_mod._processing_cards.clear()

    def _make_config(self) -> Config:
        return Config(
            trello=TrelloConfig(
                api_key="key", api_token="token", board_id="board",
                todo_list_id="todo", ready_to_try_list_id="ready",
            ),
            claude=ClaudeConfig(
                binary="claude", timeout=60,
                projects={
                    "testproject": ProjectConfig(working_dir="/tmp/testproject"),
                },
            ),
        )

    def _make_card(self) -> TrelloCard:
        return TrelloCard(
            id="card-retry-ctx-1",
            name="testproject do thing",
            description="",
            url="https://trello.com/c/card-retry-ctx-1",
            last_activity="2026-05-13T10:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_timeout_posts_retry_context_comment(self, tmp_path):
        """When claude.py surfaces a timeout ('timed out after Ns'), the
        harness must post a Claude:-prefixed comment that names the
        timeout failure mode so the next run can adapt instead of just
        re-running the same plan."""
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(
            side_effect=RuntimeError("Claude Code timed out after 1200s")
        )

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        # add_comment must have been called at least once with the
        # retry-context wording (a comment starting with "Claude:" that
        # mentions timeout).
        retry_comments = [
            c for c in trello.add_comment.call_args_list
            if "Claude:" in c.args[1] and "timeout" in c.args[1].lower()
        ]
        assert len(retry_comments) == 1, (
            f"expected one timeout retry-context comment, got: "
            f"{trello.add_comment.call_args_list}"
        )
        comment_text = retry_comments[0].args[1]
        # The comment should mention 'retry' or 'investigate' so the
        # next run knows not to just re-run the same plan.
        lowered = comment_text.lower()
        assert "retry" in lowered or "investigate" in lowered or "report" in lowered
        # And it should be on the right card.
        assert retry_comments[0].args[0] == card.id

    @pytest.mark.asyncio
    async def test_generic_error_posts_retry_context_comment(self, tmp_path):
        """Same expectation for a non-timeout error: the harness should
        surface what failed so the next run has context. The wording
        differs from the timeout case (no 'killed after X minutes') but
        it must still start with 'Claude:' and quote the error."""
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(
            side_effect=RuntimeError("Claude Code failed: subprocess crashed")
        )

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        retry_comments = [
            c for c in trello.add_comment.call_args_list
            if c.args[1].startswith("Claude:") and "subprocess crashed" in c.args[1]
        ]
        assert len(retry_comments) == 1, (
            f"expected one retry-context comment quoting the error, got: "
            f"{trello.add_comment.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_monthly_limit_does_not_post_retry_context_comment(self, tmp_path):
        """Account-wide usage limits aren't card-specific — posting a
        'previous run failed' comment on every TODO card would be noise.
        The MonthlyLimitError branch must NOT post a retry-context
        comment."""
        from trellm.claude import MonthlyLimitError
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        trello.add_comment = AsyncMock()
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(side_effect=MonthlyLimitError("hit limit"))

        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert trello.add_comment.call_count == 0, (
            f"MonthlyLimitError must not post a retry-context comment, "
            f"got: {trello.add_comment.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_add_comment_failure_does_not_break_retry_state(self, tmp_path):
        """If posting the retry-context comment itself fails (Trello
        API hiccup), the failure must still be recorded in
        _card_retry_state so the polling loop applies backoff. The
        retry-context comment is best-effort — it must never be allowed
        to crash the failure handler."""
        from trellm.state import StateManager
        from trellm.__main__ import process_card_for_project, _card_retry_state

        state = StateManager(str(tmp_path / "state.json"))
        config = self._make_config()
        card = self._make_card()

        trello = MagicMock()
        # add_comment fails — but the handler must swallow it.
        trello.add_comment = AsyncMock(side_effect=RuntimeError("trello 500"))
        trello.move_to_ready = AsyncMock()

        claude = MagicMock()
        claude.run = AsyncMock(side_effect=RuntimeError("Claude Code failed: boom"))

        # Must not propagate the add_comment error.
        await process_card_for_project(
            card=card, project="testproject",
            trello=trello, state=state, claude=claude, config=config,
        )

        assert card.id in _card_retry_state
        assert _card_retry_state[card.id].error_count == 1
