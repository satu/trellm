"""Completion detection for the interactive `claude` transport.

`docs/claude-interactive.md` plans a second `claude` transport — a long-lived
interactive TUI per project (M4's `InteractiveSession`) — so cards stay on the
subscription seat instead of becoming metered `claude -p` calls. In print mode
the subprocess *exiting* is the completion signal; the interactive process is
long-lived, so "the process exited" no longer means "task done". M3 (this
module) builds the explicit detector that replaces subprocess exit.

The layered §4 strategy, in order of authority — no load-bearing signal
scrapes the rendered TUI pane:

  1. **Stop hook** (primary trigger). `scripts/trellm-stop-hook.sh`, registered
     in each interactive project's `.claude/settings.json`, appends
     `<session_id> <iso8601>` to `~/.trellm/interactive/<project>.signal` every
     time `claude` finishes a turn. `SignalWatcher` awaits that append.
  2. **Sentinel marker** (confirmation). The dispatched prompt ends with
     `⟦TRELLM-DONE cardId=<id>⟧`; `transcript_has_sentinel` confirms `claude`
     actually printed it, distinguishing genuine completion from a turn that
     stopped to ask a clarifying question (which also fires `Stop`).
  3. **Wall-clock timeout** (backstop). `detect_completion(timeout=…)` bounds
     the whole wait; M4 passes `Config.get_timeout(project)`.
  4. **Trello card list** (ground truth for success vs. fail). Stays in the
     polling loop — out of scope here.

The transcript JSONL also supplies the summary text and token usage, reusing
`claude._get_session_jsonl_path` / `claude._read_token_usage_from_jsonl` rather
than adding a new parser (doc §6.3).

M4 wires this into `InteractiveSession`; M3 ships the pieces, unit-tested in
isolation with canned signal files and transcripts.
"""

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional

from .claude import _get_session_jsonl_path, _read_token_usage_from_jsonl

logger = logging.getLogger(__name__)

# Per-project Stop-hook signal files live here (doc §6.2).
INTERACTIVE_DIR = "~/.trellm/interactive"

# The sentinel marker the dispatched prompt asks `claude` to print last.
# Wording is the portability contract (doc §8) — a Node port copies it
# verbatim. `sentinel_marker(card_id)` builds the full line.
SENTINEL_PREFIX = "⟦TRELLM-DONE cardId="
SENTINEL_SUFFIX = "⟧"


def sentinel_marker(card_id: str) -> str:
    """The exact line `claude` must print last to confirm completion of `card_id`."""
    return f"{SENTINEL_PREFIX}{card_id}{SENTINEL_SUFFIX}"


def signal_path(project: str, *, base_dir: Optional[str] = None) -> Path:
    """Path of `project`'s Stop-hook signal file.

    Defaults to `~/.trellm/interactive/<project>.signal`; `base_dir`
    overrides the directory (used by tests and any non-default state dir).
    """
    directory = Path(base_dir).expanduser() if base_dir else Path(INTERACTIVE_DIR).expanduser()
    return directory / f"{project}.signal"


@dataclass(frozen=True)
class SignalEntry:
    """One line of a signal file: a Stop-hook firing.

    `session_id` is `claude`'s session id *as of that turn* — it rotates
    after `/compact`, so this is also how M4 captures the new id (doc §6.1).
    `timestamp` is the ISO-8601 UTC instant the hook wrote, kept verbatim.
    """

    session_id: str
    timestamp: str


def parse_signal_file(path) -> list[SignalEntry]:
    """Parse a signal file into `SignalEntry` objects, in file order.

    The file is append-only lines of `<session_id> <iso8601>`. Blank and
    malformed lines are skipped; a missing file yields an empty list — the
    expected state before the first turn ever finishes.
    """
    try:
        text = Path(path).read_text()
    except (OSError, IOError):
        return []
    entries: list[SignalEntry] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            entries.append(SignalEntry(session_id=parts[0], timestamp=parts[1]))
    return entries


