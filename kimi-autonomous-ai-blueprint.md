# Reverse-Engineered Blueprint: Building Your Own Autonomous AI

> Based on deep analysis of **Hermes Agent** (Python), **OpenClaw** (TypeScript/Node.js), and **OpenHuman** (Rust + React/Tauri).

---

## What These Projects Actually Are

| Project | Language | Core Concept |
|---------|----------|-------------|
| **Hermes Agent** | Python | Self-improving agent with a *closed learning loop* — it creates skills from experience, curates them, and persists knowledge across sessions |
| **OpenClaw** | TypeScript/Node.js | Personal AI *gateway* — local-first, multi-channel (20+ messaging platforms), plugin-heavy architecture |
| **OpenHuman** | Rust + React/Tauri | Desktop-native *personal AI superintelligence* — persistent memory tree, 118+ integrations, background "subconscious" processing |

All three solve the same problem with different tradeoffs: **how do you give an LLM persistent identity, tools, memory, and the ability to act over time?**

---

## The Universal Architecture Pattern

Every autonomous AI system follows this loop:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Trigger   │────▶│   Agent     │────▶│    LLM      │
│ (User/Cron/ │     │  Orchestrator│     │  (Reasoning) │
│  Webhook)   │     │             │     │             │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                    ┌──────────────────────────┘
                    ▼
            ┌─────────────┐
            │  Tool Call  │
            └──────┬──────┘
                   │
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
┌─────────┐  ┌─────────┐  ┌─────────┐
│  File   │  │  Web    │  │ Memory  │
│ System  │  │ Search  │  │  Write  │
└─────────┘  └─────────┘  └─────────┘
```

---

## Core Components You Need to Build

### 1. The Agent Loop (The "Brain")

This is the simplest but most critical piece. All three projects implement essentially the same loop:

```python
# Pseudocode — this pattern appears in all three projects
while iteration < max_iterations and not done:
    response = llm.chat(messages=history, tools=available_tools)
    
    if response.has_tool_calls():
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call)
            history.append(tool_result(result))
    else:
        return response.content  # Final answer
```

**Key design decisions from the codebases:**

- **Iteration budgets**: Hermes shares a budget across parent + subagents to prevent runaway loops
- **Interruptibility**: All three support clean shutdown mid-loop (`/stop`, Ctrl+C, timeout)
- **Grace calls**: One extra API call after budget exhaustion so the model can summarize instead of dying mid-thought
- **Per-session write locks**: OpenClaw uses file-based locks to prevent race conditions on the same session

---

### 2. Tool System (The "Hands")

You need an extensible tool registry. Here's the pattern:

```python
class ToolRegistry:
    def __init__(self):
        self.tools = {}
    
    def register(self, name, handler, schema, destructive=False):
        self.tools[name] = {
            "handler": handler,
            "schema": schema,  # JSON Schema for LLM function calling
            "destructive": destructive  # Serialized vs parallel execution
        }
    
    async def execute(self, name, args):
        tool = self.tools[name]
        return await tool["handler"](**args)
```

**Tool categories all three projects implement:**

| Category | Essential Tools |
|----------|----------------|
| **File System** | `read_file`, `write_file`, `edit_file`, `search_files` |
| **Execution** | `bash`, `execute_code` (Python/JS sandboxed) |
| **Web** | `web_search`, `web_fetch`, `browser_navigate` |
| **Memory** | `memory_read`, `memory_write`, `memory_search` |
| **Planning** | `todo_add`, `todo_complete`, `todo_list` |
| **Meta** | `delegate_task` (spawn subagent), `send_message` |
| **Scheduling** | `cron_schedule`, `cron_list` |

**Critical insight from Hermes**: Mark tools as `destructive` or `path-scoped` — safe tools run in parallel via `ThreadPoolExecutor`, destructive ones are serialized to prevent race conditions.

---

### 3. Memory System (The "Long-term Self")

This is where most DIY agents fail. You need **multiple memory layers**:

**Layer 1: Session Transcript**
- Append-only JSONL file per conversation
- Used for context window / KV-cache
- All three projects store this as `sessions/<id>.jsonl`

**Layer 2: Working Memory (Curated Notes)**
- `MEMORY.md` — agent's notes about environment, tool quirks, conventions
- `USER.md` — user preferences, communication style, workflow habits
- Hermes bounds these (~2,200 chars memory, ~1,375 chars user) and injects them into the system prompt
- **Frozen snapshot pattern**: Capture memory at session start so the system prompt stays stable (preserves prefix cache)

**Layer 3: Searchable Archive**
- SQLite with FTS5 (full-text search) for all past messages
- Optional: Vector embeddings for semantic search (OpenHuman uses this + graph relations)
- Tool: `session_search(query)` returns relevant past conversation snippets

**Layer 4: Daily Notes** (OpenClaw pattern)
- `memory/2026-05-18.md` — working notes from today
- Auto-indexed but not auto-injected (too much noise)
- Background "dreaming" process consolidates these into `MEMORY.md`

**Implementation sketch:**

```python
class MemoryManager:
    def __init__(self):
        self.working_memory = self.load_markdown("~/.agent/memories/MEMORY.md")
        self.user_profile = self.load_markdown("~/.agent/memories/USER.md")
        self.db = sqlite3.connect("~/.agent/state.db")
        self.db.execute("CREATE VIRTUAL TABLE messages USING fts5(content)")
    
    def get_context_for_prompt(self):
        return f"## Your Memories\n{self.working_memory}\n\n## User Profile\n{self.user_profile}"
    
    def search(self, query, limit=5):
        return self.db.execute(
            "SELECT content FROM messages WHERE content MATCH ? ORDER BY rank LIMIT ?",
            (query, limit)
        ).fetchall()
    
    def append_to_session(self, session_id, role, content):
        with open(f"~/.agent/sessions/{session_id}.jsonl", "a") as f:
            f.write(json.dumps({"role": role, "content": content, "ts": time.time()}) + "\n")
        self.db.execute("INSERT INTO messages(content) VALUES (?)", (content,))
