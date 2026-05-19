# M5 — interactive-mode PoC live-test runbook

This is the execution runbook for milestone **M5** of
`docs/claude-interactive.md` (§7) — the live end-to-end test of the
`runner: interactive` transport against a throwaway project.

## Why a runbook instead of an autonomous run

M5 is a *live operational* test, and the trellm agent that picks up the
M5 card runs **inside the production trellm daemon** (`trellm -v` spawns a
`claude -p` per card; that subprocess is the agent). Two hard blockers
make a self-driven M5 impossible from there:

1. **Gate 3 requires restarting trellm mid-card.** The production daemon
   is the agent's own parent process — restarting it kills the agent.
2. **20-minute card timeout.** `claude.timeout: 1200` bounds every card;
   a multi-card live run with real nested `claude` TUI sessions does not
   fit, and a kill mid-run would orphan a tmux `claude` window.

So M5 runs as its **own isolated trellm instance**, started by an
operator (or any session not bounded by the production daemon's timeout).
The card `6a0a3a2fda9f503addc61c99` prepared everything below; this
runbook is the remaining live step.

## What is already set up (by the M5 prep card)

| Artifact | Location | Purpose |
|----------|----------|---------|
| `itest` repo | `~/src/itest` (remote `~/src/itest-remote.git`) | interactive PoC project; `README.md` carries a deliberate typo ("teh") |
| Stop hook | `~/src/itest/.claude/settings.json` | registers `scripts/trellm-stop-hook.sh` (completion signal) |
| `iprint` repo | `~/src/iprint` (remote `~/src/iprint-remote.git`) | print-mode regression baseline (gate 5) |
| Isolated config | `~/.trellm/itest-config.yaml` | own state file + `itest-*` lists + web off |
| `itest-TODO (M5 PoC)` | list `6a0bcd6a98633bf612a7414c` | the isolated instance's TODO |
| `itest-READY (M5 PoC)` | list `6a0bcd6ab2cc00c21b51d685` | the isolated instance's READY |

The production daemon never polls the `itest-*` lists, so the isolated
instance and production run side by side without conflict.

## Preflight

```bash
tmux -V                              # tmux must be installed
claude --version                     # claude TUI must be authenticated
ls ~/src/trellm/scripts/trellm-stop-hook.sh   # must exist + be executable
test -f ~/.trellm/itest-state.json && rm ~/.trellm/itest-state.json  # fresh state
```

Start the isolated instance in its own terminal (NOT the production one):

```bash
cd ~/src/trellm && .venv/bin/trellm -c ~/.trellm/itest-config.yaml -v
```

Watch the interactive `claude` TUI live at any time with:

```bash
tmux attach -t trellm-interactive    # Ctrl-b d to detach
```

## Gates

Cards go in `itest-TODO (M5 PoC)`. The first word of each card name is the
project (`itest` = interactive, `iprint` = print).

### Gate 1 — ≥3 cards complete cleanly end-to-end

File these three cards in `itest-TODO`:

1. `itest add a function in dates.py that returns today's date as YYYY-MM-DD`
2. `itest fix the typo in README.md`
3. `itest add a module docstring to dates.py`

**Pass:** each card runs dispatch → `Stop` hook fires (a line appended to
`~/.trellm/interactive/itest.signal`) → sentinel `⟦TRELLM-DONE cardId=…⟧`
present in the transcript → card moved to `itest-READY (M5 PoC)`.

### Gate 2 — `/compact` between two cards

`InteractiveSession` pre-compacts automatically whenever the new card id
differs from `last_card_id` for the project. Gate 1's three cards already
exercise this twice.

**Pass:** logs show a `/compact` turn dispatched before cards 2 and 3, and
`~/.trellm/itest-state.json` shows the `itest` session id **changed**
after the compact (the id rotates on `/compact` — gotcha #1).

### Gate 3 — induced trellm restart mid-card

While a card is mid-run (TUI actively working), `Ctrl-C` the isolated
`trellm`, then immediately restart it with the same command. File a fresh
card just before, or use a slightly longer task, to guarantee a window.

**Pass:** on restart, logs show `InteractiveSession` **reused** the live
`trellm-interactive:itest` tmux window (via `tmux list-windows`) rather
than creating a new one; the in-flight card still completes and moves to
READY.

### Gate 4 — induced failure leaves the card in TODO

File a card that genuinely cannot be completed, e.g.:

- `itest apply the database migration described in JIRA ticket OPS-4471`

The agent has no access to that ticket; it should stop without emitting
the sentinel (`CompletionOutcome.STOPPED_EARLY`), so `InteractiveSession`
raises and the polling loop's per-card retry path runs.

**Pass:** the card is **left in `itest-TODO`** (not moved to READY), and a
retry-context comment is posted on the card — parity with print mode's
`_build_retry_context_comment`.

### Gate 5 — print-mode regression in the same run

File: `iprint fix the typo in README.md` (project `iprint` is print mode).

**Pass:** `iprint` is processed by the same trellm run via `PrintSession`
(one `claude -p` subprocess), commits, pushes, and moves to READY exactly
as before — no behaviour change from interactive mode being present.

## Results template — paste into a comment on card `6a0a3a2fda9f503addc61c99`

```
Claude: M5 live-test results

Gate 1 (>=3 cards clean E2E):  PASS / FAIL  — <card ids + notes>
Gate 2 (/compact between cards): PASS / FAIL — <old->new session id>
Gate 3 (restart mid-card re-attaches): PASS / FAIL — <notes>
Gate 4 (induced failure left in TODO): PASS / FAIL — <retry comment link>
Gate 5 (print-mode regression): PASS / FAIL — <notes>

Transcript excerpts:
<paste the Stop-signal lines, sentinel lines, and any error output>
```

Per `docs/claude-interactive.md` §M6: if **all** gates pass, M6 may flip
one low-risk real project to `interactive`. If **any** gate fails, file a
follow-up investigation card with the failing gate + transcript excerpt
and do **not** promote `interactive` to any real project.

## Cleanup

```bash
tmux kill-session -t trellm-interactive    # stop the PoC claude TUI
rm -f ~/.trellm/itest-state.json
# Optional: archive the itest-TODO/itest-READY lists and the itest cards.
# Keep ~/src/itest + ~/.trellm/itest-config.yaml for re-runs / M6.
```