class SignalWatcher:
    """Awaits a newly appended entry in a project's Stop-hook signal file.

    Construct the watcher *before* dispatching a prompt: construction
    snapshots the current entry count as the baseline, so `wait` returns
    only an entry appended afterwards — a stale entry from a previous card
    is never mistaken for this card's completion.

    Resolution is **watch + poll fallback** (doc §6.2): when `inotifywait`
    (inotify-tools) is on `PATH`, each wait blocks on a real filesystem
    event for near-zero latency; otherwise it polls the file every
    `poll_interval` seconds. Both backends resolve identically — re-read
    the signal file and return the first entry beyond the baseline.
    """

    def __init__(
        self,
        path,
        *,
        poll_interval: float = 1.0,
        inotify_binary: str = "inotifywait",
    ):
        self._path = Path(path)
        self._poll_interval = poll_interval
        self._inotify_binary = inotify_binary
        self._baseline = len(parse_signal_file(self._path))

    @property
    def uses_inotify(self) -> bool:
        """True when the event-driven watch backend is available; False
        means this watcher polls."""
        return shutil.which(self._inotify_binary) is not None

    def _new_entry(self) -> Optional[SignalEntry]:
        """The first signal entry appended since construction, if any."""
        entries = parse_signal_file(self._path)
        if len(entries) > self._baseline:
            return entries[self._baseline]
        return None

    async def wait(self, *, timeout: float) -> Optional[SignalEntry]:
        """Block until a new signal entry appears, or `timeout` elapses.

        Returns the first entry appended after construction, or None on
        timeout (the §4 wall-clock backstop applies upstream too).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # The hook may have fired already — check before sleeping at all.
        entry = self._new_entry()
        if entry is not None:
            return entry
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await self._sleep_until_change(remaining)
            entry = self._new_entry()
            if entry is not None:
                return entry

    async def _sleep_until_change(self, budget: float) -> None:
        """Wait for a possible change to the signal file, capped at `budget`
        seconds. Uses the inotify backend when available, else a poll tick."""
        if self.uses_inotify:
            try:
                await self._inotify_wait(budget)
                return
            except Exception as e:  # noqa: BLE001 — any failure ⇒ poll fallback
                logger.debug("inotify watch failed (%s); falling back to poll", e)
        await asyncio.sleep(min(self._poll_interval, budget))

    async def _inotify_wait(self, budget: float) -> None:
        """Block on a single filesystem event via `inotifywait`.

        Watches the parent directory when the signal file does not exist
        yet (the cold-start case) so the file's *creation* is caught too.
        """
        target = self._path if self._path.exists() else self._path.parent
        proc = await asyncio.create_subprocess_exec(
            self._inotify_binary,
            "-q",
            "-e", "modify",
            "-e", "create",
            "-e", "moved_to",
            "-t", str(max(1, int(budget))),
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


def _iter_assistant_text(transcript_path) -> Iterator[str]:
    """Yield the text of every assistant text block in a Claude Code
    transcript JSONL file.

    Only `type: "assistant"` turns are considered: the dispatched prompt
    quotes the sentinel as an *instruction* in a `user` turn, and counting
    that would make every dispatch self-confirm. A missing file or a
    malformed line is skipped silently — to the §4 stack, "no transcript"
    and "transcript without the sentinel" are the same answer.
    """
    try:
        text = Path(transcript_path).read_text()
    except (OSError, IOError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "assistant":
            continue
        content = data.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                block = item.get("text", "")
                if block:
                    yield block


def transcript_has_sentinel(transcript_path, card_id: str) -> bool:
    """True when `claude` printed the sentinel for `card_id` in the transcript.

    This is the §4 confirmation step: it distinguishes a genuine completion
    from a turn that fired `Stop` only to ask a clarifying question.
    """
    marker = sentinel_marker(card_id)
    return any(marker in block for block in _iter_assistant_text(transcript_path))


def _strip_sentinel_line(text: str) -> str:
    """Drop any sentinel line from `text` — it is a control marker, not prose."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith(SENTINEL_PREFIX)
    )


