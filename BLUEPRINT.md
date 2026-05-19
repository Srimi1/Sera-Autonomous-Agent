# Sera — Autonomous Agent Blueprint

> **A hybrid autonomous agent: Hermes's brain in OpenHuman's body.**
> Python core (self-improving ReAct loop, skills, curator) ↔ Tauri desktop shell (mascot, memory tree, OS integration).

---

## 1. Vision

**Sera** is a personal autonomous agent that lives on your machine, learns from every task it completes, and acts across messaging apps and 100+ SaaS services on your behalf.

It is **not** a chatbot wrapper. It's a working agent with:

- A self-improving reasoning loop (Hermes-style closed learning loop)
- Persistent memory that grows over weeks (OpenHuman-style Memory Tree)
- A native desktop presence (Tauri shell with system tray, notifications, approval gates)
- Multi-channel front doors (CLI, Telegram, Discord, Slack, WhatsApp)
- 100+ SaaS integrations via Composio (Gmail, GitHub, Notion, Calendar, …)
- Background "subconscious" that auto-fetches context every 20 minutes

Positioned against the references:

| | Hermes Agent | OpenClaw | OpenHuman | **Sera** |
|---|---|---|---|---|
| Language | Python | TypeScript | Rust + React | **Python core + Tauri shell** |
| Strength | Self-improvement | Multi-channel gateway | Memory + desktop | **Brain + body fused** |
| Weakness | No native desktop | No skills/curator | Rust is slow to iterate | — |

---

## 2. The Hybrid Architecture

```
┌──────────────────────────────────────────────────────────┐
│           TAURI DESKTOP SHELL (Rust + React)             │
│   • System tray / native notifications                   │
│   • Chat UI, memory tree browser, integration panel      │
│   • OS hooks: clipboard, accessibility, screen capture   │
│   • Approval gate dialogs for dangerous tools            │
└──────────────────────┬───────────────────────────────────┘
                       │ JSON-RPC 2.0 over localhost:11111
                       │ (bearer-auth, sidecar process)
                       │ Socket.io for token streaming
┌──────────────────────▼───────────────────────────────────┐
│          PYTHON CORE — "Sera Engine" (Hermes lineage)    │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  Agent Loop  │  │ Tool Registry│  │   Curator    │    │
│  │  (ReAct +    │  │ (auto-disc., │  │ (background  │    │
│  │   streaming) │  │  permissions)│  │  skill self- │    │
│  └──────┬───────┘  └──────┬───────┘  │  improve)    │    │
│         │                 │           └──────────────┘    │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────────────┐    │
│  │  LLM Adapter │  │ Subagent     │  │  Auto-Fetch  │    │
│  │  (OpenAI,    │  │ Delegation   │  │  (cron, 20m, │    │
│  │   Anthropic, │  │              │  │  per integr.)│    │
│  │   local)     │  └──────────────┘  └──────────────┘    │
│  └──────────────┘                                        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │              Memory Layer (3 tiers)                │  │
│  │  • In-turn: messages list                          │  │
│  │  • Session: SQLite + FTS5  (Hermes)                │  │
│  │  • Long-term: Memory Tree = SQLite-VSS + Obsidian  │  │
│  │       vault Markdown (OpenHuman) + skills/ dir     │  │
│  │       (Hermes procedural memory)                   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │      Gateway (multi-channel inbound/outbound)      │  │
│  │  Telegram • Discord • Slack • WhatsApp • Email     │  │
│  │  + Composio for 100+ SaaS integrations             │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**Why Python core + Tauri shell (and not pure Python or pure Rust):**

- Python wins for the agent loop: 95% of wall-time is LLM API I/O, so raw runtime perf is irrelevant; the Python LLM ecosystem (openai, anthropic, mcp, prompt_toolkit) is unmatched.
- Tauri wins for the shell: native OS integration (tray, notifications, accessibility), small binaries, React UI for memory browser and approval dialogs.
- The bridge is dead simple: shell spawns Python as a sidecar process and talks to it over JSON-RPC on `localhost:11111` with a bearer token (identical to OpenHuman's pattern).
- Headless mode: the Python core also runs standalone (`sera` CLI) — the shell is optional.

---

## 3. Borrowed Patterns (matrix)

| Layer | From | Source file:line | Sera implementation |
|---|---|---|---|
| Agent loop | Hermes | `agent/conversation_loop.py:526` | `sera/agent/loop.py` |
| LLM provider abstraction | Hermes | `agent/anthropic_adapter.py` etc. | `sera/llm/adapters/` |
| Tool registry (auto-discovery) | Hermes | `tools/registry.py` | `sera/tools/registry.py` |
| Tool dispatch trait | OpenHuman | `agent/dispatcher.rs` | `sera/tools/dispatcher.py` |
| Permission levels | OpenHuman | `tools/traits.rs` | `sera/tools/permissions.py` |
| Approval gates | OpenHuman | (Rust core) | `sera/safety/approval.py` |
| Skills system | Hermes | `skills/` directory | `~/.sera/skills/` |
| Session store | Hermes | `hermes_state.py:1-120` | `sera/memory/session.py` |
| Memory tree | OpenHuman | `src/openhuman/memory/tree/` | `sera/memory/tree.py` |
| Chunker | OpenHuman | `memory/chunker.rs` | `sera/memory/chunker.py` |
| Ingestion pipeline | OpenHuman | `memory/ingestion/queue.rs` | `sera/memory/ingestion.py` |
| Auto-fetch cron | OpenHuman | `src/openhuman/cron/` | `sera/cron/autofetch.py` |
| Output compression | OpenHuman | TokenJuice | `sera/context/compressor.py` |
| Mid-turn compression | Hermes | `agent/context_compressor.py` | (same file as above) |
| Multi-channel gateway | Hermes | `gateway/run.py` | `sera/gateway/` |
| SaaS integrations | OpenHuman | Composio | `sera/integrations/composio.py` |
| JSON-RPC | OpenHuman | `app/src-tauri/src/core_rpc.rs` | `sera/rpc/server.py` |
| Tauri shell | OpenHuman | `app/src-tauri/` | `sera-shell/` |
| Subagent delegation | Both | `tools/delegate_tool.py` | `sera/tools/delegate.py` |
| MCP integration | Hermes | `tools/mcp_tool.py` | `sera/tools/mcp.py` |
| Config | Hermes | `gateway/config.py` | `~/.sera/config.yaml` |
| Secrets | OpenHuman | OS keychain | `sera/safety/secrets.py` (keyring lib) |
| Curator | Hermes | `curator.py` (background fork) | `sera/curator/` |

---

## 4. Module 1 — Agent Loop (Python core)

Heritage: Hermes `agent/conversation_loop.py`. ReAct (Reason + Act) tool-calling with streaming, interrupts, and mid-turn context compression.

```python
# sera/agent/loop.py — annotated pseudocode

