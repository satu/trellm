# TreLLM - Technical Design Document

## Executive Summary

This document presents the technical design for TreLLM, a bridge between Trello task management and AI coding assistants. After evaluating multiple approaches, I recommend implementing TreLLM as a **Python subprocess orchestrator** that invokes Claude Code with `--resume` for session persistence.

**Key insight**: Claude Code supports session persistence via `--resume <session_id>`. TreLLM stores session IDs per project and resumes from them, maintaining full development context across tasks. This eliminates the need for:
- Running Claude Code interactively in tmux
- MCP server complexity
- Any manual terminal interaction

**Why polling is good enough**: Trello's API allows 300 requests per 10 seconds. With 5-second polling intervals, TreLLM uses only ~12 requests/minute—well within limits. This provides 0-5 second latency without the complexity of webhooks.

---

## Recommended Approach: Python Subprocess Orchestrator

### Why This Approach?

1. **Zero Manual Interaction**: TreLLM runs Claude Code as a subprocess - no terminal needed
2. **Session Persistence**: `--resume` maintains full context (files, permissions, working dir)
3. **Simple Architecture**: Just subprocess invocation and JSON parsing
4. **Non-Invasive**: Uses Claude Code's existing CLI, no extensions needed
5. **Fully Automated**: Add Trello card from phone → task gets done automatically

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            TreLLM Orchestrator                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                         Main Loop                                 │   │
│  │                                                                   │   │
│  │  while True:                                                      │   │
│  │    cards = trello.get_todo_cards()                               │   │
│  │    for card in cards:                                            │   │
│  │      if not state.is_processed(card.id):                         │   │
│  │        project = parse_project(card.name)                        │   │
│  │        session_id = state.get_session(project)                   │   │
│  │        result = run_claude(card, session_id)                     │   │
│  │        state.update_session(project, result.session_id)          │   │
│  │        trello.move_to_ready(card.id)                             │   │
│  │    sleep(POLL_INTERVAL)                                          │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │  Trello Client  │  │  State Manager  │  │  Claude Runner  │         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Trello API    │    │   state.json    │    │   Claude Code   │
│                 │    │                 │    │   (subprocess)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### Core Components

#### 1. Main Entry Point

```python
#!/usr/bin/env python3
# trellm/__main__.py

import asyncio
import logging
from .trello import TrelloClient
from .state import StateManager
from .claude import ClaudeRunner
from .config import load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    config = load_config()
    trello = TrelloClient(config.trello)
    state = StateManager(config.state_file)
    claude = ClaudeRunner(config.claude)

    logger.info("TreLLM started, polling every %d seconds", config.poll_interval)

    while True:
        try:
            await process_cards(trello, state, claude, config)
        except Exception as e:
            logger.error("Error processing cards: %s", e)

        await asyncio.sleep(config.poll_interval)


async def process_cards(trello, state, claude, config):
    cards = await trello.get_todo_cards()
    logger.info("Found %d cards in TODO", len(cards))

    for card in cards:
        if state.is_processed(card.id):
            continue

        # Check if card was moved back (has our comment but back in TODO)
        if state.should_reprocess(card.id, card.last_activity):
            logger.info("Card %s moved back to TODO, reprocessing", card.id)

        project = parse_project(card.name)
        logger.info("Processing card %s for project %s", card.id, project)

        # Add acknowledgment comment
        await trello.add_comment(
            card.id,
            f"Claude: Starting work on this task..."
        )

        # Get session ID for this project (if exists)
        session_id = state.get_session(project)

        # Run Claude Code
        try:
            result = await claude.run(
                card=card,
                project=project,
                session_id=session_id,
                working_dir=config.get_working_dir(project)
            )

            # Update session ID for next task
            if result.session_id:
                state.set_session(project, result.session_id)

            # Mark as processed and move card
            state.mark_processed(card.id)
            await trello.move_to_ready(card.id)
            await trello.add_comment(
                card.id,
                f"Claude: Task completed.\n\n{result.summary}"
            )
            logger.info("Completed card %s", card.id)

        except Exception as e:
            logger.error("Failed to process card %s: %s", card.id, e)
            await trello.add_comment(
                card.id,
                f"Claude: Error processing task: {e}"
            )


def parse_project(card_name: str) -> str:
    """Extract project name (first word) from card name."""
    return card_name.split()[0].lower()


if __name__ == "__main__":
    asyncio.run(main())
```

