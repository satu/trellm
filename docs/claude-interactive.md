# Supporting `claude` interactive mode in trellm — implementation plan

This is the answer to the [`trellm add support for claude interactive`
card](https://trello.com/c/u5GuspW3). It is an implementation plan: it
defines the abstraction, compares three concrete solutions, picks one,
and sequences a proof of concept. Implementation lands in follow-up
cards (see "Milestones").

> **Status (2026-05-17).** M0.5 is resolved: streaming-JSON `claude -p`
> is *also* metered, so the interactive TUI is the only
> subscription-covered shape. The transport is decided — **Alternative 1
> (tmux TUI)**; Alternative 3 is retired. Build milestones M1–M6 are now
> filed as individual ICE BOX cards.

## TL;DR

- **Why.** Claude Code is expected to start metering headless `claude -p`
  invocations separately from the interactive-TUI subscription seat.
  trellm spawns one `claude -p` subprocess per card today, so every card
  would become a metered call. The interactive TUI stays
  subscription-covered.
- **Premise confirmed (M0.5 done).** The billing-boundary question is
  resolved: streaming-JSON `claude -p` is *also* metered, not just
  one-shot `-p`. Only the interactive TUI stays subscription-covered, so
  Alternative 3 is off the table and **Alternative 1 (tmux TUI) is the
  build**.
- **Abstraction.** Introduce a `ClaudeSession` seam. The current
  subprocess code becomes `PrintSession` (one stateless subprocess per
  card). The new path is `InteractiveSession` (one long-lived `claude`
  TUI per project). Selection is per-project config —
  `claude.projects.<name>.runner: print | interactive` — mirroring the
  existing `BrowserConfig` / `get_timeout` override shape.
- **Recommended solution: Alternative 1 — tmux-driven interactive TUI.**
  One `tmux` window per project running `claude --dangerously-skip-permissions`.
  Dispatch a task by typing a prompt; detect completion with a
  **Stop hook** (primary) + **sentinel marker** (confirmation) +
  **wall-clock timeout** (backstop) + **Trello card list** (ground truth).
- **The existing per-project lock already gives interactive mode the
  concurrency guarantee it needs** — trellm never runs two cards for the
  same project at once, so one window per project never has two writers.
- **Reusable artifact for Humphrey (Node).** The deliverable other
  projects copy is the *interactive control contract* (window naming,
  prompt-file dispatch, Stop-hook signal format, sentinel marker), not
  Python code. See "Portability contract".

## 1. Current state — how `claude` is used today

`trellm/claude.py` (`ClaudeRunner`) is the only place `claude` runs.
Every card spawns a fresh subprocess:

```
claude -p <prompt> --output-format json|stream-json [--verbose]
       [--dangerously-skip-permissions] [--mcp-config <json>]
       [--resume <session_id>]
```

Key properties of the print-mode model, all of which the new path must
account for:

- **Stateless per card.** `asyncio.create_subprocess_exec` → `communicate()`
  → process exits. Subprocess exit *is* the completion signal.
- **Session continuity is explicit.** trellm passes `--resume <session_id>`
  and persists the returned id in `state.json`. The id changes after
  `/compact` (CLAUDE.md gotcha #1).
- **Pre-compaction between cards.** `run()` runs `/compact` when the card
  differs from `last_card_id`.
- **Result parsing.** `_parse_output` reads the final `result` JSON line
  for the summary; `_run_cost` reads `<session>.jsonl` for tokens.
- **Two call sites** — `process_cards` and `process_card_for_project` in
  `__main__.py` — plus `maintenance.py`. Each already threads
  `browser_enabled`, `mcp_config_json`, and `timeout` through.
- **Errors** (rate limit, prompt-too-long, monthly limit, session-not-found)
  are detected by regex over captured stdout/stderr in `_check_for_errors`.

The interactive model inverts the core property: the `claude` process is
**stateful and long-lived per project**, so "the process exited" is no
longer the done signal. Everything below follows from that one change.

## 2. The billing boundary (resolved by M0.5)

> **Resolved 2026-05-17.** The streaming-JSON session shape is metered
> too — *any* `claude -p` invocation is, one-shot or streaming. The
> interactive TUI is the only subscription-covered shape. **Alternative 1
> (tmux) is the plan**; Alternative 3 is retired. The §3 seam is still
> built first (M1) exactly as written — only the transport choice is now
> fixed. The analysis below is kept as the reasoning that led here.

The card states `claude -p` "will soon start charging". `claude` has
three relevant invocation shapes:

| Shape | Description | Billing assumption |
|-------|-------------|--------------------|
| One-shot `claude -p` | What trellm does today, one process per card | **Assumed metered** — this is the problem |
| Streaming-JSON session | `claude -p --input-format stream-json --output-format stream-json`, one long-lived process fed many turns over stdin | **Unknown** — still `-p`, may or may not be metered |
| Interactive TUI | `claude` (no `-p`), the full-screen terminal app | **Assumed subscription-covered** — the target |

**This table is an assumption, not a fact.** M0.5 confirms it against
Anthropic's published billing terms / Claude Code release notes. The
outcome decides the build:

- If the streaming-JSON session is subscription-covered → **Alternative 3
  wins** (no tmux, no TUI scraping — by far the smallest build).
- If only the interactive TUI is covered → **Alternative 1** (tmux) is
  the plan, as written below.

Do not skip M0.5. Building the tmux path when streaming-JSON would have
sufficed is weeks of avoidable fragility.

## 3. The abstraction — `ClaudeSession`

A backend seam so trellm can switch transports per project without the
polling loop knowing which is in use.

```python
# trellm/session.py  (new)
class ClaudeSession(Protocol):
    """One card's worth of work, transport-agnostic."""

    async def run_task(
        self,
        card: TrelloCard,
        *,
        timeout: int,
        output_callback: Optional[callable] = None,
    ) -> ClaudeResult: ...
```

Two implementations:

- **`PrintSession`** — the current `ClaudeRunner` code, unchanged in
  behaviour. Stateless: `run_task` spawns and reaps one subprocess.
- **`InteractiveSession`** — owns a long-lived `claude` TUI for one
  project. `run_task` dispatches a prompt into the running TUI and waits
  for a completion signal.

Because interactive sessions are stateful and per-project, a registry
owns their lifecycle:

```python
class SessionManager:
    """Resolves a project to its ClaudeSession, creating/reusing the
    long-lived interactive window lazily. Print-mode projects get a
    fresh stateless PrintSession each call (cheap)."""

    def session_for(self, project: str) -> ClaudeSession: ...
    async def shutdown(self) -> None: ...   # detach/kill windows
```

Selection mirrors `Config.get_timeout` / `is_browser_enabled`:

```yaml
# ~/.trellm/config.yaml
claude:
  runner: print          # global default — unchanged behaviour
  projects:
    itest:
      working_dir: "~/src/itest"
      runner: interactive   # opt-in, per project
```

```python
def get_runner_mode(self, project: str) -> str:
    """'print' (default) or 'interactive'. Per-project beats global."""
    proj = self.claude.projects.get(project)
    if proj is not None and proj.runner is not None:
        return proj.runner
    return self.claude.runner or "print"
```

The default is `print` for every project, so landing the seam is a pure
refactor with **zero behaviour change** — that is M1, and it is the
de-risking step: the new transport is built behind a seam that already
has full test coverage on the old path.

## 4. Detecting "task is over" — the central challenge

In print mode, process exit is the signal. Interactive mode needs an
explicit one. Six strategies, scored:

| Strategy | How | Verdict |
|----------|-----|---------|
| **Stop hook → signal file** | A `Stop` hook in the project's `.claude/settings.json` runs a tiny script that appends `<session_id> <iso8601>` to `~/.trellm/interactive/<project>.signal`. trellm watches the file. | **Primary.** Claude Code fires `Stop` deterministically when the agent finishes a turn. No screen scraping. |
| **Sentinel marker** | The dispatched prompt ends with: *"Output exactly this line last: `⟦TRELLM-DONE cardId=<id>⟧`"*. trellm scans the transcript / pane for it. | **Confirmation.** Distinguishes genuine completion from a turn that stopped to ask a clarifying question (which also fires `Stop`). |
| **Wall-clock timeout** | Reuse `Config.get_timeout(project)`. No signal in budget → interrupt the pane (`Esc`, then `Ctrl-C`), mark timed-out. | **Backstop.** Same semantics as today's `asyncio.wait_for`. |
| **Trello card list** | The prompt already says "move the card to READY TO TRY". trellm polls Trello anyway; the card leaving TODO is unambiguous. | **Ground truth / success-vs-fail.** Slow (poll interval) but transport-independent and authoritative. |
| **Transcript JSONL tailing** | Tail `~/.claude/projects/<dir>/<session>.jsonl` for the final assistant message. trellm already reads these files in `_run_cost`. | **Used for the summary**, and as a cross-check of the Stop signal. |
| Idle / spinner detection | Watch the pane for the TUI's working spinner to disappear. | **Rejected.** Brittle across Claude Code versions; a mid-task pause (e.g. a long tool call) reads as idle. |

**Recommended combination:**

1. **Stop hook fires** → candidate "done".
2. **Confirm** the sentinel marker is present in the transcript. Present
   → real completion. Absent → the turn stopped early (asked a question,
   hit an error); re-dispatch a nudge or fail the card.
3. **Timeout** bounds the whole wait.
4. **Trello list** is the final arbiter of success vs. failure — exactly
   as today, `process_card_for_project` already calls `move_to_ready`
   and inspects card state.
5. **Transcript JSONL** supplies the summary text and token costs,
   reusing `_get_session_jsonl_path` / `_read_token_usage_from_jsonl`.

This layering means no load-bearing signal depends on scraping the TUI's
rendered screen — the fragile part — while still giving a fast trigger
(the hook) instead of waiting a whole poll interval on Trello.

## 5. Three alternative solutions

### Alternative 1 — tmux-driven interactive TUI  *(recommended)*

One `tmux` window per project inside a dedicated session
(`trellm-interactive`), running the real `claude` TUI.

- **Dispatch.** Write the full prompt to
  `~/.trellm/interactive/tasks/<cardid>.md`, then
  `tmux send-keys -t trellm-interactive:<project> -l "Read ~/.trellm/interactive/tasks/<cardid>.md and complete the task it describes."`
  followed by a separate `send-keys ... Enter`. The prompt-via-file
  trick sidesteps every multiline / shell-escaping / bracketed-paste
  problem and keeps the scrollback clean.
- **Observe.** `tmux capture-pane` for the web dashboard live view;
  completion via the strategy stack in §4.
- **Pros.** tmux gives the TUI a real PTY, so it runs fine inside trellm's
  Docker container with no attached terminal. The tmux server outlives
  trellm — if trellm crashes/restarts it re-attaches to live windows
  instead of losing in-flight work and context. Human-observable: a
  developer can `tmux attach` and watch or take over. tmux is a
  battle-tested PTY multiplexer; we are not reinventing it.
- **Cons.** External `tmux` dependency. Extracting the live result still
  means reading the transcript (the pane is rendered, not structured).

### Alternative 2 — in-process PTY-driven TUI

Same interactive TUI, but trellm owns the pseudo-terminal directly
(`pexpect` / stdlib `pty`), one PTY per project, held inside the trellm
process.

- **Pros.** No external dependency. Direct byte stream — no `capture-pane`
  round trips.
- **Cons.** The PTYs die with trellm: a restart loses every in-flight
  session and all context. Not human-observable (no `attach`). trellm
  becomes a terminal emulator, which it is not. Strictly worse than
  Alternative 1 on the two properties that matter most — crash recovery
  and observability — for a marginal dependency saving.

### Alternative 3 — persistent streaming-JSON session subprocess

One long-lived `claude -p --input-format stream-json --output-format
stream-json` process per project. Feed JSON user-message turns over
stdin; read structured JSON events from stdout; the `result` event ends
a turn.

- **Pros.** No TUI scraping, no tmux. Structured I/O — completion,
  summary, tokens, and errors all arrive as JSON, so most of
  `claude.py`'s parsing is reusable. By far the smallest, most robust
  build.
- **Cons / risk.** It is still a `claude -p` invocation. **If `-p` is
  metered regardless of one-shot vs. streaming, this does not solve the
  billing problem at all.** Its viability is entirely decided by M0.5.
- **Verdict — retired.** M0.5 (resolved 2026-05-17) found streaming-JSON
  is metered too, so this does not solve the billing problem. Alternative
  1 is the build. The `ClaudeSession` seam still keeps a streaming-JSON
  backend a drop-in possibility should Anthropic's billing terms change.

**Decision: build the seam (§3) unconditionally; pick the transport
after M0.5.** The seam is identical work either way.

## 6. Recommended design in detail (Alternative 1)

### 6.1 Window lifecycle

| Event | Action |
|-------|--------|
| First card for an interactive project | `tmux new-session -d -s trellm-interactive` if absent; `tmux new-window -d -t trellm-interactive -n <project> -c <working_dir> 'claude --continue --dangerously-skip-permissions \|\| claude --dangerously-skip-permissions'` — `--continue` resumes the project's prior session; the fallback handles the first-ever run. |
| trellm restart | `tmux list-windows` — reuse any live window, recreate the rest. In-flight context survives. |
| `/compact` between cards | Dispatch `/compact` as its own turn, wait for the Stop signal, then dispatch the task turn. The new session id arrives via the Stop hook payload. |
| trellm shutdown | Leave the tmux session running (like the browser stack — long-lived host process; avoids cold start; keeps context). `SessionManager.shutdown` only detaches. |

One window per project + the existing per-project `asyncio` lock in
`process_card_for_project` ⇒ never two writers on one pane. No new
locking needed.

### 6.2 Completion signal plumbing

A tiny shipped script, `scripts/trellm-stop-hook.sh`, reads the hook
JSON from stdin (`session_id`, `cwd`, `transcript_path`), derives the
project from `cwd`, and appends one line to
`~/.trellm/interactive/<project>.signal`. Each interactive project's
`.claude/settings.json` registers it once:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
                     "command": "~/src/trellm/scripts/trellm-stop-hook.sh" } ] }
    ]
  }
}
```

Installing this hook is a per-project setup step (the PoC `create-project`
flow can automate it for `runner: interactive` projects). `InteractiveSession`
`await`s a change to the signal file (watch + poll fallback), then
applies the §4 confirmation stack.

### 6.3 Result, errors, streaming

- **Summary + tokens** — read from `<session>.jsonl` via the existing
  `_get_session_jsonl_path` helpers; no new parser.
- **Errors** — `_check_for_errors`' regexes still apply, run against the
  transcript text instead of subprocess stderr. Rate-limit / monthly-limit
  / prompt-too-long detection is preserved; this is important — gotcha #8.
- **Web dashboard streaming** — replace the stream-json `output_callback`
  feed with a periodic `capture-pane` diff, or tail the transcript JSONL
  and forward assistant text. The `output_callback` signature is unchanged.

### 6.4 Config & docs

- `ProjectConfig.runner: Optional[str]`, `ClaudeConfig.runner: str = "print"`,
  `Config.get_runner_mode(project)`, parsing in `load_config`, and a
  `compare_configs` line — all mirroring the `timeout` override added in
  commit [`77235cd`](https://github.com/satu/trellm/commit/77235cd).
- Document the override in `CLAUDE.md` alongside `get_timeout`.

## 7. Proof-of-concept plan

Strictly sequential milestones, each its own card and commit, each with
a stop gate — same discipline as `docs/patchright-mcp.md`.

### M0.5 — Confirm the billing boundary  ✅ *done (2026-05-17)*
**Outcome:** streaming-JSON `claude -p` is metered too — every `-p`
invocation is. Only the interactive TUI is subscription-covered.
**Decision:** build Alternative 1 (tmux TUI); Alternative 3 is retired.
M1–M6 below proceed with the tmux transport fixed.

### M1 — Land the `ClaudeSession` seam  *(pure refactor)*
Extract the `ClaudeSession` protocol and `SessionManager`; wrap today's
code as `PrintSession`; add `runner` config defaulting to `print`. Wire
both `__main__.py` call sites + `maintenance.py` through the manager.
**Gate:** full existing suite green, zero behaviour change — verified by
`trellm --once` producing byte-identical `claude` commands.

### M2 — `tmux` control module
`trellm/tmux.py`: create / list / send-keys / capture-pane / kill-window.
Unit-tested against a mocked `tmux` binary; one integration test against
real `tmux` in CI. No `claude` yet.

### M3 — Completion detector
`scripts/trellm-stop-hook.sh` + the signal-file watcher + the §4
confirmation stack, unit-tested in isolation with canned signal files,
transcripts, and sentinel-present / sentinel-absent fixtures.

### M4 — `InteractiveSession`
Wire M2 + M3 into a `ClaudeSession`. Behind `runner: interactive`, off
for every real project. Unit tests mock `tmux.py` and the detector.

### M5 — PoC test project  *(the live test)*

> **Status (2026-05-19).** The PoC scaffold is built — `~/src/itest`
> (interactive) + `~/src/iprint` (print baseline) repos with the Stop
> hook installed, an isolated `~/.trellm/itest-config.yaml`, and the
> `itest-TODO`/`itest-READY` Trello lists. The live run is not driven by
> the M5 prep agent itself: that agent runs inside the production trellm
> daemon (its own parent), which gate 3 would have to restart, and is
> bounded by the 20-minute card timeout. The live test is therefore an
> operator step — see **`docs/m5-poc-runbook.md`** for the exact
> per-gate procedure and results template.

Create a throwaway project — `itest`, a minimal git repo at `~/src/itest`
(use the `create-project` skill; it installs the Stop hook). Set
`runner: interactive`. File a sequence of trivial cards: *"itest add a
function returning today's date"*, *"itest fix the README typo"*, etc.
Drive them through the real flow. **Gate, all required:**

1. ≥ 3 cards complete cleanly end-to-end (dispatch → Stop → sentinel →
   card moved to READY TO TRY).
2. A `/compact` between two cards works; the new session id is captured.
3. An induced trellm restart mid-card re-attaches to the live window and
   the card still finishes.
4. An induced failure (a card that cannot be done) is detected and the
   card is left in TODO with a retry-context comment — parity with print
   mode's `_build_retry_context_comment` path.
5. No regression: a print-mode project processed in the same run behaves
   exactly as before.

### M6 — Decision & rollout
If M5 is fully green: flip one low-risk real project to `interactive`,
soak it, then migrate the rest. If any M5 gate fails: file a follow-up
investigation card with the failing gate and the transcript excerpt —
**do not** promote `interactive` to any real project.

```
M0.5 ─► M1 ─► M2 ─► M3 ─► M4 ─► M5 ─► M6
 gate   gate  gate  gate  gate  gate  gate
