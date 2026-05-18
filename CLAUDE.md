# TreLLM Development Guide

## Quick Reference

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=trellm

# Run specific test file
pytest tests/test_claude.py

# Run the application
trellm                  # Start polling loop
trellm --once          # Process one batch and exit
trellm -v              # Verbose logging
```

**Entrypoint**: `start-trellm.sh` is the canonical long-running entrypoint (Docker-compatible: ensures venv, installs the package, optionally brings up the patchright browser stack, then runs `trellm -v`). The `trellm` CLI is fine for one-shots. The earlier systemd-user-service path is retired.

## Architecture

TreLLM is a polling-based automation tool that bridges Trello boards with Claude Code:

- **`__main__.py`**: Entry point with polling loop, command-line argument parsing, abort/restart command handlers, and web server lifecycle management
- **`claude.py`**: Subprocess-based Claude Code integration using `asyncio.create_subprocess_exec`, with `output_callback` support for live streaming
- **`trello.py`**: Async Trello API client using `aiohttp`
- **`config.py`**: Dataclass-based configuration with file + environment variable loading
- **`state.py`**: JSON-based state persistence for session IDs, ticket counts, and maintenance timestamps
- **`maintenance.py`**: Periodic maintenance skill that runs every N tickets
- **`web/server.py`**: Embedded aiohttp web dashboard with REST API, SSE streaming, usage caching, and task history
- **`docs/`**: Long-form investigation and decision notes for cards that produce no code change (e.g. `prd-web-dashboard.md`, `patchright-mcp.md`, `claude-interactive.md`, `dashboard-ux-handoff.md`). Future investigation cards should land their findings here.

## Key Patterns

### Subprocess Execution
All Claude Code interactions use `asyncio.create_subprocess_exec` with JSON output parsing:
```python
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=cwd,
    limit=10 * 1024 * 1024,  # 10MB buffer
)
stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
```

### JSON Output Parsing
Claude output may contain multiple JSON lines. **Always parse in reverse** to find the final result:
```python
for line in reversed(output.strip().split("\n")):
    if line.strip().startswith("{"):
        try:
            data = json.loads(line)
            if "session_id" in data:
                session_id = data["session_id"]
                break
        except json.JSONDecodeError:
            continue
```

### Project Alias Resolution
Projects can have aliases (short names). `Config.resolve_project()` maps aliases to canonical names:
```python
# In config.yaml:
#   smugcoin:
#     working_dir: ~/src/smugcoin
#     aliases: ["smg"]
# Card "smg fix bug" resolves to project "smugcoin"
project = config.resolve_project(parse_project(card.name))
```

### Configuration Override Pattern
Global settings with per-project overrides:
```python
def get_maintenance_config(self, project: str) -> Optional[MaintenanceConfig]:
    proj = self.claude.projects.get(project)
    if proj and proj.maintenance is not None:
        return proj.maintenance  # Per-project takes priority
    return self.claude.maintenance  # Fall back to global
