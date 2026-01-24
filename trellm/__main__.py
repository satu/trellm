"""Main entry point for TreLLM."""

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

from .claude import ClaudeRunner, fetch_claude_usage_limits
from .config import Config, load_config
from .maintenance import run_maintenance, should_run_maintenance
from .state import StateManager
from .trello import TrelloClient, TrelloCard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Per-project locks to ensure only one Claude instance runs per project
_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Track cards currently being processed to avoid duplicate processing
_processing_cards: set[str] = set()

# Track running tasks for cleanup
_running_tasks: set[asyncio.Task] = set()


def parse_project(card_name: str) -> str:
    """Extract project name (first word) from card name.

    Supports both "project task" and "project: task" formats.
    """
    parts = card_name.split()
    if not parts:
        return "unknown"
    # Strip trailing colon if present (e.g., "trellm:" -> "trellm")
    return parts[0].rstrip(":").lower()


def is_stats_command(card_name: str, valid_projects: set[str] | None = None) -> bool:
    """Check if a card is a /stats command.

    The /stats command must appear immediately after the project name:
    - "project /stats" - valid
    - "project: /stats" - valid
    - "project problem with /stats" - NOT valid (stats appears later)

    Args:
        card_name: The card name to check
        valid_projects: Optional set of valid project names. If provided,
            only cards with a matching project name will be recognized.

    Returns:
        True if the card is a valid /stats command, False otherwise.
    """
    parts = card_name.lower().split()
    if len(parts) < 2:
        return False

    # Extract project name (first word, strip colon)
    project = parts[0].rstrip(":")

    # Check if project is valid (if filter provided)
    if valid_projects is not None and project not in valid_projects:
        return False

    # /stats must be the second word (immediately after project name)
    return parts[1] == "/stats"


async def handle_stats_command(
    card: TrelloCard,
    trello: TrelloClient,
    state: StateManager,
    ready_list_id: Optional[str] = None,
) -> bool:
    """Handle a /stats command card.

    Posts stats as a comment on the card and moves it to READY TO TRY.

    Args:
        card: The Trello card with the /stats command
        trello: Trello client for API calls
        state: State manager for stats data
        ready_list_id: Optional list ID to move to (defaults to READY TO TRY)

    Returns:
        True if handled successfully, False otherwise.
    """
    project = parse_project(card.name)

    try:
        # Fetch real-time usage limits from Claude API
        usage_limits = fetch_claude_usage_limits()

        # Generate stats report (includes historical stats)
        stats_report = state.format_stats_report(project if project != "unknown" else None)

        # Combine real-time usage limits with historical stats
        usage_report = usage_limits.format_report()
        full_report = f"{usage_report}\n\n{stats_report}"

        # Add comment with stats
        comment = f"Claude: /stats command processed\n\n{full_report}"
        await trello.add_comment(card.id, comment)

        # Move card to ready
        await trello.move_to_ready(card.id)

        logger.info("Handled /stats command for card %s", card.id)
        return True

    except Exception as e:
        logger.error("Failed to handle /stats command for card %s: %s", card.id, e)
        return False


