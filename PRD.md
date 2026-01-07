# TreLLM - Product Requirements Document

## Overview

TreLLM is an automation tool that bridges Trello boards with AI coding assistants (Claude Code, Gemini CLI). It monitors a Trello board for new TODO items and makes them available to AI assistants, enabling a hands-free workflow for software development.

## Problem Statement

Currently, the workflow for using AI coding assistants with Trello-based task management requires manual intervention:

1. User creates a task card in the Trello TODO list
2. User switches to the terminal running Claude Code
3. User manually types `/next` to trigger task processing
4. Claude Code picks up and completes the task

This manual step breaks the flow and requires constant attention to know when new tasks are available.

## Solution

TreLLM eliminates the manual step by acting as an **MCP (Model Context Protocol) server** that Claude Code connects to. When Claude Code needs a task, it queries TreLLM directly:

1. TreLLM runs as a background MCP server
2. Claude Code connects to TreLLM via MCP
3. When the user runs `/next`, Claude Code queries TreLLM for the next task
4. TreLLM returns the task details directly to Claude Code
5. Claude Code works on the task

**Key insight**: Instead of injecting commands into a terminal (tmux), TreLLM provides tasks via a clean API. The AI assistant pulls tasks when ready, rather than having tasks pushed to it.

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
- The project identifier is used by TreLLM to filter tasks for a specific project
- One Trello board serves all projects, with filtering handled by the AI assistant's project context
- This allows adding tasks from anywhere (mobile, web) to any project without switching contexts

## User Stories

### Primary User Story
As a software engineer using AI coding assistants, I want my Claude Code session to automatically know about new Trello tasks so that I can add tasks from anywhere (mobile, web) and have them processed without manual intervention.

### Secondary User Stories
- As a user, I want to configure which Trello board and list to monitor
- As a user, I want Claude Code to query for tasks relevant to my current project
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
- Cache card data locally for fast queries

#### FR2: MCP Server Interface
- Implement MCP server protocol for Claude Code integration
- Provide tools for AI assistants to:
  - `get_next_task(project)` - Get the next unprocessed task for a project
  - `list_tasks(project)` - List all pending tasks for a project
  - `mark_task_started(card_id)` - Mark a task as in-progress
  - `mark_task_complete(card_id)` - Mark a task as complete (move to READY TO TRY)
  - `add_comment(card_id, text)` - Add a comment to a card
- Support filtering by project identifier

#### FR3: Configuration
- YAML or JSON configuration file for settings:
  - Trello API credentials
  - Board ID and TODO list ID
  - Polling interval
  - MCP server settings (port, auth)
- Environment variable support for sensitive credentials

#### FR4: State Management
- Persist state of processed cards across restarts
- Track card IDs that have been sent to the AI assistant
- Handle cards that are moved back to TODO (re-queue based on activity)
- Sync state with Trello to detect external changes

### Optional Features (Future)

#### FR5: Webhook Support
- Register a Trello webhook for real-time notifications
- Push notifications to connected Claude Code instances
- Requires a publicly accessible endpoint (ngrok or similar for local dev)

#### FR6: Advanced Project Management
- Optional project validation against a configured list
- Project-specific configurations
- Project aliases (e.g., `tl` maps to `trellm`)

#### FR7: Status Dashboard
- Simple web UI showing pending and processed tasks
- Real-time status of connected AI assistants

## Non-Functional Requirements

### NFR1: Reliability
- Graceful handling of network failures and API rate limits
- Automatic retry with exponential backoff
- No duplicate task processing

### NFR2: Resource Efficiency
- Minimal CPU and memory footprint when idle
- Efficient API usage to stay within Trello rate limits
- Local caching to minimize API calls

### NFR3: Ease of Installation
- Single binary or simple Python/Node.js package
- Clear setup instructions
- Works as a Claude Code MCP server with minimal configuration

## Technical Architecture

### Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Claude Code                               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    MCP Client                            │    │
│  │  - get_next_task(project="trellm")                      │    │
│  │  - mark_task_started(card_id)                           │    │
│  │  - add_comment(card_id, "Claude: Starting...")          │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ MCP Protocol (stdio/HTTP)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TreLLM MCP Server                          │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐         │
│  │   Trello    │    │    Task     │    │    MCP      │         │
│  │   Poller    │───▶│    Cache    │◀───│   Handler   │         │
│  └─────────────┘    └─────────────┘    └─────────────┘         │
│         │                  │                  │                 │
│         ▼                  ▼                  ▼                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐         │
│  │   Trello    │    │   State     │    │    Tool     │         │
│  │    API      │    │   Store     │    │  Handlers   │         │
│  └─────────────┘    └─────────────┘    └─────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

