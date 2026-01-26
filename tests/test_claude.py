"""Tests for claude module."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from trellm.claude import (
    ClaudeRunner,
    ClaudeResult,
    CostInfo,
    PromptTooLongError,
    RateLimitError,
    PROMPT_TOO_LONG_DETAILED_PATTERN,
    PROMPT_TOO_LONG_SIMPLE_PATTERN,
    RATE_LIMIT_PATTERN,
    RATE_LIMIT_USER_PATTERN,
    RATE_LIMIT_RESET_DURATION_PATTERN,
    RATE_LIMIT_RESET_TIME_PATTERN,
    UsageLimitInfo,
    ClaudeUsageLimits,
    fetch_claude_usage_limits,
    _parse_usage_limit,
    _get_session_jsonl_path,
    _read_token_usage_from_jsonl,
    _get_context_size_from_jsonl,
    CLAUDE_PROJECTS_DIR,
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

    def test_build_prompt_includes_github_commit_link_requirement(self):
        """Test that prompt requires GitHub links for commit mentions."""
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

        # Should include requirement for GitHub commit links
        assert "commit hash" in prompt or "commit SHA" in prompt
        assert "GitHub link" in prompt
        assert "git remote get-url origin" in prompt

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

    def test_rate_limit_api_pattern(self):
        """Test rate limit API error pattern matching."""
        error_msg = '{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit."}}'
        assert RATE_LIMIT_PATTERN.search(error_msg) is not None

    def test_rate_limit_user_pattern(self):
        """Test rate limit user-facing pattern matching."""
        error_msg = "You've hit your limit · resets 8pm (UTC)"
        assert RATE_LIMIT_USER_PATTERN.search(error_msg) is not None

    def test_rate_limit_user_pattern_case_insensitive(self):
        """Test rate limit user pattern is case insensitive."""
        for msg in ["You've hit your limit", "you've hit your limit", "YOU'VE HIT YOUR LIMIT"]:
            assert RATE_LIMIT_USER_PATTERN.search(msg) is not None, f"Failed for: {msg}"

    def test_rate_limit_reset_duration_pattern_hours(self):
        """Test rate limit reset duration parsing - hours."""
        msg = "Session limit reached – resets in 2 hours"
        match = RATE_LIMIT_RESET_DURATION_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "2"
        assert match.group(2) == "hours"

    def test_rate_limit_reset_duration_pattern_minutes(self):
        """Test rate limit reset duration parsing - minutes."""
        msg = "resets in 30 minutes"
        match = RATE_LIMIT_RESET_DURATION_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "30"
        assert match.group(2) == "minutes"

    def test_rate_limit_reset_duration_pattern_days(self):
        """Test rate limit reset duration parsing - days."""
        msg = "Weekly limits reset 2 days"
        match = RATE_LIMIT_RESET_DURATION_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "2"
        assert match.group(2) == "days"

    def test_rate_limit_reset_duration_pattern_short_form(self):
        """Test rate limit reset with short form (h, m, d)."""
        for msg, expected_val, expected_unit in [
            ("resets in 2h", "2", "h"),
            ("resets in 30m", "30", "m"),
            ("resets in 1d", "1", "d"),
        ]:
            match = RATE_LIMIT_RESET_DURATION_PATTERN.search(msg)
            assert match is not None, f"Failed for: {msg}"
            assert match.group(1) == expected_val
            assert match.group(2) == expected_unit

    def test_rate_limit_reset_time_pattern_pm(self):
        """Test rate limit reset clock time parsing - PM."""
        msg = "You've hit your limit · resets 8pm (UTC)"
        match = RATE_LIMIT_RESET_TIME_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "8"
        assert match.group(2) is None  # no minutes
        assert match.group(3) == "pm"
        assert match.group(4) == "UTC"

    def test_rate_limit_reset_time_pattern_am(self):
        """Test rate limit reset clock time parsing - AM."""
        msg = "resets 10am"
        match = RATE_LIMIT_RESET_TIME_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "10"
        assert match.group(3) == "am"

    def test_rate_limit_reset_time_pattern_with_minutes(self):
        """Test rate limit reset clock time parsing with minutes."""
        msg = "resets 8:30pm (UTC)"
        match = RATE_LIMIT_RESET_TIME_PATTERN.search(msg)
        assert match is not None
        assert match.group(1) == "8"
        assert match.group(2) == "30"
        assert match.group(3) == "pm"


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
        assert exc_info.value.session_id is None  # No session_id passed

    def test_check_for_prompt_too_long_error_simple(self, runner):
        """Test detection of simple 'Prompt is too long' from result."""
        # This simulates the actual format from Claude Code output
        stdout = '{"type":"result","result":"Prompt is too long"}'

        with pytest.raises(PromptTooLongError) as exc_info:
            runner._check_for_errors("", stdout)

        # Simple format has no token counts
        assert exc_info.value.tokens is None
        assert exc_info.value.maximum is None
        assert exc_info.value.session_id is None  # No session_id passed

    def test_check_for_prompt_too_long_error_with_session_id(self, runner):
        """Test that session_id is captured in PromptTooLongError."""
        stdout = '{"type":"result","result":"Prompt is too long"}'

        with pytest.raises(PromptTooLongError) as exc_info:
            runner._check_for_errors("", stdout, session_id="test-session-123")

        assert exc_info.value.session_id == "test-session-123"

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

    def test_check_for_rate_limit_user_facing_message(self, runner):
        """Test detection of user-facing rate limit message."""
        # This is the actual format from Claude Code output
        stdout = '{"type":"result","result":"You\'ve hit your limit · resets 8pm (UTC)"}'

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors("", stdout)

        # Should have parsed the reset time
        assert exc_info.value.reset_seconds is not None
        # Reset time should be positive (some time in the future)
        assert exc_info.value.reset_seconds > 0

    def test_check_for_rate_limit_user_facing_no_utc(self, runner):
        """Test detection of user-facing rate limit without UTC suffix."""
        stdout = "You've hit your limit · resets 10am"

        with pytest.raises(RateLimitError) as exc_info:
            runner._check_for_errors("", stdout)

        # Should have parsed the reset time
        assert exc_info.value.reset_seconds is not None

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
            with patch.object(runner, "_run_cost", return_value=None):
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
            with patch.object(runner, "_run_cost", return_value=None):
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
            with patch.object(runner, "_run_cost", return_value=None):
                result = await runner._run_compact(
                    session_id="old-session-456",
                    working_dir="/tmp/test",
                    prefix="[test] ",
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_compact_with_custom_prompt(self, runner):
        """Test /compact with custom prompt passes prompt to command."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","session_id":"new-session-123"}\n',
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            with patch.object(runner, "_run_cost", return_value=None):
                result = await runner._run_compact(
                    session_id="old-session-456",
                    working_dir="/tmp/test",
                    prefix="[test] ",
                    compact_prompt="Preserve API patterns and test conventions",
                )

        assert result == "new-session-123"
        # Verify the prompt was passed correctly
        call_args = mock_exec.call_args
        cmd_args = call_args[0]  # positional args
        # Find the -p argument
        for i, arg in enumerate(cmd_args):
            if arg == "-p" and i + 1 < len(cmd_args):
                assert cmd_args[i + 1] == "/compact Preserve API patterns and test conventions"
                break
        else:
            pytest.fail("Could not find -p argument in command")

    @pytest.mark.asyncio
    async def test_run_compact_logs_context_sizes(self, runner, caplog):
        """Test that /compact logs context sizes before and after."""
        import logging
        from pathlib import Path
        caplog.set_level(logging.INFO)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","session_id":"new-session-123"}\n',
                b"",
            )
        )

        call_count = 0

        def mock_get_session_jsonl_path(session_id, working_dir):
            # Return a mock path for both sessions
            return Path(f"/mock/path/{session_id}.jsonl")

        def mock_get_context_size_from_jsonl(jsonl_path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 125000  # Before compaction context size
            return 36000  # After compaction context size

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("trellm.claude._get_session_jsonl_path", side_effect=mock_get_session_jsonl_path):
                with patch("trellm.claude._get_context_size_from_jsonl", side_effect=mock_get_context_size_from_jsonl):
                    result = await runner._run_compact(
                        session_id="old-session-456",
                        working_dir="/tmp/test",
                        prefix="[test] ",
                    )

        assert result == "new-session-123"

        # Check that logs contain context size information
        log_messages = [r.message for r in caplog.records]
        # Before compaction log
        before_log = [m for m in log_messages if "Context size before compaction" in m]
        assert len(before_log) == 1
        assert "125000" in before_log[0]
        # After compaction log with reduction
        after_log = [m for m in log_messages if "/compact successful" in m and "reduction" in m]
        assert len(after_log) == 1
        assert "125000 -> 36000" in after_log[0]  # Before -> After
        assert "89000" in after_log[0]  # Reduction amount
        assert "71.2%" in after_log[0]  # Reduction percentage

    @pytest.mark.asyncio
    async def test_run_compact_logs_only_after_when_before_fails(self, runner, caplog):
        """Test that /compact logs after context size even when before fails."""
        import logging
        from pathlib import Path
        caplog.set_level(logging.INFO)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"result","session_id":"new-session-123"}\n',
                b"",
            )
        )

        call_count = 0

        def mock_get_session_jsonl_path(session_id, working_dir):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # Before session path not found
            return Path(f"/mock/path/{session_id}.jsonl")

        def mock_get_context_size_from_jsonl(jsonl_path):
            return 36000  # After compaction context size

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("trellm.claude._get_session_jsonl_path", side_effect=mock_get_session_jsonl_path):
                with patch("trellm.claude._get_context_size_from_jsonl", side_effect=mock_get_context_size_from_jsonl):
                    result = await runner._run_compact(
                        session_id="old-session-456",
                        working_dir="/tmp/test",
                        prefix="[test] ",
                    )

        assert result == "new-session-123"

        # Check that logs contain after-compaction context size information
        log_messages = [r.message for r in caplog.records]
        after_log = [m for m in log_messages if "/compact successful" in m and "context size after" in m]
        assert len(after_log) == 1
        assert "36000" in after_log[0]  # Context size after


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
            with patch.object(runner, "_run_compact", return_value="compacted-session"):
                with patch.object(runner, "_run_cost", return_value=None):
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
                with patch.object(runner, "_run_cost", return_value=None):
                    result = await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id=mock_card.id,  # Same card to skip pre-compaction
                    )

        assert result == expected_result
        # Called once for error recovery (not for pre-compaction since same card)
        mock_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_prompt_too_long_no_session(self, runner, mock_card):
        """Test prompt too long without session (cannot compact)."""
        async def mock_run_once(*args, **kwargs):
            raise PromptTooLongError("Too long", tokens=250000, maximum=200000)

        with patch.object(runner, "_run_once", side_effect=mock_run_once):
            with patch.object(runner, "_run_cost", return_value=None):
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
                with patch.object(runner, "_run_cost", return_value=None):
                    result = await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id=mock_card.id,  # Same card to skip pre-compaction
                    )

        assert result == expected_result
        # Called once for error recovery (not for pre-compaction since same card)
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
            with patch.object(runner, "_run_compact", return_value="compacted-session"):
                with patch.object(runner, "_run_cost", return_value=None):
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
            with patch.object(runner, "_run_compact", return_value="compacted-session"):
                with patch.object(runner, "_run_cost", return_value=None):
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
            with patch.object(runner, "_run_compact", return_value="compacted-session"):
                with patch.object(runner, "_run_cost", return_value=None):
                    with patch("asyncio.sleep"):
                        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
                            await runner.run(
                                card=mock_card,
                                project="test",
                                session_id="session",
                                working_dir="/tmp/test",
                            )


