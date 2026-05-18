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

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from .claude import ClaudeResult, ClaudeRunner, CostInfo
from .completion import (
    INTERACTIVE_DIR,
    CompletionOutcome,
    SignalWatcher,
    detect_completion,
    read_transcript_text,
    sentinel_marker,
    signal_path,
    transcript_path_resolver,
)
from .config import Config
from .state import StateManager
from .tmux import TmuxController, TmuxError
from .trello import TrelloCard

logger = logging.getLogger(__name__)

# Runner modes recognised by SessionManager. Both have a backend as of M4:
# "print" (PrintSession) and "interactive" (InteractiveSession).
RUNNER_PRINT = "print"
RUNNER_INTERACTIVE = "interactive"

# The shell command an interactive window runs (doc §6.1). `--continue`
# resumes the project's prior `claude` session; the `||` fallback covers the
# first-ever run, when there is no session to continue.
INTERACTIVE_CLAUDE_COMMAND = (
    "claude --continue --dangerously-skip-permissions "
    "|| claude --dangerously-skip-permissions"
)
# Seconds to wait for a `/compact` turn's Stop signal before proceeding to
# dispatch the task anyway — mirrors print mode's 120s `_run_compact` budget.
DEFAULT_COMPACT_TIMEOUT = 120.0
# Seconds to let the `claude` TUI start before typing into a freshly created
# window. A starting guess to be tuned by M5; tests pass 0.
DEFAULT_STARTUP_DELAY = 8.0
# capture-pane poll cadence for the dashboard live-view diff (doc §6.3).
DEFAULT_PANE_POLL_INTERVAL = 2.0


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


def _pane_delta(old: str, new: str) -> str:
    """The portion of pane text `new` not already present in `old`.

    `tmux capture-pane` returns the whole visible screen each call. When the
    screen only grew, forward just the appended tail; when it scrolled or
    redrew, forward the whole screen. Best-effort — this feeds the dashboard
    view only (doc §6.3), never a completion signal.
    """
    if new == old:
        return ""
    if old and new.startswith(old):
        return new[len(old):]
    return new


