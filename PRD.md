# TreLLM - Product Requirements Document

## Overview

TreLLM is an automation tool that bridges Trello boards with AI coding assistants (Claude Code, Gemini CLI). It monitors a Trello board for new TODO items and automatically triggers the AI assistant to work on tasks, enabling a hands-free workflow for software development.

## Problem Statement

Currently, the workflow for using AI coding assistants with Trello-based task management requires manual intervention:

1. User creates a task card in the Trello TODO list
2. User switches to the terminal running Claude Code
3. User manually types `/next` to trigger task processing
4. Claude Code picks up and completes the task

This manual step breaks the flow and requires constant attention to know when new tasks are available.

## Solution

TreLLM automates step 2 and 3 by:

1. Running as a background service that monitors the Trello board
2. Detecting when new cards are added to the TODO list
3. Parsing the card name to determine which project (and tmux window) should handle the task
4. Automatically injecting the appropriate command into the correct tmux window running the AI assistant

## Card Naming Convention

Cards follow a structured naming format that enables automatic routing to the correct project:

```
<project-name> <task-description>
```

**Examples:**
- `trellm implement polling for Trello API`
- `myapp fix authentication bug in login flow`
- `website update homepage hero section`

**Key principles:**
- The first word of the card name is the **project identifier**
- The project identifier matches the **tmux window name** where the Claude Code instance for that project is running
- One Trello board serves all projects, with routing handled by TreLLM based on the project prefix
- This allows adding tasks from anywhere (mobile, web) to any project without switching contexts

## User Stories

### Primary User Story
As a software engineer using AI coding assistants, I want my Claude Code session to automatically start working on new Trello tasks so that I can add tasks from anywhere (mobile, web) and have them processed without manual intervention.

### Secondary User Stories
- As a user, I want to configure which Trello board and list to monitor
- As a user, I want to specify which tmux session/window to inject commands into
- As a user, I want to see logs of what tasks were triggered
- As a user, I want the system to handle rate limiting gracefully
- As a user, I want to be able to pause/resume automation without stopping the service

## Functional Requirements

### Core Features

#### FR1: Trello Board Monitoring
- Poll the configured Trello board for new cards in the TODO list
- Support configurable polling interval (default: 30 seconds)
- Track which cards have already been processed to avoid duplicates
- Parse the project identifier from the card name (first word)
- Use Trello API webhooks as an optional enhancement for real-time notifications

#### FR2: Command Injection & Project Routing
- Route commands to the correct tmux window based on the project identifier in the card name
- The project identifier (first word of card name) maps directly to the tmux window name
- Inject commands into the matched tmux session/window
- **Inject the specific card ID** rather than a generic `/next` command to avoid race conditions
- Support configurable command templates with card interpolation:
  - `{card_id}` - The Trello card ID
  - `{card_name}` - The full card name
  - `{card_description}` - The card description
  - `{card_url}` - Direct link to the card
  - `{task_description}` - Card name without the project prefix
- Wait for the AI assistant to be idle before injecting new commands
- Handle cases where the target tmux window doesn't exist (log warning, skip card)

#### FR3: Configuration
- YAML or JSON configuration file for settings:
  - Trello API credentials
  - Board ID and TODO list ID
  - tmux session name and window identifier
  - Polling interval
  - Command template
- Environment variable support for sensitive credentials

#### FR4: State Management
- Persist state of processed cards across restarts
- Track card IDs that have been sent to the AI assistant
- Handle cards that are moved back to TODO (re-queue based on activity)

### Optional Features (Future)

#### FR5: Webhook Support
- Register a Trello webhook for real-time notifications
- Requires a publicly accessible endpoint (ngrok or similar for local dev)

#### FR6: Advanced Project Management
- Optional project validation against a configured list
- Project-specific command templates (e.g., different commands for different AI assistants)
- Project aliases (e.g., `tl` maps to `trellm` window)

#### FR7: Status Dashboard
- Simple web UI showing processed tasks
- Real-time status of the AI assistant

## Non-Functional Requirements

### NFR1: Reliability
- Graceful handling of network failures and API rate limits
- Automatic retry with exponential backoff
- No duplicate task processing

### NFR2: Resource Efficiency
- Minimal CPU and memory footprint when idle
- Efficient API usage to stay within Trello rate limits

### NFR3: Ease of Installation
- Single binary or simple Python/Node.js package
- Clear setup instructions
- Minimal dependencies

## Technical Architecture

### Components