def compare_configs(old: Config, new: Config) -> list[str]:
    """Compare two configs and return a list of changes.

    Returns a list of human-readable change descriptions.
    """
    changes: list[str] = []

    # Compare poll interval
    if old.poll_interval != new.poll_interval:
        changes.append(f"poll_interval: {old.poll_interval} → {new.poll_interval}")

    # Compare Claude config
    if old.claude.binary != new.claude.binary:
        changes.append(f"claude.binary: {old.claude.binary} → {new.claude.binary}")
    if old.claude.timeout != new.claude.timeout:
        changes.append(f"claude.timeout: {old.claude.timeout} → {new.claude.timeout}")
    if old.claude.yolo != new.claude.yolo:
        changes.append(f"claude.yolo: {old.claude.yolo} → {new.claude.yolo}")

    # Compare projects
    old_projects = set(old.claude.projects.keys())
    new_projects = set(new.claude.projects.keys())

    for proj in new_projects - old_projects:
        changes.append(f"Added project: {proj}")

    for proj in old_projects - new_projects:
        changes.append(f"Removed project: {proj}")

    for proj in old_projects & new_projects:
        old_proj = old.claude.projects[proj]
        new_proj = new.claude.projects[proj]
        if old_proj.working_dir != new_proj.working_dir:
            changes.append(
                f"{proj}.working_dir: {old_proj.working_dir} → {new_proj.working_dir}"
            )
        if old_proj.session_id != new_proj.session_id:
            changes.append(
                f"{proj}.session_id: {old_proj.session_id} → {new_proj.session_id}"
            )
        if old_proj.compact_prompt != new_proj.compact_prompt:
            changes.append(
                f"{proj}.compact_prompt: {old_proj.compact_prompt} → {new_proj.compact_prompt}"
            )

    # Compare Trello config (only relevant fields)
    if old.trello.ready_to_try_list_id != new.trello.ready_to_try_list_id:
        changes.append(
            f"ready_to_try_list_id: {old.trello.ready_to_try_list_id} → "
            f"{new.trello.ready_to_try_list_id}"
        )
    if old.trello.done_board_id != new.trello.done_board_id:
        changes.append(
            f"done_board_id: {old.trello.done_board_id} → {new.trello.done_board_id}"
        )
    if old.trello.done_list_id != new.trello.done_list_id:
        changes.append(
            f"done_list_id: {old.trello.done_list_id} → {new.trello.done_list_id}"
        )

    return changes


def configs_equal(old: Config, new: Config) -> bool:
    """Check if two configs are functionally equal."""
    return len(compare_configs(old, new)) == 0


async def process_cards(
    trello: TrelloClient,
    state: StateManager,
    claude: ClaudeRunner,
    config: Config,
) -> int:
    """Process all cards in TODO list.

    Returns the number of cards processed.
    """
    cards = await trello.get_todo_cards()
    logger.debug("Found %d cards in TODO", len(cards))

    processed_count = 0

    for card in cards:
        # Skip if already processed (unless moved back to TODO)
        if state.is_processed(card.id):
            if state.should_reprocess(card.id, card.last_activity):
                logger.info("Card %s moved back to TODO, reprocessing", card.id)
                state.clear_processed(card.id)
            else:
                continue

        # Check for /stats command - handle directly without Claude
        # Only recognize /stats for configured projects
        if is_stats_command(card.name, set(config.claude.projects.keys())):
            logger.info("Detected /stats command: %s", card.name)
            success = await handle_stats_command(
                card=card,
                trello=trello,
                state=state,
                ready_list_id=config.trello.ready_to_try_list_id,
            )
            if success:
                state.mark_processed(card.id)
                processed_count += 1
            continue

        project = parse_project(card.name)

        # Skip cards for unrecognized projects
        if project not in config.claude.projects:
            logger.warning(
                "Skipping card %s: project '%s' not in config (card: %s)",
                card.id,
                project,
                card.name[:50],
            )
            continue

        logger.info("Processing card %s for project %s: %s", card.id, project, card.name)

        # Get session ID and last card ID for this project
        # Priority: 1) state file (from previous runs), 2) config file (initial setup)
        session_id = state.get_session(project)
        if not session_id:
            session_id = config.get_initial_session_id(project)
        last_card_id = state.get_last_card_id(project)

        # Run Claude Code
        try:
            result = await claude.run(
                card=card,
                project=project,
                session_id=session_id,
                working_dir=config.get_working_dir(project),
                last_card_id=last_card_id,
                compact_prompt=config.get_compact_prompt(project),
            )

            # Update session ID and last card ID for next task
            if result.session_id:
                state.set_session(project, result.session_id, last_card_id=card.id)

            # Record cost/usage statistics
            if result.cost_info:
                state.record_cost(
                    card_id=card.id,
                    project=project,
                    total_cost=result.cost_info.total_cost,
                    api_duration=result.cost_info.api_duration,
                    wall_duration=result.cost_info.wall_duration,
                    code_changes=result.cost_info.code_changes,
                    input_tokens=result.cost_info.input_tokens,
                    output_tokens=result.cost_info.output_tokens,
                    cache_creation_tokens=result.cost_info.cache_creation_tokens,
                    cache_read_tokens=result.cost_info.cache_read_tokens,
                )

            # Mark as processed and move card
            state.mark_processed(card.id)
            await trello.move_to_ready(card.id)
            logger.info("Completed card %s", card.id)
            processed_count += 1

            # Increment ticket count and check if maintenance is needed
            ticket_count = state.increment_ticket_count(project)
            maintenance_config = config.get_maintenance_config(project)

            if should_run_maintenance(ticket_count, maintenance_config):
                logger.info(
                    "[%s] Triggering maintenance (ticket #%d)",
                    project,
                    ticket_count,
                )
                working_dir = config.get_working_dir(project)
                if working_dir and maintenance_config:
                    maint_result = await run_maintenance(
                        project=project,
                        working_dir=working_dir,
                        session_id=result.session_id,
                        claude_config=config.claude,
                        maintenance_config=maintenance_config,
                        ticket_count=ticket_count,
                        last_maintenance=state.get_last_maintenance(project),
                        trello_client=trello,
                        icebox_list_id=config.trello.icebox_list_id,
                    )
                    if maint_result.success:
                        state.set_last_maintenance(project)
                        if maint_result.session_id:
                            state.set_session(
                                project,
                                maint_result.session_id,
                                last_card_id=card.id,
                            )
                        logger.info(
                            "[%s] Maintenance completed: %s",
                            project,
                            maint_result.summary[:100],
                        )
                    else:
                        logger.warning(
                            "[%s] Maintenance failed: %s",
                            project,
                            maint_result.summary[:100],
                        )

        except Exception as e:
            logger.error("Failed to process card %s: %s", card.id, e)
            # Leave card in TODO for retry; Claude Code handles comments

    return processed_count


