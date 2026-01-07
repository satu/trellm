# TreLLM - Technical Design Document

## Executive Summary

This document presents the technical design for TreLLM, a bridge between Trello task management and AI coding assistants. After evaluating multiple approaches, I recommend implementing TreLLM as an **MCP (Model Context Protocol) server** that Claude Code connects to natively.

**Key insight**: Instead of injecting commands into terminals (tmux), TreLLM provides tasks via a clean API. The AI assistant **pulls** tasks when ready, rather than having tasks **pushed** to it. This eliminates the fragile tmux dependency and provides a robust, bidirectional communication channel.

---

## Recommended Approach: MCP Server with TypeScript

### Why This Approach?

1. **Native Integration**: MCP is Claude Code's standard protocol for extensions
2. **Pull Model**: AI assistant queries for tasks when ready (no busy/idle detection needed)
3. **Bidirectional**: AI can query, update status, and add comments through the same channel
4. **No tmux**: Eliminates fragile terminal injection
5. **Official SDK**: TypeScript has first-class MCP SDK support

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Claude Code                                    │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                         MCP Client                                 │  │
│  │                                                                    │  │
│  │  Tool Calls:                                                       │  │
│  │  ├── get_next_task(project: "trellm") → Task                      │  │
│  │  ├── list_tasks(project: "trellm") → Task[]                       │  │
│  │  ├── mark_task_started(card_id: "abc123")                         │  │
│  │  ├── mark_task_complete(card_id: "abc123")                        │  │
│  │  └── add_comment(card_id: "abc123", text: "Claude: Done!")        │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ stdio (JSON-RPC)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         TreLLM MCP Server                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                        Tool Handlers                             │   │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────────┐ │   │
│  │  │ get_next_task│ │ list_tasks   │ │ mark_task_started/complete│ │   │
│  │  └──────────────┘ └──────────────┘ └──────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                         Task Manager                             │   │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐             │   │
│  │  │  Task Cache  │ │ State Store  │ │ Project Filter│             │   │
│  │  │  (in-memory) │ │   (JSON)     │ │              │             │   │
│  │  └──────────────┘ └──────────────┘ └──────────────┘             │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                       Trello Service                             │   │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐             │   │
│  │  │   Poller     │ │  API Client  │ │  Rate Limiter │             │   │
│  │  │ (background) │ │              │ │              │             │   │
│  │  └──────────────┘ └──────────────┘ └──────────────┘             │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                         ┌─────────────────┐
                         │   Trello API    │
                         └─────────────────┘
```

### Core Components

#### 1. MCP Server Entry Point

```typescript
// src/index.ts
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { TrelloService } from "./services/trello.js";
import { TaskManager } from "./services/taskManager.js";
import { registerTools } from "./tools/index.js";

async function main() {
  const server = new Server(
    { name: "trellm", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  // Initialize services
  const trelloService = new TrelloService({
    apiKey: process.env.TRELLO_API_KEY!,
    apiToken: process.env.TRELLO_API_TOKEN!,
    boardId: process.env.TRELLO_BOARD_ID!,
    todoListId: process.env.TRELLO_TODO_LIST_ID!,
  });

  const taskManager = new TaskManager(trelloService);

  // Register MCP tools
  registerTools(server, taskManager);

  // Start background polling
  taskManager.startPolling(30_000); // 30 seconds

  // Connect via stdio
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
```

#### 2. Tool Registration

```typescript
// src/tools/index.ts
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { TaskManager } from "../services/taskManager.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

export function registerTools(server: Server, taskManager: TaskManager) {
  // List available tools
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      {
        name: "get_next_task",
        description: "Get the next unprocessed task for a project from Trello",
        inputSchema: {
          type: "object",
          properties: {
            project: {
              type: "string",
              description: "Project name to filter tasks (e.g., 'trellm')",
            },
          },
          required: ["project"],
        },
      },
      {
        name: "list_tasks",
        description: "List all pending tasks for a project",
        inputSchema: {
          type: "object",
          properties: {
            project: {
              type: "string",
              description: "Project name to filter tasks",
            },
          },
          required: ["project"],
        },
      },
      {
        name: "mark_task_started",
        description: "Mark a task as started (in progress)",
        inputSchema: {
          type: "object",
          properties: {
            card_id: { type: "string", description: "Trello card ID" },
          },
          required: ["card_id"],
        },
      },
      {
        name: "mark_task_complete",
        description: "Mark a task as complete and move to READY TO TRY",
        inputSchema: {
          type: "object",
          properties: {
            card_id: { type: "string", description: "Trello card ID" },
          },
          required: ["card_id"],
        },
      },
      {
        name: "add_comment",
        description: "Add a comment to a Trello card",
        inputSchema: {
          type: "object",
          properties: {
            card_id: { type: "string", description: "Trello card ID" },
            text: { type: "string", description: "Comment text" },
          },
          required: ["card_id", "text"],
        },
      },
    ],
  }));

  // Handle tool calls
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;

    switch (name) {
      case "get_next_task":
        return await handleGetNextTask(taskManager, args.project);
      case "list_tasks":
        return await handleListTasks(taskManager, args.project);
      case "mark_task_started":
        return await handleMarkStarted(taskManager, args.card_id);
      case "mark_task_complete":
        return await handleMarkComplete(taskManager, args.card_id);
      case "add_comment":
        return await handleAddComment(taskManager, args.card_id, args.text);
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  });
}