```
The same shape applies to `Config.get_timeout(project)` — per-project
`timeout` overrides the global `claude.timeout`. `__main__.py` resolves it
per card and threads it into `claude.run(..., timeout=…)`. Add e.g.
`smugcoin: { timeout: 1800 }` in `config.yaml` when 20 minutes isn't enough.

`Config.get_runner_mode(project)` follows the identical pattern: per-project
`runner` overrides the global `claude.runner`, default `"print"`. It selects
the `claude` transport — `print` (one `claude -p` subprocess per card) or
`interactive` (a long-lived TUI). The `ClaudeSession` seam in `session.py`
(`PrintSession`, `InteractiveSession`, `SessionManager`) is what the polling
loop and `maintenance.py` go through so the transport is chosen per project
without the call site knowing which is in use. Both transports have a
backend: `PrintSession` (one `claude -p` per card) and `InteractiveSession`
(one long-lived tmux `claude` TUI per project, driving M2's `tmux.py` + M3's
completion detector — see `docs/claude-interactive.md`). `interactive` stays
off for every real project until the M5 PoC validates it; `maintenance.py`
is not yet wired through `InteractiveSession`, so it skips interactive
projects.

### Live Output Streaming
`claude.py` supports an `output_callback` for streaming parsed stdout (text, thinking, tool results) to SSE clients. When set, it enables `--output-format stream-json` and forwards decoded output to the web dashboard:
```python
async def run(self, ..., output_callback: Optional[callable] = None) -> ClaudeResult:
```

### Usage API Rate Limiting
The web dashboard caches usage data with a 5-minute cooldown between API calls, persisted across restarts via the state file:
```python
self._usage_cooldown = 300  # Minimum seconds between API calls
persisted = self.state.state.get("usage_cache", {})
self._usage_cache: Optional[dict] = persisted.get("data")
```
Don't cache 429 errors — allow retry on next request. Use Claude Code's `User-Agent` header.

### State Management
`StateManager` uses JSON persistence:
- Session IDs are updated after each Claude interaction
- Session IDs change after `/compact` - must capture and persist the new ID
- Ticket counts are tracked per-project for maintenance scheduling
- Unique ticket IDs prevent double-counting when cards are moved back to TODO

## Gotchas

1. **Session IDs change after `/compact`** - The compaction command creates a new session with a new ID. Always capture the new session ID from the JSON output and update state.

2. **JSON output parsing** - Claude's JSON output stream may have multiple lines or partial JSON. Always iterate in reverse and handle `JSONDecodeError`.

3. **Rate limiting** - Trello API has rate limits. The `_handle_api_error` method parses `Retry-After` headers and sleeps appropriately.

4. **Buffer limits** - Claude can output a lot of text. The subprocess uses a 10MB buffer limit to prevent memory issues.

5. **Timeout handling** - Claude tasks can take a long time. Default timeout is 20 minutes (1200 seconds) but maintenance tasks use 10 minutes (600 seconds).

6. **Usage API 429** - The Anthropic usage API requires the correct `User-Agent` header (matching Claude Code's version) and aggressive rate limiting (5-minute cooldown). Caching failures or starting cooldown on errors causes cascading issues.

7. **Stale session IDs** - Sessions can become invalid (e.g., after Claude Code updates). `claude.py` detects "session not found" errors via `SESSION_NOT_FOUND_PATTERN` and retries without the session ID.

8. **Claude monthly limit / extra-usage failures pause polling globally** - When Claude reports an account-wide usage limit, `claude.py` raises `MonthlyLimitError` (intentionally NOT a `RateLimitError` subclass, so the in-process retry loop doesn't swallow it). `__main__.py` then pauses the **global** polling loop via `is_globally_rate_limited()` — not per-card and not per-project, since these limits are account-wide. Cards stay in TODO; commands (`/abort`, `/restart`, etc.) still work. Two patterns currently trigger this:
   - `MONTHLY_LIMIT_PATTERN` ("you've hit your org's monthly usage limit") — no parseable reset, defaults to 1h pause. Original incident: commit `6f31e07` (44 retries in production before the fix).
   - `EXTRA_USAGE_PATTERN` ("you're out of extra usage · resets X:XXam (UTC)") — Claude Code's OAuth credit-depletion message; the reset clock-time is parsed via `_parse_rate_limit_reset_time`. Original incident: card `ZCwyx8wO` (388 retries across two smugcoin cards over ~7h before the fix). If you ever see a new wording for a usage-limit-style failure that isn't pausing the loop, add a pattern in `_check_for_errors` rather than tightening the polling loop — that's the single dispatch site.

9. **Per-card retry backoff** - Gotcha #8 covers account-wide usage limits, but other failures (timeouts, generic `RuntimeError`s) would still busy-loop the same card every poll cycle. `__main__.py` keeps a `CardRetryState` per card id in `_card_retry_state`. A failure that exits within `FAST_FAILURE_THRESHOLD_SECONDS` (60s) counts as a *fast failure* and pushes the card into an **exponential per-card backoff** window — `BASE_BACKOFF_SECONDS` (30s) doubling on each consecutive fast failure, capped at `MAX_BACKOFF_SECONDS` (30 min): 30, 60, 120, ... 1800. A slow failure (≥60s — it did real work, not a busy-loop) resets the streak. The picker skips a card via `should_skip_card_for_backoff()` while it is in backoff, and a *success* clears the card's state entirely (`_card_retry_state.pop`). Two companion behaviors: (a) `find_pending_sibling_for_project()` defers other TODO cards for the same project while a just-failed sibling is mid-retry, so the picker "sticks with" the failing card instead of clobbering its session context; (b) on each failure a retry-context comment (`_build_retry_context_comment`) is posted to the card so the next run knows the previous one died — and whether by timeout or by error. This is per-card and per-failure-mode, distinct from the global pause in #8.

## Testing

Tests mirror the source structure in `tests/`:
- `test_browser_scripts.py` - Static-structure checks on the browser-stack shell scripts (Xvfb + Chrome + x11vnc + noVNC)
- `test_claude.py` - Claude subprocess integration tests
- `test_claude_md.py` - Structural checks pinning load-bearing CLAUDE.md content
- `test_completion.py` - Interactive-mode completion detector (`trellm/completion.py`: Stop-hook signal watcher, sentinel marker, §4 stack) + `scripts/trellm-stop-hook.sh` checks
- `test_config.py` - Configuration loading tests
- `test_icon_utils.py` - `icon_utils` image-processing helper tests
- `test_interactive_session.py` - `InteractiveSession` interactive `claude` transport backend (`trellm/session.py`: tmux window lifecycle, prompt-file dispatch, §4 confirmation stack, transcript error scan incl. gotcha #8 regression)
- `test_main.py` - Command handlers (abort, restart, reset-session), polling loop, and per-card retry/backoff tests
- `test_maintenance.py` - Maintenance skill tests
- `test_session.py` - `ClaudeSession` transport seam (`PrintSession`, `InteractiveSession` resolution, `SessionManager`)
- `test_start_script.py` - `start-trellm.sh` startup script tests
- `test_start_trellm.py` - Browser-stack auto-start path (`scripts/needs-browser-stack.py` + `start-trellm.sh`)
- `test_state.py` - State persistence tests
- `test_tmux.py` - `tmux` control module (`TmuxController`) — mocked-`tmux` unit tests + a real-`tmux` integration test
- `test_web.py` - Web dashboard API, SSE streaming, usage caching tests

`test_claude_md.py` keeps this list honest — adding a `tests/test_*.py` file without listing it here, or listing one that no longer exists, fails the suite.

Use `pytest` with fixtures for async tests. Mock subprocess calls to avoid actual Claude invocations.