async def process_card_for_project(
    card: TrelloCard,
    project: str,
    trello: TrelloClient,
    state: StateManager,
    claude: ClaudeRunner,
    config: Config,
) -> Optional[str]:
    """Process a single card for a project, with per-project locking.

    Returns the card ID if processed successfully, None otherwise.
    Cards are tracked in _processing_cards while being processed.
    """
    lock = _project_locks[project]

    async with lock:
        logger.info(
            "[%s] Processing card %s: %s",
            project,
            card.id,
            card.name,
        )

        # Get session ID and last card ID for this project
        session_id = state.get_session(project)
        if not session_id:
            session_id = config.get_initial_session_id(project)
        last_card_id = state.get_last_card_id(project)

        # Run Claude Code
        try:
            result = await claude.run(
                card=card,
                project=project,
                session_id=session_id,
                working_dir=config.get_working_dir(project),
                last_card_id=last_card_id,
                compact_prompt=config.get_compact_prompt(project),
            )

            # Update session ID and last card ID for next task
            if result.session_id:
                state.set_session(project, result.session_id, last_card_id=card.id)

            # Record cost/usage statistics
            if result.cost_info:
                state.record_cost(
                    card_id=card.id,
                    project=project,
                    total_cost=result.cost_info.total_cost,
                    api_duration=result.cost_info.api_duration,
                    wall_duration=result.cost_info.wall_duration,
                    code_changes=result.cost_info.code_changes,
                    input_tokens=result.cost_info.input_tokens,
                    output_tokens=result.cost_info.output_tokens,
                    cache_creation_tokens=result.cost_info.cache_creation_tokens,
                    cache_read_tokens=result.cost_info.cache_read_tokens,
                )

            # Mark as processed and move card
            state.mark_processed(card.id)
            await trello.move_to_ready(card.id)
            logger.info("[%s] Completed card %s", project, card.id)

            # Increment ticket count and check if maintenance is needed
            ticket_count = state.increment_ticket_count(project)
            maintenance_config = config.get_maintenance_config(project)

            if should_run_maintenance(ticket_count, maintenance_config):
                logger.info(
                    "[%s] Triggering maintenance (ticket #%d)",
                    project,
                    ticket_count,
                )
                working_dir = config.get_working_dir(project)
                if working_dir and maintenance_config:
                    maint_result = await run_maintenance(
                        project=project,
                        working_dir=working_dir,
                        session_id=result.session_id,
                        claude_config=config.claude,
                        maintenance_config=maintenance_config,
                        ticket_count=ticket_count,
                        last_maintenance=state.get_last_maintenance(project),
                        trello_client=trello,
                        icebox_list_id=config.trello.icebox_list_id,
                    )
                    if maint_result.success:
                        state.set_last_maintenance(project)
                        # Update session ID if maintenance returned a new one
                        if maint_result.session_id:
                            state.set_session(
                                project,
                                maint_result.session_id,
                                last_card_id=card.id,
                            )
                        logger.info(
                            "[%s] Maintenance completed: %s",
                            project,
                            maint_result.summary[:100],
                        )
                    else:
                        logger.warning(
                            "[%s] Maintenance failed: %s",
                            project,
                            maint_result.summary[:100],
                        )

            return card.id

        except Exception as e:
            logger.error("[%s] Failed to process card %s: %s", project, card.id, e)
            return None
        finally:
            # Always remove from processing set when done
            _processing_cards.discard(card.id)