#### 2. Claude Runner

```python
# trellm/claude.py

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    success: bool
    session_id: Optional[str]
    summary: str
    output: str


class ClaudeRunner:
    def __init__(self, config):
        self.binary = config.get("binary", "claude")
        self.timeout = config.get("timeout", 600)  # 10 minutes default

    async def run(
        self,
        card,
        project: str,
        session_id: Optional[str],
        working_dir: Optional[str]
    ) -> ClaudeResult:
        """Run Claude Code as a subprocess with the given task."""

        # Build the prompt
        prompt = self._build_prompt(card)

        # Build command
        cmd = [
            self.binary,
            "-p", prompt,
            "--output-format", "json"
        ]

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info("Running: %s", " ".join(cmd[:4]) + "...")

        # Run subprocess
        cwd = Path(working_dir).expanduser() if working_dir else None

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise Exception(f"Claude Code timed out after {self.timeout}s")

        if proc.returncode != 0:
            raise Exception(f"Claude Code failed: {stderr.decode()}")

        # Parse JSON output
        output = stdout.decode()
        return self._parse_output(output)

    def _build_prompt(self, card) -> str:
        """Build the prompt for Claude Code."""
        parts = [
            f"Work on Trello card {card.id}: {card.name}",
            "",
            f"Card URL: {card.url}",
        ]

        if card.description:
            parts.extend(["", "Description:", card.description])

        parts.extend([
            "",
            "When done, commit your changes and provide a brief summary."
        ])

        return "\n".join(parts)

    def _parse_output(self, output: str) -> ClaudeResult:
        """Parse Claude Code's JSON output."""
        try:
            # Claude outputs multiple JSON objects, get the last one
            lines = output.strip().split("\n")
            for line in reversed(lines):
                if line.startswith("{"):
                    data = json.loads(line)
                    return ClaudeResult(
                        success=True,
                        session_id=data.get("session_id"),
                        summary=data.get("result", "Task completed"),
                        output=output
                    )
        except json.JSONDecodeError:
            pass

        # Fallback if JSON parsing fails
        return ClaudeResult(
            success=True,
            session_id=None,
            summary="Task completed (no JSON output)",
            output=output
        )
```

#### 3. State Manager

```python
# trellm/state.py

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_file: str):
        self.path = Path(state_file).expanduser()
        self.state = self._load()

    def _load(self) -> dict:
        """Load state from file."""
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception as e:
                logger.error("Failed to load state: %s", e)
        return {"sessions": {}, "processed": {}}

    def _save(self):
        """Save state to file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))

    def get_session(self, project: str) -> Optional[str]:
        """Get session ID for a project."""
        session = self.state.get("sessions", {}).get(project)
        return session.get("session_id") if session else None

    def set_session(self, project: str, session_id: str):
        """Store session ID for a project."""
        self.state.setdefault("sessions", {})[project] = {
            "session_id": session_id,
            "last_activity": datetime.utcnow().isoformat()
        }
        self._save()

    def is_processed(self, card_id: str) -> bool:
        """Check if a card has been processed."""
        return card_id in self.state.get("processed", {})

    def should_reprocess(self, card_id: str, last_activity: str) -> bool:
        """Check if a card should be reprocessed (moved back to TODO)."""
        processed = self.state.get("processed", {}).get(card_id)
        if not processed:
            return False
        return last_activity > processed.get("processed_at", "")

    def mark_processed(self, card_id: str):
        """Mark a card as processed."""
        self.state.setdefault("processed", {})[card_id] = {
            "processed_at": datetime.utcnow().isoformat(),
            "status": "complete"
        }
        self._save()
```