```

## 8. Portability contract (knowledge transfer to Humphrey / Node)

Humphrey needs the same change in Node. The reusable artifact is **the
interactive control contract**, transport details a Node port copies
verbatim — the code is incidental, the protocol is the asset:

- **tmux layout.** One session `trellm-interactive` (or `humphrey-interactive`);
  one window per project, window name == project name; started with
  `claude --continue --dangerously-skip-permissions` in the project cwd.
- **Task dispatch.** Prompt written to a file; `tmux send-keys -l` the
  one-line "Read `<file>` and complete it" instruction, then `Enter`.
  Never type a multiline prompt directly.
- **Completion signal.** A `Stop` hook appends `<session_id> <iso8601>`
  to `<state-dir>/<project>.signal`; the orchestrator watches the file.
- **Sentinel.** Final transcript line `⟦TRELLM-DONE cardId=<id>⟧`
  confirms genuine completion vs. a clarifying-question stop.
- **Summary source.** Tail `~/.claude/projects/<dir-with-slashes-as-dashes>/<session>.jsonl`.

A Node port is `child_process.execFile('tmux', …)` + `fs.watch` on the
signal file — same protocol, different language. Once trellm's M5 is
green, file a Humphrey card linking this section.

## 9. Risks & open questions

- **The billing assumption (§2)** — the single biggest risk; M0.5 exists
  to retire it before code is written.
- **TUI version drift** — Claude Code TUI redraws are unstable to scrape.
  Mitigated: every load-bearing signal is a hook or a transcript file,
  never the rendered pane. `capture-pane` is used only for the
  best-effort dashboard view.
- **Clarifying-question stop** — `Stop` fires when a turn ends to ask a
  question, not only on completion. Mitigated by the sentinel and by
  `--dangerously-skip-permissions` + self-contained prompts.
- **Maintenance turns** — `maintenance.py` would type into the same
  window. In scope for M4's wiring; the PoC project keeps maintenance
  disabled until then.
- **Error detection parity** — gotcha #8 (monthly-limit global pause)
  must keep working; M4 must run `_check_for_errors` over the transcript
  and carry a regression test for it.

## 10. Out of scope

- Migrating any real project to `interactive` — that is M6 and beyond,
  each its own opt-in card.
- The Humphrey/Node implementation itself — only the §8 contract is
  produced here.
- Replacing print mode. `PrintSession` stays as the default and the
  fallback; the seam keeps both alive indefinitely.