async def run_turn(session: Session, user_msg: str) -> str:
    session.append(role="user", content=user_msg)

    for iteration in range(MAX_ITERATIONS):
        # 1. Pre-call hooks (memory injection, prompt-cache prep)
        messages = await build_messages(session)

        # 2. Compress if over budget BEFORE call
        if estimate_tokens(messages) > model.context_budget * 0.8:
            messages = await compress(messages)

        # 3. Call LLM (streaming) with tool schemas
        try:
            response = await llm.chat(
                messages=messages,
                tools=registry.schemas_for(session),
                stream=True,
            )
        except ContextOverflow:
            messages = await compress(messages, aggressive=True)
            continue  # retry

        # 4. Stream assistant text to UI; collect tool_calls
        assistant_text, tool_calls = await consume_stream(response, ui_sink)
        session.append(role="assistant", content=assistant_text, tool_calls=tool_calls)

        # 5. No tools? Turn complete.
        if not tool_calls:
            return assistant_text

        # 6. Execute tools (parallel for read-only, sequential for write)
        for call in tool_calls:
            if call.permission >= Permission.DANGEROUS:
                approved = await approval_gate.request(call)  # OpenHuman pattern
                if not approved:
                    result = "User denied."
                else:
                    result = await dispatcher.execute(call)
            else:
                result = await dispatcher.execute(call)

            # 7. TokenJuice compression on tool output (OpenHuman pattern)
            compressed = await output_compressor.shrink(result)
            session.append(role="tool", tool_call_id=call.id, content=compressed)

        # 8. Loop back: feed tool results to LLM for next reasoning step

    raise MaxIterationsExceeded()