#### 4. Trello Client

```python
# trellm/trello.py

import aiohttp
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TrelloCard:
    id: str
    name: str
    description: str
    url: str
    last_activity: str


class TrelloClient:
    BASE_URL = "https://api.trello.com/1"

    def __init__(self, config):
        self.api_key = config["api_key"]
        self.api_token = config["api_token"]
        self.board_id = config["board_id"]
        self.todo_list_id = config["todo_list_id"]
        self.ready_list_id = config.get("ready_to_try_list_id")

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated request to Trello API."""
        url = f"{self.BASE_URL}{path}"
        params = kwargs.pop("params", {})
        params["key"] = self.api_key
        params["token"] = self.api_token

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, params=params, **kwargs) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_todo_cards(self) -> List[TrelloCard]:
        """Get all cards in the TODO list."""
        data = await self._request("GET", f"/lists/{self.todo_list_id}/cards")
        return [
            TrelloCard(
                id=c["id"],
                name=c["name"],
                description=c.get("desc", ""),
                url=c["url"],
                last_activity=c.get("dateLastActivity", "")
            )
            for c in data
        ]

    async def add_comment(self, card_id: str, text: str):
        """Add a comment to a card."""
        await self._request(
            "POST",
            f"/cards/{card_id}/actions/comments",
            json={"text": text}
        )

    async def move_to_ready(self, card_id: str):
        """Move a card to the READY TO TRY list."""
        if not self.ready_list_id:
            # Discover the list
            lists = await self._request("GET", f"/boards/{self.board_id}/lists")
            for lst in lists:
                if lst["name"] == "READY TO TRY":
                    self.ready_list_id = lst["id"]
                    break

        if self.ready_list_id:
            await self._request(
                "PUT",
                f"/cards/{card_id}",
                json={"idList": self.ready_list_id}
            )
```

#### 5. Configuration

```python
# trellm/config.py

import os
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class Config:
    trello: dict
    claude: dict
    poll_interval: int
    state_file: str
    projects: Dict[str, dict]

    def get_working_dir(self, project: str) -> Optional[str]:
        """Get working directory for a project."""
        proj = self.projects.get(project, {})
        return proj.get("working_dir")


def load_config() -> Config:
    """Load configuration from file and environment."""
    config_path = Path("~/.trellm/config.yaml").expanduser()

    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f)
    else:
        data = {}

    # Override with environment variables
    trello = data.get("trello", {})
    trello["api_key"] = os.environ.get("TRELLO_API_KEY", trello.get("api_key"))
    trello["api_token"] = os.environ.get("TRELLO_API_TOKEN", trello.get("api_token"))
    trello["board_id"] = os.environ.get("TRELLO_BOARD_ID", trello.get("board_id"))
    trello["todo_list_id"] = os.environ.get("TRELLO_TODO_LIST_ID", trello.get("todo_list_id"))

    return Config(
        trello=trello,
        claude=data.get("claude", {}),
        poll_interval=data.get("polling", {}).get("interval_seconds", 5),
        state_file=data.get("state", {}).get("file", "~/.trellm/state.json"),
        projects=data.get("claude", {}).get("projects", {})
    )
```

### Directory Structure

```
trellm/
├── pyproject.toml
├── README.md
├── trellm/
│   ├── __init__.py
│   ├── __main__.py      # Entry point
│   ├── claude.py        # Claude Code subprocess runner
│   ├── trello.py        # Trello API client
│   ├── state.py         # State persistence
│   └── config.py        # Configuration loading
└── tests/
    ├── test_claude.py
    ├── test_trello.py
    └── test_state.py
```

### Installation & Usage

