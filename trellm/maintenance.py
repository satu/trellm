"""Maintenance skill for TreLLM - automatic context maintenance every N tickets."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ClaudeConfig, MaintenanceConfig, TrelloConfig
from .trello import TrelloClient

logger = logging.getLogger(__name__)


@dataclass
class MaintenanceResult:
    """Result from a maintenance run."""

    success: bool
    summary: str
    session_id: Optional[str] = None


def build_maintenance_prompt(
    project: str,
    ticket_count: int,
    last_maintenance: Optional[str],
    interval: int,
) -> str:
    """Build the prompt for the maintenance skill.

    Args:
        project: Project name
        ticket_count: Current ticket count for this project
        last_maintenance: ISO timestamp of last maintenance run (or None)
        interval: Maintenance interval (tickets between runs)

    Returns:
        The maintenance prompt string
    """
    last_maint_str = last_maintenance or "never"
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return f"""You are performing maintenance on the {project} project.

Recent ticket count: {ticket_count}
Last maintenance: {last_maint_str}
Maintenance interval: every {interval} tickets

Please perform the following maintenance tasks and output your findings as a summary. DO NOT create any files.

## 1. CLAUDE.md Review
- Check if CLAUDE.md exists in the project directory
- If it exists, review its contents
- Analyze recent work patterns from git history (last {interval} commits)
- Note any updates needed for:
  - New coding conventions discovered
  - Architecture decisions made
  - Test patterns established
  - Common gotchas/pitfalls found

## 2. Compaction Prompt Optimization
- Review the current compact_prompt (if any) in use
- Based on git history and file access patterns, identify:
  - Context that frequently needs to be preserved
  - Patterns that keep getting re-read
- Suggest updates to the compact_prompt configuration

## 3. Documentation Freshness Check
- Scan for outdated README sections
- Check if any API docs exist and if they match implementation
- Flag stale TODOs in code (over 30 days old based on git blame)
- Report any documentation gaps

## Output Format
Your final output MUST be a summary in this exact format (this will be saved to a Trello card):

---
## {project} Maintenance - {current_date}

### Ticket Count: {ticket_count}

### Observations
- [List files frequently accessed in recent work]
- [List patterns established]
- [List decisions made]

### Recommendations

#### CLAUDE.md Updates
- [Specific suggestions for CLAUDE.md changes]

#### Compact Prompt Updates
- [Suggested compact_prompt configuration changes]

#### Documentation
- [Documentation issues found]
- [Stale TODOs found]
---

