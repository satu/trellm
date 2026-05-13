# TreLLM Dashboard — UX Redesign Handoff Brief

This document is the design brief for an external UX team that will redesign
the TreLLM web dashboard. The current dashboard is functional but utilitarian:
it was built phase-by-phase to surface information, not designed as a product.
This brief catalogues every information surface and every Critical User
Journey (CUJ) the redesign must preserve, so the UX team can re-imagine the
layout, hierarchy, and visual style without losing capability.

It deliberately does **not** prescribe a visual direction. The single
constraint on look-and-feel is: dark theme by default (see §6).

Card: [mPxxrzRm — trellm dashboard redesign](https://trello.com/c/mPxxrzRm).

---

## 1. Product context (read first)

TreLLM is a single-user automation tool that runs Claude Code against Trello
cards. A background polling loop watches a "TODO" list on a Trello board,
picks up cards as they appear, runs Claude Code in the matching project's
working directory, posts results back to the card, and moves the card.

The dashboard is **not** the primary UI for *creating* work — the user
creates work by writing Trello cards. The dashboard is the **observability
and control plane** that sits alongside the polling loop. It answers
questions like "is anything running right now?", "how much have I spent
this week?", "why has this card been retrying for an hour?", and "kill
it, please".

The dashboard is intentionally local/single-user. It is served by an
embedded `aiohttp` server inside the same process as the polling loop, on
`127.0.0.1:8077` by default (LAN-accessible if the user changes `host`).
There is no auth, no multi-tenancy, no permission model. Assume one
operator.

## 2. The user

One person — the developer who owns the TreLLM instance. They are technical,
comfortable with Trello, comfortable with terminals, and likely have the
dashboard open in a background tab while they do other work. They check it
when they want to:

- Confirm the polling loop is alive.
- See what Claude is currently doing (live).
- Investigate why a card is stuck or repeating.
- See how much money they've spent.
- Pull the emergency brake (abort / restart).

They will also open the dashboard from **mobile** — both Android (works
out of the box) and iOS Safari "Add to Home Screen" PWA mode (manifest +
icons + custom pull-to-refresh are already wired). Mobile is not an
afterthought; treat it as a first-class viewport.

## 3. Information surfaces (what the dashboard knows)

These are the pieces of state the dashboard receives from the backend.
Each one comes from a real REST endpoint and is enumerated here so the
designers know exactly what fields they have to work with. Field names
are the literal keys in the JSON payloads — useful when annotating
mockups.

### 3.1 Polling status (`GET /api/status`)

The macro-level "is the loop alive" signal.

| Field | Meaning | Notes |
|---|---|---|
| `status` | `"running"` / `"error"` (badge) | The dashboard currently sets the badge to `"error"` purely on a fetch failure — there's no backend-reported error state yet. Designers should treat this as a binary up/down indicator. |
| `uptime_seconds` | Seconds since the TreLLM process started | Currently rendered as `2h 15m`. Useful but secondary. |
| `poll_interval` | Configured poll interval, seconds | Static once running; mostly there for sanity check. |
| `active_tasks` | Number of cards currently being processed | Same number is implied by the Running Tasks table. |
| `projects` | Map of project name → `{working_dir, aliases}` | Project count is shown as a stat tile; full list is rendered in the Projects section (§3.6). |

### 3.2 Running tasks (`GET /api/tasks`)

The list of cards Claude is **actively working on right now**. Each task is
the live picture of one Claude subprocess.

Per-task fields:

| Field | Meaning |
|---|---|
| `card_id` | Trello card ID |
| `project` | Resolved canonical project name |
| `card_name` | Card title (first word is the project alias) |
| `card_url` | Trello URL — should always render as a link |
| `duration_seconds` | How long this run has been executing |
| `output_lines` | Number of stdout lines buffered so far |
| `has_output` | Whether buffered output exists at all |
| `error_count` | Failed runs of this card so far (current session) |
| `timeout_count` | Times this card has hit the Claude timeout |
| `fast_failure_streak` | Consecutive failures that came back faster than the 60s "fast-failure" threshold — drives exponential backoff |

Implication: a task can have non-zero retry counters while *currently
running*, meaning "this is the Nth attempt at this card and previous
attempts crashed/timed out". The current design surfaces this as a small
orange chip in an "Attempts" column. The UX team should decide how
prominent this should be — for a card on its first run, the column is
empty; for a card on its 5th timeout retry, it's the most important
signal on the screen.

### 3.3 Queue (`GET /api/queue`)

The snapshot of cards sitting in Trello's TODO list waiting to be picked
up. Refreshed on every poll cycle. The UI hides this section entirely
when there's nothing waiting; that "absent when empty" behaviour is
important — most of the time it should not occupy screen real estate.

Per-card fields:

| Field | Meaning |
|---|---|
| `card_id`, `card_name`, `card_url` | Trello card identity + link |
| `project` | Resolved project name (matches §3.6) |
| `queued_for_seconds` | How long the card has sat in TODO (Trello last-activity → now) |
| `is_running` | True if this card has *just* been picked up — front-end filters these out so they don't double-show alongside Running Tasks |
| `retry` (nullable) | Per-card retry info: `error_count`, `timeout_count`, `fast_failure_streak`, `backoff_remaining_seconds` — meaningful when a card has previously failed and is currently in cooldown |

The current renderer groups by project so "3 cards queueing for project X"
is visible at a glance. Worth preserving.

### 3.4 Recent completions (`GET /api/completed`)

The last 10 *successful* completions (failed/cancelled runs are
deliberately excluded — see `server.py:89-115`). This is "what just got
done", not "what got attempted".

Per-task fields:

| Field | Meaning |
|---|---|
| `card_id`, `card_name`, `card_url`, `project` | Identity + link |
| `run_id` | Composite `{card_id}_{epoch}` — needed because the same card can re-run and we want each run distinct in the output viewer |
| `duration_seconds` | Total run wall time |
| `completed_ago_seconds` | "5m ago" style relative time |
| `output_lines` | Buffered output line count |
| `input_tokens`, `output_tokens` | Per-run token usage (sourced from ticket_history in state) |

The whole section auto-hides when there are no completions. That's
correct — the very first time the user opens the dashboard on a fresh
process, they shouldn't see an empty "Recent Completions" placeholder.

### 3.5 Live output (`GET /api/stream/{task_id}` — SSE)

The dashboard streams Claude's parsed stdout (text, thinking, tool
results) over Server-Sent Events. Used for **both** live tailing of
running tasks **and** replaying buffered output for the last 10
completed tasks.