**Query Flow:**
1. User runs `/next` in Claude Code
2. Claude Code's MCP client calls `get_next_task(project="trellm")`
3. TreLLM checks local cache for pending tasks
4. TreLLM returns task details (card_id, name, description, url)
5. Claude Code calls `mark_task_started(card_id)` and `add_comment(card_id, "Starting...")`
6. Claude Code works on the task
7. When done, Claude Code calls `mark_task_complete(card_id)` and `add_comment(card_id, "Done...")`

**Why MCP instead of tmux injection?**

| Aspect | tmux Injection | MCP Server |
|--------|---------------|------------|
| Coupling | Tight (requires tmux) | Loose (standard protocol) |
| Reliability | Fragile (text injection) | Robust (structured API) |
| Bidirectional | No | Yes (Claude can query) |
| State sync | External | Integrated |
| Multi-client | Complex | Native |
| Testing | Difficult | Easy (mock server) |

### Technology Options

**Option A: Python**
- Pros: Simple, quick to develop, good MCP libraries emerging
- Cons: Requires Python environment

**Option B: TypeScript/Node.js**
- Pros: Official MCP SDK support, native async
- Cons: Larger dependency footprint

**Option C: Go**
- Pros: Single binary, efficient
- Cons: No official MCP SDK yet

**Recommendation**: TypeScript for best MCP SDK support, or Python for rapid prototyping.

### Key Dependencies
- MCP SDK (TypeScript: `@modelcontextprotocol/sdk`, Python: emerging libraries)
- Trello API client
- State persistence (SQLite or simple JSON file)

## Configuration Example

```yaml
# ~/.trellm/config.yaml
trello:
  api_key: ${TRELLO_API_KEY}
  api_token: ${TRELLO_API_TOKEN}
  board_id: "694dd9802e3ad21db9ca5da1"
  todo_list_id: "694dd98f57680df4b26fe1c1"

polling:
  interval_seconds: 30

mcp:
  transport: stdio  # or http
  # http_port: 8765  # if using HTTP transport

state:
  file: "~/.trellm/state.json"

# Optional: Define known projects for validation
projects:
  - name: "trellm"
    description: "TreLLM automation tool"
  - name: "myapp"
    description: "My application project"
```

## Claude Code Integration

Add TreLLM to Claude Code's MCP configuration:

```json
// ~/.claude/mcp_servers.json
{
  "trellm": {
    "command": "trellm",
    "args": ["serve"],
    "env": {
      "TRELLO_API_KEY": "...",
      "TRELLO_API_TOKEN": "..."
    }
  }
}
```

Then in Claude Code, the `/next` command can use TreLLM tools:

```markdown
<!-- ~/.claude/commands/next.md -->
Use the trellm MCP server to get the next task:
1. Call get_next_task with the current project name
2. If a task is returned, call mark_task_started
3. Add an acknowledgment comment
4. Work on the task
5. When done, call mark_task_complete and add completion comment
```

## Implementation Phases

### Phase 1: MVP
- Basic Trello polling with local cache
- MCP server with stdio transport
- Core tools: get_next_task, list_tasks, mark_task_started, mark_task_complete, add_comment
- Simple JSON state file
- Configuration via environment variables

### Phase 2: Enhanced Features
- YAML configuration file
- HTTP transport option
- Logging and error handling improvements
- Card filtering and search

### Phase 3: Advanced Features
- Webhook support for real-time notifications
- Push notifications to connected clients
- Web dashboard

## Success Metrics

- Tasks are available to Claude Code within polling interval
- No duplicate task processing
- Service runs reliably for days without intervention
- Setup time under 5 minutes (just add MCP server config)

## Open Questions

1. ~~Should we detect when Claude Code is busy/idle before injecting commands?~~ **Resolved**: With MCP, Claude Code pulls tasks when ready
2. ~~Should we support card-specific commands beyond just `/next`?~~ **Resolved**: MCP tools provide full flexibility
3. What's the preferred language for the initial implementation? (TypeScript recommended for MCP SDK support)
4. Should we support multiple simultaneous Claude Code instances querying the same TreLLM server?

## Appendix

### Trello API Resources
- [Trello API Documentation](https://developer.atlassian.com/cloud/trello/)
- [Trello Webhooks](https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/)

### MCP Resources
- [Model Context Protocol Specification](https://modelcontextprotocol.io/)
- [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk)
