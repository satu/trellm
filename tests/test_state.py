"""Tests for state module."""

import json
from pathlib import Path

import pytest

from trellm.state import StateManager


class TestStateManager:
    """Tests for StateManager class."""

    def test_initial_state(self, tmp_path):
        """Test initial state when file doesn't exist."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert manager.get_session("project") is None
        assert not manager.is_processed("card123")

    def test_set_and_get_session(self, tmp_path):
        """Test setting and getting session IDs."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.set_session("project1", "session-abc")
        manager.set_session("project2", "session-xyz")

        assert manager.get_session("project1") == "session-abc"
        assert manager.get_session("project2") == "session-xyz"
        assert manager.get_session("unknown") is None

    def test_session_persistence(self, tmp_path):
        """Test that sessions are persisted to file."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.set_session("project", "session-123")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_session("project") == "session-123"

    def test_mark_processed(self, tmp_path):
        """Test marking cards as processed."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert not manager.is_processed("card123")

        manager.mark_processed("card123")

        assert manager.is_processed("card123")

    def test_processed_persistence(self, tmp_path):
        """Test that processed cards are persisted."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.mark_processed("card123")

        manager2 = StateManager(str(state_file))
        assert manager2.is_processed("card123")

    def test_clear_processed(self, tmp_path):
        """Test clearing processed status."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.mark_processed("card123")
        assert manager.is_processed("card123")

        manager.clear_processed("card123")
        assert not manager.is_processed("card123")

    def test_should_reprocess(self, tmp_path):
        """Test reprocess detection."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Card not processed yet - should not reprocess
        assert not manager.should_reprocess("card123", "2026-01-08T12:00:00Z")

        # Mark as processed
        manager.mark_processed("card123")

        # Activity before processing - should not reprocess
        assert not manager.should_reprocess("card123", "2020-01-01T00:00:00Z")

        # Activity after processing - should reprocess
        assert manager.should_reprocess("card123", "2099-01-01T00:00:00Z")

    def test_load_corrupted_file(self, tmp_path):
        """Test handling of corrupted state file."""
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json")

        # Should not raise, should return empty state
        manager = StateManager(str(state_file))
        assert manager.get_session("project") is None

    def test_set_session_with_last_card_id(self, tmp_path):
        """Test setting session with last_card_id."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.set_session("project1", "session-abc", last_card_id="card123")

        assert manager.get_session("project1") == "session-abc"
        assert manager.get_last_card_id("project1") == "card123"

    def test_get_last_card_id_none(self, tmp_path):
        """Test get_last_card_id returns None when not set."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # No session set
        assert manager.get_last_card_id("project1") is None

        # Session set without last_card_id
        manager.set_session("project2", "session-xyz")
        assert manager.get_last_card_id("project2") is None

    def test_last_card_id_persistence(self, tmp_path):
        """Test that last_card_id is persisted to file."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.set_session("project", "session-123", last_card_id="card456")

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        assert manager2.get_last_card_id("project") == "card456"

    def test_update_session_preserves_last_card_id(self, tmp_path):
        """Test updating session preserves existing last_card_id if not provided."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Set initial session with card ID
        manager.set_session("project", "session-1", last_card_id="card123")
        assert manager.get_last_card_id("project") == "card123"

        # Update session without providing last_card_id - should preserve existing
        manager.set_session("project", "session-2")
        assert manager.get_session("project") == "session-2"
        assert manager.get_last_card_id("project") == "card123"

    def test_update_last_card_id(self, tmp_path):
        """Test updating last_card_id to a new value."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.set_session("project", "session-1", last_card_id="card123")
        manager.set_session("project", "session-2", last_card_id="card456")

        assert manager.get_last_card_id("project") == "card456"


