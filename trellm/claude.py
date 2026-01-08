"""Claude Code subprocess runner for TreLLM."""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import ClaudeConfig
from .trello import TrelloCard

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    """Result from a Claude Code execution."""

    success: bool
    session_id: Optional[str]
    summary: str
    output: str


class ClaudeRunner:
    """Runs Claude Code as a subprocess."""

    def __init__(self, config: ClaudeConfig, verbose: bool = False):
        self.binary = config.binary
        self.timeout = config.timeout
        self.yolo = config.yolo
        self.verbose = verbose

    async def run(
        self,
        card: TrelloCard,
        project: str,
        session_id: Optional[str],
        working_dir: Optional[str],
    ) -> ClaudeResult:
        """Run Claude Code as a subprocess with the given task.

        Args:
            card: The Trello card with task details
            project: Project name (for logging)
            session_id: Optional session ID to resume
            working_dir: Working directory for Claude Code

        Returns:
            ClaudeResult with success status, new session ID, and output
        """
        # Build the prompt
        prompt = self._build_prompt(card)

        # Build command
        cmd = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
        ]

        if self.yolo:
            cmd.append("--dangerously-skip-permissions")

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(
            "Running Claude Code for project %s (session: %s)",
            project,
            session_id or "new",
        )

        # Determine working directory
        cwd = Path(working_dir).expanduser() if working_dir else None

        # Run subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            if self.verbose:
                # Stream output to terminal while also capturing it
                stdout_lines: list[str] = []
                stderr_lines: list[str] = []

                async def read_stream(
                    stream: asyncio.StreamReader, lines: list[str], print_output: bool
                ) -> None:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode()
                        lines.append(decoded)
                        if print_output:
                            print(decoded, end="", flush=True)

                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(proc.stdout, stdout_lines, True),  # type: ignore[arg-type]
                        read_stream(proc.stderr, stderr_lines, True),  # type: ignore[arg-type]
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

        if proc.returncode != 0:
            logger.error("Claude Code failed with return code %d", proc.returncode)
            logger.error("stderr: %s", stderr_output)
            raise RuntimeError(f"Claude Code failed: {stderr_output}")

        # Parse JSON output
        return self._parse_output(output)

    def _build_prompt(self, card: TrelloCard) -> str:
        """Build the prompt for Claude Code."""
        return f"""Work on Trello card {card.id}: {card.name}

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
- When done, add a comment starting with "Claude:" summarizing what was done"""

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
