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
            print("\n" + "=" * 60, flush=True)
            print("[Prompt]", flush=True)
            print("-" * 60, flush=True)
            print(prompt, flush=True)
            print("=" * 60 + "\n", flush=True)

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
                        self._print_stream_json_line(decoded)

                async def read_stderr_stream(stream: asyncio.StreamReader) -> None:
                    """Read stderr and print it directly."""
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode()
                        stderr_lines.append(decoded)
                        print(decoded, end="", flush=True)

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

        if proc.returncode != 0:
            logger.error("Claude Code failed with return code %d", proc.returncode)
            logger.error("stderr: %s", stderr_output)
            raise RuntimeError(f"Claude Code failed: {stderr_output}")

        # Parse JSON output
        return self._parse_output(output)

    def _print_stream_json_line(self, line: str) -> None:
        """Parse a stream-json line and print human-readable content."""
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
                            print(f"\n[Thinking] {preview}", flush=True)
                    elif item_type == "text":
                        text = item.get("text", "")
                        if text:
                            print(f"\n[Claude] {text}", flush=True)
                    elif item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        # Show tool name and brief input summary
                        if tool_name == "Edit":
                            file_path = tool_input.get("file_path", "")
                            print(f"\n[Tool: {tool_name}] {file_path}", flush=True)
                        elif tool_name == "Read":
                            file_path = tool_input.get("file_path", "")
                            print(f"\n[Tool: {tool_name}] {file_path}", flush=True)
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")[:80]
                            print(f"\n[Tool: {tool_name}] {cmd}", flush=True)
                        elif tool_name == "Grep":
                            pattern = tool_input.get("pattern", "")
                            print(f"\n[Tool: {tool_name}] {pattern}", flush=True)
                        else:
                            print(f"\n[Tool: {tool_name}]", flush=True)

            elif msg_type == "user":
                # Tool results or user messages
                content = data.get("message", {}).get("content", [])
                for item in content:
                    if item.get("type") == "tool_result":
                        is_error = item.get("is_error", False)
                        status = "error" if is_error else "done"
                        print(f"  [{status}]", flush=True)

            elif msg_type == "result":
                # Final result
                result = data.get("result", "")
                if result:
                    print("\n" + "=" * 60, flush=True)
                    print("[Result]", flush=True)
                    print("-" * 60, flush=True)
                    print(result, flush=True)
                    print("=" * 60, flush=True)

        except json.JSONDecodeError:
            pass

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
