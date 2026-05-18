"""Tests for the interactive `claude` transport backend (trellm/session.py).

docs/claude-interactive.md M4 wires the M2 tmux module and the M3 completion
detector into `InteractiveSession` — a `ClaudeSession` that owns one
long-lived `claude` TUI per project. These tests pin M4's behaviour with
`tmux.py` and the M3 detector mocked (the M4 gate):

  * Window lifecycle (§6.1) — create on first card, reuse a live window on
    restart, `/compact` as its own turn between cards.
  * Task dispatch (§6.1/§8) — prompt written to a file, then `send-keys -l`
    the one-line "Read <file>" instruction followed by a separate Enter.
  * The §4 confirmation stack — COMPLETED / STOPPED_EARLY / TIMED_OUT each
    mapped to the right `ClaudeResult` / exception.
  * Error detection (§6.3) — `_check_for_errors` run over the transcript
    text, INCLUDING the gotcha #8 regression: rate-limit / monthly-limit
    detection must survive the move off subprocess stderr.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trellm.claude import ClaudeResult, ClaudeRunner, MonthlyLimitError, RateLimitError
from trellm.completion import CompletionOutcome, CompletionResult, sentinel_marker
from trellm.config import ClaudeConfig
from trellm.session import ClaudeSession, InteractiveSession, _pane_delta
from trellm.state import StateManager
from trellm.trello import TrelloCard

# A binary name that cannot resolve on PATH — forces every SignalWatcher the
# session builds onto its poll fallback, so pre-compact waits are fast and
# deterministic whether or not inotify-tools is installed (mirrors
# test_completion.py's NO_INOTIFY).
NO_INOTIFY = "trellm-definitely-no-such-inotify-binary"


def _runner() -> ClaudeRunner:
    """A real ClaudeRunner — InteractiveSession reuses its `_build_prompt`
    and `_check_for_errors`, so a mock would not exercise either."""
    return ClaudeRunner(
        ClaudeConfig(binary="claude", timeout=60), ready_list_id="ready-list-id"
    )


def _card(card_id: str = "card-1", name: str = "demo do a thing") -> TrelloCard:
    return TrelloCard(
        id=card_id,
        name=name,
        description="",
        url=f"https://trello.com/c/{card_id}",
        last_activity="2026-05-18T00:00:00Z",
    )


def _tmux(windows: list[str] | None = None) -> MagicMock:
    """A TmuxController stand-in: every method is an AsyncMock."""
    t = MagicMock()
    t.create_session = AsyncMock(return_value=True)
    t.list_windows = AsyncMock(return_value=list(windows or []))
    t.create_window = AsyncMock()
    t.send_literal = AsyncMock()
    t.send_keys = AsyncMock()
    t.capture_pane = AsyncMock(return_value="")
    t.kill_window = AsyncMock()
    return t


def _session(
    tmp_path,
    *,
    tmux: MagicMock | None = None,
    windows: list[str] | None = None,
    runner: ClaudeRunner | None = None,
    state: StateManager | None = None,
) -> InteractiveSession:
    return InteractiveSession(
        project="demo",
        working_dir=str(tmp_path / "src" / "demo"),
        runner=runner or _runner(),
        state=state or StateManager(str(tmp_path / "state.json")),
        tmux=tmux or _tmux(windows),
        interactive_dir=str(tmp_path / "interactive"),
        startup_delay=0.0,
        compact_timeout=0.05,
        pane_poll_interval=0.01,
        inotify_binary=NO_INOTIFY,
    )


def _completion(
    outcome: CompletionOutcome,
    *,
    session_id: str | None = "sess-1",
    transcript_path=None,
    summary: str = "did the thing",
    tokens: dict | None = None,
) -> CompletionResult:
    return CompletionResult(
        outcome=outcome,
        session_id=session_id,
        signal_time="2026-05-18T00:00:00Z",
        transcript_path=transcript_path,
        summary=summary,
        tokens=tokens or {},
    )


def _detect(result: CompletionResult, *, delay: float = 0.0) -> AsyncMock:
    """An AsyncMock to patch in for trellm.session.detect_completion."""
    if delay:

        async def fake(**kwargs):
            await asyncio.sleep(delay)
            return result

        return AsyncMock(side_effect=fake)
    return AsyncMock(return_value=result)


def _assistant_line(text: str, *, usage: dict | None = None) -> str:
    """A Claude Code transcript JSONL line for an assistant text turn.

    `ensure_ascii=False` mirrors real (Node-written) transcripts."""
    message: dict = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if usage is not None:
        message["usage"] = usage
    return json.dumps({"type": "assistant", "message": message}, ensure_ascii=False)


def _write_transcript(path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n")


class TestPaneDelta:
    """`_pane_delta` — the periodic capture-pane diff feeding output_callback."""

    def test_unchanged_pane_yields_nothing(self):
        assert _pane_delta("abc", "abc") == ""

    def test_appended_text_yields_only_the_tail(self):
        assert _pane_delta("abc", "abcdef") == "def"

    def test_redrawn_pane_yields_whole_screen(self):
        assert _pane_delta("abc", "xyz") == "xyz"

    def test_empty_baseline_yields_whole_screen(self):
        assert _pane_delta("", "hello") == "hello"


class TestWindowLifecycle:
    """§6.1 — create on first card, reuse a live window on restart."""

    @pytest.mark.asyncio
    async def test_first_card_creates_session_and_window(self, tmp_path):
        tmux = _tmux(windows=[])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card(), timeout=60)
        tmux.create_session.assert_awaited()
        tmux.create_window.assert_awaited_once()
        assert tmux.create_window.await_args.args[0] == "demo"

    @pytest.mark.asyncio
    async def test_window_command_has_continue_with_fallback(self, tmp_path):
        """§6.1: the window runs `claude --continue ... || claude ...` so it
        resumes the prior session, falling back to a fresh one first-ever."""
        tmux = _tmux(windows=[])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card(), timeout=60)
        command = tmux.create_window.await_args.kwargs["command"]
        assert "claude --continue --dangerously-skip-permissions" in command
        assert "|| claude --dangerously-skip-permissions" in command

    @pytest.mark.asyncio
    async def test_existing_window_is_reused(self, tmp_path):
        """Restart path: a live window for the project is reused, not
        recreated, so in-flight context survives."""
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card(), timeout=60)
        tmux.create_window.assert_not_awaited()


class TestPreCompact:
    """§6.1 — `/compact` is dispatched as its own turn between cards."""

    @pytest.mark.asyncio
    async def test_compact_dispatched_between_different_cards(self, tmp_path):
        state = StateManager(str(tmp_path / "state.json"))
        state.set_session("demo", "old-sess", last_card_id="prev-card")
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux, state=state)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card("card-1"), timeout=60)
        literals = [c.args[1] for c in tmux.send_literal.await_args_list]
        assert "/compact" in literals

    @pytest.mark.asyncio
    async def test_no_compact_on_first_card(self, tmp_path):
        """No prior card ⇒ nothing to compact."""
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card("card-1"), timeout=60)
        literals = [c.args[1] for c in tmux.send_literal.await_args_list]
        assert "/compact" not in literals

    @pytest.mark.asyncio
    async def test_no_compact_when_retrying_same_card(self, tmp_path):
        """A retry of the SAME card must keep its context — no `/compact`."""
        state = StateManager(str(tmp_path / "state.json"))
        state.set_session("demo", "old-sess", last_card_id="card-1")
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux, state=state)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card("card-1"), timeout=60)
        literals = [c.args[1] for c in tmux.send_literal.await_args_list]
        assert "/compact" not in literals


class TestTaskDispatch:
    """§6.1/§8 — prompt written to a file, dispatched via send-keys."""

    @pytest.mark.asyncio
    async def test_task_file_written_with_sentinel(self, tmp_path):
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card("card-1"), timeout=60)
        task_file = tmp_path / "interactive" / "tasks" / "card-1.md"
        assert task_file.exists()
        content = task_file.read_text()
        assert sentinel_marker("card-1") in content
        assert "card-1" in content

    @pytest.mark.asyncio
    async def test_dispatch_types_read_instruction_then_enter(self, tmp_path):
        """The one-line instruction is typed literally, then submitted with
        a SEPARATE Enter — never a multiline prompt typed directly."""
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED)),
        ):
            await session.run_task(_card("card-1"), timeout=60)
        literals = [c.args[1] for c in tmux.send_literal.await_args_list]
        instruction = next(s for s in literals if s.startswith("Read "))
        assert "card-1.md" in instruction
        assert "complete the task" in instruction
        keys = [c.args[1:] for c in tmux.send_keys.await_args_list]
        assert ("Enter",) in keys


class TestCompletionOutcomes:
    """§4 confirmation stack — each outcome to its result / exception."""

    @pytest.mark.asyncio
    async def test_completed_returns_success_result(self, tmp_path):
        session = _session(tmp_path, windows=["demo"])
        completion = _completion(
            CompletionOutcome.COMPLETED,
            session_id="post-compact-sess",
            summary="shipped the feature",
            tokens={"input_tokens": 100, "output_tokens": 40},
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            result = await session.run_task(_card(), timeout=60)
        assert isinstance(result, ClaudeResult)
        assert result.success is True
        assert result.session_id == "post-compact-sess"
        assert result.summary == "shipped the feature"

    @pytest.mark.asyncio
    async def test_completed_carries_tokens_in_cost_info(self, tmp_path):
        session = _session(tmp_path, windows=["demo"])
        completion = _completion(
            CompletionOutcome.COMPLETED,
            tokens={
                "input_tokens": 111,
                "output_tokens": 22,
                "cache_creation_input_tokens": 33,
                "cache_read_input_tokens": 44,
            },
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            result = await session.run_task(_card(), timeout=60)
        assert result.cost_info is not None
        assert result.cost_info.input_tokens == 111
        assert result.cost_info.output_tokens == 22
        assert result.cost_info.cache_creation_tokens == 33
        assert result.cost_info.cache_read_tokens == 44

    @pytest.mark.asyncio
    async def test_completed_persists_session_id_and_card(self, tmp_path):
        state = StateManager(str(tmp_path / "state.json"))
        session = _session(tmp_path, windows=["demo"], state=state)
        completion = _completion(CompletionOutcome.COMPLETED, session_id="new-sess")
        with patch("trellm.session.detect_completion", _detect(completion)):
            await session.run_task(_card("card-1"), timeout=60)
        assert state.get_session("demo") == "new-sess"
        assert state.get_last_card_id("demo") == "card-1"

    @pytest.mark.asyncio
    async def test_timed_out_raises_and_interrupts_pane(self, tmp_path):
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        completion = _completion(CompletionOutcome.TIMED_OUT, session_id=None)
        with patch("trellm.session.detect_completion", _detect(completion)):
            with pytest.raises(RuntimeError, match="timed out after"):
                await session.run_task(_card(), timeout=60)
        # §4 backstop: the runaway turn is interrupted (Escape, then C-c).
        sent = [c.args[1:] for c in tmux.send_keys.await_args_list]
        assert ("Escape",) in sent
        assert ("C-c",) in sent

    @pytest.mark.asyncio
    async def test_stopped_early_clean_transcript_raises_runtimeerror(self, tmp_path):
        """STOPPED_EARLY with no error pattern in the transcript ⇒ a plain
        failure: the card stays in TODO with a retry-context comment, as in
        print mode. It must NOT be a usage-limit error."""
        session = _session(tmp_path, windows=["demo"])
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(transcript, _assistant_line("I need more information."))
        completion = _completion(
            CompletionOutcome.STOPPED_EARLY, transcript_path=transcript
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            with pytest.raises(RuntimeError) as exc:
                await session.run_task(_card(), timeout=60)
        assert not isinstance(exc.value, (MonthlyLimitError, RateLimitError))

    @pytest.mark.asyncio
    async def test_stopped_early_persists_session_id_before_raising(self, tmp_path):
        """Even a failed turn advanced the live window's session — persist
        the id so a restart resumes the right session."""
        state = StateManager(str(tmp_path / "state.json"))
        session = _session(tmp_path, windows=["demo"], state=state)
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(transcript, _assistant_line("Stuck."))
        completion = _completion(
            CompletionOutcome.STOPPED_EARLY,
            session_id="stopped-sess",
            transcript_path=transcript,
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            with pytest.raises(RuntimeError):
                await session.run_task(_card("card-1"), timeout=60)
        assert state.get_session("demo") == "stopped-sess"


class TestErrorDetectionOverTranscript:
    """§6.3 — `_check_for_errors` regexes run over the transcript text.

    CRITICAL (CLAUDE.md gotcha #8): moving error detection off subprocess
    stderr must not lose rate-limit / monthly-limit detection — that is what
    pauses the global polling loop."""

    @pytest.mark.asyncio
    async def test_monthly_limit_in_transcript_raises_monthly_limit_error(
        self, tmp_path
    ):
        """The gotcha #8 regression test: a turn that stopped because of the
        org/monthly usage limit must still raise MonthlyLimitError so
        __main__.py applies the global pause."""
        session = _session(tmp_path, windows=["demo"])
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(
            transcript,
            _assistant_line("Starting the task..."),
            _assistant_line("You've hit your org's monthly usage limit."),
        )
        completion = _completion(
            CompletionOutcome.STOPPED_EARLY, transcript_path=transcript
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            with pytest.raises(MonthlyLimitError):
                await session.run_task(_card(), timeout=60)

    @pytest.mark.asyncio
    async def test_rate_limit_in_transcript_raises_rate_limit_error(self, tmp_path):
        session = _session(tmp_path, windows=["demo"])
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(
            transcript,
            _assistant_line("API call failed: rate_limit_error"),
        )
        completion = _completion(
            CompletionOutcome.STOPPED_EARLY, transcript_path=transcript
        )
        with patch("trellm.session.detect_completion", _detect(completion)):
            with pytest.raises(RateLimitError):
                await session.run_task(_card(), timeout=60)


class TestPaneStreaming:
    """§6.3 — output_callback fed by a periodic capture-pane diff."""

    @pytest.mark.asyncio
    async def test_output_callback_receives_pane_text(self, tmp_path):
        tmux = _tmux(windows=["demo"])
        tmux.capture_pane = AsyncMock(return_value="claude is working...")
        session = _session(tmp_path, tmux=tmux)
        received: list[str] = []
        completion = _completion(CompletionOutcome.COMPLETED)
        # The detector takes a beat so the streamer ticks at least once.
        with patch(
            "trellm.session.detect_completion", _detect(completion, delay=0.08)
        ):
            await session.run_task(
                _card(), timeout=60, output_callback=received.append
            )
        assert "claude is working..." in received

    @pytest.mark.asyncio
    async def test_no_pane_capture_without_callback(self, tmp_path):
        """With no output_callback there is no dashboard consumer — the
        capture-pane poll must not run."""
        tmux = _tmux(windows=["demo"])
        session = _session(tmp_path, tmux=tmux)
        with patch(
            "trellm.session.detect_completion",
            _detect(_completion(CompletionOutcome.COMPLETED), delay=0.05),
        ):
            await session.run_task(_card(), timeout=60)
        tmux.capture_pane.assert_not_awaited()


class TestClaudeSessionProtocol:
    """InteractiveSession is a drop-in ClaudeSession."""

    def test_satisfies_claude_session_protocol(self, tmp_path):
        assert isinstance(_session(tmp_path), ClaudeSession)
