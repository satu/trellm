"""Transport seam between TreLLM's polling loop and the `claude` CLI.

trellm spawns one `claude -p` subprocess per card today. `docs/claude-interactive.md`
plans a second transport — a long-lived interactive `claude` TUI per project — so
cards stay on the subscription seat rather than each becoming a metered `-p` call.
This module is the seam that lets the polling loop pick a transport per project
without knowing which is in use.

M1 (this file) lands the seam with a single backend, `PrintSession`, wrapping
today's `ClaudeRunner` unchanged — a pure refactor, zero behaviour change. M4
adds `InteractiveSession` behind the same `ClaudeSession` protocol.
"""

import logging
from typing import Optional, Protocol, runtime_checkable

from .claude import ClaudeResult, ClaudeRunner
from .config import Config
from .state import StateManager
from .trello import TrelloCard

logger = logging.getLogger(__name__)

# Runner modes recognised by SessionManager. "print" is the only transport
# with a backend in M1; "interactive" is reserved for M4.
RUNNER_PRINT = "print"
RUNNER_INTERACTIVE = "interactive"


@runtime_checkable
class ClaudeSession(Protocol):
    """One card's worth of work, transport-agnostic.

    Implementations own whatever session/continuity bookkeeping their
    transport needs (for print mode that is the `--resume` id in
    `state.json`); the caller only supplies the per-card inputs.
    """

    async def run_task(
        self,
        card: TrelloCard,
        *,
        timeout: int,
        output_callback: Optional[callable] = None,
    ) -> ClaudeResult:
        ...


class PrintSession:
    """`ClaudeSession` backed by today's one-subprocess-per-card model.

    Stateless: each `run_task` spawns and reaps one `claude -p` subprocess via
    `ClaudeRunner`. Session continuity is the explicit `--resume` id persisted
    in `state.json`, exactly as before — `run_task` reads the project's session
    id from state, threads it into the run, and writes the new id back. Landing
    this wrapper changes no behaviour.
    """

    def __init__(
        self,
        project: str,
        runner: ClaudeRunner,
        config: Config,
        state: StateManager,
    ):
        self._project = project
        self._runner = runner
        self._config = config
        self._state = state

    async def run_task(
        self,
        card: TrelloCard,
        *,
        timeout: int,
        output_callback: Optional[callable] = None,
    ) -> ClaudeResult:
        project = self._project
        config = self._config
        state = self._state

        # Session id: state.json (previous runs) first, config file second —
        # the same priority the __main__.py call site applied.
        session_id = state.get_session(project)
        if not session_id:
            session_id = config.get_initial_session_id(project)
        last_card_id = state.get_last_card_id(project)

        result = await self._runner.run(
            card=card,
            project=project,
            session_id=session_id,
            working_dir=config.get_working_dir(project),
            last_card_id=last_card_id,
            compact_prompt=config.get_compact_prompt(project),
            output_callback=output_callback,
            browser_enabled=config.is_browser_enabled(project),
            mcp_config_json=config.patchright_mcp_config_json(),
            timeout=timeout,
        )

        # Persist the (possibly new — /compact rotates it) session id so the
        # next card resumes it. Keyed by the card just processed.
        if result.session_id:
            state.set_session(project, result.session_id, last_card_id=card.id)
        return result


class SessionManager:
    """Resolves a project to its `ClaudeSession`.

    Print-mode projects get a fresh, stateless `PrintSession` each call (cheap —
    there is nothing to reuse). The interactive transport (M4) will keep a
    long-lived window per project, which is why resolution goes through a
    manager rather than the call site building sessions directly.
    """

    def __init__(self, config: Config, runner: ClaudeRunner, state: StateManager):
        self._config = config
        self._runner = runner
        self._state = state

    def get_runner_mode(self, project: str) -> str:
        """`claude` transport for `project`: 'print' (default) or 'interactive'.

        Delegates to `Config.get_runner_mode` — per-project beats global.
        """
        return self._config.get_runner_mode(project)

    def session_for(self, project: str) -> ClaudeSession:
        """Return the `ClaudeSession` for `project`'s configured transport."""
        mode = self.get_runner_mode(project)
        if mode == RUNNER_PRINT:
            return PrintSession(
                project=project,
                runner=self._runner,
                config=self._config,
                state=self._state,
            )
        # Interactive (and any other future mode) has no backend until M4.
        # Fail loudly: silently falling back to print would mask a config
        # mistake and the M1 gate is "default print for every project".
        raise ValueError(
            f"runner mode {mode!r} for project {project!r} has no backend yet "
            f"— only {RUNNER_PRINT!r} is implemented (interactive lands in M4)"
        )

    async def shutdown(self) -> None:
        """Tear down long-lived sessions.

        No-op while only print mode exists — `PrintSession` owns no resources.
        M4's `InteractiveSession` detaches its tmux windows here.
        """
        return None
