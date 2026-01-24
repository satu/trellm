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
        """Test that ticket history is limited to 100 entries."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add 105 tickets
        for i in range(105):
            manager.record_cost(
                card_id=f"card{i}",
                project="proj",
                total_cost="$0.01",
            )

        # Check history is trimmed
        assert len(manager.state["stats"]["ticket_history"]) == 100
        # Verify oldest entries were removed (last 100 remain)
        assert manager.state["stats"]["ticket_history"][0]["card_id"] == "card5"
        assert manager.state["stats"]["ticket_history"][-1]["card_id"] == "card104"

    def test_get_stats_nonexistent_project(self, tmp_path):
        """Test getting stats for a project that doesn't exist."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        stats = manager.get_stats("nonexistent")
        assert stats.total_cost_cents == 0
        assert stats.total_tickets == 0


class TestStatsRollup:
    """Tests for RRDTool-style date rollup functionality."""

    def test_daily_entries_within_30_days_preserved(self, tmp_path):
        """Test that daily entries within 30 days are not rolled up."""
        from datetime import datetime, timezone

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add entries for the last 30 days
        today = datetime.now(timezone.utc).date()
        for i in range(30):
            date_key = (today - __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
            manager.state["stats"]["by_date"][date_key] = {
                "total_cost_cents": 100,
                "total_tickets": 1,
                "total_api_duration_seconds": 60,
                "total_wall_duration_seconds": 120,
                "total_lines_added": 10,
                "total_lines_removed": 5,
            }

        manager._save()

        # All 30 daily entries should still exist
        by_date = manager.state["stats"]["by_date"]
        daily_keys = [k for k in by_date.keys() if not k.startswith("week-") and not k.startswith("month-")]
        assert len(daily_keys) == 30

    def test_entries_31_to_90_days_rolled_to_weekly(self, tmp_path):
        """Test that entries 31-90 days old are rolled into weekly buckets."""
        from datetime import datetime, timezone, timedelta

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add entries for days 31-60 (should be rolled to weekly)
        today = datetime.now(timezone.utc).date()
        for i in range(31, 61):
            date_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            manager.state["stats"]["by_date"][date_key] = {
                "total_cost_cents": 100,
                "total_tickets": 1,
                "total_api_duration_seconds": 60,
                "total_wall_duration_seconds": 120,
                "total_lines_added": 10,
                "total_lines_removed": 5,
            }

        manager._save()

        # Old daily entries should be gone
        by_date = manager.state["stats"]["by_date"]
        daily_keys = [k for k in by_date.keys() if not k.startswith("week-") and not k.startswith("month-")]
        assert len(daily_keys) == 0

        # Weekly buckets should exist
        weekly_keys = [k for k in by_date.keys() if k.startswith("week-")]
        assert len(weekly_keys) > 0

    def test_entries_over_90_days_rolled_to_monthly(self, tmp_path):
        """Test that entries over 90 days old are rolled into monthly buckets."""
        from datetime import datetime, timezone, timedelta

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add entries for days 91-120 (should be rolled to monthly)
        today = datetime.now(timezone.utc).date()
        for i in range(91, 121):
            date_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            manager.state["stats"]["by_date"][date_key] = {
                "total_cost_cents": 100,
                "total_tickets": 1,
                "total_api_duration_seconds": 60,
                "total_wall_duration_seconds": 120,
                "total_lines_added": 10,
                "total_lines_removed": 5,
            }

        manager._save()

        # Old daily entries should be gone
        by_date = manager.state["stats"]["by_date"]
        daily_keys = [k for k in by_date.keys() if not k.startswith("week-") and not k.startswith("month-")]
        assert len(daily_keys) == 0

        # Monthly buckets should exist
        monthly_keys = [k for k in by_date.keys() if k.startswith("month-")]
        assert len(monthly_keys) > 0

    def test_rollup_preserves_totals(self, tmp_path):
        """Test that rollup preserves the total values."""
        from datetime import datetime, timezone, timedelta

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add 100 daily entries over 100 days
        today = datetime.now(timezone.utc).date()
        expected_cost = 0
        expected_tickets = 0

        for i in range(100):
            date_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            cost = 100 * (i + 1)  # Different cost for each day
            manager.state["stats"]["by_date"][date_key] = {
                "total_cost_cents": cost,
                "total_tickets": 1,
                "total_api_duration_seconds": 60,
                "total_wall_duration_seconds": 120,
                "total_lines_added": 10,
                "total_lines_removed": 5,
            }
            expected_cost += cost
            expected_tickets += 1

        manager._save()

        # Calculate totals from all buckets
        by_date = manager.state["stats"]["by_date"]
        actual_cost = sum(v.get("total_cost_cents", 0) for v in by_date.values())
        actual_tickets = sum(v.get("total_tickets", 0) for v in by_date.values())

        assert actual_cost == expected_cost
        assert actual_tickets == expected_tickets

    def test_rollup_weekly_to_monthly(self, tmp_path):
        """Test that old weekly buckets are rolled into monthly buckets."""
        from datetime import datetime, timezone, timedelta

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Directly add an old weekly bucket (from 100 days ago)
        today = datetime.now(timezone.utc).date()
        old_date = today - timedelta(days=100)
        year, week, _ = old_date.isocalendar()
        weekly_key = f"week-{year}-{week:02d}"

        manager.state["stats"]["by_date"][weekly_key] = {
            "total_cost_cents": 500,
            "total_tickets": 5,
            "total_api_duration_seconds": 300,
            "total_wall_duration_seconds": 600,
            "total_lines_added": 50,
            "total_lines_removed": 25,
        }

        manager._save()

        # Weekly bucket should be rolled into monthly
        by_date = manager.state["stats"]["by_date"]
        assert weekly_key not in by_date

        # Monthly bucket should exist with the values
        monthly_keys = [k for k in by_date.keys() if k.startswith("month-")]
        assert len(monthly_keys) > 0

        # Verify values were preserved
        total_cost = sum(v.get("total_cost_cents", 0) for v in by_date.values())
        assert total_cost == 500

    def test_empty_bucket_helper(self, tmp_path):
        """Test _empty_bucket returns correct structure."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        bucket = manager._empty_bucket()
        assert bucket["total_cost_cents"] == 0
        assert bucket["total_tickets"] == 0
        assert bucket["total_api_duration_seconds"] == 0
        assert bucket["total_wall_duration_seconds"] == 0
        assert bucket["total_lines_added"] == 0
        assert bucket["total_lines_removed"] == 0

    def test_aggregate_into_bucket(self, tmp_path):
        """Test _aggregate_into_bucket correctly aggregates."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        bucket = manager._empty_bucket()
        stats = {
            "total_cost_cents": 100,
            "total_tickets": 2,
            "total_api_duration_seconds": 60,
            "total_wall_duration_seconds": 120,
            "total_lines_added": 50,
            "total_lines_removed": 25,
        }

        manager._aggregate_into_bucket(bucket, stats)
        manager._aggregate_into_bucket(bucket, stats)

        assert bucket["total_cost_cents"] == 200
        assert bucket["total_tickets"] == 4
        assert bucket["total_api_duration_seconds"] == 120
        assert bucket["total_wall_duration_seconds"] == 240
        assert bucket["total_lines_added"] == 100
        assert bucket["total_lines_removed"] == 50

    def test_rollup_no_effect_on_recent_data(self, tmp_path):
        """Test that rollup doesn't affect data within 30 days."""
        from datetime import datetime, timezone, timedelta

        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Add a single recent entry
        today = datetime.now(timezone.utc).date()
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        manager.state["stats"]["by_date"][yesterday] = {
            "total_cost_cents": 100,
            "total_tickets": 1,
            "total_api_duration_seconds": 60,
            "total_wall_duration_seconds": 120,
            "total_lines_added": 10,
            "total_lines_removed": 5,
        }

        manager._save()

        # Entry should still exist unchanged
        assert yesterday in manager.state["stats"]["by_date"]
        assert manager.state["stats"]["by_date"][yesterday]["total_cost_cents"] == 100


class TestTokenTracking:
    """Tests for token usage tracking functionality."""

    def test_record_cost_with_tokens(self, tmp_path):
        """Test recording cost including token usage."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card123",
            project="myproject",
            total_cost="$1.50",
            api_duration="2m 30s",
            wall_duration="5m 15s",
            code_changes="+100 -50",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=5000,
        )

        stats = manager.get_stats()
        assert stats.total_input_tokens == 1000
        assert stats.total_output_tokens == 500
        assert stats.total_cache_creation_tokens == 200
        assert stats.total_cache_read_tokens == 5000

    def test_token_aggregation_multiple_tickets(self, tmp_path):
        """Test that tokens are properly aggregated across tickets."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
            input_tokens=1000,
            output_tokens=200,
            cache_creation_tokens=100,
            cache_read_tokens=3000,
        )
        manager.record_cost(
            card_id="card2",
            project="project2",
            total_cost="$2.00",
            input_tokens=2000,
            output_tokens=400,
            cache_creation_tokens=150,
            cache_read_tokens=4000,
        )

        stats = manager.get_stats()
        assert stats.total_input_tokens == 3000
        assert stats.total_output_tokens == 600
        assert stats.total_cache_creation_tokens == 250
        assert stats.total_cache_read_tokens == 7000

    def test_token_stats_per_project(self, tmp_path):
        """Test getting token stats filtered by project."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
            input_tokens=1000,
            output_tokens=200,
        )
        manager.record_cost(
            card_id="card2",
            project="project2",
            total_cost="$2.00",
            input_tokens=2000,
            output_tokens=400,
        )

        stats1 = manager.get_stats("project1")
        assert stats1.total_input_tokens == 1000
        assert stats1.total_output_tokens == 200

        stats2 = manager.get_stats("project2")
        assert stats2.total_input_tokens == 2000
        assert stats2.total_output_tokens == 400

    def test_format_tokens_helper(self, tmp_path):
        """Test the format_tokens helper method."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))
        stats = manager.get_stats()

        # Test small numbers
        assert stats.format_tokens(0) == "0"
        assert stats.format_tokens(500) == "500"
        assert stats.format_tokens(999) == "999"

        # Test K format
        assert stats.format_tokens(1000) == "1.0K"
        assert stats.format_tokens(1500) == "1.5K"
        assert stats.format_tokens(50000) == "50.0K"
        assert stats.format_tokens(999999) == "1000.0K"

        # Test M format
        assert stats.format_tokens(1_000_000) == "1.00M"
        assert stats.format_tokens(2_500_000) == "2.50M"
        assert stats.format_tokens(10_000_000) == "10.00M"

    def test_total_tokens_property(self, tmp_path):
        """Test the total_tokens property (input + output)."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
            input_tokens=1000,
            output_tokens=500,
        )

        stats = manager.get_stats()
        assert stats.total_tokens == 1500
        assert stats.total_tokens_formatted == "1.5K"

    def test_token_formatting_properties(self, tmp_path):
        """Test the various token formatting properties."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card1",
            project="project1",
            total_cost="$1.00",
            input_tokens=50000,
            output_tokens=5000,
            cache_read_tokens=1_000_000,
        )

        stats = manager.get_stats()
        assert stats.input_tokens_formatted == "50.0K"
        assert stats.output_tokens_formatted == "5.0K"
        assert stats.cache_read_tokens_formatted == "1.00M"

    def test_format_stats_report_includes_tokens(self, tmp_path):
        """Test that format_stats_report includes token statistics."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card123",
            project="myproject",
            total_cost="$1.00",
            input_tokens=50000,
            output_tokens=5000,
            cache_read_tokens=100000,
        )

        report = manager.format_stats_report()

        # Check that token section is present
        assert "Claude Token Usage" in report
        assert "Total Tokens" in report
        assert "Input Tokens" in report
        assert "Output Tokens" in report
        assert "Cache Read" in report

        # Check formatted values
        assert "55.0K" in report  # total tokens
        assert "50.0K" in report  # input tokens
        assert "5.0K" in report   # output tokens
        assert "100.0K" in report  # cache read

    def test_ticket_history_includes_tokens(self, tmp_path):
        """Test that ticket history records include token data."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        manager.record_cost(
            card_id="card123",
            project="myproject",
            total_cost="$1.00",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=5000,
        )

        history = manager.state["stats"]["ticket_history"]
        assert len(history) == 1
        assert history[0]["input_tokens"] == 1000
        assert history[0]["output_tokens"] == 500
        assert history[0]["cache_creation_tokens"] == 200
        assert history[0]["cache_read_tokens"] == 5000

    def test_empty_bucket_includes_token_fields(self, tmp_path):
        """Test that _empty_bucket includes token fields."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        bucket = manager._empty_bucket()
        assert "total_input_tokens" in bucket
        assert "total_output_tokens" in bucket
        assert "total_cache_creation_tokens" in bucket
        assert "total_cache_read_tokens" in bucket
        assert bucket["total_input_tokens"] == 0

    def test_aggregate_into_bucket_includes_tokens(self, tmp_path):
        """Test that _aggregate_into_bucket handles token fields."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        bucket = manager._empty_bucket()
        stats = {
            "total_cost_cents": 100,
            "total_tickets": 1,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_cache_creation_tokens": 100,
            "total_cache_read_tokens": 5000,
        }

        manager._aggregate_into_bucket(bucket, stats)
        manager._aggregate_into_bucket(bucket, stats)

        assert bucket["total_input_tokens"] == 2000
        assert bucket["total_output_tokens"] == 1000
        assert bucket["total_cache_creation_tokens"] == 200
        assert bucket["total_cache_read_tokens"] == 10000

    def test_backward_compatibility_no_tokens(self, tmp_path):
        """Test backward compatibility with old stats without token data."""
        state_file = tmp_path / "state.json"
        manager = StateManager(str(state_file))

        # Simulate old state without token fields
        manager.state["stats"]["global"] = {
            "total_cost_cents": 100,
            "total_tickets": 1,
            "total_api_duration_seconds": 60,
            "total_wall_duration_seconds": 120,
            "total_lines_added": 50,
            "total_lines_removed": 25,
            # No token fields
        }

        # Should not error, tokens should default to 0
        stats = manager.get_stats()
        assert stats.total_input_tokens == 0
        assert stats.total_output_tokens == 0
        assert stats.total_tokens == 0
