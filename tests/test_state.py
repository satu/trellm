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
