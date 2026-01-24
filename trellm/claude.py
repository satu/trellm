"""Claude Code subprocess runner for TreLLM."""

import asyncio
import json
import logging
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ClaudeConfig
from .trello import TrelloCard

logger = logging.getLogger(__name__)

# Claude Code projects directory
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Default credentials path for Claude Code OAuth
DEFAULT_CREDENTIALS_PATH = "~/.claude/.credentials.json"

# Anthropic OAuth usage API endpoint
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"

# Error patterns for Claude Code
# Detailed pattern with token counts (e.g., "prompt is too long: 206453 tokens > 200000 maximum")
PROMPT_TOO_LONG_DETAILED_PATTERN = re.compile(r"prompt is too long: (\d+) tokens? > (\d+) maximum")
# Simple pattern for when Claude just says "Prompt is too long"
PROMPT_TOO_LONG_SIMPLE_PATTERN = re.compile(r"prompt is too long", re.IGNORECASE)
# API error pattern for rate limits
RATE_LIMIT_PATTERN = re.compile(r"rate_limit_error")
# User-facing rate limit pattern (e.g., "You've hit your limit")
RATE_LIMIT_USER_PATTERN = re.compile(r"you've hit your limit", re.IGNORECASE)
# Reset time as duration (e.g., "resets in 2 hours", "resets in 30 minutes")
RATE_LIMIT_RESET_DURATION_PATTERN = re.compile(r"resets?\s+(?:in\s+)?(\d+)\s*(hours?|minutes?|h|m|days?|d)", re.IGNORECASE)
# Reset time as clock time (e.g., "resets 8pm (UTC)", "resets 10am")
RATE_LIMIT_RESET_TIME_PATTERN = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(?(UTC|GMT)?\)?)?", re.IGNORECASE)


@dataclass
class UsageLimitInfo:
    """Usage limit information for a specific time window."""

    utilization: float  # Percentage used (0-100)
    resets_at: Optional[datetime] = None  # When the limit resets

    def format_reset_time(self) -> str:
        """Format reset time as human-readable date/time string."""
        if not self.resets_at:
            return "N/A"
        now = datetime.now(timezone.utc)
        if self.resets_at <= now:
            return "now"
        # Format as "Jan 24, 2026 5:59 PM UTC"
        return self.resets_at.strftime("%b %d, %Y %-I:%M %p UTC")


@dataclass
class ClaudeUsageLimits:
    """Claude Code usage limits (from Anthropic OAuth API)."""

    five_hour: Optional[UsageLimitInfo] = None
    seven_day: Optional[UsageLimitInfo] = None
    seven_day_opus: Optional[UsageLimitInfo] = None
    seven_day_sonnet: Optional[UsageLimitInfo] = None
    error: Optional[str] = None  # Error message if fetch failed

    def format_report(self) -> str:
        """Format usage limits as a human-readable report section."""
        lines = ["### Claude Usage Limits (Real-Time)"]

        if self.error:
            lines.append(f"- **Error:** {self.error}")
            return "\n".join(lines)

        if self.five_hour:
            lines.append(
                f"- **5-Hour Session:** {self.five_hour.utilization:.0f}% used "
                f"(resets at {self.five_hour.format_reset_time()})"
            )

        if self.seven_day:
            lines.append(
                f"- **7-Day Weekly:** {self.seven_day.utilization:.0f}% used "
                f"(resets at {self.seven_day.format_reset_time()})"
            )

        if self.seven_day_opus and self.seven_day_opus.utilization > 0:
            lines.append(
                f"- **7-Day Opus:** {self.seven_day_opus.utilization:.0f}% used "
                f"(resets at {self.seven_day_opus.format_reset_time()})"
            )

        if self.seven_day_sonnet and self.seven_day_sonnet.utilization > 0:
            lines.append(
                f"- **7-Day Sonnet:** {self.seven_day_sonnet.utilization:.0f}% used "
                f"(resets at {self.seven_day_sonnet.format_reset_time()})"
            )

        return "\n".join(lines)


