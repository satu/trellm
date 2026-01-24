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
4. Invokes Claude Code with the task, resuming existing sessions
5. **Logs cost and usage stats** after each task completion
6. Moves completed cards to READY TO TRY list
7. Persists session IDs and last processed card ID for conversation continuity

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
