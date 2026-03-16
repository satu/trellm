"""Tests for __main__ module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from trellm.__main__ import (
    parse_project,
    is_stats_command,
    is_maintenance_command,
    is_reset_session_command,
    is_abort_command,
    handle_stats_command,
    handle_maintenance_command,
    handle_reset_session_command,
    handle_abort_command,
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