```bash
# Install
pip install trellm

# Or install from source
git clone https://github.com/satu/trellm
cd trellm
pip install -e .

# Configure
mkdir -p ~/.trellm
cat > ~/.trellm/config.yaml << EOF
trello:
  api_key: ${TRELLO_API_KEY}
  api_token: ${TRELLO_API_TOKEN}
  board_id: "your-board-id"
  todo_list_id: "your-todo-list-id"

claude:
  projects:
    trellm:
      working_dir: ~/src/trellm
EOF

# Run
trellm
# Or: python -m trellm
```

---

## Alternative Approaches Considered

### Alternative 1: MCP Server (Previous Approach)

**Description**: TreLLM runs as an MCP server that Claude Code connects to. Claude Code queries for tasks via MCP protocol.

**Architecture**:
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Claude Code   │◀───▶│  TreLLM (MCP)   │◀───▶│   Trello API    │
│   (MCP client)  │     │   Server        │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Pros**:
- Bidirectional communication
- Claude Code can query for tasks
- Standard protocol

**Cons**:
- Still requires running Claude Code interactively
- User must manually invoke `/next`
- Doesn't solve the "go to terminal" problem
- More complex setup (MCP configuration)

**When to choose**: When you want Claude Code to pull tasks on demand (semi-automated).

---

### Alternative 2: tmux Injection (Original Approach)

**Description**: TreLLM monitors Trello and injects commands into a running Claude Code session via `tmux send-keys`.

**Architecture**:
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Trello API    │────▶│    TreLLM       │────▶│  tmux send-keys │
│                 │     │   (monitor)     │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                                ┌─────────────────┐
                                                │  Claude Code    │
                                                │  (interactive)  │
                                                └─────────────────┘
```

**Pros**:
- Works with interactive Claude Code
- No changes to Claude Code needed

**Cons**:
- Requires tmux (not universal)
- Fragile text injection
- No feedback channel
- Can't detect busy/idle state
- Still requires terminal running

**When to choose**: Quick POC or when subprocess approach isn't viable.

---

### Alternative 3: File-based Queue

**Description**: TreLLM writes tasks to a file, a separate script reads and invokes Claude Code.

**Architecture**:
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Trello API    │────▶│    TreLLM       │────▶│   tasks.json    │
│                 │     │   (writer)      │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                                ┌─────────────────┐
                                                │  Worker script  │
                                                │  (reads & runs) │
                                                └─────────────────┘
```

**Pros**:
- Decoupled components
- Easy to debug (inspect file)
- Can be processed by any tool

**Cons**:
- More moving parts
- File coordination complexity
- No advantage over direct subprocess

**When to choose**: When you want to decouple polling from execution.

---

## Comparison Matrix

| Criteria | Polling (Recommended) | Webhook (Optional) | MCP Server | tmux Injection |
|----------|----------------------|-------------------|------------|----------------|
| Manual intervention | None | None | Yes (/next) | None |
| Latency | 0-5 seconds | <1 second | Immediate | 0-30 seconds |
| Session persistence | Native (--resume) | Native (--resume) | External | N/A |
| Complexity | **Low** | Medium | Medium | Low |
| Dependencies | Claude CLI | Claude CLI + Firebase | MCP SDK | tmux |
| Reliability | High | High | High | Low |
| Debugging | Easy (logs) | Easy (logs + Firestore) | Easy | Hard |
| Terminal required | No | No | Yes | Yes |
| Cloud cost | **None** | Free tier OK | None | None |
| API usage | 12 req/min (limit: 1800) | 0 | 0 | 12 req/min |

---

## Webhook Proxy Architecture (Optional Future Enhancement)

The 5-second polling approach provides excellent latency for most use cases. However, if sub-second latency is ever needed, webhooks with a **Firebase Cloud Functions proxy** are an option.

### Challenge

- TreLLM runs on a dev machine behind NAT
- Trello webhooks require a publicly accessible HTTPS endpoint
- We don't want to expose the dev machine directly to the internet