```

---

### 4. Planning & Task Decomposition

All three use an in-memory todo list that persists across context compression:

```python
class TodoManager:
    def __init__(self):
        self.items = []  # {id, content, status, created_at}
    
    def add(self, content): ...
    def complete(self, id): ...
    def list(self, status=None): ...
    def inject_into_prompt(self):
        # Returns formatted todo list for system prompt
        # Survives context compression by being re-injected
```

**Subagent delegation** (all three support this):
- Parent agent spawns child agents with isolated context + restricted toolsets
- OpenClaw supports `fork`, `light`, `none` context inheritance modes
- Hermes has `leaf` (worker, can't delegate further) vs `orchestrator` roles
- Concurrency cap (default 3 parallel subagents)

---

### 5. Context Compression

When you hit ~80% of the context window, you MUST compress:

```python
class ContextCompressor:
    def compress(self, messages, target_tokens):
        # Protect head (system prompt + early context)
        # Protect tail (recent messages + active todos)
        # Summarize middle with auxiliary LLM
        head = messages[:4]  
        tail = messages[-6:]
        middle = messages[4:-6]
        
        summary = auxiliary_llm.summarize(middle)
        return head + [summary_message(summary)] + tail
```

Hermes limits this to 3 passes, then creates a new session with `parent_session_id` chaining.

---

### 6. Multi-Channel Gateway (Optional but Powerful)

If you want your agent to exist across platforms:

```
Gateway Daemon (WebSocket server on localhost)
    ├── Telegram Bot Adapter
    ├── Discord Bot Adapter  
    ├── Slack Bolt Adapter
    ├── WhatsApp Adapter
    ├── Email IMAP Adapter
    └── Web Dashboard (React SPA)
```

All incoming messages route to the same agent runtime. Outgoing messages route back through the originating channel.

---

### 7. Cron / Background Autonomy

```python
class CronScheduler:
    def tick(self):
        due_jobs = self.db.query("SELECT * FROM jobs WHERE next_run <= now()")
        for job in due_jobs:
            # Spawn isolated agent turn
            asyncio.create_task(self.execute_job(job))
```

- Hermes stores jobs in SQLite/JSON, supports natural language scheduling ("every monday 9am")
- OpenHuman has a "subconscious engine" that runs on intervals, evaluating background tasks with a local model
- All use file-lock deduplication to prevent double-execution

---

### 8. Provider Abstraction

You need to support multiple LLM providers without locking into one:

```python
class ProviderRegistry:
    def __init__(self):
        self.providers = {
            "openai": OpenAIProvider(),
            "anthropic": AnthropicProvider(),
            "openrouter": OpenRouterProvider(),  # Access to 200+ models
        }
    
    async def complete(self, model_id, messages, tools):
        provider, model = self.parse_model_id(model_id)
        return await provider.complete(model, messages, tools)
    
    def get_fallback(self, failed_provider, error):
        # Auto-failover on rate limits, auth errors, context overflow
        return self.providers["openrouter"]  # Universal fallback