Be concise. Focus on actionable improvements. DO NOT create or modify any files."""


async def run_maintenance(
    project: str,
    working_dir: str,
    session_id: Optional[str],
    claude_config: ClaudeConfig,
    maintenance_config: MaintenanceConfig,
    ticket_count: int,
    last_maintenance: Optional[str],
    trello_client: Optional[TrelloClient] = None,
    icebox_list_id: Optional[str] = None,
) -> MaintenanceResult:
    """Run the maintenance skill for a project.

    Args:
        project: Project name
        working_dir: Working directory for Claude Code
        session_id: Optional session ID to resume
        claude_config: Claude configuration
        maintenance_config: Maintenance configuration for this project
        ticket_count: Current ticket count
        last_maintenance: ISO timestamp of last maintenance run
        trello_client: Optional Trello client for creating maintenance cards
        icebox_list_id: Optional ICE BOX list ID for maintenance cards

    Returns:
        MaintenanceResult with success status and summary
    """
    prefix = f"[{project}] "
    logger.info(
        "%sRunning maintenance (ticket_count=%d, interval=%d)",
        prefix,
        ticket_count,
        maintenance_config.interval,
    )

    # Build maintenance prompt
    prompt = build_maintenance_prompt(
        project=project,
        ticket_count=ticket_count,
        last_maintenance=last_maintenance,
        interval=maintenance_config.interval,
    )

    # Build command
    cmd = [
        claude_config.binary,
        "-p",
        prompt,
        "--output-format",
        "json",
    ]

    if claude_config.yolo:
        cmd.append("--dangerously-skip-permissions")

    if session_id:
        cmd.extend(["--resume", session_id])

    cwd = Path(working_dir).expanduser()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,  # 10MB buffer
        )

        # Use a longer timeout for maintenance tasks (10 minutes)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=600,
        )

        if proc.returncode != 0:
            stderr_text = stderr.decode()
            logger.error(
                "%sMaintenance failed with return code %d: %s",
                prefix,
                proc.returncode,
                stderr_text,
            )
            return MaintenanceResult(
                success=False,
                summary=f"Maintenance failed: {stderr_text[:200]}",
            )

        # Parse output to get session ID and result
        output = stdout.decode()
        new_session_id = None
        summary = "Maintenance completed"

        for line in reversed(output.strip().split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    if "session_id" in data:
                        new_session_id = data["session_id"]
                    if "result" in data:
                        summary = data["result"]
                    if new_session_id:
                        break
                except json.JSONDecodeError:
                    continue

        # Create/update Trello card in ICE BOX if configured
        if trello_client and icebox_list_id:
            await _update_maintenance_card(
                trello_client=trello_client,
                icebox_list_id=icebox_list_id,
                project=project,
                summary=summary,
                prefix=prefix,
            )

        logger.info("%sMaintenance completed: %s", prefix, summary[:100])

        return MaintenanceResult(
            success=True,
            summary=summary,
            session_id=new_session_id,
        )

    except asyncio.TimeoutError:
        logger.error("%sMaintenance timed out", prefix)
        return MaintenanceResult(
            success=False,
            summary="Maintenance timed out after 10 minutes",
        )
    except Exception as e:
        logger.error("%sMaintenance failed: %s", prefix, e)
        return MaintenanceResult(
            success=False,
            summary=f"Maintenance failed: {e}",
        )


async def _update_maintenance_card(
    trello_client: TrelloClient,
    icebox_list_id: str,
    project: str,
    summary: str,
    prefix: str,
) -> None:
    """Create or update a maintenance card in the ICE BOX.

    Args:
        trello_client: Trello client for API calls
        icebox_list_id: The ICE BOX list ID
        project: Project name
        summary: Maintenance summary to use as card description
        prefix: Log prefix
    """
    card_name = f"{project} regular maintenance"

    try:
        # Search for existing card
        existing_card = await trello_client.find_card_by_name(
            list_id=icebox_list_id,
            name=card_name,
        )

        if existing_card:
            # Update existing card's description
            await trello_client.update_card_description(
                card_id=existing_card.id,
                description=summary,
            )
            logger.info(
                "%sUpdated maintenance card: %s",
                prefix,
                existing_card.url,
            )
        else:
            # Create new card
            new_card = await trello_client.create_card(
                list_id=icebox_list_id,
                name=card_name,
                description=summary,
            )
            logger.info(
                "%sCreated maintenance card: %s",
                prefix,
                new_card.url,
            )

    except Exception as e:
        logger.warning(
            "%sFailed to update maintenance card: %s",
            prefix,
            e,
        )


def should_run_maintenance(
    ticket_count: int,
    maintenance_config: Optional[MaintenanceConfig],
) -> bool:
    """Check if maintenance should run based on completed ticket count.

    Maintenance runs when:
    1. Maintenance is enabled in config
    2. We have completed at least N tickets since last maintenance

    This should be called BEFORE processing a new ticket. When a new ticket
    arrives and we've completed N tickets, maintenance runs first, then the
    counter is reset to 0, and the new ticket is processed normally.

    Example with interval=10:
    - Tickets 1-10 are processed, counter reaches 10
    - Ticket 11 arrives
    - should_run_maintenance(10, config) returns True
    - Maintenance runs, counter resets to 0
    - Ticket 11 is processed, counter becomes 1

    Args:
        ticket_count: Number of completed tickets since last maintenance
        maintenance_config: Maintenance configuration (or None if not configured)

    Returns:
        True if maintenance should run before the next ticket
    """
    if not maintenance_config or not maintenance_config.enabled:
        return False

    # Run maintenance when we've completed at least interval tickets
    return ticket_count >= maintenance_config.interval
