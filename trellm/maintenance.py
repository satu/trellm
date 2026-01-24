"""Maintenance skill for TreLLM - automatic context maintenance every N tickets."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ClaudeConfig, MaintenanceConfig

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

    return f"""You are performing maintenance on the {project} project.

Recent ticket count: {ticket_count}
Last maintenance: {last_maint_str}
Maintenance interval: every {interval} tickets

Please perform the following maintenance tasks:

## 1. CLAUDE.md Review
- Check if CLAUDE.md exists in the project directory
- If it exists, review its contents
- Analyze recent work patterns from git history (last {interval} commits)
- Suggest updates for:
  - New coding conventions discovered
  - Architecture decisions made
  - Test patterns established
  - Common gotchas/pitfalls found
- Output any recommendations but DO NOT auto-apply changes to CLAUDE.md

## 2. Compaction Prompt Optimization
- Review the current compact_prompt (if any) in use
- Based on git history and file access patterns, identify:
  - Context that frequently needs to be preserved
  - Patterns that keep getting re-read
- Suggest updates to the compact_prompt configuration
- Output suggestions for user review (DO NOT modify any config files)

## 3. Documentation Freshness Check
- Scan for outdated README sections
- Check if any API docs exist and if they match implementation
- Flag stale TODOs in code (over 30 days old based on git blame)
- Report any documentation gaps

## 4. Write Maintenance Log
Create or update the file `.claude/maintenance-log.md` in the project directory with a summary:

```markdown
## Maintenance Run - {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

### Ticket Count: {ticket_count}

### Observations
- [List files frequently accessed in recent work]
- [List patterns established]
- [List decisions made]

### Recommendations
- [List specific, actionable suggestions]
- [Include suggested compact_prompt updates if any]
```

Be concise. Focus on actionable improvements. Do not make any changes other than updating the maintenance log."""


async def run_maintenance(
    project: str,
    working_dir: str,
    session_id: Optional[str],
    claude_config: ClaudeConfig,
    maintenance_config: MaintenanceConfig,
    ticket_count: int,
    last_maintenance: Optional[str],
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

        # Parse output to get session ID
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


def should_run_maintenance(
    ticket_count: int,
    maintenance_config: Optional[MaintenanceConfig],
) -> bool:
    """Check if maintenance should run based on ticket count.

    Maintenance runs when:
    1. Maintenance is enabled in config
    2. ticket_count is a multiple of the configured interval

    Args:
        ticket_count: Current ticket count (after incrementing)
        maintenance_config: Maintenance configuration (or None if not configured)

    Returns:
        True if maintenance should run
    """
    if not maintenance_config or not maintenance_config.enabled:
        return False

    # Run maintenance every N tickets (when count is divisible by interval)
    return ticket_count > 0 and (ticket_count % maintenance_config.interval == 0)
