"""Tests for claude module."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from trellm.claude import (
    ClaudeRunner,
    ClaudeResult,
    PromptTooLongError,
    RateLimitError,
    PROMPT_TOO_LONG_DETAILED_PATTERN,
    PROMPT_TOO_LONG_SIMPLE_PATTERN,
    RATE_LIMIT_PATTERN,
    RATE_LIMIT_RESET_PATTERN,
)
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


class TestErrorPatterns:
    """Tests for error detection regex patterns."""

    def test_prompt_too_long_detailed_pattern(self):
        """Test prompt too long error pattern matching with token counts."""
        error_msg = 'Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 206453 tokens > 200000 maximum"}}'
        match = PROMPT_TOO_LONG_DETAILED_PATTERN.search(error_msg)
        assert match is not None
        assert match.group(1) == "206453"
        assert match.group(2) == "200000"

    def test_prompt_too_long_detailed_pattern_singular_token(self):
        """Test prompt too long with singular 'token'."""
        error_msg = "prompt is too long: 1 token > 200000 maximum"
        match = PROMPT_TOO_LONG_DETAILED_PATTERN.search(error_msg)
        assert match is not None
        assert match.group(1) == "1"

    def test_prompt_too_long_simple_pattern(self):
        """Test simple 'Prompt is too long' message from Claude result."""
        # This is the actual format from Claude Code when it hits the limit
        error_msg = '{"type":"result","result":"Prompt is too long"}'
        assert PROMPT_TOO_LONG_SIMPLE_PATTERN.search(error_msg) is not None

    def test_prompt_too_long_simple_pattern_case_insensitive(self):
        """Test simple pattern is case insensitive."""
        for msg in ["Prompt is too long", "prompt is too long", "PROMPT IS TOO LONG"]:
            assert PROMPT_TOO_LONG_SIMPLE_PATTERN.search(msg) is not None, f"Failed for: {msg}"

    def test_rate_limit_pattern(self):
        """Test rate limit error pattern matching."""
        error_msg = '{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit."}}'
        assert RATE_LIMIT_PATTERN.search(error_msg) is not None

    def test_rate_limit_reset_pattern_hours(self):
        """Test rate limit reset time parsing - hours."""
        msg = "Session limit reached – resets in 2 hours"
        match = RATE_LIMIT_RESET_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "2"
        assert match.group(2) == "hours"

    def test_rate_limit_reset_pattern_minutes(self):
        """Test rate limit reset time parsing - minutes."""
        msg = "resets in 30 minutes"
        match = RATE_LIMIT_RESET_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "30"
        assert match.group(2) == "minutes"

    def test_rate_limit_reset_pattern_days(self):
        """Test rate limit reset time parsing - days."""
        msg = "Weekly limits reset 2 days"
        match = RATE_LIMIT_RESET_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "2"
        assert match.group(2) == "days"

    def test_rate_limit_reset_pattern_short_form(self):
        """Test rate limit reset with short form (h, m, d)."""
        for msg, expected_val, expected_unit in [
            ("resets in 2h", "2", "h"),
            ("resets in 30m", "30", "m"),
            ("resets in 1d", "1", "d"),
        ]:
            match = RATE_LIMIT_RESET_PATTERN.search(msg)
            assert match is not None, f"Failed for: {msg}"
            assert match.group(1) == expected_val
            assert match.group(2) == expected_unit


class TestClaudeRunnerErrorChecking:
    """Tests for ClaudeRunner error detection."""

    @pytest.fixture
    def runner(self):
        """Create a ClaudeRunner instance."""
        config = ClaudeConfig(
            binary="claude",
            timeout=60,
            yolo=True,
            projects={},
        )
        return ClaudeRunner(config)

    def test_check_for_prompt_too_long_error_detailed(self, runner):
        """Test detection of prompt too long error with token counts."""
        stderr = 'Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 250000 tokens > 200000 maximum"}}'

        with pytest.raises(PromptTooLongError) as exc_info:
            runner._check_for_errors(stderr, "")

        assert exc_info.value.tokens == 250000
        assert exc_info.value.maximum == 200000

    def test_check_for_prompt_too_long_error_simple(self, runner):
        """Test detection of simple 'Prompt is too long' from result."""
        # This simulates the actual format from Claude Code output
        stdout = '{"type":"result","result":"Prompt is too long"}'

        with pytest.raises(PromptTooLongError) as exc_info:
            runner._check_for_errors("", stdout)

        # Simple format has no token counts
        assert exc_info.value.tokens is None
        assert exc_info.value.maximum is None

    def test_check_for_rate_limit_error(self, runner):
        """Test detection of rate limit error."""
        stderr = 'Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit."}}'

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors(stderr, "")

        # No reset time in this message
        assert exc_info.value.reset_seconds is None

    def test_check_for_rate_limit_error_with_reset(self, runner):
        """Test detection of rate limit error with reset time."""
        stderr = 'rate_limit_error - Session limit reached – resets in 2 hours'

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors(stderr, "")

        # 2 hours = 7200 seconds
        assert exc_info.value.reset_seconds == 7200

    def test_check_for_rate_limit_error_minutes(self, runner):
        """Test detection of rate limit error with minutes reset."""
        stderr = 'rate_limit_error - resets in 30 minutes'

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors(stderr, "")

        # 30 minutes = 1800 seconds
        assert exc_info.value.reset_seconds == 1800

    def test_check_for_rate_limit_error_days(self, runner):
        """Test detection of rate limit error with days reset."""
        stderr = 'rate_limit_error - Weekly limits reset 2 days'

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors(stderr, "")

        # 2 days = 172800 seconds
        assert exc_info.value.reset_seconds == 172800

    def test_no_error_on_success(self, runner):
        """Test that no error is raised on normal output."""
        stderr = ""
        stdout = '{"type":"result","result":"Task completed"}'

        # Should not raise
        runner._check_for_errors(stderr, stdout)


class TestClaudeRunnerCompact:
    """Tests for the /compact functionality."""

    @pytest.fixture
    def runner(self):
        """Create a ClaudeRunner instance."""
        config = ClaudeConfig(
            binary="claude",
            timeout=60,
            yolo=True,
            projects={},
        )
        return ClaudeRunner(config)

    @pytest.mark.asyncio
    async def test_run_compact_success(self, runner):
        """Test successful /compact execution."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","session_id":"new-session-123"}\n',
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_compact(
                session_id="old-session-456",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result == "new-session-123"

    @pytest.mark.asyncio
    async def test_run_compact_failure(self, runner):
        """Test failed /compact execution."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Error running compact")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_compact(
                session_id="old-session-456",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_compact_timeout(self, runner):
        """Test /compact timeout handling."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_compact(
                session_id="old-session-456",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is None


class TestClaudeRunnerRetryLogic:
    """Tests for retry logic in run method."""

    @pytest.fixture
    def runner(self):
        """Create a ClaudeRunner instance."""
        config = ClaudeConfig(
            binary="claude",
            timeout=60,
            yolo=True,
            projects={},
        )
        return ClaudeRunner(config)

    @pytest.fixture
    def mock_card(self):
        """Create a mock TrelloCard."""
        return TrelloCard(
            id="card123",
            name="Test Card",
            description="Test description",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-01T00:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_run_success_no_retry(self, runner, mock_card):
        """Test successful run without any retries."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result) as mock_run:
            result = await runner.run(
                card=mock_card,
                project="test",
                session_id="old-session",
                working_dir="/tmp/test",
            )

        assert result == expected_result
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_prompt_too_long_retry_with_compact(self, runner, mock_card):
        """Test retry after prompt too long with /compact."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-after-compact",
            summary="Task completed",
            output="{}",
        )

        call_count = 0

        async def mock_run_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise PromptTooLongError("Too long", tokens=250000, maximum=200000)
            return expected_result

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch.object(
                runner, "_run_compact", return_value="compacted-session"
            ) as mock_compact:
                result = await runner.run(
                    card=mock_card,
                    project="test",
                    session_id="old-session",
                    working_dir="/tmp/test",
                )

        assert result == expected_result
        mock_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_prompt_too_long_no_session(self, runner, mock_card):
        """Test prompt too long without session (cannot compact)."""
        async def mock_run_once(*args, **kwargs):
            raise PromptTooLongError("Too long", tokens=250000, maximum=200000)

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with pytest.raises(RuntimeError, match="Prompt too long"):
                await runner.run(
                    card=mock_card,
                    project="test",
                    session_id=None,  # No session to compact
                    working_dir="/tmp/test",
                )

    @pytest.mark.asyncio
    async def test_run_prompt_too_long_simple_retry_with_compact(self, runner, mock_card):
        """Test retry after simple 'Prompt is too long' error (no token counts)."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-after-compact",
            summary="Task completed",
            output="{}",
        )

        call_count = 0

        async def mock_run_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simple error without token counts (as from actual Claude output)
                raise PromptTooLongError("Prompt is too long")
            return expected_result

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch.object(
                runner, "_run_compact", return_value="compacted-session"
            ) as mock_compact:
                result = await runner.run(
                    card=mock_card,
                    project="test",
                    session_id="old-session",
                    working_dir="/tmp/test",
                )

        assert result == expected_result
        mock_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_rate_limit_retry_after_sleep(self, runner, mock_card):
        """Test retry after rate limit with sleep."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        call_count = 0

        async def mock_run_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate rate limit with 1 second reset for test speed
                raise RateLimitError("Rate limit", reset_seconds=1)
            return expected_result

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch("asyncio.sleep") as mock_sleep:
                result = await runner.run(
                    card=mock_card,
                    project="test",
                    session_id="session",
                    working_dir="/tmp/test",
                )

        assert result == expected_result
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_run_rate_limit_default_sleep(self, runner, mock_card):
        """Test rate limit with no reset time uses default."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        call_count = 0

        async def mock_run_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # No reset time specified
                raise RateLimitError("Rate limit", reset_seconds=None)
            return expected_result

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch("asyncio.sleep") as mock_sleep:
                result = await runner.run(
                    card=mock_card,
                    project="test",
                    session_id="session",
                    working_dir="/tmp/test",
                )

        assert result == expected_result
        # Default is 300 seconds (5 minutes)
        mock_sleep.assert_called_once_with(300)

    @pytest.mark.asyncio
    async def test_run_max_retries_exceeded(self, runner, mock_card):
        """Test that max retries is respected."""
        async def mock_run_once(*args, **kwargs):
            raise RateLimitError("Rate limit", reset_seconds=1)

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch("asyncio.sleep"):
                with pytest.raises(RuntimeError, match="Rate limit exceeded"):
                    await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="session",
                        working_dir="/tmp/test",
                    )
