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
3. Invokes Claude Code with the task, resuming existing sessions
4. Moves completed cards to READY TO TRY list
5. Persists session IDs for conversation continuity
