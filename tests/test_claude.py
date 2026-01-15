"""Tests for claude module."""

import pytest

from trellm.claude import ClaudeRunner, ClaudeResult
from trellm.config import ClaudeConfig
from trellm.trello import TrelloCard


class TestClaudeRunner:
    """Tests for ClaudeRunner class."""

    def test_build_prompt(self):
        """Test prompt building."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        card = TrelloCard(
            id="abc123",
            name="myproject implement feature",
            description="Add a new button to the UI",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-08T12:00:00Z",
        )

        prompt = runner._build_prompt(card)

        # Prompt includes card ID and URL but NOT name/description
        # (Claude fetches these from Trello directly)
        assert "abc123" in prompt
        assert "https://trello.com/c/abc123" in prompt
        assert "commit your changes" in prompt
        # Name and description should NOT be in prompt
        assert "myproject implement feature" not in prompt
        assert "Add a new button to the UI" not in prompt

    def test_build_prompt_includes_voice_note_instructions(self):
        """Test that prompt includes voice note handling instructions."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        card = TrelloCard(
            id="abc123",
            name="test card",
            description="",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-08T12:00:00Z",
        )

        prompt = runner._build_prompt(card)

        # Should include voice note handling instructions
        assert "Voice note handling:" in prompt
        assert "audio file attachments" in prompt
        assert "Transcribed:" in prompt
        assert ".opus" in prompt or "voice notes" in prompt

    def test_build_prompt_no_description(self):
        """Test prompt building without description."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        card = TrelloCard(
            id="abc123",
            name="myproject fix bug",
            description="",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-08T12:00:00Z",
        )

        prompt = runner._build_prompt(card)

        assert "abc123" in prompt
        assert "Description:" not in prompt

    def test_parse_output_with_session_id(self):
        """Test parsing output with session ID."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        output = """Some text
{"type": "message", "content": "Working on it..."}
{"type": "result", "session_id": "sess-123", "result": "Task done"}
"""

        result = runner._parse_output(output)

        assert result.success
        assert result.session_id == "sess-123"
        assert result.summary == "Task done"

    def test_parse_output_no_session_id(self):
        """Test parsing output without session ID."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        output = """Some text without JSON
Or maybe malformed { json
"""

        result = runner._parse_output(output)

        assert result.success
        assert result.session_id is None
        assert result.summary == "Task completed"

    def test_parse_output_multiple_json_lines(self):
        """Test parsing output with multiple JSON lines."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config)

        output = """{"type": "init", "session_id": "old-session"}
{"type": "message", "content": "Working..."}
{"type": "result", "session_id": "new-session", "result": "All done"}
"""

        result = runner._parse_output(output)

        # Should get the last session_id
        assert result.session_id == "new-session"
        assert result.summary == "All done"

    def test_print_prefixed_single_line(self, capsys):
        """Test _print_prefixed with single line."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config, verbose=True)

        runner._print_prefixed("Hello world", "[test] ")

        captured = capsys.readouterr()
        assert captured.out == "[test] Hello world\n"

    def test_print_prefixed_multiline(self, capsys):
        """Test _print_prefixed with multiline text."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config, verbose=True)

        runner._print_prefixed("Line 1\nLine 2\nLine 3", "[proj] ")

        captured = capsys.readouterr()
        assert captured.out == "[proj] Line 1\n[proj] Line 2\n[proj] Line 3\n"

    def test_print_prefixed_empty_prefix(self, capsys):
        """Test _print_prefixed with empty prefix."""
        config = ClaudeConfig()
        runner = ClaudeRunner(config, verbose=True)

        runner._print_prefixed("No prefix", "")

        captured = capsys.readouterr()
        assert captured.out == "No prefix\n"