class TestClaudeRunnerCost:
    """Tests for the /cost functionality."""

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
    async def test_run_cost_success(self, runner):
        """Test successful /cost execution with JSON format."""
        # The actual JSON format from Claude Code /cost command
        json_output = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 81,
            "duration_api_ms": 379700,  # ~6m 19.7s
            "num_turns": 36,
            "result": "",
            "session_id": "test-session",
            "total_cost_usd": 0.55,
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                (json.dumps(json_output) + "\n").encode(),
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_cost(
                session_id="test-session",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is not None
        assert result.total_cost == "$0.5500"
        assert result.api_duration == "6m 19.7s"
        assert result.wall_duration == "81ms"
        # code_changes is not available in JSON format
        assert result.code_changes is None

    @pytest.mark.asyncio
    async def test_run_cost_large_values(self, runner):
        """Test /cost with larger duration values."""
        json_output = {
            "type": "result",
            "duration_ms": 3600000,  # 1 hour
            "duration_api_ms": 7200000,  # 2 hours
            "total_cost_usd": 1.2345,
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                (json.dumps(json_output) + "\n").encode(),
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_cost(
                session_id="test-session",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is not None
        assert result.total_cost == "$1.2345"
        assert result.api_duration == "2h 0m"
        assert result.wall_duration == "1h 0m"

    @pytest.mark.asyncio
    async def test_run_cost_timeout(self, runner):
        """Test /cost timeout handling."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_cost(
                session_id="test-session",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_cost_failure(self, runner):
        """Test /cost failure handling."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            side_effect=Exception("Some error")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner._run_cost(
                session_id="test-session",
                working_dir="/tmp/test",
                prefix="[test] ",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_cost_with_token_usage(self, runner):
        """Test /cost with token usage read from JSONL file."""
        json_output = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1000,
            "duration_api_ms": 5000,
            "total_cost_usd": 0.25,
            # Note: /cost command returns 0 for all token fields,
            # so we read from JSONL file instead
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                (json.dumps(json_output) + "\n").encode(),
                b"",
            )
        )

        # Mock the JSONL file reading to return token usage
        mock_usage = {
            "input_tokens": 1500,
            "output_tokens": 500,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 30000,
        }

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("trellm.claude._get_session_jsonl_path") as mock_get_path:
                with patch("trellm.claude._read_token_usage_from_jsonl", return_value=mock_usage):
                    mock_get_path.return_value = "/tmp/mock.jsonl"
                    result = await runner._run_cost(
                        session_id="test-session",
                        working_dir="/tmp/test",
                        prefix="[test] ",
                    )

        assert result is not None
        assert result.input_tokens == 1500
        assert result.output_tokens == 500
        assert result.cache_creation_tokens == 200
        assert result.cache_read_tokens == 30000

    @pytest.mark.asyncio
    async def test_run_cost_without_token_usage(self, runner):
        """Test /cost when JSONL file is not found returns zero tokens."""
        json_output = {
            "type": "result",
            "duration_ms": 1000,
            "duration_api_ms": 5000,
            "total_cost_usd": 0.25,
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                (json.dumps(json_output) + "\n").encode(),
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("trellm.claude._get_session_jsonl_path", return_value=None):
                result = await runner._run_cost(
                    session_id="test-session",
                    working_dir="/tmp/test",
                    prefix="[test] ",
                )

        assert result is not None
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cache_creation_tokens == 0
        assert result.cache_read_tokens == 0


class TestFormatDurationMs:
    """Tests for the _format_duration_ms helper method."""

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

    def test_format_milliseconds(self, runner):
        """Test formatting for sub-second durations."""
        assert runner._format_duration_ms(500) == "500ms"
        assert runner._format_duration_ms(1) == "1ms"
        assert runner._format_duration_ms(999) == "999ms"

    def test_format_seconds(self, runner):
        """Test formatting for second-range durations."""
        assert runner._format_duration_ms(1000) == "1.0s"
        assert runner._format_duration_ms(1500) == "1.5s"
        assert runner._format_duration_ms(59000) == "59.0s"

    def test_format_minutes(self, runner):
        """Test formatting for minute-range durations."""
        assert runner._format_duration_ms(60000) == "1m 0.0s"
        assert runner._format_duration_ms(90000) == "1m 30.0s"
        assert runner._format_duration_ms(379700) == "6m 19.7s"
        assert runner._format_duration_ms(3599000) == "59m 59.0s"

    def test_format_hours(self, runner):
        """Test formatting for hour-range durations."""
        assert runner._format_duration_ms(3600000) == "1h 0m"
        assert runner._format_duration_ms(5400000) == "1h 30m"
        assert runner._format_duration_ms(7200000) == "2h 0m"


class TestClaudeRunnerPreCompaction:
    """Tests for pre-task compaction functionality."""

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
            id="new-card-456",
            name="Test Card",
            description="Test description",
            url="https://trello.com/c/abc123",
            last_activity="2026-01-01T00:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_pre_compaction_with_new_card(self, runner, mock_card):
        """Test that pre-compaction runs when processing a different card."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result):
            with patch.object(runner, "_run_compact", return_value="compacted-session") as mock_compact:
                with patch.object(runner, "_run_cost", return_value=None):
                    await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id="previous-card-123",  # Different from mock_card.id
                    )

        # Should have called compact because card IDs are different
        mock_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pre_compaction_with_same_card(self, runner, mock_card):
        """Test that pre-compaction doesn't run when processing the same card."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result):
            with patch.object(runner, "_run_compact", return_value="compacted-session") as mock_compact:
                with patch.object(runner, "_run_cost", return_value=None):
                    await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id=mock_card.id,  # Same as mock_card.id
                    )

        # Should NOT have called compact because card IDs are the same
        mock_compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_pre_compaction_without_session(self, runner, mock_card):
        """Test that pre-compaction doesn't run without an existing session."""
        expected_result = ClaudeResult(
            success=True,
            session_id="new-session",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result):
            with patch.object(runner, "_run_compact", return_value="compacted-session") as mock_compact:
                with patch.object(runner, "_run_cost", return_value=None):
                    await runner.run(
                        card=mock_card,
                        project="test",
                        session_id=None,  # No existing session
                        working_dir="/tmp/test",
                        last_card_id="previous-card-123",
                    )

        # Should NOT have called compact because no session to compact
        mock_compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_compaction_with_no_last_card(self, runner, mock_card):
        """Test that pre-compaction runs when last_card_id is None (first card)."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result):
            with patch.object(runner, "_run_compact", return_value="compacted-session") as mock_compact:
                with patch.object(runner, "_run_cost", return_value=None):
                    await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id=None,  # No previous card
                    )

        # Should have called compact because this is the first card for existing session
        mock_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_pre_compaction_failure_continues(self, runner, mock_card):
        """Test that processing continues even if pre-compaction fails."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )

        with patch.object(runner, "_run_once", return_value=expected_result) as mock_run:
            with patch.object(runner, "_run_compact", return_value=None):  # Compact fails
                with patch.object(runner, "_run_cost", return_value=None):
                    result = await runner.run(
                        card=mock_card,
                        project="test",
                        session_id="old-session",
                        working_dir="/tmp/test",
                        last_card_id="previous-card-123",
                    )

        # Should still have run the task
        mock_run.assert_called_once()
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_cost_info_attached_to_result(self, runner, mock_card):
        """Test that cost info is attached to the result."""
        expected_result = ClaudeResult(
            success=True,
            session_id="session-123",
            summary="Task completed",
            output="{}",
        )
        cost_info = CostInfo(
            total_cost="$0.50",
            api_duration="5m",
            wall_duration="30m",
            code_changes="100 lines",
        )

        with patch.object(runner, "_run_once", return_value=expected_result):
            with patch.object(runner, "_run_cost", return_value=cost_info):
                result = await runner.run(
                    card=mock_card,
                    project="test",
                    session_id=None,
                    working_dir="/tmp/test",
                )

        assert result.cost_info == cost_info
        assert result.cost_info.total_cost == "$0.50"


class TestGetSessionJsonlPath:
    """Tests for _get_session_jsonl_path helper function."""

    def test_returns_none_without_working_dir(self):
        """Test that None is returned if working_dir is None."""
        result = _get_session_jsonl_path("test-session", None)
        assert result is None

    def test_returns_none_if_file_not_exists(self, tmp_path):
        """Test that None is returned if JSONL file doesn't exist."""
        with patch("trellm.claude.CLAUDE_PROJECTS_DIR", tmp_path):
            result = _get_session_jsonl_path("test-session", "/home/user/project")
        assert result is None

    def test_returns_path_if_file_exists(self, tmp_path):
        """Test that correct path is returned if JSONL file exists."""
        # Create the expected directory structure
        project_dir = tmp_path / "-home-user-project"
        project_dir.mkdir()
        jsonl_file = project_dir / "test-session.jsonl"
        jsonl_file.touch()

        with patch("trellm.claude.CLAUDE_PROJECTS_DIR", tmp_path):
            result = _get_session_jsonl_path("test-session", "/home/user/project")

        assert result is not None
        assert result == jsonl_file

    def test_handles_tilde_expansion(self, tmp_path):
        """Test that ~ in working_dir is expanded correctly."""
        # Create the expected directory structure
        # ~ expands to home dir, so ~/src/project -> /home/user/src/project
        # The resulting path should be -home-user-src-project
        from pathlib import Path
        expanded_path = Path("~/src/project").expanduser().resolve()
        project_dir_name = str(expanded_path).replace("/", "-")
        project_dir = tmp_path / project_dir_name
        project_dir.mkdir()
        jsonl_file = project_dir / "test-session.jsonl"
        jsonl_file.touch()

        with patch("trellm.claude.CLAUDE_PROJECTS_DIR", tmp_path):
            result = _get_session_jsonl_path("test-session", "~/src/project")

        assert result is not None
        assert result == jsonl_file


class TestReadTokenUsageFromJsonl:
    """Tests for _read_token_usage_from_jsonl helper function."""

    def test_reads_and_aggregates_token_usage(self, tmp_path):
        """Test that token usage is correctly aggregated from JSONL."""
        jsonl_file = tmp_path / "test.jsonl"
        # Write multiple lines with usage data
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 1000,
                    }
                }
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 150,
                        "output_tokens": 75,
                        "cache_creation_input_tokens": 300,
                        "cache_read_input_tokens": 2000,
                    }
                }
            }),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _read_token_usage_from_jsonl(jsonl_file)

        assert result["input_tokens"] == 250  # 100 + 150
        assert result["output_tokens"] == 125  # 50 + 75
        assert result["cache_creation_input_tokens"] == 500  # 200 + 300
        assert result["cache_read_input_tokens"] == 3000  # 1000 + 2000

    def test_handles_empty_file(self, tmp_path):
        """Test that empty file returns zero tokens."""
        jsonl_file = tmp_path / "empty.jsonl"
        jsonl_file.touch()

        result = _read_token_usage_from_jsonl(jsonl_file)

        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0

    def test_handles_lines_without_usage(self, tmp_path):
        """Test that lines without usage data are skipped."""
        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": "Hello"}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    }
                }
            }),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _read_token_usage_from_jsonl(jsonl_file)

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_handles_malformed_json(self, tmp_path):
        """Test that malformed JSON lines are skipped."""
        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            "not valid json",
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    }
                }
            }),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _read_token_usage_from_jsonl(jsonl_file)

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_handles_file_read_error(self, tmp_path):
        """Test that file read errors return zero tokens."""
        # Non-existent file
        jsonl_file = tmp_path / "nonexistent.jsonl"

        result = _read_token_usage_from_jsonl(jsonl_file)

        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0


