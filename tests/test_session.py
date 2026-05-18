"""Tests for the ClaudeSession transport seam (trellm/session.py).

docs/claude-interactive.md M1 introduces a backend seam so the polling
loop can pick a `claude` transport per project without knowing which is
in use. M1 ships one backend — PrintSession — wrapping today's
one-subprocess-per-card model unchanged. These tests pin:

  * SessionManager resolves a project to the right ClaudeSession.
  * PrintSession threads byte-identical arguments into ClaudeRunner.run,
    so landing the seam is a pure refactor with zero behaviour change.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from trellm.claude import ClaudeResult
from trellm.config import ClaudeConfig, Config, ProjectConfig, TrelloConfig
from trellm.session import (
    ClaudeSession,
    InteractiveSession,
    PrintSession,
    SessionManager,
)
from trellm.state import StateManager
from trellm.trello import TrelloCard


def _config(*, global_runner="print", **demo_kwargs) -> Config:
    """Config with a single print-mode project `demo`."""
    return Config(
        trello=TrelloConfig(
            api_key="k", api_token="t", board_id="b", todo_list_id="todo",
            ready_to_try_list_id="ready",
        ),
        claude=ClaudeConfig(
            binary="claude", timeout=1200, runner=global_runner,
            projects={
                "demo": ProjectConfig(working_dir="~/src/demo", **demo_kwargs),
            },
        ),
    )


def _card() -> TrelloCard:
    return TrelloCard(
        id="card-1",
        name="demo do a thing",
        description="",
        url="https://trello.com/c/card-1",
        last_activity="2026-05-17T10:00:00Z",
    )


def _runner(result: ClaudeResult | None = None) -> MagicMock:
    """A ClaudeRunner stand-in whose .run is an AsyncMock."""
    runner = MagicMock()
    runner.run = AsyncMock(
        return_value=result
        if result is not None
        else ClaudeResult(success=True, session_id="new-sess", summary="", output="")
    )
    return runner


class TestSessionManagerResolution:
    """SessionManager.session_for / get_runner_mode."""

    def test_get_runner_mode_delegates_to_config(self, tmp_path):
        config = _config(global_runner="print", runner="interactive")
        manager = SessionManager(
            config=config, runner=_runner(), state=StateManager(str(tmp_path / "s.json")),
        )
        # Per-project override is what config.get_runner_mode resolves to.
        assert manager.get_runner_mode("demo") == "interactive"

    def test_session_for_print_returns_print_session(self, tmp_path):
        config = _config()
        manager = SessionManager(
            config=config, runner=_runner(), state=StateManager(str(tmp_path / "s.json")),
        )
        session = manager.session_for("demo")
        assert isinstance(session, PrintSession)
        # A PrintSession satisfies the ClaudeSession protocol.
        assert isinstance(session, ClaudeSession)

    def test_session_for_print_is_fresh_each_call(self, tmp_path):
        """Print-mode projects get a fresh, stateless PrintSession per call —
        nothing is reused between cards."""
        config = _config()
        manager = SessionManager(
            config=config, runner=_runner(), state=StateManager(str(tmp_path / "s.json")),
        )
        assert manager.session_for("demo") is not manager.session_for("demo")

    def test_session_for_interactive_returns_interactive_session(self, tmp_path):
        """The interactive transport has a backend as of M4 — resolving an
        interactive project yields an InteractiveSession that satisfies the
        ClaudeSession protocol."""
        config = _config(runner="interactive")
        manager = SessionManager(
            config=config, runner=_runner(), state=StateManager(str(tmp_path / "s.json")),
        )
        session = manager.session_for("demo")
        assert isinstance(session, InteractiveSession)
        assert isinstance(session, ClaudeSession)

    def test_session_for_interactive_is_reused(self, tmp_path):
        """Interactive sessions are long-lived per project — the manager
        returns the SAME instance across calls so the tmux window and the
        `claude` context persist between cards (unlike fresh PrintSessions)."""
        config = _config(runner="interactive")
        manager = SessionManager(
            config=config, runner=_runner(), state=StateManager(str(tmp_path / "s.json")),
        )
        assert manager.session_for("demo") is manager.session_for("demo")

    @pytest.mark.asyncio
    async def test_shutdown_is_noop(self, tmp_path):
        """shutdown() is awaitable and a no-op while only print mode exists —
        PrintSession owns no resources to tear down."""
        manager = SessionManager(
            config=_config(), runner=_runner(),
            state=StateManager(str(tmp_path / "s.json")),
        )
        assert await manager.shutdown() is None


class TestPrintSessionRunTask:
    """PrintSession.run_task delegates to ClaudeRunner.run unchanged."""

    @pytest.mark.asyncio
    async def test_passes_byte_identical_kwargs(self, tmp_path):
        """run_task must hand ClaudeRunner.run exactly the arguments the
        old __main__.py call site did — this is the zero-behaviour-change
        gate for M1."""
        config = _config()
        state = StateManager(str(tmp_path / "s.json"))
        state.set_session("demo", "existing-sess", last_card_id="prev-card")
        runner = _runner()
        card = _card()
        cb = lambda line: None

        session = PrintSession(
            project="demo", runner=runner, config=config, state=state,
        )
        await session.run_task(card, timeout=999, output_callback=cb)

        runner.run.assert_awaited_once()
        kwargs = runner.run.await_args.kwargs
        assert kwargs == {
            "card": card,
            "project": "demo",
            "session_id": "existing-sess",
            "working_dir": config.get_working_dir("demo"),
            "last_card_id": "prev-card",
            "compact_prompt": config.get_compact_prompt("demo"),
            "output_callback": cb,
            "browser_enabled": config.is_browser_enabled("demo"),
            "mcp_config_json": config.patchright_mcp_config_json(),
            "timeout": 999,
        }

    @pytest.mark.asyncio
    async def test_state_session_beats_config_session(self, tmp_path):
        """A session id in state.json wins over the config-file initial id."""
        config = _config(session_id="config-sess")
        state = StateManager(str(tmp_path / "s.json"))
        state.set_session("demo", "state-sess")
        runner = _runner()

        session = PrintSession(
            project="demo", runner=runner, config=config, state=state,
        )
        await session.run_task(_card(), timeout=1)

        assert runner.run.await_args.kwargs["session_id"] == "state-sess"

    @pytest.mark.asyncio
    async def test_falls_back_to_config_session(self, tmp_path):
        """With no session in state, run_task uses the config-file id."""
        config = _config(session_id="config-sess")
        state = StateManager(str(tmp_path / "s.json"))
        runner = _runner()

        session = PrintSession(
            project="demo", runner=runner, config=config, state=state,
        )
        await session.run_task(_card(), timeout=1)

        assert runner.run.await_args.kwargs["session_id"] == "config-sess"

    @pytest.mark.asyncio
    async def test_persists_new_session_id(self, tmp_path):
        """After a successful run, the returned session id is persisted to
        state keyed by the card just processed — exactly as the old call
        site did."""
        config = _config()
        state = StateManager(str(tmp_path / "s.json"))
        runner = _runner(
            ClaudeResult(success=True, session_id="fresh-sess", summary="", output="")
        )

        session = PrintSession(
            project="demo", runner=runner, config=config, state=state,
        )
        card = _card()
        await session.run_task(card, timeout=1)

        assert state.get_session("demo") == "fresh-sess"
        assert state.get_last_card_id("demo") == card.id

    @pytest.mark.asyncio
    async def test_does_not_persist_when_no_session_id(self, tmp_path):
        """A result with no session id leaves state untouched."""
        config = _config()
        state = StateManager(str(tmp_path / "s.json"))
        runner = _runner(
            ClaudeResult(success=True, session_id=None, summary="", output="")
        )

        session = PrintSession(
            project="demo", runner=runner, config=config, state=state,
        )
        await session.run_task(_card(), timeout=1)

        assert state.get_session("demo") is None

    @pytest.mark.asyncio
    async def test_returns_runner_result(self, tmp_path):
        """run_task returns the ClaudeResult produced by the runner."""
        expected = ClaudeResult(
            success=True, session_id="s", summary="done", output="out",
        )
        session = PrintSession(
            project="demo", runner=_runner(expected), config=_config(),
            state=StateManager(str(tmp_path / "s.json")),
        )
        result = await session.run_task(_card(), timeout=1)
        assert result is expected

    @pytest.mark.asyncio
    async def test_propagates_runner_errors(self, tmp_path):
        """Errors from ClaudeRunner.run propagate unchanged so the polling
        loop's MonthlyLimitError / retry-backoff handling still fires."""
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=RuntimeError("boom"))
        session = PrintSession(
            project="demo", runner=runner, config=_config(),
            state=StateManager(str(tmp_path / "s.json")),
        )
        with pytest.raises(RuntimeError, match="boom"):
            await session.run_task(_card(), timeout=1)