### Solution: Firebase as Webhook Proxy

Firebase Cloud Functions provides a serverless, publicly accessible endpoint that:
1. Receives Trello webhook notifications
2. Stores them in Firestore as a queue
3. TreLLM polls Firestore (much faster than Trello API)

Alternatively, TreLLM can use **Firestore real-time listeners** for true push notifications.

### Architecture

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                              Cloud (Firebase)                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │                        Cloud Function                                    │  │
│  │                                                                          │  │
│  │   POST /webhook                                                          │  │
│  │   ├── Verify Trello webhook signature                                   │  │
│  │   ├── Parse card event (create, update, moveToList)                     │  │
│  │   └── Write to Firestore: /tasks/{card_id}                              │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
│                                     │                                          │
│                                     ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │                          Firestore                                       │  │
│  │                                                                          │  │
│  │   /tasks/{card_id}                                                       │  │
│  │   {                                                                      │  │
│  │     "card_id": "abc123",                                                 │  │
│  │     "card_name": "trellm add feature",                                   │  │
│  │     "action": "moveToList",                                              │  │
│  │     "list_name": "TODO",                                                 │  │
│  │     "timestamp": "2026-01-08T...",                                       │  │
│  │     "processed": false                                                   │  │
│  │   }                                                                      │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
│                                     │                                          │
└─────────────────────────────────────│──────────────────────────────────────────┘
                                      │ Real-time listener / polling
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            Dev Machine (behind NAT)                              │
│                                                                                  │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │                           TreLLM Orchestrator                              │  │
│  │                                                                            │  │
│  │   Firestore Listener (real-time)                                          │  │
│  │   ├── on_snapshot(/tasks where processed == false)                        │  │
│  │   └── process_task(card) → Claude Code subprocess                         │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Implementation

#### 1. Firebase Cloud Function (Webhook Receiver)

```typescript
// functions/src/index.ts
import * as functions from "firebase-functions";
import * as admin from "firebase-admin";
import * as crypto from "crypto";

admin.initializeApp();
const db = admin.firestore();

// Trello webhook secret (from environment)
const TRELLO_SECRET = functions.config().trello.webhook_secret;

function verifyTrelloWebhook(
  payload: string,
  signature: string,
  callbackURL: string
): boolean {
  const base64Digest = crypto
    .createHmac("sha1", TRELLO_SECRET)
    .update(payload + callbackURL)
    .digest("base64");
  return base64Digest === signature;
}

export const trelloWebhook = functions.https.onRequest(async (req, res) => {
  // HEAD request = Trello webhook verification
  if (req.method === "HEAD") {
    res.status(200).send();
    return;
  }

  // Verify signature
  const signature = req.headers["x-trello-webhook"] as string;
  const callbackURL = `https://${req.hostname}${req.path}`;

  if (!verifyTrelloWebhook(JSON.stringify(req.body), signature, callbackURL)) {
    res.status(401).send("Invalid signature");
    return;
  }

  const { action, model } = req.body;

  // Only process card events for TODO list
  if (action.type === "updateCard" && action.data.listAfter) {
    const listName = action.data.listAfter.name;
    if (listName === "TODO") {
      await db.collection("tasks").doc(model.id).set({
        card_id: model.id,
        card_name: model.name,
        card_url: model.url,
        card_desc: model.desc || "",
        action: "moveToList",
        list_name: listName,
        timestamp: admin.firestore.FieldValue.serverTimestamp(),
        processed: false,
      });
    }
  } else if (action.type === "createCard") {
    const listName = action.data.list?.name;
    if (listName === "TODO") {
      await db.collection("tasks").doc(action.data.card.id).set({
        card_id: action.data.card.id,
        card_name: action.data.card.name,
        card_url: `https://trello.com/c/${action.data.card.shortLink}`,
        card_desc: action.data.card.desc || "",
        action: "createCard",
        list_name: listName,
        timestamp: admin.firestore.FieldValue.serverTimestamp(),
        processed: false,
      });
    }
  }

  res.status(200).send("OK");
});
```

#### 2. TreLLM with Firestore Listener

```python
# trellm/firebase_listener.py

