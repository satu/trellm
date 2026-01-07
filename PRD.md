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
3. Automatically injecting the appropriate command into the tmux session running the AI assistant

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
- Use Trello API webhooks as an optional enhancement for real-time notifications

#### FR2: Command Injection
- Inject commands into a specified tmux session/window
- Support configurable command templates (default: `/next`)
- Support injecting card-specific prompts (e.g., card name and description)
- Wait for the AI assistant to be idle before injecting new commands

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

#### FR6: Multiple AI Assistant Support
- Support multiple tmux sessions (Claude Code, Gemini CLI)
- Round-robin or priority-based task distribution

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
+------------------+     +-----------------+     +------------------+
|   Trello API     |<--->|    TreLLM       |<--->|  tmux Session    |
|   (Polling)      |     |   (Monitor)     |     |  (Claude Code)   |
+------------------+     +-----------------+     +------------------+
                               |
                               v
                         +-----------+
                         |   State   |
                         |   Store   |
                         +-----------+
```

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
  window: "claude"  # optional, uses active window if not specified

polling:
  interval_seconds: 30

command:
  template: "/next"
  # Alternative: include card details
  # template: "Work on: {card_name}\n\nDetails:\n{card_description}"

state:
  file: "~/.trellm/state.json"
```

## Implementation Phases

### Phase 1: MVP
- Basic polling of Trello TODO list
- tmux command injection with `/next`
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
2. Should we support card-specific commands beyond just `/next`?
3. What's the preferred language for the initial implementation?
4. Should the service run as a systemd service or just in a tmux window?

## Appendix

### Trello API Resources
- [Trello API Documentation](https://developer.atlassian.com/cloud/trello/)
- [Trello Webhooks](https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/)

### tmux Command Reference
- Send keys to session: `tmux send-keys -t session:window "command" Enter`
- List sessions: `tmux list-sessions`