async function handleGetNextTask(taskManager: TaskManager, project: string) {
  const task = await taskManager.getNextTask(project);
  if (!task) {
    return {
      content: [{ type: "text", text: "No pending tasks for this project" }],
    };
  }
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(task, null, 2),
      },
    ],
  };
}
```

#### 3. Task Manager

```typescript
// src/services/taskManager.ts
import { TrelloService, TrelloCard } from "./trello.js";
import { StateStore } from "./stateStore.js";

export interface Task {
  card_id: string;
  card_name: string;
  card_description: string;
  card_url: string;
  project: string;
  task_description: string;
}

export class TaskManager {
  private trello: TrelloService;
  private state: StateStore;
  private cache: Map<string, Task> = new Map();
  private pollingInterval?: NodeJS.Timeout;

  constructor(trello: TrelloService) {
    this.trello = trello;
    this.state = new StateStore();
  }

  startPolling(intervalMs: number) {
    this.poll(); // Initial poll
    this.pollingInterval = setInterval(() => this.poll(), intervalMs);
  }

  private async poll() {
    const cards = await this.trello.getTodoCards();
    this.cache.clear();

    for (const card of cards) {
      const task = this.parseCard(card);
      this.cache.set(card.id, task);
    }
  }

  private parseCard(card: TrelloCard): Task {
    const parts = card.name.split(" ", 2);
    const project = parts[0].toLowerCase();
    const taskDescription = parts.length > 1 ? card.name.slice(project.length + 1) : "";

    return {
      card_id: card.id,
      card_name: card.name,
      card_description: card.desc,
      card_url: card.url,
      project,
      task_description: taskDescription,
    };
  }

  async getNextTask(project: string): Promise<Task | null> {
    for (const task of this.cache.values()) {
      if (task.project === project && !this.state.isProcessed(task.card_id)) {
        return task;
      }
    }
    return null;
  }

  async listTasks(project: string): Promise<Task[]> {
    return Array.from(this.cache.values()).filter(
      (task) => task.project === project && !this.state.isProcessed(task.card_id)
    );
  }

  async markStarted(cardId: string) {
    this.state.markStarted(cardId);
    await this.trello.addComment(cardId, "Claude: Starting work on this task...");
  }

  async markComplete(cardId: string) {
    this.state.markComplete(cardId);
    await this.trello.moveToReadyToTry(cardId);
  }

  async addComment(cardId: string, text: string) {
    await this.trello.addComment(cardId, text);
  }
}
```

#### 4. Trello Service

```typescript
// src/services/trello.ts
export interface TrelloCard {
  id: string;
  name: string;
  desc: string;
  url: string;
}

export class TrelloService {
  private apiKey: string;
  private apiToken: string;
  private boardId: string;
  private todoListId: string;
  private readyToTryListId?: string;

  constructor(config: {
    apiKey: string;
    apiToken: string;
    boardId: string;
    todoListId: string;
  }) {
    this.apiKey = config.apiKey;
    this.apiToken = config.apiToken;
    this.boardId = config.boardId;
    this.todoListId = config.todoListId;
  }