def read_transcript_summary(transcript_path) -> str:
    """The task summary: the final assistant text block, sentinel removed.

    Empty string when the transcript has no assistant text or is missing.
    """
    blocks = list(_iter_assistant_text(transcript_path))
    if not blocks:
        return ""
    return _strip_sentinel_line(blocks[-1]).strip()


def transcript_path_resolver(working_dir: Optional[str]) -> Callable[[str], Optional[Path]]:
    """Build a `session_id -> transcript JSONL path` resolver for a project.

    M4 passes the result to `detect_completion`, keeping the §4 stack free
    of any knowledge of the `~/.claude/projects` layout. Wraps
    `claude._get_session_jsonl_path`, which returns None when the file is
    not (yet) on disk.
    """
    def resolve(session_id: str) -> Optional[Path]:
        return _get_session_jsonl_path(session_id, working_dir)

    return resolve


class CompletionOutcome(Enum):
    """The verdict of the §4 confirmation stack for one dispatched turn."""

    COMPLETED = "completed"          # Stop fired and the sentinel is present.
    STOPPED_EARLY = "stopped_early"  # Stop fired but no sentinel — asked a
    #                                  question / errored; caller nudges or fails.
    TIMED_OUT = "timed_out"          # No Stop signal within the timeout budget.


@dataclass
class CompletionResult:
    """Outcome of `detect_completion`, plus what the transcript yielded.

    `session_id` is the id from the Stop signal — the post-`/compact` id
    when the turn rotated it (doc §6.1) — so M4 persists it to `state.json`.
    """

    outcome: CompletionOutcome
    session_id: Optional[str] = None
    signal_time: Optional[str] = None
    transcript_path: Optional[Path] = None
    summary: str = ""
    tokens: dict = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """True only for an outcome-confirmed completion."""
        return self.outcome is CompletionOutcome.COMPLETED


async def detect_completion(
    *,
    card_id: str,
    watcher: SignalWatcher,
    timeout: float,
    resolve_transcript: Callable[[str], Optional[Path]],
) -> CompletionResult:
    """Run the §4 confirmation stack for one dispatched turn.

    Awaits the Stop-hook signal via `watcher` (bounded by `timeout`, the
    wall-clock backstop), then confirms the sentinel marker for `card_id`
    in the turn's transcript.

    Args:
        card_id: Trello card id — selects the sentinel marker to look for.
        watcher: A `SignalWatcher` constructed *before* the prompt was
            dispatched, so its baseline excludes earlier cards.
        timeout: Seconds to wait for the Stop signal. M4 supplies
            `Config.get_timeout(project)`.
        resolve_transcript: Maps the signalled `session_id` to its
            transcript JSONL path — see `transcript_path_resolver`.

    Returns:
        A `CompletionResult`:
          * COMPLETED     — Stop fired and the sentinel is present.
          * STOPPED_EARLY — Stop fired but no sentinel (a clarifying-question
            stop or an error); the caller decides whether to nudge or fail.
          * TIMED_OUT     — no Stop signal arrived within `timeout`.

    The Trello card list stays the ground-truth arbiter of success vs.
    failure (doc §4); that check lives in the polling loop, not here.
    """
    entry = await watcher.wait(timeout=timeout)
    if entry is None:
        logger.info("Completion detect: timed out after %ss (card %s)", timeout, card_id)
        return CompletionResult(outcome=CompletionOutcome.TIMED_OUT)

    transcript_path = resolve_transcript(entry.session_id)
    summary = read_transcript_summary(transcript_path) if transcript_path else ""
    tokens = _read_token_usage_from_jsonl(transcript_path) if transcript_path else {}
    has_sentinel = (
        transcript_has_sentinel(transcript_path, card_id)
        if transcript_path
        else False
    )
    outcome = (
        CompletionOutcome.COMPLETED if has_sentinel else CompletionOutcome.STOPPED_EARLY
    )
    logger.info(
        "Completion detect: %s (card %s, session %s)",
        outcome.value, card_id, entry.session_id,
    )
    return CompletionResult(
        outcome=outcome,
        session_id=entry.session_id,
        signal_time=entry.timestamp,
        transcript_path=transcript_path,
        summary=summary,
        tokens=tokens,
    )