class TestGetContextSizeFromJsonl:
    """Tests for _get_context_size_from_jsonl helper function."""

    def test_returns_last_input_tokens(self, tmp_path):
        """Test that it returns the input_tokens from the last message with usage."""
        jsonl_file = tmp_path / "test.jsonl"
        # Write multiple lines with usage data - context grows over time
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 1000,  # First message - small context
                        "output_tokens": 50,
                    }
                }
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 5000,  # Second message - larger context
                        "output_tokens": 100,
                    }
                }
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 10000,  # Last message - largest context
                        "output_tokens": 200,
                    }
                }
            }),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _get_context_size_from_jsonl(jsonl_file)

        # Should return the last message's input_tokens
        assert result == 10000

    def test_handles_empty_file(self, tmp_path):
        """Test that empty file returns zero."""
        jsonl_file = tmp_path / "empty.jsonl"
        jsonl_file.touch()

        result = _get_context_size_from_jsonl(jsonl_file)

        assert result == 0

    def test_handles_lines_without_usage(self, tmp_path):
        """Test that lines without usage data are skipped."""
        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 5000,
                        "output_tokens": 50,
                    }
                }
            }),
            # These lines don't have usage - should be skipped
            json.dumps({"type": "user", "message": {"content": "Hello"}}),
            json.dumps({"type": "system", "message": {"content": "System message"}}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _get_context_size_from_jsonl(jsonl_file)

        # Should return the last message WITH usage data
        assert result == 5000

    def test_skips_zero_input_tokens(self, tmp_path):
        """Test that messages with zero input_tokens are skipped."""
        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 8000,  # Real context size
                        "output_tokens": 100,
                    }
                }
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 0,  # Zero - should be skipped
                        "output_tokens": 50,
                    }
                }
            }),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _get_context_size_from_jsonl(jsonl_file)

        # Should return the last non-zero input_tokens
        assert result == 8000

    def test_handles_malformed_json(self, tmp_path):
        """Test that malformed JSON lines are skipped."""
        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 3000,
                        "output_tokens": 50,
                    }
                }
            }),
            "not valid json",
        ]
        jsonl_file.write_text("\n".join(lines))

        result = _get_context_size_from_jsonl(jsonl_file)

        assert result == 3000

    def test_handles_file_read_error(self, tmp_path):
        """Test that file read errors return zero."""
        # Non-existent file
        jsonl_file = tmp_path / "nonexistent.jsonl"

        result = _get_context_size_from_jsonl(jsonl_file)

        assert result == 0


