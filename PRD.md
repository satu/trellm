# TreLLM - Product Requirements Document

## Overview

TreLLM is an automation tool that bridges Trello boards with AI coding assistants (Claude Code, Gemini CLI). It monitors a Trello board for new TODO items and automatically invokes the AI assistant to work on each task, enabling a fully hands-free workflow for software development.

## Problem Statement

Currently, the workflow for using AI coding assistants with Trello-based task management requires manual intervention:

1. User creates a task card in the Trello TODO list
2. User switches to the terminal running Claude Code
3. User manually types `/next` to trigger task processing
4. Claude Code picks up and completes the task

This manual step breaks the flow and requires constant attention to know when new tasks are available.

## Solution

TreLLM eliminates the manual step by running as an **orchestrator** that invokes Claude Code as a subprocess for each task:

1. TreLLM polls the Trello board for new cards in TODO
2. When a new card is found, TreLLM invokes Claude Code with the task as a prompt
3. Claude Code runs in non-interactive mode (`-p` flag) and completes the task
4. TreLLM uses `--resume` to maintain session state across tasks for the same project
5. TreLLM moves the card to READY TO TRY when done

**Key insight**: Claude Code supports session persistence via `--resume <session_id>`. The orchestrator stores the session ID per project and resumes from it, maintaining full development context (files, permissions, working directory) across tasks.

```bash
# First task for a project
claude -p "Implement feature from Trello card abc123" --output-format json
# Parse session_id from JSON output

# Subsequent tasks resume the session
claude -p "Now work on Trello card def456" --resume <session_id>
```

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
- The project identifier maps to a session ID stored by TreLLM
- One Trello board serves all projects, with routing handled by TreLLM
- This allows adding tasks from anywhere (mobile, web) to any project

## User Stories

### Primary User Story
As a software engineer using AI coding assistants, I want TreLLM to automatically run Claude Code on new Trello tasks so that I can add tasks from anywhere (mobile, web) and have them processed without any manual intervention.

### Secondary User Stories
- As a user, I want to configure which Trello board and list to monitor
- As a user, I want TreLLM to maintain session state per project across tasks
- As a user, I want to see logs of what tasks were processed
- As a user, I want the system to handle errors gracefully and retry
- As a user, I want to be able to pause/resume automation

## Functional Requirements

### Core Features

#### FR1: Trello Board Monitoring
- Poll the configured Trello board for new cards in the TODO list
- Support configurable polling interval (default: 30 seconds)
- Track which cards have already been processed to avoid duplicates
- Parse the project identifier from the card name (first word)
- Detect when cards are moved back to TODO (re-process)

#### FR2: Claude Code Orchestration
- Invoke Claude Code as a subprocess with `-p` flag for non-interactive mode
- Use `--output-format json` to parse results and session ID
- Use `--resume <session_id>` to maintain session state per project
- Store session IDs in state file, mapped by project name
- Pass card details (ID, name, description, URL) in the prompt
- Wait for Claude Code to complete before processing next task

#### FR3: Task Lifecycle Management
- Add acknowledgment comment when starting a task ("Claude: Starting...")
- Move card to READY TO TRY list when task completes
- Add completion comment with summary of what was done
- Handle errors: log, add error comment, leave in TODO for retry

#### FR4: Configuration
- YAML or JSON configuration file for settings:
  - Trello API credentials
  - Board ID and TODO list ID
  - Polling interval
  - Claude Code binary path
  - Working directories per project
- Environment variable support for sensitive credentials

#### FR5: State Management
- Persist state across restarts:
  - Processed card IDs with timestamps
  - Session IDs per project
  - Current task status
- Handle session expiration gracefully (start new session)

### Optional Features (Future)

#### FR6: Webhook Support
- Register a Trello webhook for real-time notifications
- Trigger task processing immediately on card creation
- Requires a publicly accessible endpoint

#### FR7: Parallel Projects
- Process tasks for different projects in parallel
- Each project maintains its own Claude Code session
- Configurable concurrency limit

#### FR8: Status Dashboard
- Simple web UI showing task queue and history
- Real-time status of running Claude Code instances

## Non-Functional Requirements

### NFR1: Reliability
- Graceful handling of Claude Code failures
- Automatic retry with exponential backoff
- No duplicate task processing
- Session recovery after TreLLM restart

### NFR2: Resource Efficiency
- One Claude Code instance per active project (not per task)
- Efficient Trello API usage to stay within rate limits
- Clean subprocess management

### NFR3: Ease of Installation
- Single binary or simple Python package
- Clear setup instructions
- Minimal dependencies

## Technical Architecture