import asyncio
import logging
from google.cloud import firestore
from .claude import ClaudeRunner
from .state import StateManager
from .config import load_config

logger = logging.getLogger(__name__)


class FirestoreTaskListener:
    def __init__(self, config):
        self.db = firestore.Client()
        self.config = config
        self.state = StateManager(config.state_file)
        self.claude = ClaudeRunner(config.claude)
        self._callback = None

    def start(self):
        """Start listening for new tasks in Firestore."""
        tasks_ref = self.db.collection("tasks")
        query = tasks_ref.where("processed", "==", False)

        # Real-time listener
        self._callback = query.on_snapshot(self._on_snapshot)
        logger.info("Listening for tasks on Firestore...")

    def stop(self):
        """Stop the listener."""
        if self._callback:
            self._callback.unsubscribe()

    def _on_snapshot(self, doc_snapshot, changes, read_time):
        """Handle new/changed documents."""
        for change in changes:
            if change.type.name == "ADDED":
                doc = change.document
                asyncio.create_task(self._process_task(doc))

    async def _process_task(self, doc):
        """Process a single task from Firestore."""
        data = doc.to_dict()
        card_id = data["card_id"]

        logger.info("New task from webhook: %s", data["card_name"])

        # Create a card-like object
        class Card:
            def __init__(self, d):
                self.id = d["card_id"]
                self.name = d["card_name"]
                self.url = d["card_url"]
                self.description = d.get("card_desc", "")
                self.last_activity = str(d.get("timestamp", ""))

        card = Card(data)
        project = card.name.split()[0].lower()
        session_id = self.state.get_session(project)

        try:
            result = await self.claude.run(
                card=card,
                project=project,
                session_id=session_id,
                working_dir=self.config.get_working_dir(project)
            )

            if result.session_id:
                self.state.set_session(project, result.session_id)

            self.state.mark_processed(card.id)

            # Mark as processed in Firestore
            doc.reference.update({"processed": True})

            logger.info("Completed task: %s", card.name)

        except Exception as e:
            logger.error("Failed to process task %s: %s", card.name, e)


async def main():
    config = load_config()
    listener = FirestoreTaskListener(config)
    listener.start()

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

#### 3. Hybrid Mode (Main Entry Point)

```python
# trellm/__main__.py (updated)

import asyncio
import logging
from .config import load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    config = load_config()

    if config.use_firebase_webhooks:
        # Real-time mode via Firebase
        from .firebase_listener import FirestoreTaskListener
        listener = FirestoreTaskListener(config)
        listener.start()
        logger.info("TreLLM started in webhook mode (Firebase)")

        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            listener.stop()
    else:
        # Polling mode (original)
        from .trello import TrelloClient
        from .state import StateManager
        from .claude import ClaudeRunner

        trello = TrelloClient(config.trello)
        state = StateManager(config.state_file)
        claude = ClaudeRunner(config.claude)

        logger.info("TreLLM started in polling mode (every %ds)", config.poll_interval)

        while True:
            # ... original polling logic ...
            await asyncio.sleep(config.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
```

### Setup Instructions

#### 1. Deploy Firebase Function

```bash
# Install Firebase CLI
npm install -g firebase-tools

# Initialize Firebase project
firebase login
firebase init functions

# Deploy the webhook function
cd functions
npm install
firebase deploy --only functions

# Note the function URL (e.g., https://us-central1-myproject.cloudfunctions.net/trelloWebhook)
```

#### 2. Register Trello Webhook