class TestUsageLimitInfo:
    """Tests for UsageLimitInfo dataclass."""

    def test_format_reset_time_no_reset(self):
        """Test formatting when no reset time is set."""
        info = UsageLimitInfo(utilization=50.0, resets_at=None)
        assert info.format_reset_time() == "N/A"

    def test_format_reset_time_shows_date_and_time(self):
        """Test formatting reset time shows actual date and time."""
        # Use a time far in the future for predictable output
        reset_time = datetime(2030, 6, 15, 17, 59, 0, tzinfo=timezone.utc)
        info = UsageLimitInfo(utilization=50.0, resets_at=reset_time)
        result = info.format_reset_time()
        # Should show "Jun 15, 2030 5:59 PM UTC"
        assert "Jun 15, 2030" in result
        assert "5:59 PM UTC" in result

    def test_format_reset_time_morning_hours(self):
        """Test formatting reset time in morning hours."""
        # Use a time far in the future
        reset_time = datetime(2030, 3, 15, 9, 30, 0, tzinfo=timezone.utc)
        info = UsageLimitInfo(utilization=50.0, resets_at=reset_time)
        result = info.format_reset_time()
        assert "Mar 15, 2030" in result
        assert "9:30 AM UTC" in result

    def test_format_reset_time_past(self):
        """Test formatting when reset time is in the past."""
        from datetime import timedelta
        reset_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        info = UsageLimitInfo(utilization=50.0, resets_at=reset_time)
        assert info.format_reset_time() == "now"