- Up to 5000 lines per task are buffered server-side.
- Lines arrive one per `data:` event; a `done` event closes the stream.
- The UI shows the output in a single fixed-height `<pre>` panel
  (currently 500px max with internal scroll), an "Auto-scroll" checkbox,
  and a Close button.
- The panel currently appears as just-another card mid-page. On mobile
  and even on desktop, this means the user often has to scroll back up
  to see the table while the stream is running. The UX team should
  decide whether this becomes a docked panel, a side drawer, a modal, a
  separate route, or stays inline.

### 3.6 Projects (`GET /api/projects`)

The configured project roster. One row per project. Currently
deliberately minimal — only ticket count is shown because cost/changes
per-project were considered unreliable signals.

Per-project fields available (the renderer only uses some):

| Field | Used today? | Notes |
|---|---|---|
| `name` | Yes | Canonical name |
| `aliases` | Yes | Shown as `(smg, smug)` inline |
| `working_dir` | No (in this section) | Available in the Configuration section instead |
| `last_card_id`, `last_activity` | No | Could power a "last activity 2h ago" indicator |
| `stats.total_tickets` | Yes | Only field surfaced |
| `stats.total_cost_dollars`, `average_cost_dollars`, `total_lines_added`, `total_lines_removed` | No, in this view | Available in the Stats > By Project tab |

Designers can lean into this section more if they want — the data is
there. But check with the operator before re-introducing per-project cost
to the Projects card; it was specifically simplified.

### 3.7 Claude usage limits (`GET /api/stats`, top of payload)

Anthropic's plan-level usage meters: 5-hour rolling window, 7-day weekly,
plus per-model 7-day Opus and Sonnet meters when applicable. Cached
aggressively (5-minute cooldown) because the upstream API rate-limits
hard.

Per-meter fields: `utilization` (0–100, may exceed 100), `resets_at`
(formatted local time string).