class InteractiveSession:
    """`ClaudeSession` backed by one long-lived `claude` TUI per project.

    Where `PrintSession` spawns a fresh `claude -p` subprocess per card,
    `InteractiveSession` owns a single `claude` TUI running in a dedicated
    tmux window (doc §6.1), so cards stay on the subscription seat instead of
    each becoming a metered `-p` call. Because the process is long-lived,
    "the process exited" is no longer the completion signal — `run_task`
    dispatches a prompt into the window and then runs the M3 §4 confirmation
    stack (`detect_completion`) to learn when the turn is genuinely done.

    One window per project plus the per-project lock in `__main__.py`
    guarantees only one writer per pane, so this class needs no locking of
    its own. `SessionManager` creates it once per project and reuses it
    across cards — that reuse is what keeps the tmux window and the `claude`
    context alive between cards.
    """

    def __init__(
        self,
        project: str,
        working_dir: Optional[str],
        runner: ClaudeRunner,
        state: StateManager,
        *,
        tmux: Optional[TmuxController] = None,
        interactive_dir: Optional[str] = None,
        startup_delay: float = DEFAULT_STARTUP_DELAY,
        compact_timeout: float = DEFAULT_COMPACT_TIMEOUT,
        pane_poll_interval: float = DEFAULT_PANE_POLL_INTERVAL,
        inotify_binary: str = "inotifywait",
    ):
        self._project = project
        self._working_dir = working_dir
        # ClaudeRunner is reused for two pieces of behaviour, NOT to spawn
        # subprocesses: `_build_prompt` (so the interactive task prompt is
        # identical to print mode's) and `_check_for_errors` (the §6.3 error
        # scan over transcript text).
        self._runner = runner
        self._state = state
        self._tmux = tmux if tmux is not None else TmuxController()
        self._interactive_dir = interactive_dir
        self._startup_delay = startup_delay
        self._compact_timeout = compact_timeout
        self._pane_poll_interval = pane_poll_interval
        self._inotify_binary = inotify_binary
        base = (
            Path(interactive_dir).expanduser()
            if interactive_dir
            else Path(INTERACTIVE_DIR).expanduser()
        )
        # Dispatched prompts are written here, one file per card (doc §6.1).
        self._tasks_dir = base / "tasks"

    async def run_task(
        self,
        card: TrelloCard,
        *,
        timeout: int,
        output_callback: Optional[callable] = None,
    ) -> ClaudeResult:
        """Dispatch `card` into the project's `claude` TUI and await completion.

        Ensures the window exists, pre-compacts between cards, types the
        prompt-file dispatch, then runs the §4 confirmation stack:
          * COMPLETED     — Stop fired and the sentinel is present → success.
          * STOPPED_EARLY — Stop fired without the sentinel → run the error
            regexes over the transcript (a rate / monthly limit propagates as
            MonthlyLimitError / RateLimitError); otherwise fail the card.
          * TIMED_OUT     — no Stop within `timeout` → interrupt the pane and
            raise, matching print mode's timeout `RuntimeError`.
        """
        await self._ensure_window()

        # Pre-compact between cards (doc §6.1): a card different from the last
        # one processed gets the prior context compacted into its own turn
        # first. A retry of the SAME card skips this — it must keep context.
        last_card_id = self._state.get_last_card_id(self._project)
        if last_card_id is not None and last_card_id != card.id:
            await self._dispatch_compact()

        # Construct the SignalWatcher BEFORE dispatching so its baseline
        # excludes the /compact turn's signal and any earlier card's.
        watcher = SignalWatcher(
            signal_path(self._project, base_dir=self._interactive_dir),
            inotify_binary=self._inotify_binary,
        )
        resolve = transcript_path_resolver(self._working_dir)

        await self._dispatch_task(card)

        # Feed the dashboard from a capture-pane diff while the turn runs.
        streamer: Optional[asyncio.Task] = None
        if output_callback is not None:
            streamer = asyncio.create_task(self._stream_pane(output_callback))
        try:
            completion = await detect_completion(
                card_id=card.id,
                watcher=watcher,
                timeout=timeout,
                resolve_transcript=resolve,
            )
        finally:
            if streamer is not None:
                streamer.cancel()
                with suppress(asyncio.CancelledError):
                    await streamer

        return await self._finish(card, completion, timeout)

    async def _ensure_window(self) -> None:
        """Create the project's tmux window if it is not already live.

        `SessionManager` reuses one InteractiveSession per project, but on a
        trellm restart the in-process object is new while the tmux window may
        still be running (doc §6.1) — so this checks `list_windows` and reuses
        a live window rather than recreating it, keeping in-flight context.
        """
        await self._tmux.create_session()
        if self._project in await self._tmux.list_windows():
            logger.info("[%s] Reusing live interactive window", self._project)
            return
        await self._tmux.create_window(
            self._project,
            self._working_dir or ".",
            command=INTERACTIVE_CLAUDE_COMMAND,
        )
        logger.info("[%s] Created interactive window", self._project)
        # The claude TUI needs a moment to come up before it accepts keys.
        if self._startup_delay > 0:
            await asyncio.sleep(self._startup_delay)

    async def _dispatch_compact(self) -> None:
        """Dispatch `/compact` as its own turn and wait for it to finish.

        Per doc §6.1 the prior card's context is compacted in a dedicated
        turn before the new task is typed into the same pane. We wait for the
        Stop signal so the compact turn has fully ended first; if it does not
        signal within `compact_timeout` we log and proceed anyway rather than
        stalling the card forever.
        """
        watcher = SignalWatcher(
            signal_path(self._project, base_dir=self._interactive_dir),
            inotify_binary=self._inotify_binary,
        )
        await self._tmux.send_literal(self._project, "/compact")
        await self._tmux.send_keys(self._project, "Enter")
        entry = await watcher.wait(timeout=self._compact_timeout)
        if entry is None:
            logger.warning(
                "[%s] /compact turn did not signal within %ss; proceeding",
                self._project,
                self._compact_timeout,
            )
        else:
            logger.info(
                "[%s] /compact done (session now %s)",
                self._project,
                entry.session_id,
            )

    async def _dispatch_task(self, card: TrelloCard) -> None:
        """Write the prompt to a file and type the one-line dispatch.

        Per doc §8 the prompt is never typed directly: it goes to
        `~/.trellm/interactive/tasks/<cardid>.md`, then `send-keys -l` types a
        one-line "Read <file> ..." instruction and a SEPARATE Enter submits
        it — sidestepping every multiline / shell-escaping problem.
        """
        task_file = self._write_task_file(card)
        instruction = f"Read {task_file} and complete the task it describes."
        await self._tmux.send_literal(self._project, instruction)
        await self._tmux.send_keys(self._project, "Enter")
        logger.info("[%s] Dispatched card %s", self._project, card.id)

    def _write_task_file(self, card: TrelloCard) -> Path:
        """Write `card`'s prompt to its task file and return the path."""
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        path = self._tasks_dir / f"{card.id}.md"
        path.write_text(self._build_task_prompt(card))
        return path

    def _build_task_prompt(self, card: TrelloCard) -> str:
        """The interactive task prompt: print mode's prompt plus the §4
        sentinel instruction so completion can be confirmed in the transcript.
        """
        base = self._runner._build_prompt(card)
        marker = sentinel_marker(card.id)
        return (
            f"{base}\n\n"
            "Completion marker:\n"
            "- After everything above is done and committed, print this exact "
            "line, on its own line, as the very last thing — nothing after "
            f"it:\n{marker}\n"
        )

    async def _finish(
        self, card: TrelloCard, completion, timeout: int
    ) -> ClaudeResult:
        """Map a `CompletionResult` to a `ClaudeResult` or an exception."""
        if completion.outcome is CompletionOutcome.TIMED_OUT:
            # §4 backstop: interrupt the runaway turn so the pane is free for
            # the next card, then raise the SAME message print mode uses for
            # a timeout — __main__.py keys its timeout categorisation and
            # retry-context comment off "timed out after".
            await self._interrupt()
            raise RuntimeError(f"Claude Code timed out after {timeout}s")

        # COMPLETED and STOPPED_EARLY both carry a session id from the Stop
        # signal — the post-/compact id when the turn rotated it (doc §6.1).
        # Persist it (keyed by this card) so a restart resumes the right
        # session and the next card's pre-compact decision is correct. Done
        # before any raise below so a usage-limit failure still records it.
        if completion.session_id:
            self._state.set_session(
                self._project, completion.session_id, last_card_id=card.id
            )

        transcript_text = (
            read_transcript_text(completion.transcript_path)
            if completion.transcript_path
            else ""
        )

        if completion.outcome is CompletionOutcome.STOPPED_EARLY:
            # §6.3 / gotcha #8: run the error regexes over the transcript
            # text. A turn that stopped without the sentinel may have stopped
            # because of a rate / monthly limit — `_check_for_errors` raises
            # MonthlyLimitError / RateLimitError / etc.; let it propagate so
            # __main__.py's global polling pause still fires.
            self._runner._check_for_errors(
                "", transcript_text, session_id=completion.session_id
            )
            # No known error pattern — a genuine early stop (asked a
            # clarifying question, or left the work unfinished). Fail the card
            # so it stays in TODO with a retry-context comment, as print mode
            # does for a generic failure.
            raise RuntimeError(
                "Claude stopped without completing the task — the completion "
                "sentinel was not printed (the turn may have stopped to ask a "
                "question or ended with the work unfinished)."
            )

        # COMPLETED — Stop fired and the sentinel confirmed genuine completion.
        tokens = completion.tokens or {}
        cost_info = CostInfo(
            input_tokens=tokens.get("input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
            cache_creation_tokens=tokens.get("cache_creation_input_tokens", 0),
            cache_read_tokens=tokens.get("cache_read_input_tokens", 0),
        )
        return ClaudeResult(
            success=True,
            session_id=completion.session_id,
            summary=completion.summary or "Task completed",
            output=transcript_text,
            cost_info=cost_info,
        )

    async def _interrupt(self) -> None:
        """Interrupt the current turn in the pane — the §4 timeout backstop.

        Sends Escape then Ctrl-C. Best-effort: a tmux failure here must not
        mask the timeout `RuntimeError` the caller is about to raise.
        """
        with suppress(TmuxError):
            await self._tmux.send_keys(self._project, "Escape")
            await self._tmux.send_keys(self._project, "C-c")

    async def _stream_pane(self, output_callback) -> None:
        """Forward new pane text to `output_callback` while a turn runs.

        Replaces print mode's stream-json feed (doc §6.3): the rendered pane
        is the only interactive live view, so this polls `capture-pane` and
        forwards whatever is new via `_pane_delta`. Best-effort — a capture
        failure is swallowed and the next tick retries. `run_task` cancels
        this task once completion is detected.
        """
        last = ""
        try:
            while True:
                await asyncio.sleep(self._pane_poll_interval)
                try:
                    pane = await self._tmux.capture_pane(self._project)
                except TmuxError:
                    continue
                delta = _pane_delta(last, pane)
                if delta:
                    output_callback(delta)
                last = pane
        except asyncio.CancelledError:
            pass


class SessionManager:
    """Resolves a project to its `ClaudeSession`.

    Print-mode projects get a fresh, stateless `PrintSession` each call (cheap
    — there is nothing to reuse). Interactive-mode projects get a single
    long-lived `InteractiveSession`, created lazily and reused across cards so
    the tmux window and the `claude` context persist between cards.
    """

    def __init__(
        self,
        config: Config,
        runner: ClaudeRunner,
        state: StateManager,
        *,
        tmux: Optional[TmuxController] = None,
    ):
        self._config = config
        self._runner = runner
        self._state = state
        # One TmuxController drives every interactive window (they all live in
        # one tmux session). None ⇒ the default is built lazily on first need,
        # so print-only deployments never construct one.
        self._tmux = tmux
        # Long-lived interactive sessions, keyed by project.
        self._interactive: dict[str, InteractiveSession] = {}

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
        if mode == RUNNER_INTERACTIVE:
            existing = self._interactive.get(project)
            if existing is None:
                if self._tmux is None:
                    self._tmux = TmuxController()
                existing = InteractiveSession(
                    project=project,
                    working_dir=self._config.get_working_dir(project),
                    runner=self._runner,
                    state=self._state,
                    tmux=self._tmux,
                )
                self._interactive[project] = existing
            return existing
        # An unrecognised mode is a config mistake — fail loudly rather than
        # silently running print mode.
        raise ValueError(
            f"unknown runner mode {mode!r} for project {project!r} "
            f"— expected {RUNNER_PRINT!r} or {RUNNER_INTERACTIVE!r}"
        )

    async def shutdown(self) -> None:
        """Tear down long-lived sessions.

        Interactive tmux windows are intentionally LEFT RUNNING (doc §6.1):
        the tmux session outlives trellm like the browser stack, so a restart
        re-attaches to live windows with their context intact. trellm never
        attaches to those windows (they are created detached), so there is
        nothing to detach in-process — shutdown is a no-op for both
        transports. `PrintSession` likewise owns no resources.
        """
        return None