```

**Key design points:**

- **Streaming first.** Token-by-token to UI via Socket.io. Same stream collects tool calls.
- **Interrupt-aware.** Ctrl+C / shell stop button sets a per-thread flag; loop checks it between iterations and after every tool.
- **Compression in-band.** Hitting context overflow doesn't error — it compresses and continues.
- **Permission-gated tools.** Anything ≥ `DANGEROUS` waits on approval (OpenHuman's pattern, routed through shell UI).
- **Session is the source of truth.** Persisted to SQLite after every append.

---

## 5. Module 2 — Tool System

Heritage: Hermes registry + OpenHuman dispatcher trait + permission model.

### Tool definition

```python
# sera/tools/base.py
from enum import Enum
from pydantic import BaseModel

class Permission(Enum):
    NONE = 0
    READ_ONLY = 1
    WRITE = 2
    EXECUTE = 3       # runs code, shell, network calls
    DANGEROUS = 4     # destructive, irreversible

class ToolScope(Enum):
    SYSTEM = "system"    # built-in Python tool
    SKILL = "skill"      # user-curated skill from skills/ dir
    INTEGRATION = "integration"  # Composio / external SaaS

class Tool(BaseModel):
    name: str
    description: str
    parameters: dict        # JSON schema
    permission: Permission
    scope: ToolScope
    handler: Callable       # async def handler(args, ctx) -> str

# sera/tools/registry.py
_registry: dict[str, Tool] = {}

def register(tool: Tool) -> None:
    _registry[tool.name] = tool

def all_tools(session: Session) -> list[Tool]:
    return [t for t in _registry.values() if session.allows(t)]

def discover() -> None:
    """Import every tool module so they self-register."""
    for mod in pkgutil.walk_packages(sera.tools.impl.__path__):
        importlib.import_module(mod.name)
```

### Tool dispatcher (OpenHuman trait → Python)

```python
# sera/tools/dispatcher.py
class ToolDispatcher(Protocol):
    async def parse(self, response: LLMResponse) -> list[ToolCall]: ...
    def format_result(self, call: ToolCall, result: str) -> dict: ...

class NativeFunctionDispatcher:        # OpenAI / Anthropic native tool API
    ...

class XmlDispatcher:                   # legacy <tool_call>...</tool_call> tags
    ...
```

### Starter tool set (week 1)

| Tool | Scope | Permission |
|---|---|---|
| `file_read` | SYSTEM | READ_ONLY |
| `file_write` | SYSTEM | WRITE |
| `shell_run` | SYSTEM | EXECUTE (DANGEROUS for `rm -rf`, `sudo`, `kill`) |
| `web_search` | SYSTEM | READ_ONLY |
| `web_fetch` | SYSTEM | READ_ONLY |
| `memory_store` | SYSTEM | WRITE |
| `memory_recall` | SYSTEM | READ_ONLY |
| `delegate` | SYSTEM | EXECUTE |
| `python_eval` | SYSTEM | EXECUTE (sandboxed subprocess) |
| `mcp_call` | SYSTEM | varies (per MCP server tool) |

---

## 6. Module 3 — LLM Adapter

Multi-provider abstraction with **OpenAI SDK as lingua franca** + native adapters for richer features.

```python
# sera/llm/base.py
class LLM(Protocol):
    name: str
    context_budget: int
    async def chat(self, messages, tools, stream) -> Response: ...

# sera/llm/adapters/openai.py
class OpenAIAdapter(LLM):
    """Also handles OpenRouter, Groq, Together, local OpenAI-compatible endpoints."""

# sera/llm/adapters/anthropic.py
class AnthropicAdapter(LLM):
    """Uses Anthropic SDK natively — prompt caching, extended thinking, vision."""

# sera/llm/adapters/local.py
class OllamaAdapter(LLM): ...

# sera/llm/router.py
class ModelRouter:
    """Routes by profile: reasoning / fast / vision."""
    def for_task(self, task_kind: str) -> LLM: ...