```
+------------------+     +-----------------+     +----------------------+
|   Trello API     |<--->|    TreLLM       |<--->|  tmux Session        |
|   (Polling)      |     |   (Monitor)     |     |  +-----------------+ |
+------------------+     +-----------------+     |  | window: trellm  | |
                               |                 |  | (Claude Code)   | |
                               |                 |  +-----------------+ |
                               |                 |  | window: myapp   | |
                               |                 |  | (Claude Code)   | |
                               v                 |  +-----------------+ |
                         +-----------+           |  | window: website | |
                         |   State   |           |  | (Gemini CLI)    | |
                         |   Store   |           |  +-----------------+ |
                         +-----------+           +----------------------+
```

**Routing Flow:**
1. New card appears: `trellm add webhook support` (card ID: `abc123`)
2. TreLLM parses project identifier: `trellm`
3. TreLLM finds tmux window named `trellm`
4. TreLLM injects a command with the specific card ID into that window

**Command Injection Approaches:**

| Approach | Command Injected | Pros | Cons |
|----------|-----------------|------|------|
| Generic `/next` | `/next` | Simple, AI queries for next card | Race conditions if multiple cards added |
| Card ID reference | `/card abc123` | Precise, no ambiguity | Requires custom slash command |
| Direct prompt | `Work on Trello card abc123: add webhook support` | No custom command needed | Longer injection, more complex |

**Recommended approach**: Direct prompt injection with card context, avoiding the need for custom slash commands while providing precise task targeting

### Technology Options

**Option A: Python**
- Pros: Simple, quick to develop, good Trello API libraries
- Cons: Requires Python environment

**Option B: Node.js**
- Pros: Native async, good for webhook handling
- Cons: Larger dependency footprint

**Option C: Go**
- Pros: Single binary, efficient, good for long-running services
- Cons: Longer development time

**Recommendation**: Start with Python for rapid prototyping, consider Go for production if distribution is important.

### Key Dependencies
- Trello API client library
- tmux command-line interface (via subprocess)
- State persistence (SQLite or simple JSON file)

## Configuration Example

```yaml
trello:
  api_key: ${TRELLO_API_KEY}
  api_token: ${TRELLO_API_TOKEN}
  board_id: "694dd9802e3ad21db9ca5da1"
  todo_list_id: "694dd98f57680df4b26fe1c1"

tmux:
  session: "main"
  # Window is determined dynamically from card name prefix
  # Card "trellm fix bug" -> routes to window "trellm"
  # Card "myapp add feature" -> routes to window "myapp"

polling:
  interval_seconds: 30

command:
  # Direct card ID injection (recommended) - avoids race conditions
  template: |
    Work on Trello card {card_id}: {task_description}

    Card URL: {card_url}

    {card_description}

  # Alternative: simple /next command (legacy, not recommended)
  # template: "/next"

  # Alternative: custom slash command with card ID
  # template: "/card {card_id}"

state:
  file: "~/.trellm/state.json"

# Optional: Define known projects for validation
# If not defined, any first word is accepted as a project
projects:
  - name: "trellm"
    description: "TreLLM automation tool"
  - name: "myapp"
    description: "My application project"
```

## Implementation Phases

### Phase 1: MVP
- Basic polling of Trello TODO list
- Parse project identifier from card name (first word)
- Route commands to matching tmux window
- **Direct card ID injection** with configurable template (including card ID, name, description, URL)
- Simple JSON state file
- Configuration via environment variables

### Phase 2: Enhanced Configuration
- YAML configuration file
- Command templates with card interpolation
- Logging and error handling improvements

### Phase 3: Advanced Features
- Webhook support for real-time notifications
- Idle detection for AI assistant
- Multiple session support

## Success Metrics

- Tasks are automatically triggered within polling interval
- No duplicate task processing
- Service runs reliably for days without intervention
- Setup time under 10 minutes

## Open Questions

1. Should we detect when Claude Code is busy/idle before injecting commands?
2. ~~Should we support card-specific commands beyond just `/next`?~~ **Resolved**: Yes, direct card ID injection is now the recommended approach
3. What's the preferred language for the initial implementation?
4. Should the service run as a systemd service or just in a tmux window?

## Appendix

### Trello API Resources
- [Trello API Documentation](https://developer.atlassian.com/cloud/trello/)
- [Trello Webhooks](https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/)

### tmux Command Reference
- Send keys to session: `tmux send-keys -t session:window "command" Enter`
- List sessions: `tmux list-sessions`
