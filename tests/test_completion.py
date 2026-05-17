"""Tests for the interactive-mode completion detector (trellm/completion.py)
and its Stop hook (scripts/trellm-stop-hook.sh).

docs/claude-interactive.md M3 builds the explicit "task is over" detector
the interactive (tmux TUI) transport needs: in print mode subprocess exit
*is* the completion signal, but the interactive `claude` process is
long-lived per project, so completion needs the layered §4 stack:

  * Stop hook (primary trigger) — a `Stop` hook script appends
    `<session_id> <iso8601>` to `~/.trellm/interactive/<project>.signal`.
  * Sentinel marker (confirmation) — `⟦TRELLM-DONE cardId=<id>⟧` in the
    transcript distinguishes genuine completion from a turn that stopped
    to ask a question.
  * Wall-clock timeout (backstop).
  * Trello card list (ground truth) — stays in the polling loop, not here.

These tests pin every piece in isolation with canned signal files, canned
transcripts, and both sentinel-present and sentinel-absent fixtures — the
M3 gate. No load-bearing signal scrapes the rendered TUI pane.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from trellm.completion import (
    INTERACTIVE_DIR,
    SENTINEL_PREFIX,
    SENTINEL_SUFFIX,
    CompletionOutcome,
    CompletionResult,
    SignalEntry,
    SignalWatcher,
    detect_completion,
    parse_signal_file,
    read_transcript_summary,
    sentinel_marker,
    signal_path,
    transcript_has_sentinel,
    transcript_path_resolver,
)

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
STOP_HOOK = SCRIPTS_DIR / "trellm-stop-hook.sh"

# Force the watcher onto its poll fallback in unit tests: a binary name
# that cannot resolve on PATH makes `uses_inotify` False deterministically,
# regardless of whether inotify-tools happens to be installed in CI.
NO_INOTIFY = "trellm-definitely-no-such-inotify-binary"


def _assistant_line(text: str, *, usage: dict | None = None) -> str:
    """One Claude Code transcript JSONL line for an assistant text turn.

    `ensure_ascii=False` mirrors real Claude Code transcripts, which write
    literal UTF-8 — so the sentinel's `⟦`/`⟧` appear verbatim, not as
    `\\u27e6` escapes.
    """
    message: dict = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if usage is not None:
        message["usage"] = usage
    return json.dumps({"type": "assistant", "message": message}, ensure_ascii=False)


def _user_line(text: str) -> str:
    """One transcript JSONL line for a user turn (e.g. the dispatched prompt)."""
    return json.dumps(
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    )


def _transcript(
    *, card_id: str, with_sentinel: bool, summary: str = "did the thing"
) -> str:
    """A canned transcript: the dispatched prompt (a *user* turn that
    quotes the sentinel as an instruction) followed by an assistant turn
    that does — or does not — actually emit the sentinel."""
    text = summary
    if with_sentinel:
        text = f"{summary}\n\n{sentinel_marker(card_id)}"
    lines = [
        # The prompt instructs Claude to print the sentinel last. This is a
        # USER turn — it must never be mistaken for the agent emitting it.
        _user_line(f"Read the task ... Output exactly this line last: "
                   f"{sentinel_marker(card_id)}"),
        _assistant_line(
            text,
            usage={
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 50,
            },
        ),
    ]
    return "\n".join(lines) + "\n"


class TestSentinelMarker:
    """The sentinel is the exact line the dispatched prompt asks `claude`
    to print last (doc §4 / §8). Its wording is load-bearing — M4 builds
    the prompt from it and scans the transcript for it."""

    def test_marker_is_documented_form(self):
        assert sentinel_marker("card-123") == "⟦TRELLM-DONE cardId=card-123⟧"

    def test_marker_wraps_card_id_in_prefix_and_suffix(self):
        marker = sentinel_marker("abc")
        assert marker.startswith(SENTINEL_PREFIX)
        assert marker.endswith(SENTINEL_SUFFIX)
        assert "abc" in marker


class TestSignalPath:
    """Each interactive project has one signal file the Stop hook appends
    to and the watcher reads — `~/.trellm/interactive/<project>.signal`."""

    def test_default_path_is_under_interactive_dir(self):
        path = signal_path("smugcoin")
        assert path == Path(INTERACTIVE_DIR).expanduser() / "smugcoin.signal"
        # `~` must be expanded — the watcher opens this path directly.
        assert "~" not in str(path)

    def test_base_dir_override(self, tmp_path):
        path = signal_path("demo", base_dir=str(tmp_path))
        assert path == tmp_path / "demo.signal"


class TestParseSignalFile:
    """The signal file is append-only lines of `<session_id> <iso8601>`."""

    def test_parses_well_formed_lines_in_order(self, tmp_path):
        sig = tmp_path / "demo.signal"
        sig.write_text(
            "sess-a 2026-05-17T10:00:00Z\nsess-b 2026-05-17T11:30:00Z\n"
        )
        entries = parse_signal_file(sig)
        assert entries == [
            SignalEntry(session_id="sess-a", timestamp="2026-05-17T10:00:00Z"),
            SignalEntry(session_id="sess-b", timestamp="2026-05-17T11:30:00Z"),
        ]

    def test_skips_blank_and_malformed_lines(self, tmp_path):
        sig = tmp_path / "demo.signal"
        sig.write_text(
            "\n"
            "garbage-one-token\n"
            "sess-good 2026-05-17T12:00:00Z\n"
            "   \n"
        )
        entries = parse_signal_file(sig)
        assert entries == [
            SignalEntry(session_id="sess-good", timestamp="2026-05-17T12:00:00Z"),
        ]

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert parse_signal_file(tmp_path / "never-created.signal") == []


class TestSignalWatcher:
    """The reusable signal-file watcher M4's InteractiveSession awaits.

    Strategy is watch + poll fallback: it returns the first entry
    appended *after* the watcher was constructed (the baseline), so a
    stale entry from a previous card is never mistaken for completion.
    """

    @pytest.mark.asyncio
    async def test_returns_entry_appended_after_construction(self, tmp_path):
        sig = tmp_path / "demo.signal"
        sig.write_text("stale-sess 2026-05-17T09:00:00Z\n")  # baseline
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)

        async def appender():
            await asyncio.sleep(0.1)
            with open(sig, "a") as f:
                f.write("new-sess 2026-05-17T11:00:00Z\n")

        task = asyncio.create_task(appender())
        entry = await watcher.wait(timeout=3)
        await task
        assert entry == SignalEntry(
            session_id="new-sess", timestamp="2026-05-17T11:00:00Z"
        )

    @pytest.mark.asyncio
    async def test_detects_entry_appended_before_wait_is_called(self, tmp_path):
        """The hook may fire before the caller gets to await — the entry
        must still be picked up by the up-front check."""
        sig = tmp_path / "demo.signal"
        sig.write_text("")
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)
        with open(sig, "a") as f:
            f.write("eager-sess 2026-05-17T11:00:00Z\n")
        entry = await watcher.wait(timeout=2)
        assert entry is not None and entry.session_id == "eager-sess"

    @pytest.mark.asyncio
    async def test_ignores_stale_entry_present_at_construction(self, tmp_path):
        """An entry already in the file when the watcher is built is the
        baseline — it must not be returned as this card's completion."""
        sig = tmp_path / "demo.signal"
        sig.write_text("stale-sess 2026-05-17T09:00:00Z\n")
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)
        assert await watcher.wait(timeout=0.3) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_signal_ever_appears(self, tmp_path):
        watcher = SignalWatcher(
            tmp_path / "absent.signal", poll_interval=0.05, inotify_binary=NO_INOTIFY
        )
        assert await watcher.wait(timeout=0.3) is None

    def test_uses_inotify_false_when_binary_absent(self, tmp_path):
        watcher = SignalWatcher(tmp_path / "x.signal", inotify_binary=NO_INOTIFY)
        assert watcher.uses_inotify is False

    def test_uses_inotify_true_when_binary_on_path(self, tmp_path):
        """The watch backend is gated on a real PATH probe — `ls` stands in
        for an inotify binary to prove it is not hard-coded False."""
        watcher = SignalWatcher(tmp_path / "x.signal", inotify_binary="ls")
        assert watcher.uses_inotify is True

    @pytest.mark.skipif(
        shutil.which("inotifywait") is None, reason="inotify-tools not installed"
    )
    @pytest.mark.asyncio
    async def test_wait_works_over_real_inotify_backend(self, tmp_path):
        """With polling effectively disabled (30s interval) and a 5s
        deadline, only the event-driven inotify backend can catch a
        signal appended 0.2s in."""
        sig = tmp_path / "demo.signal"
        sig.write_text("")
        watcher = SignalWatcher(sig, poll_interval=30.0)  # default inotify binary
        assert watcher.uses_inotify is True

        async def appender():
            await asyncio.sleep(0.2)
            with open(sig, "a") as f:
                f.write("evt-sess 2026-05-17T12:00:00Z\n")

        task = asyncio.create_task(appender())
        entry = await watcher.wait(timeout=5)
        await task
        assert entry is not None and entry.session_id == "evt-sess"