class TestClaudeUsageLimits:
    """Tests for ClaudeUsageLimits dataclass."""

    def test_format_report_with_data(self):
        """Test formatting report with usage data."""
        from datetime import timedelta
        reset_5h = datetime.now(timezone.utc) + timedelta(hours=3)
        reset_7d = datetime.now(timezone.utc) + timedelta(days=2)

        limits = ClaudeUsageLimits(
            five_hour=UsageLimitInfo(utilization=25.0, resets_at=reset_5h),
            seven_day=UsageLimitInfo(utilization=60.0, resets_at=reset_7d),
        )
        report = limits.format_report()

        assert "Claude Usage Limits" in report
        assert "5-Hour Session" in report
        assert "25%" in report
        assert "7-Day Weekly" in report
        assert "60%" in report

    def test_format_report_with_error(self):
        """Test formatting report when there's an error."""
        limits = ClaudeUsageLimits(error="Token expired")
        report = limits.format_report()

        assert "Claude Usage Limits" in report
        assert "Error" in report
        assert "Token expired" in report

    def test_format_report_with_opus_usage(self):
        """Test formatting report with Opus-specific usage."""
        limits = ClaudeUsageLimits(
            five_hour=UsageLimitInfo(utilization=10.0),
            seven_day=UsageLimitInfo(utilization=30.0),
            seven_day_opus=UsageLimitInfo(utilization=15.0),
        )
        report = limits.format_report()
        assert "7-Day Opus" in report
        assert "15%" in report

    def test_format_report_hides_zero_opus(self):
        """Test that zero Opus usage is hidden."""
        limits = ClaudeUsageLimits(
            five_hour=UsageLimitInfo(utilization=10.0),
            seven_day=UsageLimitInfo(utilization=30.0),
            seven_day_opus=UsageLimitInfo(utilization=0.0),
        )
        report = limits.format_report()
        assert "7-Day Opus" not in report