```

**Transport modes to support:**
- `chat_completions` (OpenAI-compatible — industry standard)
- `anthropic_messages` (Native Claude API with prompt caching)
- `codex_responses` (OpenAI Codex / xAI Responses API)

---

## Tech Stack Recommendations

### Option A: Python (Fastest to prototype)

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| LLM Client | `openai` SDK + `httpx` for async |
| CLI | `rich` + `prompt_toolkit` |
| Database | `sqlite3` (built-in) + `sqlite-vec` for embeddings |
| Scheduling | `croniter` + `asyncio` |
| Web Gateway | `fastapi` + `uvicorn` + `python-socketio` |
| Frontend | React + Vite (optional dashboard) |

**Why Python**: Hermes Agent proves you can build a production-grade autonomous agent entirely in Python. The ecosystem for LLM tooling is richest here.

---

### Option B: TypeScript/Node.js (Best for web integrations)

| Layer | Technology |
|-------|-----------|
| Language | TypeScript (strict, ESM) |
| Runtime | Node.js 22+ |
| LLM Client | `openai` npm package |
| Database | `better-sqlite3` |
| Scheduling | `node-cron` |
| Web Gateway | `ws` (WebSocket) + `fastify`/`express` |
| Monorepo | `pnpm` workspaces |

**Why Node.js**: OpenClaw's architecture shows this is ideal if your agent needs deep web service integrations (OAuth, webhooks, browser automation).

---

### Option C: Rust + Tauri (Best native desktop experience)

| Layer | Technology |
|-------|-----------|
| Core | Rust (`tokio`, `axum`, `rusqlite`) |
| Desktop Shell | Tauri v2 |
| Frontend | React 19 + Vite |
| LLM HTTP | `reqwest` |
| Embeddings | `ort` (ONNX Runtime) or call external |

**Why Rust**: OpenHuman's architecture gives you the best performance and native desktop integration, but development velocity is slower.

---

## Minimal Viable Autonomous Agent

If you want to start building **today**, here's the smallest useful system:

### File Structure
```
my-agent/
├── main.py              # Agent loop + CLI
├── tools/
│   ├── __init__.py      # Registry
│   ├── files.py         # read, write, search
│   ├── bash.py          # execute shell
│   ├── web.py           # search, fetch
│   └── memory.py        # read, write, search memories
├── memory/
│   ├── MEMORY.md        # Agent's long-term notes
│   └── USER.md          # User profile
├── sessions/
│   └── <id>.jsonl       # Conversation history
├── state.db             # SQLite: messages, todos, sessions
└── config.yaml          # Model, keys, settings
```

---

### Core Loop (`main.py`)

```python
import asyncio
import json
import sqlite3
from datetime import datetime
from openai import AsyncOpenAI
from tools import registry

class AutonomousAgent:
    def __init__(self):
        self.client = AsyncOpenAI(api_key="...")
        self.db = sqlite3.connect("state.db")
        self.memory = self._load_memory()
        self.todos = []
        self._init_db()
    
    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(content);
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY,
                content TEXT,
                status TEXT DEFAULT 'pending'
            );
        """)
    
    def _load_memory(self):
        try:
            with open("memory/MEMORY.md") as f:
                return f.read()
        except FileNotFoundError:
            return ""
    
    def build_system_prompt(self):
        return f"""You are an autonomous AI assistant. You have access to tools.
        
## Your Memories
{self.memory}

## Active Tasks
{json.dumps(self.todos) if self.todos else "None"}