def _task_done_callback(task: asyncio.Task) -> None:
    """Callback to clean up completed tasks and track results."""
    _running_tasks.discard(task)
    try:
        result = task.result()
        if isinstance(result, str):  # Card ID returned on success
            # Store for config reload notifications (best effort)
            task._last_processed_card_id = result  # type: ignore[attr-defined]
    except Exception:
        # Task failed, already logged in process_card_for_project
        pass


async def run_polling_loop(
    config: Config,
    verbose: bool = False,
    config_path: Optional[str] = None,
) -> None:
    """Run the main polling loop with hot config reloading and parallel execution.

    Tasks are spawned in the background and polling continues while they run.
    Per-project locks ensure only one task runs per project at a time.
    """
    trello = TrelloClient(config.trello)
    state = StateManager(config.state_file)
    claude = ClaudeRunner(
        config.claude,
        verbose=verbose,
        ready_list_id=config.trello.ready_to_try_list_id,
    )

    # Track current config and last processed card for reload notifications
    current_config = config
    last_processed_card_id: Optional[str] = None

    logger.info("TreLLM started, polling every %d seconds", config.poll_interval)

    try:
        while True:
            # Try to reload config
            try:
                new_config = load_config(config_path)

                # Check if config changed
                if not configs_equal(current_config, new_config):
                    changes = compare_configs(current_config, new_config)
                    logger.info("Configuration reloaded with %d changes", len(changes))

                    # Update components that need the new config
                    # Note: TrelloClient is recreated since credentials might change
                    await trello.close()
                    trello = TrelloClient(new_config.trello)
                    claude = ClaudeRunner(
                        new_config.claude,
                        verbose=verbose,
                        ready_list_id=new_config.trello.ready_to_try_list_id,
                    )

                    # Add comment to last processed card about config reload
                    if last_processed_card_id:
                        changes_text = "\n".join(f"- {c}" for c in changes)
                        comment = (
                            f"TreLLM: Configuration reloaded\n\n"
                            f"Changes:\n{changes_text}"
                        )
                        try:
                            await trello.add_comment(last_processed_card_id, comment)
                        except Exception as e:
                            logger.warning(
                                "Failed to add config reload comment: %s", e
                            )

                    current_config = new_config

            except Exception as e:
                # Config reload failed - keep using old config
                logger.warning("Failed to reload config, keeping current: %s", e)

            # Process cards - spawn background tasks for new cards
            try:
                cards = await trello.get_todo_cards()
                logger.debug("Found %d cards in TODO", len(cards))

                for card in cards:
                    # Skip if already being processed
                    if card.id in _processing_cards:
                        continue

                    # Skip if already processed (unless moved back to TODO)
                    if state.is_processed(card.id):
                        if state.should_reprocess(card.id, card.last_activity):
                            logger.info(
                                "Card %s moved back to TODO, reprocessing", card.id
                            )
                            state.clear_processed(card.id)
                        else:
                            continue

                    # Check for /stats command - handle directly without Claude
                    # Only recognize /stats for configured projects
                    if is_stats_command(card.name, set(current_config.claude.projects.keys())):
                        logger.info("Detected /stats command: %s", card.name)
                        _processing_cards.add(card.id)
                        success = await handle_stats_command(
                            card=card,
                            trello=trello,
                            state=state,
                            ready_list_id=current_config.trello.ready_to_try_list_id,
                        )
                        if success:
                            state.mark_processed(card.id)
                        _processing_cards.discard(card.id)
                        continue

                    project = parse_project(card.name)

                    # Skip cards for unrecognized projects
                    if project not in current_config.claude.projects:
                        logger.warning(
                            "Skipping card %s: project '%s' not in config (card: %s)",
                            card.id,
                            project,
                            card.name[:50],
                        )
                        continue

                    # Mark as being processed before spawning task
                    _processing_cards.add(card.id)

                    # Spawn background task - don't await, let it run in background
                    task = asyncio.create_task(
                        process_card_for_project(
                            card=card,
                            project=project,
                            trello=trello,
                            state=state,
                            claude=claude,
                            config=current_config,
                        )
                    )
                    task.add_done_callback(_task_done_callback)
                    _running_tasks.add(task)
                    logger.info(
                        "[%s] Spawned background task for card %s",
                        project,
                        card.id,
                    )

            except Exception as e:
                logger.error("Error in polling loop: %s", e)

            await asyncio.sleep(current_config.poll_interval)
    finally:
        # Cancel all running tasks on shutdown
        for task in _running_tasks:
            task.cancel()
        if _running_tasks:
            await asyncio.gather(*_running_tasks, return_exceptions=True)
        await trello.close()


