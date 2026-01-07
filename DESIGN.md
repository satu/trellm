# TreLLM - Technical Design Document

## Executive Summary

This document presents the technical design for TreLLM, a bridge between Trello task management and AI coding assistants. After evaluating multiple approaches, I recommend a **Python-based event-driven architecture** using asyncio with a plugin system for extensibility.

---

## Recommended Approach: Event-Driven Python with Plugin Architecture

### Why This Approach?

1. **Rapid Development**: Python enables quick iteration and prototyping
2. **Extensibility**: Plugin system allows adding new AI assistants without core changes
3. **Async-Native**: asyncio handles polling, webhooks, and multiple tmux sessions efficiently
4. **Low Barrier**: Most developers already have Python installed

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              TreLLM Core                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                 │
│  │   Source    │    │   Router    │    │    Sink     │                 │
│  │   Plugin    │───▶│   (Core)    │───▶│   Plugin    │                 │
│  └─────────────┘    └─────────────┘    └─────────────┘                 │
│        │                   │                  │                         │
│        ▼                   ▼                  ▼                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                 │
│  │   Trello    │    │   State     │    │    tmux     │                 │
│  │   Poller    │    │   Manager   │    │  Injector   │                 │
│  └─────────────┘    └─────────────┘    └─────────────┘                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

Event Flow:
  CardDiscovered → Router → ProjectMatched → CommandInjected → CardProcessed
```

### Core Components

#### 1. Event Bus (Core)

The heart of TreLLM is a simple async event bus that decouples sources from sinks:

```python
# trellm/core/events.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class CardEvent:
    card_id: str
    card_name: str
    card_description: str
    card_url: str
    project: str  # Extracted from card name prefix
    task_description: str  # Card name without project prefix

@dataclass
class InjectionEvent:
    project: str
    command: str
    card_id: str
```

#### 2. Source Plugin: Trello Poller

```python
# trellm/sources/trello.py
class TrelloSource:
    """Polls Trello for new cards in TODO list."""

    async def poll(self) -> AsyncIterator[CardEvent]:
        while True:
            cards = await self.fetch_todo_cards()
            for card in cards:
                if not self.state.is_processed(card.id):
                    yield self.parse_card(card)
            await asyncio.sleep(self.interval)

    def parse_card(self, card) -> CardEvent:
        parts = card.name.split(' ', 1)
        project = parts[0].lower()
        task = parts[1] if len(parts) > 1 else ''
        return CardEvent(
            card_id=card.id,
            card_name=card.name,
            card_description=card.desc,
            card_url=card.url,
            project=project,
            task_description=task
        )
```

#### 3. Router (Core)

```python
# trellm/core/router.py
class Router:
    """Routes cards to appropriate tmux windows based on project."""

    def __init__(self, config: Config, sinks: dict[str, Sink]):
        self.config = config
        self.sinks = sinks  # project_name -> Sink
        self.template = Template(config.command_template)

    async def route(self, event: CardEvent) -> bool:
        if event.project not in self.sinks:
            logger.warning(f"No sink for project: {event.project}")
            return False

        command = self.template.render(
            card_id=event.card_id,
            card_name=event.card_name,
            card_description=event.card_description,
            card_url=event.card_url,
            task_description=event.task_description
        )

        sink = self.sinks[event.project]
        return await sink.inject(command, event.card_id)