The current renderer is a horizontal bar with green/amber/red thresholds
at 60% / 85%. There's also a "Refresh" button and an "Updated 4m ago"
timestamp. Error states the renderer must handle:

- `usage_limits.error == "..."` — render the error string (with 429s
  rewritten to "Rate limited — will retry in up to 5 min").
- `usage_limits == {}` empty — "No usage data" empty state.

Cache age (`usage_cache_age_seconds`) is shown to the operator because a
stale meter is *worse* than a slightly-out-of-date one — they need to
know when to trust it.

### 3.8 Stats (`GET /api/stats`, rest of payload)

Aggregate accounting. Three tabs currently:

- **All-Time**: total cost, tickets, average $/ticket, API time, wall
  time, total/input/output tokens, total lines added/removed.
- **Last 30 Days**: a subset of the above scoped to the last 30 days.
- **By Project**: per-project breakdown — cost, tickets, average,
  lines changed, total tokens.

Most fields are pre-formatted on the server (`$1.23`, `12.3K`, `2h 15m`)
— the dashboard renders them verbatim. That's fine. The redesign should
keep the values formatted server-side and not try to do its own number
formatting.

### 3.9 Controls (`POST /api/abort`, `POST /api/restart`)

Two destructive buttons. Each fires a confirm dialog ("Abort all running
tasks?", "Restart TreLLM? This will cancel all tasks and restart the
process."). Both surface their result in a small transient banner.

The redesign must preserve the confirm step. Both actions are
operator-visible disruption — accidentally clicking either while
debugging is a real failure mode.

### 3.10 Configuration (`GET /api/config`)

A raw JSON dump of the loaded config, with `trello.api_key` and
`trello.api_token` masked. Currently rendered as a pre-formatted JSON
block at the bottom of the page. It's a reference panel, not an
interaction. The redesign is free to push it behind a disclosure, a tab,
or a separate route — the operator looks at it rarely.

## 4. Critical User Journeys

Eight CUJs the redesign must support. Each lists what the user wants to
*see*, *understand*, and *do*, plus the failure mode if the journey is
made hard.

### CUJ-1 — "Is TreLLM alive right now?"

**Trigger.** Opening the dashboard cold (e.g. starting work for the day,
or after a noticed outage).

**Want to see (within 1s of load).** The status badge is green/running.
Uptime > 0. Active task count and queue size are believable.

**Failure mode.** If the page renders but the operator can't tell at a
glance whether the loop is alive, they lose trust and start sshing into
the box. The badge must be unambiguous.

### CUJ-2 — "What is Claude doing right now?"

**Trigger.** A card has been moved to TODO in Trello and the operator
wants to watch it execute. Or a card has been running for a while and
they want to verify it isn't stuck.

**Want to see.** The card in Running Tasks with project, name (linked
to Trello), and a *growing* duration. A clear affordance to view the
live stdout stream. The stream itself, readable, auto-scrolling by
default but pause-able.

**Want to do.** Click into the live output, watch tool calls scroll by,
read Claude's thinking, close the panel and come back.

**Failure mode.** The stream is too small / buried below the fold / the
auto-scroll fights the user's reading. The current design has all of
these problems on mobile.

### CUJ-3 — "Why has this card been retrying for an hour?"

**Trigger.** The operator notices the same card name appearing in
Running Tasks repeatedly, or sees retry chips with high counts.

**Want to see.** The retry counters (`error_count`, `timeout_count`,
`fast_failure_streak`) prominently when they're non-zero. The same
counters on queued cards (`backoff_remaining_seconds`) so they can tell
"this card will be retried in 8 minutes" vs "this card is permanently
broken". The output buffer of the *previous* failed run if possible
(harder — currently buffers don't survive a run boundary except for the
last 10 successful runs).

**Want to do.** Click through to the Trello card to read the
"Claude: previous run failed with..." retry-context comments the harness
leaves on failures (see commit
[5d90a67](https://github.com/satu/trellm/commit/5d90a67)). Or escalate
to the global Abort button.

**Failure mode.** Retry state is hidden in a quiet orange chip when it
should be screaming. Designers should pick the visual hierarchy.

### CUJ-4 — "How much money have I spent?"

**Trigger.** Curiosity, end-of-week review, "wait, did that card really
cost $4?".

**Want to see.** All-time cost. Last-30-day cost. Per-project breakdown.
Average per ticket. Optionally tokens (input/output split).

**Want to do.** Switch between time windows and group-bys quickly.

**Failure mode.** Numbers are reformatted client-side and disagree with
the CLI's `/stats` output. Don't do that — values are pre-formatted on
the server precisely so both surfaces agree.

### CUJ-5 — "Am I about to hit a plan limit?"

**Trigger.** Glance at the dashboard before kicking off a big card. Or
after seeing the loop pause itself (the harness pauses globally on
account-wide usage errors — see `__main__.py` `is_globally_rate_limited`).

**Want to see.** The 5-hour and 7-day utilisation bars. Their reset
times. The amber/red threshold colouring. The cache age so they know
whether to refresh.

**Want to do.** Click Refresh if the data looks stale (with the
understanding that the API itself rate-limits aggressively).

**Failure mode.** A meter sits at 95% with no visual urgency. Treat
near-full meters as actionable warnings.

### CUJ-6 — "Kill it. Now."

**Trigger.** Claude is doing the wrong thing (deleting files, in a
runaway loop, eating tokens). The operator wants the polling loop to
stop and the running task cancelled.

**Want to see.** A clearly destructive Abort button. After clicking,
unambiguous confirmation that N tasks were cancelled and the loop is
either still polling or stopped.

**Want to do.** Confirm the action; see it happen.

**Failure mode.** The Abort button is too easy to misclick (no
confirm), or the success banner is missed (auto-dismissed too fast).

### CUJ-7 — "Something is wedged — restart the whole thing."

**Trigger.** TreLLM appears unresponsive, or the operator wants to pick
up a config change (config reload is supposed to be hot, but Restart is
the nuclear option).

**Want to see.** Restart button distinct from Abort (different colour;
currently amber vs red). A confirm dialog. The page reconnects after
the restart completes.

**Want to do.** Confirm, watch it reconnect.

**Failure mode.** Restart and Abort look identical and the operator
picks the wrong one. They are currently colour-distinguished — preserve
this.

### CUJ-8 — "What is the system configured to do?"

**Trigger.** Debugging "why isn't card X being processed?", or
sanity-checking that a config edit landed.

**Want to see.** The loaded config, with secrets masked. Project
working directories. Per-project maintenance intervals. Whether the web
server is bound to localhost or LAN.

**Want to do.** Read it, copy a value, move on.

**Failure mode.** None acute — this is a reference surface, not a
control surface. Can live behind a disclosure or tab.

## 5. Existing UX nuances worth preserving

These are non-obvious behaviours that already exist and represent real
operator preferences. Don't lose them in the redesign without checking.

1. **Empty states hide the section, not show a placeholder.** Recent
   Completions, Queue, and Live Output all vanish from the layout when
   they have nothing to render, instead of showing "No data" boxes. This
   keeps the dashboard quiet during idle periods.
2. **Live Output is for both running *and* completed tasks.** The same
   panel is reused — clicking "View" on a row in Running Tasks streams
   live; clicking "View" on Recent Completions replays the buffered
   output. This shared affordance is worth keeping.
3. **Auto-refresh is every 5 seconds with a visible countdown footer.**
   The countdown reassures the operator that the dashboard is alive even
   when nothing is changing on screen.
4. **iOS PWA pull-to-refresh.** Already custom-built (`pull-to-refresh.js`)
   because iOS standalone mode strips Safari's native gesture. Preserve
   the gesture and its visual indicator.
5. **Cache age is shown for usage limits but not for stats.** Stats are
   computed locally from `state.json` and always fresh; usage limits are
   fetched from Anthropic with a 5-minute cooldown and can be stale. The
   asymmetry is intentional.
6. **Cost values are server-formatted (`$1.23`), token counts are
   server-formatted (`12.3K`).** The dashboard does not re-format. Keep
   this — it guarantees CLI `/stats` and dashboard agree.
7. **The badge in the header is the single "is it alive" signal.**
   `running` / `error` / `loading`. The error state is currently
   triggered by fetch failures, not by backend-reported errors.
8. **Retry chips are intentionally muted when counts are 0.** Don't
   render `errors 0 · timeouts 0` for first attempts — the column is
   meant to be quiet for the common case.
9. **Project grouping in Queue.** The waiting-cards view groups by
   project so "2 cards waiting for `smugcoin`" is visible at a glance.
   This shape is load-bearing — it tells the operator which project is
   bottlenecking.
10. **The favicon/manifest/icons are already wired.** A redesign that
    introduces a new visual identity should produce a refreshed set
    (`/static/icons/*` — 32, 64, 192, 512, maskable 192, maskable 512,
    apple-touch-icon).

## 6. Visual / interaction constraints

The redesign is otherwise unconstrained, but these are hard rules:

- **Dark theme default.** The dashboard is used at all hours; the
  current palette is `#0f1117` background, `#161b22` card, `#e1e4e8`
  text. A light-mode variant is welcome but dark must be the default
  and must be the more polished of the two.
- **No build step.** The static assets are served directly. Vanilla
  HTML/CSS/JS, no npm/webpack/tailwind/etc. If a CSS framework is
  desired, it must be a single `<link>` to a CDN-hostable file or a
  vendored `.css` file copied into `trellm/web/static/`.
- **No external runtime dependencies for critical paths.** The status
  badge / running tasks / abort button must work even if a CDN font or
  icon set fails to load.
- **Mobile-first or mobile-equal.** Don't degrade to "barely works on
  phones". The current layout collapses to a single column at narrow
  widths; the redesign should be considered against a 375px-wide
  iPhone viewport in addition to a 1440px desktop.
- **No animations that fight content updates.** The 5-second auto-refresh
  redraws sections; gratuitous fade-ins on every refresh will read as
  flicker.
- **Accessibility.** Buttons need accessible labels. Colour is currently
  the only signal on the usage bars and the status badge — designers
  should add a secondary signal (icon, text, pattern).

## 7. Explicit non-goals

So the UX team doesn't go too far:

- **No new functionality.** This is a *redesign*. New features (richer
  filtering, search, multi-user, scheduling, charts beyond what
  `/api/stats` already returns) are out of scope and should go through
  their own product cards.
- **No auth UI.** Single-user tool on localhost. Designers should not
  invent a login screen, account avatar, or user-switcher.
- **No backend changes.** The redesign must work against the existing
  REST/SSE endpoints (§3). If a layout idea genuinely requires a new
  field, file that as a follow-up card — don't bake assumptions about
  new endpoints into the mockups.
- **No persistent data layer beyond `state.json`.** Don't design for a
  database that doesn't exist.

## 8. Reference materials to hand over

When briefing the external UX team, send them:

- This document.
- A live screencast or screenshots of the current dashboard in each of
  these states (record on a real instance):
  1. Idle (no tasks, no queue, recent completions hidden).
  2. One task running with the live output panel open.
  3. Queue with 3+ waiting cards in 2 different projects.
  4. A task showing retry chips (e.g. 2 errors + 1 timeout).
  5. Usage limits at 50% / 80% / 95% to show all three colour states.
  6. Stats > By Project tab populated with 3+ projects.
  7. The Configuration section expanded.
  8. The same dashboard at iPhone width.
- The relevant source files for context (not for them to edit):
  `trellm/web/static/index.html`, `style.css`, `app.js`,
  `pull-to-refresh.js`, and `trellm/web/server.py` for the API shapes.
- This brief's §3 (data model) annotated with example JSON payloads
  pulled from a live instance.

## 9. Acceptance criteria for the redesign deliverable

When the UX team comes back, the deliverable should include:

1. **High-fidelity mockups** for desktop (≥1440px) and mobile (375px)
   covering at minimum: idle state, active task with live output, queue
   with retries, stats screen, controls confirmation flow, configuration
   reference panel. The eight states from §8 are a good shot-list.
2. **A component inventory** — buttons, badges, table rows, cards,
   tabs, bars — with all colour/typography tokens. Vanilla CSS is fine;
   no design-system framework required.
3. **A redlined annotation pass** explaining which CUJ each section
   serves (cross-referencing §4).
4. **Notes on what they would change about the data model** if
   unconstrained — captured separately as suggested follow-up cards,
   not folded into the mockups.

## Out of scope for this card

- Implementing the redesign. This card produces the brief only.
- Re-architecting the embedded server or the REST API shape.
- Picking the UX vendor.
