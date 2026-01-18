"""Claude Code subprocess runner for TreLLM."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import ClaudeConfig
from .trello import TrelloCard

logger = logging.getLogger(__name__)

# Error patterns for Claude Code
# Detailed pattern with token counts (e.g., "prompt is too long: 206453 tokens > 200000 maximum")
PROMPT_TOO_LONG_DETAILED_PATTERN = re.compile(r"prompt is too long: (\d+) tokens? > (\d+) maximum")
# Simple pattern for when Claude just says "Prompt is too long"
PROMPT_TOO_LONG_SIMPLE_PATTERN = re.compile(r"prompt is too long", re.IGNORECASE)
RATE_LIMIT_PATTERN = re.compile(r"rate_limit_error")
RATE_LIMIT_RESET_PATTERN = re.compile(r"resets?\s+(?:in\s+)?(\d+)\s*(hours?|minutes?|h|m|days?|d)", re.IGNORECASE)


@dataclass
class ClaudeResult:
    """Result from a Claude Code execution."""

    success: bool
    session_id: Optional[str]
    summary: str
    output: str


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

        # Check for rate limit
        if RATE_LIMIT_PATTERN.search(combined):
            # Try to extract reset time
            reset_seconds = None
            reset_match = RATE_LIMIT_RESET_PATTERN.search(combined)
            if reset_match:
                value = int(reset_match.group(1))
                unit = reset_match.group(2).lower()
                if unit.startswith("h"):
                    reset_seconds = value * 3600
                elif unit.startswith("m"):
                    reset_seconds = value * 60
                elif unit.startswith("d"):
                    reset_seconds = value * 86400
            raise RateLimitError(
                "Rate limit exceeded",
                reset_seconds=reset_seconds,
            )

    async def _run_compact(
        self,
        session_id: str,
        working_dir: Optional[str],
        prefix: str,
    ) -> Optional[str]:
        """Run /compact command on a session to reduce context size.

        Args:
            session_id: The session ID to compact
            working_dir: Working directory for Claude Code
            prefix: Project prefix for logging

        Returns:
            New session ID if successful, None otherwise
        """
        logger.info("%sRunning /compact to reduce context size", prefix)

        cmd = [
            self.binary,
            "-p",
            "/compact",
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
            for line in reversed(output.strip().split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        if "session_id" in data:
                            logger.info(
                                "%s/compact successful, new session: %s",
                                prefix,
                                data["session_id"],
                            )
                            return data["session_id"]
                    except json.JSONDecodeError:
                        continue

            logger.warning("%s/compact completed but no session ID found", prefix)
            return None

        except asyncio.TimeoutError:
            logger.warning("%s/compact timed out", prefix)
            return None
        except Exception as e:
            logger.warning("%s/compact failed: %s", prefix, e)
            return None

    async def run(
        self,
        card: TrelloCard,
        project: str,
        session_id: Optional[str],
        working_dir: Optional[str],
    ) -> ClaudeResult:
        """Run Claude Code as a subprocess with the given task.

        Handles recoverable errors:
        - Prompt too long: Runs /compact and retries
        - Rate limit: Sleeps until reset time and retries

        Args:
            card: The Trello card with task details
            project: Project name (for logging)
            session_id: Optional session ID to resume
            working_dir: Working directory for Claude Code

        Returns:
            ClaudeResult with success status, new session ID, and output
        """
        # Capture project prefix as local variable to avoid race conditions
        # when multiple tasks run in parallel with different projects
        prefix = f"[{project}] " if project else ""

        current_session_id = session_id
        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return await self._run_once(
                    card=card,
                    project=project,
                    session_id=current_session_id,
                    working_dir=working_dir,
                    prefix=prefix,
                )
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
