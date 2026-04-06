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

## Architecture

TreLLM is a polling-based automation tool that bridges Trello boards with Claude Code:

- **`__main__.py`**: Entry point with polling loop, command-line argument parsing, abort/restart command handlers, and web server lifecycle management
- **`claude.py`**: Subprocess-based Claude Code integration using `asyncio.create_subprocess_exec`, with `output_callback` support for live streaming
- **`trello.py`**: Async Trello API client using `aiohttp`
- **`config.py`**: Dataclass-based configuration with file + environment variable loading
- **`state.py`**: JSON-based state persistence for session IDs, ticket counts, and maintenance timestamps
- **`maintenance.py`**: Periodic maintenance skill that runs every N tickets
- **`web/server.py`**: Embedded aiohttp web dashboard with REST API, SSE streaming, usage caching, and task history

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

## Testing

Tests mirror the source structure in `tests/`:
- `test_claude.py` - Claude subprocess integration tests
- `test_config.py` - Configuration loading tests
- `test_main.py` - Command handlers (abort, restart, reset-session) and polling loop tests
- `test_maintenance.py` - Maintenance skill tests
- `test_state.py` - State persistence tests
- `test_trello.py` - Trello API client tests
- `test_web.py` - Web dashboard API, SSE streaming, usage caching tests

Use `pytest` with fixtures for async tests. Mock subprocess calls to avoid actual Claude invocations.