  private async request(path: string, options: RequestInit = {}) {
    const url = new URL(`https://api.trello.com/1${path}`);
    url.searchParams.set("key", this.apiKey);
    url.searchParams.set("token", this.apiToken);

    const response = await fetch(url, options);
    if (!response.ok) {
      throw new Error(`Trello API error: ${response.status}`);
    }
    return response.json();
  }

  async getTodoCards(): Promise<TrelloCard[]> {
    return this.request(`/lists/${this.todoListId}/cards`);
  }

  async addComment(cardId: string, text: string) {
    return this.request(`/cards/${cardId}/actions/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  async moveToReadyToTry(cardId: string) {
    if (!this.readyToTryListId) {
      // Discover the READY TO TRY list
      const lists = await this.request(`/boards/${this.boardId}/lists`);
      const readyList = lists.find((l: any) => l.name === "READY TO TRY");
      if (readyList) {
        this.readyToTryListId = readyList.id;
      }
    }

    if (this.readyToTryListId) {
      return this.request(`/cards/${cardId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idList: this.readyToTryListId }),
      });
    }
  }
}
```

#### 5. State Store

```typescript
// src/services/stateStore.ts
import fs from "fs";
import path from "path";
import os from "os";

interface State {
  processed: Record<string, { status: string; timestamp: string }>;
}

export class StateStore {
  private statePath: string;
  private state: State;

  constructor() {
    this.statePath = path.join(os.homedir(), ".trellm", "state.json");
    this.state = this.load();
  }

  private load(): State {
    try {
      const dir = path.dirname(this.statePath);
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
      }
      if (fs.existsSync(this.statePath)) {
        return JSON.parse(fs.readFileSync(this.statePath, "utf-8"));
      }
    } catch (e) {
      console.error("Failed to load state:", e);
    }
    return { processed: {} };
  }

  private save() {
    fs.writeFileSync(this.statePath, JSON.stringify(this.state, null, 2));
  }

  isProcessed(cardId: string): boolean {
    return cardId in this.state.processed;
  }

  markStarted(cardId: string) {
    this.state.processed[cardId] = {
      status: "started",
      timestamp: new Date().toISOString(),
    };
    this.save();
  }

  markComplete(cardId: string) {
    this.state.processed[cardId] = {
      status: "complete",
      timestamp: new Date().toISOString(),
    };
    this.save();
  }
}
```

### Directory Structure

```
trellm/
├── package.json
├── tsconfig.json
├── src/
│   ├── index.ts           # Entry point, MCP server setup
│   ├── tools/
│   │   └── index.ts       # Tool registration and handlers
│   └── services/
│       ├── trello.ts      # Trello API client
│       ├── taskManager.ts # Task caching and filtering
│       └── stateStore.ts  # State persistence
└── dist/                  # Compiled JavaScript
```

### Installation & Usage

```bash
# Install globally
npm install -g trellm

# Or run directly
npx trellm
```

Add to Claude Code's MCP configuration (`~/.claude.json` or via settings):

```json
{
  "mcpServers": {
    "trellm": {
      "command": "npx",
      "args": ["trellm"],
      "env": {
        "TRELLO_API_KEY": "your-api-key",
        "TRELLO_API_TOKEN": "your-api-token",
        "TRELLO_BOARD_ID": "694dd9802e3ad21db9ca5da1",
        "TRELLO_TODO_LIST_ID": "694dd98f57680df4b26fe1c1"
      }
    }
  }
}
```

---

## Alternative Approaches Considered

### Alternative 1: tmux Injection (Original Approach)

**Description**: Run TreLLM as a background process that monitors Trello and injects commands into tmux sessions using `tmux send-keys`.

**Architecture**:
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Trello    │────▶│   TreLLM    │────▶│    tmux     │
│   Poller    │     │   (Push)    │     │  send-keys  │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │ Claude Code │
                                        │  (passive)  │
                                        └─────────────┘
```

**Pros**:
- Works with any terminal application
- No changes needed to Claude Code
- Simple implementation

