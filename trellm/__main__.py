"""Main entry point for TreLLM."""

import argparse
import asyncio
import logging
import sys

from .claude import ClaudeRunner
from .config import Config, load_config
from .state import StateManager
from .trello import TrelloClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_project(card_name: str) -> str:
    """Extract project name (first word) from card name."""
    parts = card_name.split()
    if not parts:
        return "unknown"
    return parts[0].lower()


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

        project = parse_project(card.name)
        logger.info("Processing card %s for project %s: %s", card.id, project, card.name)

        # Get session ID for this project
        # Priority: 1) state file (from previous runs), 2) config file (initial setup)
        session_id = state.get_session(project)
        if not session_id:
            session_id = config.get_initial_session_id(project)

        # Run Claude Code
        try:
            result = await claude.run(
                card=card,
                project=project,
                session_id=session_id,
                working_dir=config.get_working_dir(project),
            )

            # Update session ID for next task
            if result.session_id:
                state.set_session(project, result.session_id)

            # Mark as processed and move card
            state.mark_processed(card.id)
            await trello.move_to_ready(card.id)
            logger.info("Completed card %s", card.id)
            processed_count += 1

        except Exception as e:
            logger.error("Failed to process card %s: %s", card.id, e)
            # Leave card in TODO for retry; Claude Code handles comments

    return processed_count


async def run_polling_loop(config: Config, verbose: bool = False) -> None:
    """Run the main polling loop."""
    trello = TrelloClient(config.trello)
    state = StateManager(config.state_file)
    claude = ClaudeRunner(config.claude, verbose=verbose)

    logger.info("TreLLM started, polling every %d seconds", config.poll_interval)

    try:
        while True:
            try:
                await process_cards(trello, state, claude, config)
            except Exception as e:
                logger.error("Error in polling loop: %s", e)

            await asyncio.sleep(config.poll_interval)
    finally:
        await trello.close()


async def run_once(config: Config, verbose: bool = False) -> int:
    """Run once and exit (for testing or one-shot mode)."""
    trello = TrelloClient(config.trello)
    state = StateManager(config.state_file)
    claude = ClaudeRunner(config.claude, verbose=verbose)

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
        asyncio.run(run_polling_loop(config, verbose=show_claude_output))


if __name__ == "__main__":
    main()