```

#### 4. Sink Plugin: tmux Injector

```python
# trellm/sinks/tmux.py
class TmuxSink:
    """Injects commands into a tmux window."""

    def __init__(self, session: str, window: str):
        self.session = session
        self.window = window

    async def inject(self, command: str, card_id: str) -> bool:
        # Escape special characters for tmux
        escaped = self.escape_for_tmux(command)

        # Check if window exists
        if not await self.window_exists():
            logger.error(f"Window {self.window} not found")
            return False

        # Inject the command
        proc = await asyncio.create_subprocess_exec(
            'tmux', 'send-keys', '-t',
            f'{self.session}:{self.window}',
            escaped, 'Enter'
        )
        await proc.wait()
        return proc.returncode == 0

    async def window_exists(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            'tmux', 'list-windows', '-t', self.session,
            stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return self.window in stdout.decode()
```

#### 5. State Manager

```python
# trellm/core/state.py
class StateManager:
    """Tracks processed cards to avoid duplicates."""

    def __init__(self, path: Path):
        self.path = path
        self.state = self.load()

    def is_processed(self, card_id: str) -> bool:
        return card_id in self.state.get('processed', {})

    def mark_processed(self, card_id: str, timestamp: str):
        self.state.setdefault('processed', {})[card_id] = {
            'timestamp': timestamp,
            'status': 'injected'
        }
        self.save()

    def should_reprocess(self, card_id: str, last_activity: str) -> bool:
        """Check if card was moved back to TODO after processing."""
        processed = self.state.get('processed', {}).get(card_id)
        if not processed:
            return True
        return last_activity > processed['timestamp']
```

### Directory Structure

```
trellm/
├── __init__.py
├── __main__.py          # Entry point: python -m trellm
├── cli.py               # Click-based CLI
├── config.py            # Configuration loading
├── core/
│   ├── __init__.py
│   ├── events.py        # Event dataclasses
│   ├── router.py        # Card routing logic
│   ├── state.py         # State persistence
│   └── bus.py           # Async event bus
├── sources/
│   ├── __init__.py
│   ├── base.py          # Source protocol
│   └── trello.py        # Trello polling source
├── sinks/
│   ├── __init__.py
│   ├── base.py          # Sink protocol
│   └── tmux.py          # tmux injection sink
└── templates/
    └── default.txt      # Default command template
```

### Configuration

```yaml
# ~/.trellm/config.yaml
trello:
  api_key: ${TRELLO_API_KEY}
  api_token: ${TRELLO_API_TOKEN}
  board_id: "694dd9802e3ad21db9ca5da1"
  todo_list_id: "694dd98f57680df4b26fe1c1"
  polling_interval: 30

tmux:
  session: "dev"
  # Windows are auto-discovered from project names

command:
  template: |
    Work on Trello card {card_id}: {task_description}

    Card: {card_url}

    {card_description}

state:
  path: ~/.trellm/state.json

logging:
  level: INFO
  file: ~/.trellm/trellm.log
```

### Main Loop

```python
# trellm/__main__.py
async def main():
    config = load_config()
    state = StateManager(config.state.path)

    # Initialize source
    source = TrelloSource(
        api_key=config.trello.api_key,
        api_token=config.trello.api_token,
        board_id=config.trello.board_id,
        list_id=config.trello.todo_list_id,
        interval=config.trello.polling_interval,
        state=state
    )

    # Initialize sinks (one per discovered project/window)
    sinks = discover_tmux_windows(config.tmux.session)

    # Initialize router
    router = Router(config, sinks)

    # Main event loop
    async for card_event in source.poll():
        logger.info(f"New card: {card_event.card_name}")

        # Add acknowledgment comment
        await source.add_comment(
            card_event.card_id,
            "Claude: Starting work on this task..."
        )

        # Route to appropriate sink
        success = await router.route(card_event)

        if success:
            state.mark_processed(card_event.card_id, datetime.utcnow().isoformat())
            logger.info(f"Injected card {card_event.card_id} to {card_event.project}")
        else:
            logger.error(f"Failed to inject card {card_event.card_id}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Alternative Approaches Considered

### Alternative 1: Go-based Single Binary

**Description**: Implement TreLLM as a statically-compiled Go binary with embedded configuration.

**Architecture**:
```
┌─────────────────────────────────────────┐
│           TreLLM (Go Binary)            │
├─────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  │ Trello  │  │  State  │  │  tmux   │  │
│  │ Client  │  │  (Bolt) │  │ Wrapper │  │
│  └─────────┘  └─────────┘  └─────────┘  │
│         │          │           │        │
│         └────────┬─────────────┘        │
│                  ▼                      │
│           ┌───────────┐                 │
│           │   Main    │                 │
│           │   Loop    │                 │
│           └───────────┘                 │
└─────────────────────────────────────────┘
```

**Pros**:
- Single binary distribution (no dependencies)
- Excellent performance and low memory footprint
- Built-in concurrency with goroutines
- Can run as a systemd service easily
- BoltDB for embedded state storage

**Cons**:
- Longer development time
- Less flexible for rapid prototyping
- Harder to add plugins/extensions
- Requires Go toolchain for modifications

**When to choose**: When distribution to multiple machines is important, or when running on resource-constrained systems.

---

### Alternative 2: Node.js with Real-time Webhooks

**Description**: Use Node.js with Express to receive Trello webhooks for instant notifications, plus Socket.io for a real-time dashboard.

**Architecture**:
```
                    ┌─────────────────┐
                    │    Trello       │
                    │   Webhooks      │
                    └────────┬────────┘
                             │ POST /webhook
                             ▼
┌─────────────────────────────────────────────────────┐
│                  TreLLM (Node.js)                   │
├─────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │   Express   │  │   Event     │  │  Socket.io  │  │
│  │   Server    │──│   Queue     │──│   Server    │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
│         │                │                │         │
│         ▼                ▼                ▼         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  Webhook    │  │   Worker    │  │  Dashboard  │  │
│  │  Handler    │  │  (Inject)   │  │   (React)   │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Pros**:
- Real-time notifications (no polling delay)
- Built-in web dashboard capability
- Excellent async handling
- Large ecosystem (npm)
- WebSocket support for live updates

**Cons**:
- Requires public endpoint for webhooks (ngrok for local dev)
- Larger dependency footprint (node_modules)
- More complex setup
- Webhook registration complexity

**When to choose**: When real-time responsiveness is critical, or when a web dashboard is a priority.

---

### Alternative 3: Shell Script + cron

**Description**: Ultra-minimal approach using bash scripts and cron for polling.

**Architecture**:
```
┌─────────────────────────────────────────┐
│              cron (every 30s)           │
│                    │                    │
│                    ▼                    │
│  ┌─────────────────────────────────┐    │
│  │         trellm.sh               │    │
│  │  ┌─────────────────────────┐    │    │
│  │  │ 1. curl Trello API      │    │    │
│  │  │ 2. jq parse JSON        │    │    │
│  │  │ 3. Check state file     │    │    │
│  │  │ 4. tmux send-keys       │    │    │
│  │  │ 5. Update state file    │    │    │
│  │  └─────────────────────────┘    │    │
│  └─────────────────────────────────┘    │
│                                         │
│  State: ~/.trellm/processed.txt         │
└─────────────────────────────────────────┘
```

**Implementation sketch**:
```bash
#!/bin/bash
# trellm.sh - Minimal TreLLM implementation

STATE_FILE="$HOME/.trellm/processed.txt"
API_KEY="${TRELLO_API_KEY}"
TOKEN="${TRELLO_TOKEN}"
BOARD_ID="694dd9802e3ad21db9ca5da1"
LIST_ID="694dd98f57680df4b26fe1c1"

# Fetch TODO cards
cards=$(curl -s "https://api.trello.com/1/lists/$LIST_ID/cards?key=$API_KEY&token=$TOKEN")

# Process each card
echo "$cards" | jq -r '.[] | "\(.id)|\(.name)"' | while IFS='|' read -r id name; do
    # Skip if already processed
    grep -q "^$id$" "$STATE_FILE" 2>/dev/null && continue

    # Extract project (first word)
    project=$(echo "$name" | cut -d' ' -f1)
    task=$(echo "$name" | cut -d' ' -f2-)

    # Inject into tmux
    tmux send-keys -t "dev:$project" "Work on card $id: $task" Enter

    # Mark as processed
    echo "$id" >> "$STATE_FILE"
done
```

**Pros**:
- Zero dependencies (just bash, curl, jq)
- Trivial to understand and modify
- Works on any Unix system
- Easy to debug
- Can run from cron or as a loop

**Cons**:
- Limited error handling
- No webhook support possible
- Harder to extend
- No configuration file (hardcoded or env vars)
- State management is basic

**When to choose**: For quick personal use, proof of concept, or systems where installing Python/Node is not possible.

---

## Comparison Matrix

| Criteria | Python (Recommended) | Go | Node.js | Shell |
|----------|---------------------|-----|---------|-------|
| Development Speed | Fast | Slow | Medium | Very Fast |
| Distribution | pip install | Single binary | npm install | Copy script |
| Dependencies | Few (requests, pyyaml) | None | Many | curl, jq |
| Extensibility | Excellent (plugins) | Good | Excellent | Poor |
| Real-time Support | Polling + optional webhook | Polling | Native webhooks | Polling only |
| Dashboard | Possible (Flask) | Possible | Easy (Express) | No |
| Memory Footprint | Medium (~30MB) | Low (~10MB) | High (~50MB+) | Minimal |
| Maintenance | Easy | Medium | Medium | Easy |

---

## Implementation Roadmap

### Week 1: MVP
- [ ] Project skeleton with Poetry
- [ ] Trello poller with state management
- [ ] tmux injector
- [ ] Basic CLI (start, stop, status)
- [ ] Configuration loading

### Week 2: Polish
- [ ] Logging and error handling
- [ ] Retry logic with exponential backoff
- [ ] Acknowledgment comments on cards
- [ ] Project validation

### Week 3: Advanced Features
- [ ] Webhook support (optional)
- [ ] Idle detection for Claude Code
- [ ] Simple status dashboard

---

## Decision

**I recommend the Python-based event-driven approach** because:

1. **Speed to MVP**: We can have a working prototype in days, not weeks
2. **Flexibility**: Plugin architecture allows easy extension
3. **Maintainability**: Python is readable and widely known
4. **Good Enough Performance**: For a polling service running every 30s, Python's performance is more than adequate
5. **Future Options**: If distribution becomes important, we can rewrite the core in Go while keeping the same architecture

The shell script alternative is a viable "plan B" for immediate use while developing the full solution.