**Cons**:
- Requires tmux (not universal)
- Fragile text injection (escaping issues)
- No feedback channel (push-only)
- Can't detect if Claude Code is busy
- Hard to handle multiple projects

**When to choose**: Quick POC or when MCP is not available.

---

### Alternative 2: File-based Queue

**Description**: TreLLM writes tasks to a file, Claude Code watches the file or reads it on `/next`.

**Architecture**:
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Trello    │────▶│   TreLLM    │────▶│  task.json  │
│   Poller    │     │   Writer    │     │   (file)    │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                                               │ read
                                               ▼
                                        ┌─────────────┐
                                        │ Claude Code │
                                        │  /next cmd  │
                                        └─────────────┘
```

**Implementation**:
```typescript
// TreLLM writes to ~/.trellm/tasks/trellm.json
{
  "tasks": [
    {
      "card_id": "abc123",
      "card_name": "trellm implement feature X",
      "status": "pending"
    }
  ]
}

// /next command reads the file
// Read ~/.trellm/tasks/{project}.json and work on first pending task
```

**Pros**:
- Simple implementation
- No network protocol needed
- Easy to debug (just read the file)
- Works with any AI assistant that can read files

**Cons**:
- No real-time updates without file watching
- State synchronization complexity
- Limited API (just read/write)
- Claude Code needs file read access

**When to choose**: When MCP is not available and simplicity is paramount.

---

### Alternative 3: HTTP API Server

**Description**: TreLLM runs as an HTTP server that Claude Code calls via `curl` or fetch.

**Architecture**:
```
┌─────────────┐     ┌─────────────────────┐
│   Trello    │────▶│      TreLLM         │
│   Poller    │     │   HTTP Server       │
└─────────────┘     │   :8765             │
                    │                     │
                    │  GET /tasks/trellm  │
                    │  POST /tasks/:id/..│
                    └─────────────────────┘
                              │
                              │ HTTP
                              ▼
                    ┌─────────────────────┐
                    │    Claude Code      │
                    │  (curl/fetch)       │
                    └─────────────────────┘
```

**Pros**:
- Language agnostic
- Easy to test with curl
- Can add web dashboard later
- RESTful, familiar pattern

**Cons**:
- Requires port management
- HTTP overhead for local communication
- Need to handle authentication
- Not as integrated as MCP

**When to choose**: When building a dashboard or when multiple non-MCP clients need access.

---

## Comparison Matrix

| Criteria | MCP Server (Recommended) | tmux Injection | File Queue | HTTP API |
|----------|-------------------------|----------------|------------|----------|
| Integration | Native Claude Code | External | File-based | HTTP calls |
| Bidirectional | Yes | No | Limited | Yes |
| Real-time | Yes | Yes | No | Yes |
| Dependencies | MCP SDK | tmux | None | HTTP server |
| Complexity | Medium | Low | Low | Medium |
| Reliability | High | Low | Medium | High |
| Multi-client | Yes | Complex | Yes | Yes |
| Testing | Easy (mock) | Hard | Easy | Easy |

---

## Implementation Roadmap

### Phase 1: MVP (Week 1)
- [x] Project skeleton with TypeScript
- [ ] MCP server with stdio transport
- [ ] Core tools: get_next_task, list_tasks
- [ ] Trello API client
- [ ] Basic state persistence

### Phase 2: Full Feature Set (Week 2)
- [ ] mark_task_started, mark_task_complete, add_comment tools
- [ ] Background polling
- [ ] Error handling and retry logic
- [ ] Configuration file support

### Phase 3: Polish (Week 3)
- [ ] npm package publishing
- [ ] Documentation
- [ ] Integration tests
- [ ] Example /next command for Claude Code

---

## Decision

**I recommend the MCP Server approach** because:

1. **Native Integration**: MCP is the standard protocol for Claude Code extensions
2. **Pull Model**: Eliminates the need for busy/idle detection - Claude pulls when ready
3. **Bidirectional**: AI can query tasks, update status, and add comments
4. **No tmux**: Removes a fragile dependency entirely
5. **Official SDK**: TypeScript MCP SDK is well-supported and documented
6. **Future-proof**: MCP is the foundation for Claude Code's extensibility

The file-based queue is a viable fallback if MCP setup proves problematic.