def _parse_usage_limit(data: Optional[dict]) -> Optional[UsageLimitInfo]:
    """Parse usage limit data from API response."""
    if not data:
        return None
    utilization = data.get("utilization")
    if utilization is None:
        return None
    resets_at = None
    if data.get("resets_at"):
        try:
            # Parse ISO format datetime
            resets_str = data["resets_at"]
            # Handle timezone offset format
            resets_at = datetime.fromisoformat(resets_str.replace("+00:00", "+00:00"))
        except (ValueError, TypeError):
            pass
    return UsageLimitInfo(utilization=float(utilization), resets_at=resets_at)


def fetch_claude_usage_limits(
    credentials_path: Optional[str] = None,
) -> ClaudeUsageLimits:
    """Fetch current Claude usage limits from Anthropic OAuth API.

    Args:
        credentials_path: Path to Claude credentials file.
            Defaults to ~/.claude/.credentials.json

    Returns:
        ClaudeUsageLimits with current usage data or error message.
    """
    cred_path = Path(credentials_path or DEFAULT_CREDENTIALS_PATH).expanduser()

    # Read credentials
    try:
        with open(cred_path) as f:
            creds = json.load(f)
    except FileNotFoundError:
        return ClaudeUsageLimits(error="Credentials file not found")
    except json.JSONDecodeError:
        return ClaudeUsageLimits(error="Invalid credentials file")

    # Extract access token
    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        return ClaudeUsageLimits(error="No OAuth access token found")

    # Make API request
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "claude-code/2.0.76",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    }

    req = urllib.request.Request(USAGE_API_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return ClaudeUsageLimits(error="OAuth token expired or invalid")
        return ClaudeUsageLimits(error=f"API error: {e.code}")
    except urllib.error.URLError as e:
        return ClaudeUsageLimits(error=f"Network error: {e.reason}")
    except Exception as e:
        return ClaudeUsageLimits(error=f"Failed to fetch usage: {e}")

    # Parse response
    return ClaudeUsageLimits(
        five_hour=_parse_usage_limit(data.get("five_hour")),
        seven_day=_parse_usage_limit(data.get("seven_day")),
        seven_day_opus=_parse_usage_limit(data.get("seven_day_opus")),
        seven_day_sonnet=_parse_usage_limit(data.get("seven_day_sonnet")),
    )


def _get_session_jsonl_path(session_id: str, working_dir: Optional[str]) -> Optional[Path]:
    """Get the path to the JSONL file for a session.

    Claude Code stores session data in ~/.claude/projects/<project-dir>/<session-id>.jsonl
    where <project-dir> is the working directory with slashes replaced by dashes.

    Args:
        session_id: The session ID (UUID format)
        working_dir: The working directory for the project

    Returns:
        Path to the JSONL file if found, None otherwise
    """
    if not working_dir:
        return None

    # Convert working directory to Claude's project directory format
    # e.g., /home/user/src/myproject -> -home-user-src-myproject
    abs_path = Path(working_dir).expanduser().resolve()
    project_dir_name = str(abs_path).replace("/", "-")

    jsonl_path = CLAUDE_PROJECTS_DIR / project_dir_name / f"{session_id}.jsonl"
    if jsonl_path.exists():
        return jsonl_path

    return None


def _read_token_usage_from_jsonl(jsonl_path: Path) -> dict:
    """Read and aggregate token usage from a session JSONL file.

    Claude Code's /cost command doesn't properly report token usage in its
    JSON output (returns 0 for all token fields). This function reads the
    actual usage data from the session's JSONL file.

    Args:
        jsonl_path: Path to the session JSONL file

    Returns:
        Dictionary with aggregated token counts:
        - input_tokens: Total input tokens
        - output_tokens: Total output tokens
        - cache_creation_input_tokens: Total cache creation tokens
        - cache_read_input_tokens: Total cache read tokens
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    usage = data.get("message", {}).get("usage", {})
                    if usage:
                        totals["input_tokens"] += usage.get("input_tokens", 0)
                        totals["output_tokens"] += usage.get("output_tokens", 0)
                        totals["cache_creation_input_tokens"] += usage.get(
                            "cache_creation_input_tokens", 0
                        )
                        totals["cache_read_input_tokens"] += usage.get(
                            "cache_read_input_tokens", 0
                        )
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError) as e:
        logger.debug("Could not read JSONL file %s: %s", jsonl_path, e)

    return totals


@dataclass
class CostInfo:
    """Cost and usage information from Claude Code /cost command."""

    total_cost: Optional[str] = None
    api_duration: Optional[str] = None
    wall_duration: Optional[str] = None
    code_changes: Optional[str] = None
    raw_output: Optional[str] = None
    # Token usage statistics
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ClaudeResult:
    """Result from a Claude Code execution."""

    success: bool
    session_id: Optional[str]
    summary: str
    output: str
    cost_info: Optional[CostInfo] = None


class PromptTooLongError(Exception):
    """Raised when Claude Code reports prompt is too long."""

    def __init__(
        self,
        message: str,
        tokens: Optional[int] = None,
        maximum: Optional[int] = None,
        session_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.tokens = tokens
        self.maximum = maximum
        self.session_id = session_id  # The session that hit the limit


class RateLimitError(Exception):
    """Raised when Claude Code hits rate limit."""

    def __init__(self, message: str, reset_seconds: Optional[int] = None):
        super().__init__(message)
        self.reset_seconds = reset_seconds


class ClaudeRunner:
    """Runs Claude Code as a subprocess."""

    # Maximum retries for recoverable errors
    MAX_RETRIES = 2

    def __init__(
        self,
        config: ClaudeConfig,
        verbose: bool = False,
        ready_list_id: Optional[str] = None,
    ):
        self.binary = config.binary
        self.timeout = config.timeout
        self.yolo = config.yolo
        self.verbose = verbose
        self.ready_list_id = ready_list_id

    def _check_for_errors(self, stderr: str, stdout: str, session_id: Optional[str] = None) -> None:
        """Check output for known Claude Code errors.

        Args:
            stderr: Standard error output
            stdout: Standard output
            session_id: Session ID from the output (for error context)

        Raises:
            PromptTooLongError: If prompt exceeds token limit
            RateLimitError: If rate limit is hit
        """
        combined = stderr + stdout

        # Check for prompt too long - try detailed pattern first for token counts
        match = PROMPT_TOO_LONG_DETAILED_PATTERN.search(combined)
        if match:
            tokens = int(match.group(1))
            maximum = int(match.group(2))
            raise PromptTooLongError(
                f"Prompt too long: {tokens} tokens > {maximum} maximum",
                tokens=tokens,
                maximum=maximum,
                session_id=session_id,
            )

        # Fall back to simple pattern (no token counts)
        if PROMPT_TOO_LONG_SIMPLE_PATTERN.search(combined):
            raise PromptTooLongError("Prompt is too long", session_id=session_id)

        # Check for rate limit (API error or user-facing message)
        if RATE_LIMIT_PATTERN.search(combined) or RATE_LIMIT_USER_PATTERN.search(combined):
            # Try to extract reset time
            reset_seconds = self._parse_rate_limit_reset_time(combined)
            raise RateLimitError(
                "Rate limit exceeded",
                reset_seconds=reset_seconds,
            )

    def _parse_rate_limit_reset_time(self, text: str) -> Optional[int]:
        """Parse reset time from rate limit error message.

        Supports two formats:
        - Duration: "resets in 2 hours", "resets in 30 minutes"
        - Clock time: "resets 8pm (UTC)", "resets 10am"

        Args:
            text: The error message text

        Returns:
            Seconds until reset, or None if cannot be parsed
        """
        # Try duration format first (e.g., "resets in 2 hours")
        duration_match = RATE_LIMIT_RESET_DURATION_PATTERN.search(text)
        if duration_match:
            value = int(duration_match.group(1))
            unit = duration_match.group(2).lower()
            if unit.startswith("h"):
                return value * 3600
            elif unit.startswith("m"):
                return value * 60
            elif unit.startswith("d"):
                return value * 86400

        # Try clock time format (e.g., "resets 8pm (UTC)")
        time_match = RATE_LIMIT_RESET_TIME_PATTERN.search(text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2)) if time_match.group(2) else 0
            am_pm = time_match.group(3).lower()

            # Convert to 24-hour format
            if am_pm == "pm" and hour != 12:
                hour += 12
            elif am_pm == "am" and hour == 12:
                hour = 0

            # Calculate seconds until that time (assume UTC)
            now = datetime.now(timezone.utc)
            reset_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If the time is in the past, assume it's tomorrow
            if reset_time <= now:
                reset_time = reset_time.replace(day=reset_time.day + 1)

            delta = reset_time - now
            return max(0, int(delta.total_seconds()))

        return None

    async def _run_compact(
        self,
        session_id: str,
        working_dir: Optional[str],
        prefix: str,
        compact_prompt: Optional[str] = None,
    ) -> Optional[str]:
        """Run /compact command on a session to reduce context size.

        Args:
            session_id: The session ID to compact
            working_dir: Working directory for Claude Code
            prefix: Project prefix for logging
            compact_prompt: Optional custom instructions for compaction

        Returns:
            New session ID if successful, None otherwise
        """
        # Get token counts before compaction
        before_cost = await self._run_cost(
            session_id=session_id,
            working_dir=working_dir,
            prefix=prefix,
        )
        before_tokens = 0
        if before_cost:
            before_tokens = (
                before_cost.input_tokens
                + before_cost.output_tokens
                + before_cost.cache_creation_tokens
            )
            logger.info(
                "%sTokens before compaction: %d (input: %d, output: %d, cache_creation: %d)",
                prefix,
                before_tokens,
                before_cost.input_tokens,
                before_cost.output_tokens,
                before_cost.cache_creation_tokens,
            )

        # Build compact command with optional custom instructions
        compact_cmd = "/compact"
        if compact_prompt:
            compact_cmd = f"/compact {compact_prompt}"
            logger.info("%sRunning /compact with custom prompt: %s", prefix, compact_prompt[:50])
        else:
            logger.info("%sRunning /compact to reduce context size", prefix)

        cmd = [
            self.binary,
            "-p",
            compact_cmd,
            "--resume",
            session_id,
            "--output-format",
            "json",
        ]

        if self.yolo:
            cmd.append("--dangerously-skip-permissions")

        cwd = Path(working_dir).expanduser() if working_dir else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=10 * 1024 * 1024,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=120,  # Compact should be quick
            )

            if proc.returncode != 0:
                logger.warning(
                    "%s/compact failed with return code %d: %s",
                    prefix,
                    proc.returncode,
                    stderr.decode(),
                )
                return None

            # Parse output to get new session ID
            output = stdout.decode()
            new_session_id = None
            for line in reversed(output.strip().split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        if "session_id" in data:
                            new_session_id = data["session_id"]
                            break
                    except json.JSONDecodeError:
                        continue

            if not new_session_id:
                logger.warning("%s/compact completed but no session ID found", prefix)
                return None

            # Get token counts after compaction
            after_cost = await self._run_cost(
                session_id=new_session_id,
                working_dir=working_dir,
                prefix=prefix,
            )
            after_tokens = 0
            if after_cost:
                after_tokens = (
                    after_cost.input_tokens
                    + after_cost.output_tokens
                    + after_cost.cache_creation_tokens
                )

            # Log compaction results with token comparison
            if before_tokens > 0 and after_tokens > 0:
                reduction = before_tokens - after_tokens
                reduction_pct = (reduction / before_tokens) * 100 if before_tokens > 0 else 0
                logger.info(
                    "%s/compact successful: %d -> %d tokens (-%d, %.1f%% reduction), new session: %s",
                    prefix,
                    before_tokens,
                    after_tokens,
                    reduction,
                    reduction_pct,
                    new_session_id,
                )
            elif after_cost:
                logger.info(
                    "%s/compact successful: tokens after: %d (input: %d, output: %d, cache_creation: %d), new session: %s",
                    prefix,
                    after_tokens,
                    after_cost.input_tokens,
                    after_cost.output_tokens,
                    after_cost.cache_creation_tokens,
                    new_session_id,
                )
            else:
                logger.info(
                    "%s/compact successful, new session: %s",
                    prefix,
                    new_session_id,
                )

            return new_session_id

        except asyncio.TimeoutError:
            logger.warning("%s/compact timed out", prefix)
            return None
        except Exception as e:
            logger.warning("%s/compact failed: %s", prefix, e)
            return None

    @staticmethod
    def _format_duration_ms(ms: int) -> str:
        """Format milliseconds into a human-readable duration string."""
        if ms < 1000:
            return f"{ms}ms"
        seconds = ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = seconds / 60
        if minutes < 60:
            secs = seconds % 60
            return f"{int(minutes)}m {secs:.1f}s"
        hours = minutes / 60
        mins = int(minutes % 60)
        return f"{int(hours)}h {mins}m"

    async def _run_cost(
        self,
        session_id: str,
        working_dir: Optional[str],
        prefix: str,
    ) -> Optional[CostInfo]:
        """Run /cost command on a session to get usage statistics.

        Args:
            session_id: The session ID to get cost for
            working_dir: Working directory for Claude Code
            prefix: Project prefix for logging

        Returns:
            CostInfo with usage statistics, or None if failed
        """
        cmd = [
            self.binary,
            "-p",
            "/cost",
            "--resume",
            session_id,
            "--output-format",
            "json",
        ]

        if self.yolo:
            cmd.append("--dangerously-skip-permissions")

        cwd = Path(working_dir).expanduser() if working_dir else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=10 * 1024 * 1024,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=30,  # Cost check should be very quick
            )

            output = stdout.decode()

            # Parse the result to extract cost info
            # The JSON output has fields directly: total_cost_usd, duration_ms, duration_api_ms
            cost_info = CostInfo(raw_output=output)

            for line in output.strip().split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        # Extract cost directly from JSON fields
                        if "total_cost_usd" in data:
                            cost_usd = data["total_cost_usd"]
                            cost_info.total_cost = f"${cost_usd:.4f}"
                        if "duration_api_ms" in data:
                            api_ms = data["duration_api_ms"]
                            cost_info.api_duration = self._format_duration_ms(api_ms)
                        if "duration_ms" in data:
                            wall_ms = data["duration_ms"]
                            cost_info.wall_duration = self._format_duration_ms(wall_ms)
                        # Note: code_changes is not available in /cost JSON output
                        break  # Found our JSON line, no need to continue
                    except json.JSONDecodeError:
                        continue

            # Read token usage from JSONL file instead of /cost output
            # Claude Code's /cost command returns 0 for all token fields,
            # but the actual usage data is in the session's JSONL file
            jsonl_path = _get_session_jsonl_path(session_id, working_dir)
            if jsonl_path:
                usage = _read_token_usage_from_jsonl(jsonl_path)
                cost_info.input_tokens = usage["input_tokens"]
                cost_info.output_tokens = usage["output_tokens"]
                cost_info.cache_creation_tokens = usage["cache_creation_input_tokens"]
                cost_info.cache_read_tokens = usage["cache_read_input_tokens"]

            return cost_info

        except asyncio.TimeoutError:
            logger.warning("%s/cost timed out", prefix)
            return None
        except Exception as e:
            logger.warning("%s/cost failed: %s", prefix, e)
            return None

    async def run(
        self,
        card: TrelloCard,
        project: str,
        session_id: Optional[str],
        working_dir: Optional[str],
        last_card_id: Optional[str] = None,
        compact_prompt: Optional[str] = None,
    ) -> ClaudeResult:
        """Run Claude Code as a subprocess with the given task.

        Handles recoverable errors:
        - Prompt too long: Runs /compact and retries
        - Rate limit: Sleeps until reset time and retries

        Pre-compacts the session if:
        - There is an existing session
        - This is a different card than the last one processed

        Args:
            card: The Trello card with task details
            project: Project name (for logging)
            session_id: Optional session ID to resume
            working_dir: Working directory for Claude Code
            last_card_id: Optional card ID of the last processed card for this project
            compact_prompt: Optional custom instructions for /compact

        Returns:
            ClaudeResult with success status, new session ID, and output
        """
        # Capture project prefix as local variable to avoid race conditions
        # when multiple tasks run in parallel with different projects
        prefix = f"[{project}] " if project else ""

        current_session_id = session_id

        # Pre-compact if we have an existing session and this is a new card
        if current_session_id and (last_card_id is None or card.id != last_card_id):
            logger.info(
                "%sPre-compacting session before new ticket (last card: %s, new card: %s)",
                prefix,
                last_card_id or "none",
                card.id,
            )
            new_session_id = await self._run_compact(
                session_id=current_session_id,
                working_dir=working_dir,
                prefix=prefix,
                compact_prompt=compact_prompt,
            )
            if new_session_id:
                current_session_id = new_session_id
                logger.info("%sUsing compacted session for new ticket", prefix)
            else:
                logger.warning(
                    "%sPre-compaction failed, continuing with original session", prefix
                )

        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                result = await self._run_once(
                    card=card,
                    project=project,
                    session_id=current_session_id,
                    working_dir=working_dir,
                    prefix=prefix,
                )

                # Get cost info after successful execution
                if result.session_id:
                    cost_info = await self._run_cost(
                        session_id=result.session_id,
                        working_dir=working_dir,
                        prefix=prefix,
                    )
                    result.cost_info = cost_info
                    if cost_info:
                        logger.info(
                            "%sSession cost: %s | API duration: %s | Wall duration: %s",
                            prefix,
                            cost_info.total_cost or "N/A",
                            cost_info.api_duration or "N/A",
                            cost_info.wall_duration or "N/A",
                        )

                return result
            except PromptTooLongError as e:
                last_error = e
                if attempt >= self.MAX_RETRIES:
                    logger.error(
                        "%sPrompt too long after %d retries, giving up",
                        prefix,
                        self.MAX_RETRIES,
                    )
                    raise RuntimeError(f"Prompt too long: {e}") from e

                # Use the session_id from the error if available, otherwise use current
                session_to_compact = e.session_id or current_session_id

                # Need a session to compact
                if not session_to_compact:
                    logger.error(
                        "%sPrompt too long but no session to compact", prefix
                    )
                    raise RuntimeError(f"Prompt too long: {e}") from e

                if e.tokens and e.maximum:
                    logger.warning(
                        "%sPrompt too long (%d tokens > %d max), running /compact on session %s",
                        prefix,
                        e.tokens,
                        e.maximum,
                        session_to_compact,
                    )
                else:
                    logger.warning(
                        "%sPrompt too long, running /compact on session %s",
                        prefix,
                        session_to_compact,
                    )

                # Run /compact to reduce context on the session that hit the limit
                new_session_id = await self._run_compact(
                    session_id=session_to_compact,
                    working_dir=working_dir,
                    prefix=prefix,
                    compact_prompt=compact_prompt,
                )

                if new_session_id:
                    current_session_id = new_session_id
                    logger.info("%sRetrying with compacted session", prefix)
                else:
                    logger.error("%s/compact failed, cannot retry", prefix)
                    raise RuntimeError(f"Prompt too long and compact failed: {e}") from e

            except RateLimitError as e:
                last_error = e
                if attempt >= self.MAX_RETRIES:
                    logger.error(
                        "%sRate limit after %d retries, giving up",
                        prefix,
                        self.MAX_RETRIES,
                    )
                    raise RuntimeError(f"Rate limit exceeded: {e}") from e

                # Calculate sleep time
                if e.reset_seconds:
                    sleep_time = e.reset_seconds
                    logger.warning(
                        "%sRate limit hit, sleeping for %d seconds until reset",
                        prefix,
                        sleep_time,
                    )
                else:
                    # Default to 5 minutes if reset time unknown
                    sleep_time = 300
                    logger.warning(
                        "%sRate limit hit (no reset time), sleeping for %d seconds",
                        prefix,
                        sleep_time,
                    )

                await asyncio.sleep(sleep_time)
                logger.info("%sRetrying after rate limit sleep", prefix)

        # Should not reach here, but just in case
        if last_error:
            raise RuntimeError(f"Claude Code failed after retries: {last_error}")
        raise RuntimeError("Claude Code failed after retries")

    async def _run_once(
        self,
        card: TrelloCard,
        project: str,
        session_id: Optional[str],
        working_dir: Optional[str],
        prefix: str,
    ) -> ClaudeResult:
        """Run Claude Code once without retry logic.

        Args:
            card: The Trello card with task details
            project: Project name (for logging)
            session_id: Optional session ID to resume
            working_dir: Working directory for Claude Code
            prefix: Project prefix for logging

        Returns:
            ClaudeResult with success status, new session ID, and output

        Raises:
            PromptTooLongError: If prompt exceeds token limit
            RateLimitError: If rate limit is hit
            RuntimeError: For other errors
        """
        # Build the prompt
        prompt = self._build_prompt(card)

        # Build command
        cmd = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json" if self.verbose else "json",
        ]

        if self.verbose:
            cmd.append("--verbose")

        if self.yolo:
            cmd.append("--dangerously-skip-permissions")

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(
            "Running Claude Code for project %s (session: %s)",
            project,
            session_id or "new",
        )

        # In verbose mode, print the prompt being sent
        if self.verbose:
            print(f"\n{prefix}" + "=" * 60, flush=True)
            print(f"{prefix}[Prompt]", flush=True)
            print(f"{prefix}" + "-" * 60, flush=True)
            self._print_prefixed(prompt, prefix)
            print(f"{prefix}" + "=" * 60 + "\n", flush=True)

        # Determine working directory
        cwd = Path(working_dir).expanduser() if working_dir else None

        # Run subprocess
        # Use a larger buffer limit (10MB) to handle long JSON lines from Claude
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,  # 10MB buffer for long lines
        )

        try:
            if self.verbose:
                # Stream output to terminal while also capturing it
                # In verbose mode, we use stream-json and extract human-readable content
                stdout_lines: list[str] = []
                stderr_lines: list[str] = []

                async def read_stdout_stream(stream: asyncio.StreamReader) -> None:
                    """Read stdout and print human-readable content from JSON."""
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode()
                        stdout_lines.append(decoded)
                        # Parse JSON and extract human-readable content
                        # Pass prefix to avoid race condition with parallel tasks
                        self._print_stream_json_line(decoded, prefix)

                async def read_stderr_stream(stream: asyncio.StreamReader) -> None:
                    """Read stderr and print it with project prefix."""
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode()
                        stderr_lines.append(decoded)
                        # Prefix stderr output (uses captured prefix from closure)
                        print(f"{prefix}{decoded}", end="", flush=True)

                await asyncio.wait_for(
                    asyncio.gather(
                        read_stdout_stream(proc.stdout),  # type: ignore[arg-type]
                        read_stderr_stream(proc.stderr),  # type: ignore[arg-type]
                    ),
                    timeout=self.timeout,
                )
                await proc.wait()
                output = "".join(stdout_lines)
                stderr_output = "".join(stderr_lines)
            else:
                # Quiet mode - just capture output
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout,
                )
                output = stdout.decode()
                stderr_output = stderr.decode()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Claude Code timed out after {self.timeout}s")

        # Check for recoverable errors before checking return code
        # (errors may appear in stderr even with non-zero exit)
        if proc.returncode != 0:
            # Try to extract session_id from output even on failure
            # This is critical for /compact to work on the correct session
            failed_session_id = None
            for line in reversed(output.strip().split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        if "session_id" in data:
                            failed_session_id = data["session_id"]
                            break
                    except json.JSONDecodeError:
                        continue

            # Check for known recoverable errors, passing the session_id
            self._check_for_errors(stderr_output, output, session_id=failed_session_id)
            # If not a known error, raise generic error
            logger.error("Claude Code failed with return code %d", proc.returncode)
            logger.error("stderr: %s", stderr_output)
            raise RuntimeError(f"Claude Code failed: {stderr_output}")

        # Parse JSON output
        return self._parse_output(output)

    def _print_prefixed(self, text: str, prefix: str, end: str = "\n") -> None:
        """Print text with project prefix.

        Args:
            text: The text to print
            prefix: The project prefix (e.g., "[myproject] ")
            end: Line ending character
        """
        # Prefix each line for multi-line output
        if "\n" in text and end == "\n":
            lines = text.split("\n")
            for line in lines:
                print(f"{prefix}{line}", flush=True)
        else:
            print(f"{prefix}{text}", end=end, flush=True)

    def _print_stream_json_line(self, line: str, prefix: str) -> None:
        """Parse a stream-json line and print human-readable content.

        Args:
            line: The JSON line from stream-json output
            prefix: The project prefix (e.g., "[myproject] ")
        """
        line = line.strip()
        if not line or not line.startswith("{"):
            return

        try:
            data = json.loads(line)
            msg_type = data.get("type")

            if msg_type == "assistant":
                # Extract content from assistant messages
                message = data.get("message", {})
                content = message.get("content", [])
                for item in content:
                    item_type = item.get("type")
                    if item_type == "thinking":
                        # Show Claude's thinking/reasoning
                        thinking = item.get("thinking", "")
                        if thinking:
                            # Show first 500 chars of thinking
                            preview = thinking[:500]
                            if len(thinking) > 500:
                                preview += "..."
                            print(f"\n{prefix}[Thinking] {preview}", flush=True)
                    elif item_type == "text":
                        text = item.get("text", "")
                        if text:
                            print(f"\n{prefix}[Claude] {text}", flush=True)
                    elif item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        # Show tool name and brief input summary
                        if tool_name == "Edit":
                            file_path = tool_input.get("file_path", "")
                            print(f"\n{prefix}[Tool: {tool_name}] {file_path}", flush=True)
                        elif tool_name == "Read":
                            file_path = tool_input.get("file_path", "")
                            print(f"\n{prefix}[Tool: {tool_name}] {file_path}", flush=True)
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")[:80]
                            print(f"\n{prefix}[Tool: {tool_name}] {cmd}", flush=True)
                        elif tool_name == "Grep":
                            pattern = tool_input.get("pattern", "")
                            print(f"\n{prefix}[Tool: {tool_name}] {pattern}", flush=True)
                        else:
                            print(f"\n{prefix}[Tool: {tool_name}]", flush=True)

            elif msg_type == "user":
                # Tool results or user messages
                content = data.get("message", {}).get("content", [])
                for item in content:
                    if item.get("type") == "tool_result":
                        is_error = item.get("is_error", False)
                        status = "error" if is_error else "done"
                        print(f"{prefix}  [{status}]", flush=True)

            elif msg_type == "result":
                # Final result
                result = data.get("result", "")
                if result:
                    print(f"\n{prefix}" + "=" * 60, flush=True)
                    print(f"{prefix}[Result]", flush=True)
                    print(f"{prefix}" + "-" * 60, flush=True)
                    self._print_prefixed(result, prefix)
                    print(f"{prefix}" + "=" * 60, flush=True)

        except json.JSONDecodeError:
            pass

    def _build_prompt(self, card: TrelloCard) -> str:
        """Build the prompt for Claude Code."""
        # Build the move instruction based on ready_list_id
        if self.ready_list_id:
            move_instruction = f"- Move the card to list ID {self.ready_list_id} when done"
        else:
            move_instruction = "- Move the card to the READY TO TRY list when done"

        return f"""Work on Trello card {card.id}

Card URL: {card.url}

When done, commit your changes and provide a brief summary.

Important guidelines:
- Fetch the card details from Trello to get the full description and requirements
- Check ALL comments on the card - if there are comments after your last "Claude:" comment, those contain feedback you need to address (the card was moved back to TODO)
- As soon as you start working, add a comment starting with "Claude:" acknowledging you've started
- Read and understand existing code before making changes
- Write clean, maintainable code following the project's style
- Add tests when appropriate
- Commit with a clear, descriptive message
- Push your changes to the remote repository
- When done, add a comment starting with "Claude:" summarizing what was done
- IMPORTANT: Whenever you mention a commit hash/SHA in comments or summaries, always include a clickable GitHub link to the commit (e.g., https://github.com/owner/repo/commit/<sha>). Use `git remote get-url origin` to determine the repository URL if needed.
{move_instruction}

Voice note handling:
- Check if the card has audio file attachments (voice notes, typically .opus, .ogg, .m4a, .mp3, .wav files)
- If voice notes exist, check comments to see if they've already been transcribed (look for "Transcribed: [filename]" in comments)
- For any new/untranscribed voice notes: download the file, transcribe it, and add a comment with the transcription like "Claude: Transcribed: [filename]\\n[transcription content]"
- If this is a new card with a voice note and minimal description, update the card name and description based on your understanding of the transcribed voice note. IMPORTANT: Always preserve the first word of the existing card name (this is the project name)
- Process the transcribed instructions along with any other card content"""

    def _parse_output(self, output: str) -> ClaudeResult:
        """Parse Claude Code's JSON output.

        Claude Code outputs multiple JSON objects (one per message).
        We look for the final result containing the session_id.
        """
        session_id = None
        summary = "Task completed"

        # Try to parse each line as JSON
        for line in reversed(output.strip().split("\n")):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue

            try:
                data = json.loads(line)
                if "session_id" in data:
                    session_id = data["session_id"]
                if "result" in data:
                    summary = data["result"]
                if session_id:
                    break
            except json.JSONDecodeError:
                continue

        return ClaudeResult(
            success=True,
            session_id=session_id,
            summary=summary,
            output=output,
        )
