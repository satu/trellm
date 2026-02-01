# TreLLM

Automation tool bridging Trello boards with AI coding assistants.

TreLLM polls a Trello TODO list, dispatches tasks to Claude Code, and moves completed cards to READY TO TRY.

## Installation

### Option 1: Using pipx (recommended for CLI tools)

```bash
cd ~/src/trellm
pipx install -e .
```

If you don't have pipx:
```bash
sudo apt install pipx
pipx ensurepath
# Restart your shell, then:
pipx install -e .
```

### Option 2: Using a virtual environment

```bash
cd ~/src/trellm
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Option 3: Run directly without installing

```bash
cd ~/src/trellm
python3 -m venv .venv
source .venv/bin/activate
pip install aiohttp pyyaml
python -m trellm
```

## Configuration

1. Copy the example config:
   ```bash
   mkdir -p ~/.trellm
   cp config.yaml.example ~/.trellm/config.yaml
   ```

2. Edit `~/.trellm/config.yaml` with your Trello credentials:
   - Get your API key from: https://trello.com/app-key
   - Generate a token from the same page (click "Token" link)
   - Find board/list IDs in the URL when viewing them in Trello

## Usage

```bash
trellm              # Start polling loop (5 second intervals)
trellm --once       # Process cards once and exit
trellm -v           # Verbose logging
trellm -c path.yaml # Use custom config file
```

If using a venv and didn't install with pipx:
```bash
source .venv/bin/activate
trellm
# Or without activating:
.venv/bin/trellm
```

## How It Works

1. Polls Trello TODO list every 5 seconds
2. For each card, extracts project name from the first word
3. **Pre-compacts** the session before processing a new ticket (preserves context while reducing tokens)
4. **Runs maintenance** if configured (every N tickets, reviews codebase and suggests improvements)
5. Invokes Claude Code with the task, resuming existing sessions
6. **Logs cost and usage stats** after each task completion
7. Moves completed cards to READY TO TRY list
8. Persists session IDs and last processed card ID for conversation continuity

## Token Management

TreLLM automatically manages token usage to prevent context exhaustion:

### Pre-task Compaction
- Runs `/compact` before processing each new ticket
- **Skips compaction** if reprocessing the same card (e.g., moved back to TODO with feedback)
- Preserves project knowledge while keeping context fresh
- Prevents "Prompt too long" errors proactively

### Cost Reporting
After each task, TreLLM logs session usage:
```
[project] Session cost: $0.5500 | API duration: 6m 19.7s | Wall duration: 30m 0.0s
```

### Error Handling
- **Prompt too long**: Automatically runs `/compact` and retries
- **Rate limit**: Parses reset time and sleeps until limit resets

## Commands

TreLLM supports special commands via Trello cards. Create a card with the project name followed by the command:

### /stats
View Claude API usage limits and historical cost statistics.

```
myproject /stats
```

Returns real-time usage limits from the Claude API plus historical statistics for the project.

### /maintenance
Manually trigger a maintenance run for a project.

```
myproject /maintenance
```

Runs the maintenance skill which:
- Reviews CLAUDE.md and suggests updates
- Analyzes git history for patterns
- Checks for documentation gaps
- Posts findings as a comment and creates/updates a maintenance card in ICE BOX

## Maintenance

TreLLM can automatically run periodic maintenance to keep project context fresh.

### Configuration

Enable maintenance globally or per-project in your config:

```yaml
claude:
  # Global maintenance settings (applies to all projects)
  maintenance:
    enabled: true
    interval: 10  # Run maintenance every 10 tickets

  projects:
    myproject:
      working_dir: "~/src/myproject"
      # Per-project settings override global
      maintenance:
        enabled: true
        interval: 5  # This project runs maintenance every 5 tickets
```

### What Maintenance Does

1. **Compacts the session** before running to ensure fresh context
2. **Reviews CLAUDE.md** - checks if it exists and suggests updates based on recent work
3. **Analyzes git history** - identifies frequently modified files and patterns
4. **Checks documentation** - looks for outdated README sections and stale TODOs
5. **Creates/updates a Trello card** in the ICE BOX list with recommendations

### ICE BOX List

To have maintenance create suggestion cards, configure the ICE BOX list ID:

```yaml
trello:
  icebox_list_id: "your-icebox-list-id"
```

Maintenance will create cards named `{project} regular maintenance` with recommendations.