When given a complex task, break it into steps using the todo tool.
Always check your memories before asking the user for information you might already know."""
    
    async def run(self, user_input, session_id="default", max_iterations=10):
        messages = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": user_input}
        ]
        
        # Load session history
        history = self.db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,)
        ).fetchall()
        for role, content in history:
            messages.append({"role": role, "content": content})
        
        for i in range(max_iterations):
            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=registry.get_schemas(),
                tool_choice="auto"
            )
            
            msg = response.choices[0].message
            
            if msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls]
                })
                
                for tc in msg.tool_calls:
                    result = await registry.execute(tc.function.name, json.loads(tc.function.arguments))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result)
                    })
                    
                    # Persist tool interactions
                    self._persist_message(session_id, "assistant", f"Tool: {tc.function.name}")
                    self._persist_message(session_id, "tool", str(result))
            else:
                # Final response
                self._persist_message(session_id, "assistant", msg.content)
                return msg.content
        
        return "Hit iteration limit."
    
    def _persist_message(self, session_id, role, content):
        self.db.execute(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, datetime.now())
        )
        self.db.execute(
            "INSERT INTO message_search(content) VALUES (?)",
            (content,)
        )
        self.db.commit()

if __name__ == "__main__":
    agent = AutonomousAgent()
    result = asyncio.run(agent.run("Research the latest in LLM agents and write a summary to ~/summary.md"))
    print(result)
```

---

### Tool Registry (`tools/__init__.py`)

```python
import json
from typing import Callable, Dict, Any
from . import files, bash, web, memory

class ToolRegistry:
    def __init__(self):
        self.tools: Dict[str, Dict[str, Any]] = {}
        self._register_core()
    
    def _register_core(self):
        # File tools
        self.register("read_file", files.read_file, {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        })
        
        self.register("write_file", files.write_file, {
            "type": "object", 
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }, destructive=True)
        
        # Bash tool
        self.register("bash", bash.execute, {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "default": 30}
            },
            "required": ["command"]
        }, destructive=True)
        
        # Web tools
        self.register("web_search", web.search, {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        })
        
        # Memory tools
        self.register("memory_read", memory.read, {
            "type": "object",
            "properties": {
                "file": {"type": "string", "enum": ["MEMORY.md", "USER.md"]}
            }
        })
        
        self.register("memory_write", memory.write, {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "content": {"type": "string"}
            }
        }, destructive=True)
        
        self.register("memory_search", memory.search, {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            }
        })
        
        # Planning tool
        self.register("todo", self._todo_handler, {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "complete", "list"]},
                "content": {"type": "string"}
            },
            "required": ["action"]
        })
    
    def register(self, name: str, handler: Callable, schema: dict, destructive=False):
        self.tools[name] = {
            "handler": handler,
            "schema": schema,
            "destructive": destructive
        }
    
    def get_schemas(self):
        return [{
            "type": "function",
            "function": {
                "name": name,
                "description": tool["handler"].__doc__ or name,
                "parameters": tool["schema"]
            }
        } for name, tool in self.tools.items()]
    
    async def execute(self, name: str, args: dict) -> str:
        if name not in self.tools:
            return f"Error: Unknown tool '{name}'"
        try:
            result = await self.tools[name]["handler"](**args)
            return json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            return f"Error: {str(e)}"
    
    def _todo_handler(self, action, content=None):
        if action == "add":
            return {"status": "added", "todo": content}
        elif action == "list":
            return {"todos": []}
        return {"status": "ok"}

registry = ToolRegistry()
```

---

## Roadmap to Full Autonomy

### Phase 1: Reactive Agent (Week 1-2)
- [ ] Basic tool-calling loop
- [ ] File system + bash tools
- [ ] Web search + fetch
- [ ] Session persistence (JSONL)
- [ ] CLI interface

### Phase 2: Persistent Identity (Week 3-4)
- [ ] `MEMORY.md` + `USER.md` injection
- [ ] SQLite session store with FTS5 search
- [ ] Todo/planning tools
- [ ] Context compression when window fills

### Phase 3: Self-Improvement (Week 5-6)
- [ ] **Skill creation**: After complex tasks (5+ tool calls), nudge the agent to save the workflow as a reusable skill file
- [ ] **Skill registry**: Load skills from `skills/` directory, inject into system prompt
- [ ] **Skill curation**: Background job that archives stale skills, runs LLM reviews

### Phase 4: Multi-Agent (Week 7-8)
- [ ] Subagent spawning with isolated context
- [ ] Concurrency limits (3 parallel)
- [ ] Role system (`leaf` worker vs `orchestrator`)
- [ ] Parent-child result aggregation

### Phase 5: Background Autonomy (Week 9-10)
- [ ] Cron scheduler with natural language parsing
- [ ] Webhook ingestion + trigger triage
- [ ] "Subconscious" loop: periodic memory consolidation, dream journaling

### Phase 6: Multi-Channel (Week 11-12)
- [ ] Gateway daemon with WebSocket
- [ ] Telegram/Discord bot adapters
- [ ] Message routing by channel
- [ ] Response delivery back to originating channel

---

## Critical Design Decisions (From the Codebases)

1. **Use SQLite for everything** — Don't overcomplicate. All three projects use SQLite as the default for sessions, memory, cron, and search. Add vector extensions (`sqlite-vec`, `sqlite-vss`) only when needed.

2. **Inject memory into system prompt, don't RAG it** — The curated `MEMORY.md` goes directly into the system prompt. Only use search/RAG for archival/session history. This is how all three projects do it.

3. **Freeze the system prompt snapshot** — Don't mutate the system prompt mid-session. Capture memory at the start and keep it stable. This preserves LLM prefix caching and reduces costs by ~75%.

4. **Serialize destructive tools** — Run safe tools in parallel, but file writes, bash commands, and memory edits must be serialized to prevent race conditions.

5. **Budget everything** — Iterations, tokens, time. Hermes has an `iteration_budget` shared across parent + subagents. OpenClaw has tool loop detection thresholds.

6. **Build the CLI first, GUI later** — Hermes has 11,000 LOC in its CLI. OpenClaw is terminal-first. The CLI is where you debug and control the agent.

7. **Use OpenRouter as your universal fallback** — Support direct provider APIs (OpenAI, Anthropic) for cost/speed, but always have OpenRouter as a failover for reliability and model access.

---

*Generated from reverse-engineering Hermes Agent, OpenClaw, and OpenHuman.*