### Components

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            TreLLM Orchestrator                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐     │
│  │  Trello Poller  │    │  Task Dispatcher │    │ Session Manager │     │
│  │                 │───▶│                 │───▶│                 │     │
│  │  - Poll cards   │    │  - Parse project│    │  - Store IDs    │     │
│  │  - Filter new   │    │  - Queue tasks  │    │  - Map projects │     │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘     │
│                                  │                      │               │
│                                  ▼                      ▼               │
│                         ┌─────────────────────────────────────────┐     │
│                         │          Claude Code Runner             │     │
│                         │                                         │     │
│                         │  claude -p "task" --resume <session_id> │     │
│                         │  --output-format json                   │     │
│                         └─────────────────────────────────────────┘     │
│                                          │                              │
│                                          ▼                              │
│                         ┌─────────────────────────────────────────┐     │
│                         │          Result Handler                 │     │
│                         │                                         │     │
│                         │  - Parse JSON output                    │     │
│                         │  - Update session ID                    │     │
│                         │  - Move card to READY TO TRY            │     │
│                         │  - Add completion comment               │     │
│                         └─────────────────────────────────────────┘     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
           ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
           │ Trello API  │  │ Claude Code │  │ State File  │
           │             │  │ (subprocess)│  │ (JSON)      │
           └─────────────┘  └─────────────┘  └─────────────┘
```

**Task Flow:**
1. Trello Poller finds new card: `trellm add webhook support` (ID: abc123)
2. Task Dispatcher parses project: `trellm`
3. Session Manager retrieves session ID for `trellm` (or null if first task)
4. Claude Code Runner invokes:
   ```bash
   claude -p "Work on Trello card abc123: add webhook support

   Card URL: https://trello.com/c/xxx

   Description: ..." \
   --resume $SESSION_ID \
   --output-format json
   ```
5. Result Handler:
   - Parses JSON output for new session_id
   - Updates session store
   - Adds comment to card
   - Moves card to READY TO TRY

### Technology Options

**Option A: Python**
- Pros: Simple subprocess handling, good Trello libraries, quick to develop
- Cons: Requires Python environment

**Option B: Go**
- Pros: Single binary, efficient subprocess management
- Cons: Longer development time

**Option C: Bash/Shell**
- Pros: Minimal dependencies, simple for basic use case
- Cons: Limited error handling, harder to maintain

**Recommendation**: Python for rapid development and good subprocess/JSON handling.

### Key Dependencies
- Trello API client (requests or py-trello)
- subprocess for Claude Code invocation
- JSON parsing for output
- State persistence (JSON file)

## Configuration Example

```yaml
# ~/.trellm/config.yaml
trello:
  api_key: ${TRELLO_API_KEY}
  api_token: ${TRELLO_API_TOKEN}
  board_id: "694dd9802e3ad21db9ca5da1"
  todo_list_id: "694dd98f57680df4b26fe1c1"
  ready_to_try_list_id: "694e7177ae98fb33dc26c3c9"

polling:
  interval_seconds: 30

claude:
  binary: "claude"  # or full path
  output_format: "json"
  # Working directory per project
  projects:
    trellm:
      working_dir: "~/src/trellm"
    myapp:
      working_dir: "~/src/myapp"

state:
  file: "~/.trellm/state.json"

logging:
  level: "INFO"
  file: "~/.trellm/trellm.log"
```

## State File Example

```json
{
  "sessions": {
    "trellm": {
      "session_id": "abc123-def456",
      "last_activity": "2026-01-07T21:00:00Z"
    },
    "myapp": {
      "session_id": "ghi789-jkl012",
      "last_activity": "2026-01-07T20:30:00Z"
    }
  },
  "processed_cards": {
    "card123": {
      "status": "complete",
      "processed_at": "2026-01-07T21:00:00Z",
      "project": "trellm"
    }
  }
}
```

## Implementation Phases

### Phase 1: MVP
- Basic Trello polling
- Claude Code subprocess invocation with `-p` flag
- Session resumption with `--resume`
- Simple JSON state file
- Move cards to READY TO TRY on completion
- Add comments to cards

### Phase 2: Enhanced Features
- YAML configuration file
- Working directory per project
- Error handling and retry logic
- Logging

### Phase 3: Advanced Features
- Trello webhooks for real-time triggering
- Parallel project processing
- Web dashboard

## Success Metrics

- Tasks are automatically processed within polling interval
- Session state is maintained across tasks for same project
- No duplicate task processing
- Service runs reliably for days without intervention
- Zero manual terminal interaction required

## Open Questions

1. How long do Claude Code sessions persist? Do we need to handle expiration?
2. Should we support multiple tasks in a single Claude Code invocation?
3. What's the best way to handle Claude Code failures mid-task?
4. Should we capture and store Claude Code's stdout for debugging?

## Appendix

### Trello API Resources
- [Trello API Documentation](https://developer.atlassian.com/cloud/trello/)
- [Trello Webhooks](https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/)

### Claude Code CLI Reference
- `-p, --prompt`: Run in non-interactive mode with given prompt
- `--resume <session_id>`: Resume from a previous session
- `--output-format json`: Output structured JSON including session_id
- `--continue`: Continue the most recent conversation (alternative to --resume)