async def run_once(config: Config, verbose: bool = False) -> int:
    """Run once and exit (for testing or one-shot mode)."""
    trello = TrelloClient(config.trello)
    state = StateManager(config.state_file)
    claude = ClaudeRunner(
        config.claude,
        verbose=verbose,
        ready_list_id=config.trello.ready_to_try_list_id,
    )

    try:
        return await process_cards(trello, state, claude, config)
    finally:
        await trello.close()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="TreLLM - Automation tool bridging Trello boards with AI coding assistants"
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Path to config file (default: ~/.trellm/config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process cards once and exit (instead of polling)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Verbose output: -v shows Claude conversation, -vv adds debug logging",
    )
    args = parser.parse_args()

    # -vv enables DEBUG logging (includes poll cycles)
    # -v just shows Claude conversation but keeps INFO logging
    if args.verbose >= 2:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    # Validate required config
    if not config.trello.api_key or not config.trello.api_token:
        logger.error(
            "Trello API credentials not configured. "
            "Set TRELLO_API_KEY and TRELLO_API_TOKEN environment variables "
            "or configure in ~/.trellm/config.yaml"
        )
        sys.exit(1)

    if not config.trello.todo_list_id:
        logger.error(
            "Trello TODO list ID not configured. "
            "Set TRELLO_TODO_LIST_ID environment variable "
            "or configure in ~/.trellm/config.yaml"
        )
        sys.exit(1)

    # Run
    # verbose >= 1 enables Claude conversation streaming
    show_claude_output = args.verbose >= 1
    if args.once:
        count = asyncio.run(run_once(config, verbose=show_claude_output))
        logger.info("Processed %d cards", count)
    else:
        asyncio.run(
            run_polling_loop(config, verbose=show_claude_output, config_path=args.config)
        )


if __name__ == "__main__":
    main()