class TestStateManagerStats:
    """Tests for StateManager stats functionality."""

    def test_record_cost_basic(self, tmp_path):
        """Test recording cost for a single ticket."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card123",
            project="myproject",
            total_cost="$1.50",
            api_duration="2m 30s",
            wall_duration="5m 15s",
            code_changes="+100 -50",
        )

        stats = manager.get_stats()
        assert stats.total_cost_cents == 150
        assert stats.total_tickets == 1
        assert stats.total_api_duration_seconds == 150  # 2m 30s
        assert stats.total_wall_duration_seconds == 315  # 5m 15s
        assert stats.total_lines_added == 100
        assert stats.total_lines_removed == 50

    def test_record_cost_multiple_tickets(self, tmp_path):
        """Test recording cost for multiple tickets."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
            api_duration="1m",
            wall_duration="2m",
            code_changes="+50 -20",
        )
        manager.record_cost(
            card_id="card2",
            project="project2",
            total_cost="$2.00",
            api_duration="3m",
            wall_duration="4m",
            code_changes="+100 -30",
        )

        stats = manager.get_stats()
        assert stats.total_cost_cents == 300  # $3.00
        assert stats.total_tickets == 2
        assert stats.total_lines_added == 150
        assert stats.total_lines_removed == 50

    def test_get_stats_per_project(self, tmp_path):
        """Test getting stats filtered by project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
        )
        manager.record_cost(
            card_id="card2",
            project="project2",
            total_cost="$2.00",
        )

        stats1 = manager.get_stats("project1")
        assert stats1.total_cost_cents == 100
        assert stats1.total_tickets == 1

        stats2 = manager.get_stats("project2")
        assert stats2.total_cost_cents == 200
        assert stats2.total_tickets == 1

    def test_stats_persistence(self, tmp_path):
        """Test that stats are persisted to file."""
        state_file = tmp_path / "state.json"

        manager1 = StateManager(str(state_file))
        manager1.record_cost(
            card_id="card123",
            project="myproject",
            total_cost="$5.00",
        )

        # Create new manager to test persistence
        manager2 = StateManager(str(state_file))
        stats = manager2.get_stats()
        assert stats.total_cost_cents == 500
        assert stats.total_tickets == 1

    def test_parse_cost_variations(self, tmp_path):
        """Test parsing various cost formats."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Dollar format
        assert manager._parse_cost("$1.23") == 123
        assert manager._parse_cost("$0.50") == 50
        assert manager._parse_cost("$10.00") == 1000

        # Without dollar sign
        assert manager._parse_cost("1.23") == 123

        # Cents format
        assert manager._parse_cost("50 cents") == 50

        # None/empty
        assert manager._parse_cost(None) == 0
        assert manager._parse_cost("") == 0

    def test_parse_duration_variations(self, tmp_path):
        """Test parsing various duration formats."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Standard formats
        assert manager._parse_duration("2m 30s") == 150
        assert manager._parse_duration("1h 5m") == 3900
        assert manager._parse_duration("30s") == 30
        assert manager._parse_duration("5m") == 300
        assert manager._parse_duration("2h") == 7200

        # Variations
        assert manager._parse_duration("2min 30sec") == 150

        # None/empty
        assert manager._parse_duration(None) == 0
        assert manager._parse_duration("") == 0

    def test_parse_code_changes(self, tmp_path):
        """Test parsing code change formats."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        assert manager._parse_code_changes("+100 -50") == (100, 50)
        assert manager._parse_code_changes("+500") == (500, 0)
        assert manager._parse_code_changes("-200") == (0, 200)
        assert manager._parse_code_changes(None) == (0, 0)
        assert manager._parse_code_changes("") == (0, 0)

    def test_aggregated_stats_properties(self, tmp_path):
        """Test AggregatedStats computed properties."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="proj",
            total_cost="$10.00",
        )
        manager.record_cost(
            card_id="card2",
            project="proj",
            total_cost="$20.00",
        )

        stats = manager.get_stats()
        assert stats.total_cost_dollars == "$30.00"
        assert stats.average_cost_cents == 1500.0
        assert stats.average_cost_dollars == "$15.00"

    def test_format_duration(self, tmp_path):
        """Test duration formatting."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="proj",
            api_duration="1h 30m",
            wall_duration="2h 45m",
        )

        stats = manager.get_stats()
        assert stats.api_duration_formatted == "1h 30m"
        assert stats.wall_duration_formatted == "2h 45m"

    def test_format_stats_report(self, tmp_path):
        """Test stats report formatting."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="myproject",
            total_cost="$5.00",
            api_duration="10m",
            wall_duration="15m",
            code_changes="+200 -100",
        )

        report = manager.format_stats_report()
        assert "## TreLLM Usage Statistics" in report
        assert "$5.00" in report
        assert "myproject" in report
        assert "+200" in report
        assert "-100" in report

    def test_empty_stats(self, tmp_path):
        """Test getting stats with no data."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        stats = manager.get_stats()
        assert stats.total_cost_cents == 0
        assert stats.total_tickets == 0
        assert stats.total_cost_dollars == "$0.00"
        assert stats.average_cost_cents == 0

    def test_ticket_history_limit(self, tmp_path):
        """Test that ticket history is limited to 1000 entries."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add 1005 tickets
        for i in range(1005):
            manager.record_cost(
                card_id=f"card{i}",
                project="proj",
                total_cost="$0.01",
            )

        # Check history is trimmed
        assert len(manager.state["stats"]["ticket_history"]) == 1000
        # Verify oldest entries were removed (last 1000 remain)
        assert manager.state["stats"]["ticket_history"][0]["card_id"] == "card5"
        assert manager.state["stats"]["ticket_history"][-1]["card_id"] == "card1004"

    def test_get_stats_nonexistent_project(self, tmp_path):
        """Test getting stats for a project that doesn't exist."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        stats = manager.get_stats("nonexistent")
        assert stats.total_cost_cents == 0
        assert stats.total_tickets == 0