```

**Lazy SDK loading.** Each adapter imports its SDK on first use, not at startup. Saves ~250 ms boot.

**Fallback chain.** Each profile (`reasoning`, `fast`, `vision`) has a primary + 2 fallbacks. On quota / 5xx, rotate.

---

## 7. Module 4 — Memory: 3 Tiers

| Tier | Storage | Lifetime | Lookup |
|---|---|---|---|
| **In-turn** | `messages: list[Message]` in RAM | This turn only | Direct array access |
| **Session** | SQLite (`sessions.db`) with FTS5 | Forever, per session_id | Full-text search across turns |
| **Long-term** | Memory Tree: SQLite-VSS + Obsidian vault `.md` | Forever, cross-session | Semantic + keyword |
| **Procedural** | `~/.sera/skills/*.md` | Forever, agent-curated | Filename + tag-based |

### Memory Tree schema (OpenHuman lineage)

```sql
-- ~/.sera/memory.db
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,        -- 'email:gmail/abc123', 'doc:notion/xyz'
    content TEXT NOT NULL,        -- markdown, ≤ 3000 tokens
    summary TEXT,
    created_at TIMESTAMP,
    ingested_at TIMESTAMP
);

CREATE VIRTUAL TABLE chunks_vss USING vss0(
    embedding(1536)               -- text-embedding-3-small
);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT,                    -- 'Alice', 'OpenAI', 'Project Sera'
    type TEXT,                    -- person | org | concept | project
    first_seen TIMESTAMP
);

CREATE TABLE relations (
    src_entity TEXT,
    dst_entity TEXT,
    kind TEXT,                    -- 'works_at', 'mentions', 'parent_of'
    chunk_id TEXT REFERENCES chunks(id)
);
```

The Obsidian vault at `~/.sera/vault/` mirrors chunks as `.md` files. User can edit; changes sync back.

---

## 8. Module 5 — Ingestion & Auto-Fetch

Every integration (Gmail, GitHub, Slack, …) has an adapter that knows how to pull recent items.

```python
# sera/integrations/base.py
class IntegrationAdapter(Protocol):
    name: str
    cadence_seconds: int = 1200  # 20 min default

    async def fetch_since(self, cursor: str | None) -> AsyncIterator[RawItem]: ...

# sera/cron/autofetch.py
async def autofetch_loop():
    while True:
        for adapter in enabled_integrations():
            cursor = state.cursor(adapter.name)
            async for item in adapter.fetch_since(cursor):
                await ingestion_queue.put(item)
            state.save_cursor(adapter.name, latest_cursor)
        await asyncio.sleep(60)

# sera/memory/ingestion.py
async def ingest(item: RawItem):
    chunks = chunker.split(item.content, max_tokens=3000)
    for c in chunks:
        summary = await llm.summarize(c.content)
        entities = await llm.extract_entities(c.content)
        embedding = await llm.embed(c.content)
        db.insert_chunk(c, summary, embedding)
        for e in entities:
            db.upsert_entity(e)
            db.add_relation(item.source_entity, e, "mentions", c.id)
        vault.write_markdown(c)
```

**Idempotency.** Each `RawItem` has a stable ID; re-ingesting is a no-op.

**Backpressure.** Ingestion queue is bounded; full = drop oldest with warning.

---

## 9. Module 6 — Context Engine

Two flavors of compression:

1. **TokenJuice (OpenHuman).** Applied to every tool result before it goes back to the LLM. HTML→Markdown, URL shortening, table de-bloating, dedup.
2. **Session compaction (Hermes).** When messages approach 80% of context budget, summarize older turns into a single "previous context" message; preserve last N turns verbatim.

```python
# sera/context/compressor.py
async def shrink_tool_output(raw: str, max_tokens: int = 2000) -> str:
    if estimate_tokens(raw) <= max_tokens:
        return raw
    return await llm.compress(raw, target_tokens=max_tokens, fast_model=True)

async def compact_session(messages: list[Message]) -> list[Message]:
    pivot = len(messages) - KEEP_TAIL          # keep last N verbatim
    old = messages[:pivot]
    tail = messages[pivot:]
    summary = await llm.summarize_messages(old)
    return [Message(role="system", content=f"[Earlier context]\n{summary}"), *tail]
```

**Prefix-cache reuse (Anthropic).** System prompt is hashed; if hash matches prior turn, mark `cache_control: ephemeral` on the system block so Anthropic reuses the prefix.

---

## 10. Module 7 — Curator (Self-Improvement)

The single feature that turns Sera from a tool-caller into a learning agent.

```python
# sera/curator/loop.py — runs as background asyncio task

async def curator_loop():
    while True:
        completed = await curator_queue.get()    # session_id of completed complex task
        await asyncio.sleep(2)                   # let main agent finish persisting

        # Fork a fresh agent instance with read-only access to the session + skills/
        critic = AgentInstance(role="curator", read_only=False)
        await critic.run_turn(f"""
            Review session {completed.id}. The user asked: "{completed.user_request}"
            The agent took {completed.tool_count} tool calls and used {completed.tokens} tokens.

            Decide:
            1. Was this task common enough to deserve a new skill in skills/?
            2. Are there existing skills that should be updated?
            3. Is there a fact about the user worth recording in MEMORY.md?

            Use file_write to create/update skills/*.md and MEMORY.md as needed.
        """)
```

**Skill anatomy.**

```markdown
---
name: weekly-github-digest
trigger: "user asks for github activity summary"
permission: READ_ONLY
---

# Weekly GitHub Digest

Steps:
1. Call `composio.github.list_user_events` for the last 7 days
2. Group by repo
3. Format as markdown table with: repo, PRs opened, PRs merged, issues closed
4. Send via `message_tool` to the requesting channel
```

The curator can also **trigger** skills, creating a recursive improvement loop — skills that improve other skills.

---

## 11. Module 8 — Tauri Shell

Heritage: OpenHuman `app/src-tauri/`. Sera shell only manages presence + UI; all logic is in the Python core.

```
sera-shell/
├── src-tauri/
│   ├── src/
│   │   ├── main.rs                # window mgmt, system tray
│   │   ├── core_process.rs        # spawn Python sidecar
│   │   ├── core_rpc.rs            # HTTP/Socket.io client to Python
│   │   └── tray.rs                # menubar icon + actions
│   └── tauri.conf.json
├── src/                            # React app
│   ├── App.tsx
│   ├── components/
│   │   ├── Chat.tsx               # main conversation pane
│   │   ├── MemoryTree.tsx         # browse Memory Tree
│   │   ├── Approvals.tsx          # dangerous-tool approval dialogs
│   │   ├── Integrations.tsx       # connect/disconnect Composio services
│   │   └── Settings.tsx
│   └── lib/rpc.ts                 # typed JSON-RPC client
└── package.json
```

**Sidecar lifecycle.** Shell spawns `python -m sera.rpc.server --port 11111 --token <secret>` on launch; sends SIGTERM on quit. Restart on crash with exponential backoff.

**Streaming.** React subscribes to Socket.io for token-by-token output + tool-call progress. No polling.

---

## 12. Module 9 — JSON-RPC Bridge

```python
# sera/rpc/server.py — FastAPI app + Socket.io
@app.post("/rpc")
async def rpc(req: JsonRpcRequest, token: str = Depends(verify_bearer)):
    handler = METHOD_REGISTRY[req.method]
    return await handler(**req.params)

METHOD_REGISTRY = {
    "sera.agent.turn":           agent_turn,
    "sera.agent.interrupt":      agent_interrupt,
    "sera.session.list":         session_list,
    "sera.session.get":          session_get,
    "sera.memory.search":        memory_search,
    "sera.memory.ingest":        memory_ingest,
    "sera.skill.list":           skill_list,
    "sera.skill.run":            skill_run,
    "sera.integration.connect":  integration_connect,
    "sera.approval.respond":     approval_respond,
    "sera.tools.list":           tools_list,
}
```

**Auth.** Bearer token generated at first launch, stored in OS keychain, passed to shell via env var. Never leaves localhost.

**Streaming.** Socket.io channels: `agent.token`, `agent.tool_start`, `agent.tool_end`, `approval.requested`.

**Versioning.** Schema versions are part of method name suffix (`sera.agent.turn.v1`) so shell and core can co-evolve.

---

## 13. Module 10 — Multi-Channel Gateway

Heritage: Hermes `gateway/run.py`. Same engine, different transports.

```
sera/gateway/
├── server.py                 # async HTTP server, webhook receiver
├── router.py                 # incoming msg → session selector
└── platforms/
    ├── telegram.py
    ├── discord.py
    ├── slack.py
    ├── whatsapp.py
    ├── email.py
    └── signal.py
```

Each platform adapter:
1. Receives platform-native event
2. Normalizes to `IncomingMessage(channel, user, text, attachments)`
3. Looks up or creates a session (`session_per_user_channel`)
4. Calls core `sera.agent.turn`
5. Sends streamed response back over the platform API

---

## 14. Module 11 — Composio Integrations

```python
# sera/integrations/composio.py
class ComposioRegistry:
    async def discover(self) -> list[Tool]:
        """Fetch Composio actions for connected accounts, wrap as Tool objects."""
        actions = await composio_client.list_actions(user=self.user_id)
        return [self._wrap(a) for a in actions]

    def _wrap(self, action: ComposioAction) -> Tool:
        return Tool(
            name=f"composio__{action.app}__{action.name}",
            description=action.description,
            parameters=action.parameters_schema,
            permission=Permission(action.metadata.get("permission", "WRITE")),
            scope=ToolScope.INTEGRATION,
            handler=lambda args, ctx: composio_client.execute(action.id, args),
        )
```

Composio handles OAuth, refresh, and rate limits. Sera just discovers and dispatches.

**Onboarding flow.** Shell shows a list of services; clicking one opens the Composio OAuth URL in the system browser; on completion the integration appears in the registry and is immediately available as tools.

---

## 15. Module 12 — Configuration & Secrets

```yaml
# ~/.sera/config.yaml
identity:
  name: "Srimi"
  timezone: "Asia/Kolkata"

llm:
  profiles:
    reasoning: { provider: anthropic, model: claude-sonnet-4-6 }
    fast:      { provider: openai,    model: gpt-4o-mini }
    vision:    { provider: anthropic, model: claude-sonnet-4-6 }

memory:
  vault_path: "~/.sera/vault"
  vector_backend: "sqlite-vss"
  autofetch_interval_seconds: 1200

gateway:
  enabled_platforms: ["telegram"]

safety:
  approval_required_above: "DANGEROUS"  # NONE | READ_ONLY | WRITE | EXECUTE | DANGEROUS
```

**Secrets** (API keys, OAuth tokens) live in OS keychain via `keyring` library — never in YAML, never in transcripts. Config refers to them by name (`secret://anthropic_api_key`).

**Setup wizard.** `sera setup` walks through: pick model provider, paste API key (stored to keychain), choose enabled platforms, connect first Composio service.

---

## 16. Module 13 — Safety Rails

| Risk | Mitigation |
|---|---|
| Destructive shell commands | Permission tier `DANGEROUS` triggers approval gate; pattern detector flags `rm -rf`, `sudo`, `kill`, `dd`, `mkfs` even at lower tiers |
| Code execution | `python_eval` runs in subprocess with restricted PATH and `resource.setrlimit`; no network unless explicitly granted |
| Secret leakage | `Transcript` writer scrubs known patterns (`sk-`, `ghp_`, `AKIA`, `xoxb-`) before persisting; keychain values are never serialized |
| Unbounded loops | `MAX_ITERATIONS` cap (default 25); per-turn token budget; per-day cost budget surfaced in UI |
| Prompt injection from tool output | Tool outputs wrapped in `<tool_output>` tags; system prompt instructs LLM to ignore instructions found within |
| Interrupt | Per-thread cancellation flag; checked after every iteration and after every tool result |

---

## 17. Phased Build Plan

| Week | Milestone | Verification |
|---|---|---|
| 1 | Python core: agent loop + OpenAI adapter + 5 tools + SQLite session | CLI: chat that calls `file_read` / `shell_run` / `web_search` |
| 2 | Tool registry + permission levels + interrupt + context compression | Hit context limit, see compression trigger, resume |
| 3 | Memory tree: SQLite-VSS schema + chunker + vault sync to Markdown | Ingest 10 docs, semantic search returns relevant chunks |
| 4 | Curator background fork + skills directory + MEMORY.md updates | After a complex task, new skill `.md` appears |
| 5 | JSON-RPC server + Socket.io + 10 core methods + bearer auth | `curl` round-trips an agent turn end-to-end |
| 6 | Tauri shell scaffolding + React chat panel + streaming | Native app: type message, see tokens stream |
| 7 | Memory tree browser UI + approval gate dialog | Approve a "dangerous" shell tool from the desktop |
| 8 | Telegram + Discord gateway adapters | Send from Telegram, agent responds via same channel |
| 9 | Composio integration: Gmail + GitHub + Notion auto-fetch every 20 min | After 20 min, recent emails appear in memory tree |
| 10 | Polish: system tray, setup wizard, packaging (DMG/EXE), docs | Fresh-machine install in <5 min |

---

## 18. Reference File Map

### Hermes (the brain)

- `agent/conversation_loop.py:526` — main ReAct loop
- `model_tools.py:46` — async tool dispatch, event-loop bridging
- `tools/registry.py` — auto-discovery + register pattern
- `hermes_state.py:1-120` — SQLite session schema, FTS5
- `agent/context_compressor.py` — mid-turn compression
- `agent/memory_manager.py` — pluggable memory providers
- `gateway/run.py` — multi-platform async HTTP server
- `agent/anthropic_adapter.py`, `bedrock_adapter.py`, `gemini_native_adapter.py` — provider adapters
- `tools/delegate_tool.py` — subagent spawning
- `tools/mcp_tool.py` — MCP server integration

### OpenHuman (the body)

- `src/openhuman/agent/harness/tool_loop.rs:100` — Rust tool loop (reference shape)
- `src/openhuman/agent/dispatcher.rs` — `ToolDispatcher` trait
- `src/openhuman/tools/traits.rs` — Tool trait with scopes + permission levels
- `src/openhuman/memory/tree/` — Memory Tree schema
- `src/openhuman/memory/chunker.rs` — ≤3k-token chunking
- `src/openhuman/memory/ingestion/queue.rs` — entity/relation extraction
- `src/openhuman/cron/` — 20-min auto-fetch scheduler
- `src/core/jsonrpc.rs` — JSON-RPC dispatch
- `app/src-tauri/src/core_process.rs` — Tauri spawns sidecar
- `app/src-tauri/src/core_rpc.rs` — HTTP bridge from shell to core
- `src/openhuman/config/schema/types.rs` — config schema (port to Pydantic)

---

## 19. Anti-Patterns (avoid)

- **Subagent peer messaging.** Subagents cannot reliably send messages to each other. Coordinate through shared memory keys + lead agent (the orchestration rule from `CLAUDE.md`).
- **Unbounded tool loops.** Always cap iterations; always have a per-turn token budget.
- **Naive history truncation.** Cutting old messages destroys context; compress them instead.
- **Secrets in transcripts.** Scrub before persisting, not after.
- **Dynamic imports in Tauri WebView.** CEF restricts them; use static imports.
- **Spawning N dependent agents in one batch** expecting them to chain via messages — they won't.
- **Tightly coupling shell to core.** All shell↔core traffic goes through JSON-RPC. No shared state, no direct DB access from shell.
- **Polling for status.** Use Socket.io events / promises. Polling burns LLM cache and CPU.

---

## 20. Verification Checklist

### Week 1
- [ ] `sera chat` opens an interactive REPL
- [ ] Agent answers a question without tools
- [ ] Agent uses `file_read` to summarize a file
- [ ] Session persists across restarts
- [ ] FTS5 search returns historical turns

### Week 3
- [ ] `sera memory ingest <path>` adds a doc to Memory Tree
- [ ] `sera memory search "query"` returns top-5 chunks by cosine similarity
- [ ] Markdown vault under `~/.sera/vault/` mirrors all chunks

### Week 4
- [ ] After completing a 10+ tool-call task, a new file appears in `~/.sera/skills/`
- [ ] MEMORY.md grows with user-specific facts

### Week 6
- [ ] Tauri shell connects to Python sidecar on launch
- [ ] Typing in shell streams tokens back without freezing
- [ ] Crashing the Python core triggers shell auto-restart

### Week 8
- [ ] Telegram message → agent response → reply in same chat
- [ ] Conversation continuity preserved across Telegram messages 24 hours apart

### Week 9
- [ ] Connect Gmail via Composio → 20 min later, recent emails are in Memory Tree
- [ ] Agent can answer "what did Alice email me last week" from memory

### Week 10
- [ ] Fresh-machine: install DMG, run setup wizard, send first message — all in under 5 minutes
- [ ] Total install size < 100 MB

---

**End of blueprint.** Next step: scaffold `sera/` Python package and ship Week 1.