class TestTranscriptHasSentinel:
    """Confirmation step: the sentinel for *this* card must appear in an
    *assistant* turn of the transcript JSONL."""

    def test_true_when_assistant_emits_sentinel(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(_transcript(card_id="card-9", with_sentinel=True))
        assert transcript_has_sentinel(t, "card-9") is True

    def test_false_when_sentinel_absent(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(_transcript(card_id="card-9", with_sentinel=False))
        assert transcript_has_sentinel(t, "card-9") is False

    def test_sentinel_in_user_prompt_does_not_count(self, tmp_path):
        """The dispatched prompt quotes the sentinel as an instruction in a
        *user* turn. Only the agent actually printing it (an assistant
        turn) confirms completion — otherwise every dispatch self-confirms."""
        t = tmp_path / "sess.jsonl"
        t.write_text(_transcript(card_id="card-9", with_sentinel=False))
        # The fixture's user turn quotes sentinel_marker("card-9"); the
        # assistant turn does not emit it.
        assert sentinel_marker("card-9") in t.read_text()  # present as text...
        assert transcript_has_sentinel(t, "card-9") is False  # ...but not counted

    def test_wrong_card_id_does_not_match(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(_transcript(card_id="card-9", with_sentinel=True))
        assert transcript_has_sentinel(t, "card-OTHER") is False

    def test_malformed_lines_are_skipped(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(
            "not json at all\n"
            "{partial json\n"
            + _assistant_line(f"done {sentinel_marker('card-9')}")
            + "\n"
        )
        assert transcript_has_sentinel(t, "card-9") is True

    def test_missing_transcript_returns_false(self, tmp_path):
        assert transcript_has_sentinel(tmp_path / "nope.jsonl", "card-9") is False


class TestReadTranscriptSummary:
    """Doc §4: the transcript JSONL supplies the summary text."""

    def test_summary_is_final_assistant_text(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(
            _assistant_line("first turn")
            + "\n"
            + _assistant_line("the final summary")
            + "\n"
        )
        assert read_transcript_summary(t) == "the final summary"

    def test_summary_strips_the_sentinel_line(self, tmp_path):
        """The sentinel is a control marker, not prose — it must not leak
        into the summary shown on the dashboard / Trello."""
        t = tmp_path / "sess.jsonl"
        t.write_text(_transcript(card_id="card-9", with_sentinel=True))
        summary = read_transcript_summary(t)
        assert summary == "did the thing"
        assert sentinel_marker("card-9") not in summary

    def test_summary_empty_when_no_assistant_text(self, tmp_path):
        t = tmp_path / "sess.jsonl"
        t.write_text(_user_line("just a user message") + "\n")
        assert read_transcript_summary(t) == ""

    def test_summary_empty_when_transcript_missing(self, tmp_path):
        assert read_transcript_summary(tmp_path / "nope.jsonl") == ""


class TestTranscriptPathResolver:
    """M4 hands detect_completion a session-id → transcript-path resolver
    so the §4 stack has no knowledge of the ~/.claude/projects layout."""

    def test_returns_a_callable(self):
        assert callable(transcript_path_resolver("~/src/demo"))

    def test_resolves_to_none_for_unknown_session(self):
        # No working dir → no transcript can be located → None, not a crash.
        resolve = transcript_path_resolver(None)
        assert resolve("any-session-id") is None


class TestDetectCompletion:
    """The assembled §4 confirmation stack: wait for the Stop signal
    (bounded by the timeout), then confirm the sentinel in the transcript."""

    @pytest.mark.asyncio
    async def test_completed_when_signal_fires_and_sentinel_present(self, tmp_path):
        sig = tmp_path / "demo.signal"
        sig.write_text("")
        (tmp_path / "sess-1.jsonl").write_text(
            _transcript(card_id="card-9", with_sentinel=True)
        )
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)
        with open(sig, "a") as f:
            f.write("sess-1 2026-05-17T12:00:00Z\n")

        result = await detect_completion(
            card_id="card-9",
            watcher=watcher,
            timeout=2,
            resolve_transcript=lambda sid: tmp_path / f"{sid}.jsonl",
        )
        assert result.outcome is CompletionOutcome.COMPLETED
        assert result.completed is True
        assert result.session_id == "sess-1"
        assert result.signal_time == "2026-05-17T12:00:00Z"
        assert result.summary == "did the thing"
        # Transcript JSONL also supplies token usage (doc §4 / §6.3).
        assert result.tokens["input_tokens"] == 1200
        assert result.tokens["output_tokens"] == 340

    @pytest.mark.asyncio
    async def test_stopped_early_when_signal_fires_but_sentinel_absent(self, tmp_path):
        """Stop fires when a turn ends to ask a question, too — no sentinel
        means the turn stopped early, not that the task is done."""
        sig = tmp_path / "demo.signal"
        sig.write_text("")
        (tmp_path / "sess-2.jsonl").write_text(
            _transcript(card_id="card-9", with_sentinel=False)
        )
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)
        with open(sig, "a") as f:
            f.write("sess-2 2026-05-17T12:00:00Z\n")

        result = await detect_completion(
            card_id="card-9",
            watcher=watcher,
            timeout=2,
            resolve_transcript=lambda sid: tmp_path / f"{sid}.jsonl",
        )
        assert result.outcome is CompletionOutcome.STOPPED_EARLY
        assert result.completed is False
        assert result.session_id == "sess-2"

    @pytest.mark.asyncio
    async def test_timed_out_when_no_signal_within_budget(self, tmp_path):
        """The wall-clock timeout is the backstop — no Stop signal in
        budget yields TIMED_OUT and no session id."""
        sig = tmp_path / "demo.signal"
        sig.write_text("")
        watcher = SignalWatcher(sig, poll_interval=0.05, inotify_binary=NO_INOTIFY)

        result = await detect_completion(
            card_id="card-9",
            watcher=watcher,
            timeout=0.3,
            resolve_transcript=lambda sid: tmp_path / f"{sid}.jsonl",
        )
        assert result.outcome is CompletionOutcome.TIMED_OUT
        assert result.completed is False
        assert result.session_id is None


class TestStopHookScriptStructure:
    """Static-structure checks on scripts/trellm-stop-hook.sh, in the
    style of tests/test_browser_scripts.py."""

    def test_script_exists(self):
        assert STOP_HOOK.exists(), f"missing: {STOP_HOOK}"

    def test_script_is_executable(self):
        assert os.access(STOP_HOOK, os.X_OK)

    def test_script_has_bash_shebang(self):
        assert STOP_HOOK.read_text().startswith("#!/usr/bin/env bash")

    def test_script_uses_strict_mode(self):
        assert "set -euo pipefail" in STOP_HOOK.read_text()

    def test_script_passes_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(STOP_HOOK)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


class TestStopHookScriptBehaviour:
    """The Stop hook script run for real: feed it canned hook JSON on
    stdin and assert the signal-file line it appends."""

    @staticmethod
    def _run(payload: dict, interactive_dir: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "TRELLM_INTERACTIVE_DIR": str(interactive_dir)},
        )

    def test_appends_session_id_and_iso_timestamp(self, tmp_path):
        proc = self._run(
            {
                "session_id": "abc-123",
                "cwd": "/home/user/src/myproj",
                "transcript_path": "/home/user/.claude/projects/x/abc-123.jsonl",
            },
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        signal = tmp_path / "myproj.signal"
        assert signal.exists()
        line = signal.read_text().strip()
        # `<session_id> <iso8601-utc>` — the contract M4 / doc §8 depend on.
        assert re.fullmatch(
            r"abc-123 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line
        ), f"unexpected signal line: {line!r}"

    def test_derives_project_from_cwd_basename(self, tmp_path):
        """Window name == project name == working-dir basename (doc §6.1)."""
        self._run({"session_id": "s1", "cwd": "/srv/repos/jcapp"}, tmp_path)
        assert (tmp_path / "jcapp.signal").exists()
        assert not (tmp_path / "repos.signal").exists()

    def test_appends_rather_than_overwrites(self, tmp_path):
        self._run({"session_id": "s1", "cwd": "/x/demo"}, tmp_path)
        self._run({"session_id": "s2", "cwd": "/x/demo"}, tmp_path)
        lines = (tmp_path / "demo.signal").read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].split()[0] == "s1"
        assert lines[1].split()[0] == "s2"

    def test_creates_interactive_dir_if_absent(self, tmp_path):
        nested = tmp_path / "does" / "not" / "exist"
        proc = self._run({"session_id": "s1", "cwd": "/x/demo"}, nested)
        assert proc.returncode == 0, proc.stderr
        assert (nested / "demo.signal").exists()

    def test_exits_nonzero_on_missing_fields(self, tmp_path):
        """A payload with no session_id / cwd is a real error — the hook
        fails loudly and writes nothing rather than a malformed line."""
        proc = self._run({}, tmp_path)
        assert proc.returncode != 0
        assert list(tmp_path.glob("*.signal")) == []

    def test_writes_one_signal_per_project(self, tmp_path):
        self._run({"session_id": "s1", "cwd": "/x/alpha"}, tmp_path)
        self._run({"session_id": "s2", "cwd": "/x/beta"}, tmp_path)
        assert (tmp_path / "alpha.signal").exists()
        assert (tmp_path / "beta.signal").exists()