```bash
# Create webhook pointing to your Cloud Function
curl -X POST "https://api.trello.com/1/webhooks" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "'$TRELLO_API_KEY'",
    "token": "'$TRELLO_API_TOKEN'",
    "callbackURL": "https://us-central1-myproject.cloudfunctions.net/trelloWebhook",
    "idModel": "'$TRELLO_BOARD_ID'",
    "description": "TreLLM webhook"
  }'
```

#### 3. Configure TreLLM for Firebase Mode

```yaml
# ~/.trellm/config.yaml
firebase:
  enabled: true
  project_id: "your-project-id"
  credentials: "~/.trellm/firebase-credentials.json"

# Polling is still available as fallback
polling:
  enabled: false  # Disable when using webhooks
  interval_seconds: 5
```

### Latency Comparison

| Mode | Latency | API Calls | Complexity |
|------|---------|-----------|------------|
| **Polling (5s)** | **0-5 seconds** | **12/min** | **Low (recommended)** |
| Polling (30s) | 0-30 seconds | 2/min | Low |
| Firebase Webhook | <1 second | 0 | Medium |

**Note**: Trello allows 300 requests/10 seconds (1800/minute). 5-second polling uses <1% of the limit.

### Benefits of Firebase Proxy

1. **Near-instant latency**: Tasks picked up within 1 second of card creation
2. **No NAT traversal needed**: Dev machine initiates outbound connection
3. **Reduced API calls**: No polling Trello API
4. **Free tier friendly**: Firebase free tier handles typical usage
5. **Reliable**: Firebase handles retries and persistence
6. **Audit trail**: Firestore stores all task events

### Alternative: Cloud Pub/Sub

For Google Cloud users, Cloud Pub/Sub with push subscriptions is another option:

```
Trello → Cloud Function → Pub/Sub → Push to Cloud Run → TreLLM
```

However, this still requires a publicly accessible endpoint for the push delivery. Firebase's client library with real-time listeners is simpler for the "dev machine behind NAT" scenario.

---

## Implementation Roadmap

### Phase 1: MVP (Polling Mode)
- [x] Project skeleton with pyproject.toml
- [ ] Trello client with async API calls
- [ ] Claude runner with subprocess
- [ ] Basic state persistence
- [ ] Main polling loop
- [ ] Comments and card movement

### Phase 2: Enhanced Features
- [ ] YAML configuration file
- [ ] Working directory per project
- [ ] Session resumption with --resume
- [ ] Error handling and retry logic
- [ ] Logging to file

### Phase 3: Webhook Mode (Low Latency)
- [ ] Firebase project setup
- [ ] Cloud Function for Trello webhook
- [ ] Firestore task queue
- [ ] TreLLM Firestore listener
- [ ] Hybrid mode (polling + webhook)

### Phase 4: Polish
- [ ] PyPI package publishing
- [ ] Documentation
- [ ] Integration tests
- [ ] Example configurations

---

## Decision

**I recommend the Subprocess + Resume approach with 5-second polling.**

### Why Polling Wins

Trello's generous rate limits (300 requests/10 seconds) make polling the clear winner:
- **5-second latency** is fast enough for task automation
- **Zero cloud dependencies** - no Firebase, no webhooks to maintain
- **Simple to debug** - just logs, no distributed systems
- **Free** - no cloud costs whatsoever

### Webhook Mode (Optional Future Enhancement)
If sub-second latency is ever needed:
- Firebase proxy architecture is documented above
- Dev machine stays behind NAT (no port forwarding)
- Only adds ~1 second improvement over 5s polling

**Core benefits:**

1. **Zero Manual Intervention**: TreLLM runs Claude Code directly - no terminal needed
2. **Session Persistence**: `--resume` maintains context across tasks automatically
3. **Simple Architecture**: Just subprocess + JSON, no protocols or servers
4. **Truly Hands-Free**: Add a Trello card from your phone, task gets done
5. **Reliable**: Each task runs in isolation, failures don't affect other tasks

This is the first approach that truly eliminates the need to interact with a terminal. You can run TreLLM as a background service (systemd, launchd) and forget about it.
