# TreLLM Web Dashboard — Mini PRD

## Problem

TreLLM runs as a background polling process with no visibility into its state. The only way to check status, view output, or trigger actions (abort, restart, stats) is through Trello cards. This creates friction: you need to create cards for simple operations, wait for the next poll cycle, and read results from card comments — which are poorly suited for streaming output or large logs.

## Goal

A lightweight web dashboard that provides real-time visibility into TreLLM's state and allows direct control without creating Trello cards.

## Non-Goals

- User authentication (single-user tool, localhost only)
- Persistent storage beyond what `state.json` already provides
- Replacing the Trello card workflow — the dashboard is complementary
- Mobile-optimized UI

## Features

### P0 — Must Have

**1. Status Overview**
- Current polling state (running / paused / error)
- Uptime, current poll interval, last poll timestamp
- List of configured projects with their working directories

**2. Running Tasks**
- Table of currently processing cards: project, card name, duration, card link
- Per-task abort button (equivalent to project-level cancel, not global `/abort`)

**3. Per-Project Last Execution Log**
- For each project, show the last completed card: name, result (success/error), duration, cost
- Expandable section with Claude's full output (stdout) from the last run
- Link to the Trello card

**4. Stats Dashboard**
- Display the same data as `/stats`: per-project and aggregate cost, token usage, card counts
- Auto-refresh after each ticket execution completes
- 30-day summary view matching the existing stats format

**5. Control Actions**
- Restart button (triggers `RestartRequested`, equivalent to `trellm /restart`)
- Global abort button (equivalent to `trellm /abort`)

### P1 — Nice to Have

**6. Live Output Streaming**
- While a task is running, stream Claude's stderr (progress) to the browser via WebSocket/SSE
- Auto-scroll with option to pause scrolling

**7. Recent Ticket History**
- Table of last N completed tickets across all projects
- Sortable by project, date, cost, duration

**8. Configuration Viewer**
- Read-only view of current config (with secrets masked)
- Show when config was last hot-reloaded and what changed

## Technical Approach

### Architecture

```
Browser  <──  HTTP/SSE  ──>  TreLLM Web Server (aiohttp)
                                    │
                                    ├── reads state.json
                                    ├── reads running task set
                                    └── triggers commands via shared async primitives
```

**Key decision: embedded server, not a separate process.** The web server runs inside the existing TreLLM process as an additional `asyncio` task alongside the polling loop. This gives it direct access to in-memory state (`_running_tasks`, `_processing_cards`, `_project_locks`) without IPC.

### Stack

- **Backend**: `aiohttp.web` — already an `aiohttp` dependency exists for the Trello client
- **Frontend**: Vanilla HTML/CSS/JS — no build step, no npm, served as static files from a `trellm/web/` directory
- **Streaming**: Server-Sent Events (SSE) for live output and stats refresh — simpler than WebSocket for one-directional data flow
- **API**: JSON REST endpoints under `/api/`

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve dashboard HTML |
| `GET` | `/api/status` | Polling state, uptime, config summary |
| `GET` | `/api/tasks` | Currently running tasks |
| `GET` | `/api/projects` | Per-project last execution + stats |
| `GET` | `/api/stats` | Full stats (same data as `/stats` command) |
| `GET` | `/api/history` | Recent completed tickets |
| `POST` | `/api/abort` | Global abort |
| `POST` | `/api/abort/{project}` | Per-project abort |
| `POST` | `/api/restart` | Trigger restart |
| `GET` | `/api/stream/{task_id}` | SSE stream of task output |

### Data Flow

**Stats refresh**: When a ticket completes, the existing `_task_done_callback` fires. The web server listens for the same event and pushes an SSE update to connected browsers.

**Live output**: Requires piping Claude subprocess stderr through a buffer that both the existing logging and the SSE endpoint can read. Implementation: an `asyncio.Queue` per running task that the stream endpoint consumes.

**Abort/Restart**: Call the same `handle_abort_command()` / raise `RestartRequested` that the Trello card commands use. No new logic needed — just new triggers.

### Configuration

```yaml
web:
  enabled: bool = false       # Opt-in
  host: str = "127.0.0.1"    # Localhost only by default
  port: int = 8077
```

### File Structure

```
trellm/
├── web/
│   ├── server.py        # aiohttp app, routes, SSE
│   ├── static/
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js
│   └── __init__.py
```

## Implementation Plan

**Phase 1** — Static dashboard (P0 items 1-4)
- Add `aiohttp.web` server as async task in polling loop
- Implement `/api/status`, `/api/tasks`, `/api/projects`, `/api/stats`
- Build HTML/JS dashboard with polling-based refresh (every 5s)
- Add configuration section, tests

**Phase 2** — Control actions (P0 item 5)
- Implement `/api/abort` and `/api/restart`
- Add buttons to UI with confirmation dialogs

**Phase 3** — Live streaming (P1 item 6)
- Add per-task output buffering in `claude.py`
- Implement SSE endpoint
- Add streaming output panel to UI

**Phase 4** — Polish (P1 items 7-8)
- Add ticket history table
- Add config viewer
- Improve UI styling

## Open Questions

1. **Should the dashboard be accessible on LAN?** Default is localhost-only, but exposing on `0.0.0.0` could be useful for checking from a phone. If so, should we add basic auth?
2. **Output retention**: How much stdout/stderr to keep in memory per task? Propose 1MB cap per task, last 10 tasks.
3. **Should stats auto-refresh use SSE push or client-side polling?** SSE is more efficient but adds complexity in Phase 1. Proposal: start with client polling, upgrade to SSE in Phase 3.
