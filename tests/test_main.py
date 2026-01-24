"""Tests for __main__ module."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from trellm.__main__ import parse_project, is_stats_command, handle_stats_command
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