class TestParseUsageLimit:
    """Tests for _parse_usage_limit helper function."""

    def test_parse_valid_data(self):
        """Test parsing valid usage limit data."""
        data = {
            "utilization": 45.5,
            "resets_at": "2026-01-24T17:59:59.952570+00:00",
        }
        result = _parse_usage_limit(data)
        assert result is not None
        assert result.utilization == 45.5
        assert result.resets_at is not None

    def test_parse_none_data(self):
        """Test parsing None data."""
        result = _parse_usage_limit(None)
        assert result is None

    def test_parse_missing_utilization(self):
        """Test parsing data without utilization."""
        data = {"resets_at": "2026-01-24T17:59:59+00:00"}
        result = _parse_usage_limit(data)
        assert result is None

    def test_parse_null_resets_at(self):
        """Test parsing data with null resets_at."""
        data = {"utilization": 0.0, "resets_at": None}
        result = _parse_usage_limit(data)
        assert result is not None
        assert result.utilization == 0.0
        assert result.resets_at is None


class TestFetchClaudeUsageLimits:
    """Tests for fetch_claude_usage_limits function."""

    def test_fetch_missing_credentials_file(self, tmp_path):
        """Test handling missing credentials file."""
        result = fetch_claude_usage_limits(str(tmp_path / "nonexistent.json"))
        assert result.error is not None
        assert "not found" in result.error

    def test_fetch_invalid_json(self, tmp_path):
        """Test handling invalid JSON in credentials file."""
        cred_file = tmp_path / "creds.json"
        cred_file.write_text("not valid json")
        result = fetch_claude_usage_limits(str(cred_file))
        assert result.error is not None
        assert "Invalid" in result.error

    def test_fetch_missing_token(self, tmp_path):
        """Test handling missing OAuth token."""
        cred_file = tmp_path / "creds.json"
        cred_file.write_text('{"claudeAiOauth": {}}')
        result = fetch_claude_usage_limits(str(cred_file))
        assert result.error is not None
        assert "access token" in result.error

    def test_fetch_success(self, tmp_path):
        """Test successful fetch with mocked API."""
        cred_file = tmp_path / "creds.json"
        cred_file.write_text('{"claudeAiOauth": {"accessToken": "test-token"}}')

        api_response = {
            "five_hour": {"utilization": 25.0, "resets_at": "2026-01-24T18:00:00+00:00"},
            "seven_day": {"utilization": 60.0, "resets_at": "2026-01-25T15:00:00+00:00"},
        }

        import io
        mock_response = io.BytesIO(json.dumps(api_response).encode())

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = fetch_claude_usage_limits(str(cred_file))

        assert result.error is None
        assert result.five_hour is not None
        assert result.five_hour.utilization == 25.0
        assert result.seven_day is not None
        assert result.seven_day.utilization == 60.0

    def test_fetch_api_error(self, tmp_path):
        """Test handling API error."""
        import urllib.error

        cred_file = tmp_path / "creds.json"
        cred_file.write_text('{"claudeAiOauth": {"accessToken": "test-token"}}')

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 401, "Unauthorized", {}, None
            )
            result = fetch_claude_usage_limits(str(cred_file))

        assert result.error is not None
        assert "expired" in result.error or "invalid" in result.error
        # Should include guidance on how to fix
        assert "claude" in result.error.lower()
