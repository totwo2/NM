# N.M — Enterprise AI Office Assistant

> A worker's own AI workstation: AI assistant + self-built OA approval + internal IM, all office work in one entry.

**中文版本: [README.md](README.md)**

---

## ✨ Highlights

N.M is not another OA system. It's an **AI Agent Office Hub** — everything you do at work, in one chat box:

```
🏢 One login → Everything done
├── ✨ AI Assistant      Natural language: file ops, code, docs, search
├── ✅ Approval Center     Leave/reimbursement/contracts/seals — full workflow + tracking
├── 💬 Internal IM        Groups, messages, read receipts, 2-min undo
├── 🔍 Query Center       Contacts, policies, documents
└── ⚙️ Admin Panel        IM/OA admin, employee roster, HR sync
```

### Why this project is worth your time

| Feature | What it means |
|---------|---------------|
| **🧠 Decoupled Window + Memory Architecture** | The context manager (window/session scope) and the memory system (cross-session persistence) are **fully decoupled**: when context is compressed, a callback event sinks the displaced tool result text into long-term memory, closing the "compress → recall" loop. CCR archives survive restarts and model switches. |
| **🔄 Memory System** | Three-layer memory (stable trajectory / decisions / facts) + extract-arbiter-distill pipeline + TTL expiry + deep distill + anti-loop detection + full verbatim transcript (transcript.jsonl) |
| **🤖 AgentLoop** | Recall → route → LLM → execute → compress → distill — the entire pipeline is traceable |
| **💰 Token-Saving Design** | Three-tier model routing (local / cloud / strong) + smart cache (LLM-only, no tool results) + type-aware compression (JSON / logs / file lists / long text) + zero-LLM form field extraction (regex-only) |
| **📋 Zero-Database OA** | Single JSON file workflow engine: 10 built-in templates (leave, reimbursement, contracts, seal requests, transfers…), state machine (draft → pending → completed/rejected/withdrawn), supports return/forward/recall. Backup = copy the folder. |
| **💬 Built-in IM** | Group management, read receipts, 2-min undo; OA approvals naturally triggered in IM (mention "leave" in a chat → approval card appears) |
| **🦾 Self-Evolution** | Automatically distills improvement suggestions from failures; Ratchet verifier ensures scores never regress |
| **🔐 Auth Security** | PBKDF2 password hash + Bearer Token sessions, anti-impersonation |
| **🪶 Minimal Deployment** | Single-file SPA frontend + FastAPI backend + Docker one-liner. Zero burden on enterprise IT. |

---

## 🚀 Quick Start

### Local

```bash
# 1. Clone & install
git clone <your-repo-url>
cd nm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure model (pick one)
# Option A: environment variables
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export NM_MODEL="gpt-4o-mini"

# Option B: auto-discover ~/.qclaw/openclaw.json (skip if Option A used)
#           Also works with any OpenAI-compatible gateway (Ollama, LM Studio, vLLM, etc.)

# 3. Start
./start.sh
# or: python web/server.py
```

Open http://localhost:8787

### Docker

```bash
docker build -t nm .
docker run -d -p 8787:8787 \
  -e OPENAI_API_KEY="sk-xxx" \
  -v nm-data:/app/.nm \
  nm
```

> Data lives in `./.nm/` (single JSON files, backup = copy the folder).

### Demo Accounts

| Account | Password | Role |
|---------|----------|------|
| `admin` | `demo123` | Super admin |
| `im_admin` | `demo123` | IM admin |
| `oa_admin` | `demo123` | OA admin |
| `zhangsan` | `demo123` | Employee (R&D) |
| `lisi` | `demo123` | Manager (R&D) |
| `wangwu` | `demo123` | Employee (Admin) |

---

## 🏗️ Architecture

### Core: Window vs Memory — Decoupled by Design

```
┌──────────────────────────────────────────────────────────────┐
│                  Web Frontend (single-file SPA)               │
└──────────────────────────┬───────────────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼───────────────────────────────────┐
│                    FastAPI Backend                            │
│  Auth middleware · LLM cache · Session persistence · Usage log │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                     AgentLoop                                  │
│                                                              │
│  ┌───────────── ContextManager (window layer) ──────────────┐ │
│  │ Window messages · system prompt assembly · token budget  │ │
│  │ Type-aware compression: tool_result → placeholder + CCR  │ │
│  │ on_compaction event ──┐                                 │ │
│  └──────────────────────┼─────────────────────────────────┘ │
│                          │ decoupled callback (window unaware)│
│                          ▼                                   │
│  ┌───────────── MemoryCore (memory layer) ─────────────────┐ │
│  │ Extract → Arbiter → Distill pipeline                     │ │
│  │ Persisted: memory_store.json + transcript.jsonl           │ │
│  │ Recall: stable(trajectory) + volatile(decisions+facts)    │ │
│  └─────────────▲──────────────────────────────────────────┘ │
│                │ recall → set_memory                          │
│  TaskRouter · ModelManager · Permission · SelfEvolution       │
│  SessionStore (cross-request continuity, separate pipeline)   │
└────────┬─────────────────────────────────────────────────────┘
         ▼
┌───────────────┬───────────────┬────────────────────────────┐
│   OA Workflow │     IM Engine │      Users / Org / Auth       │
│ Approve/Track │  Groups/Msgs  │ Roster / Auth / HR fields   │
└───────────────┴───────────────┴────────────────────────────┘
```

**Why window and memory are separate:**

- **ContextManager** (window layer): owns only *this* conversation's message list, compression, and system prompt assembly. It is volatile, in-process, and shares the service's lifecycle.
- **MemoryCore** (memory layer): owns only *long-term* cross-session memory — extraction, conflict arbitration, distillation, persistence. It is persisted to disk and independent of any single conversation.
- The only coupling point is **AgentLoop**: `clear() → recall() → set_memory()` for recall; `on_compaction` event for compress → distill. **ContextManager fires events, doesn't know about memory; MemoryCore receives events, doesn't know about windows.**

### Memory Pipeline

```
User input → AgentLoop.run()
          → context.clear()              # clear window (preserve memory context)
          → memory.recall(scope)         # recall stable + volatile memory
          → context.set_memory(text)     # inject into system prompt
          → loop: route → LLM → execute → compress
          → on_compaction → memory.ingest + distill   # sink compressed text
          → memory.ingest(user+assistant) + distill   # per-turn distill
          → session_store.save (cross-request, separate pipeline)
```

### Directory Layout

```
nm/
├── nm/                    # Core engine
│   ├── agent_loop.py          #   Orchestrator (the only coupling point)
│   ├── context_manager.py     #   Window layer: messages / compression / CCR / events
│   ├── memory/               #   Memory layer: extract / arbitrate / distill / persist
│   │   ├── memcore_impl.py   #     MemoryCore facade
│   │   ├── memory_adapter.py #     Adapter (TTL / anti-loop / deep distill)
│   │   ├── distiller.py      #     Distillation pipeline
│   │   ├── extractor.py      #     Semantic extractor (Heuristic / LLM)
│   │   └── arbiter.py        #     Conflict arbiter
│   ├── task_router.py        #   Model router (local / cloud / strong)
│   ├── model_manager.py      #   Model accounts / quota / cost
│   ├── session_store.py       #   Session history (cross-request)
│   ├── auth.py               #   PBKDF2 hash + Session
│   ├── auth_middleware.py     #   Auth middleware
│   ├── oa/                   #   OA workflow engine
│   ├── im/                   #   IM engine
│   └── users/                #   User / org / HR fields
├── web/
│   ├── server.py             #   FastAPI backend
│   └── static/index.html     #   Single-file SPA frontend
├── tests/                    # 70 unit tests
├── Dockerfile
├── start.sh
└── requirements.txt
```

---

## ⚙️ Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `NM_PORT` | `8787` | Web port |
| `OPENAI_API_KEY` | - | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API endpoint (any OpenAI-compatible gateway works) |
| `NM_MODEL` | `gpt-4o-mini` | Default model |
| `NM_MAX_TURNS` | `50` | Max turns per conversation |
| `NM_MEMORY_TTL_DAYS` | `30` | Memory TTL in days (0 = never expire) |
| `NM_ALLOW_EMPTY_PASSWORD` | `0` | Allow empty passwords in demo mode |

> Without `OPENAI_API_KEY`, the server auto-discovers `~/.qclaw/openclaw.json` (QClaw gateway config).

---

## 🧪 Tests

```bash
pytest            # 70 tests — all green
```

Coverage: auth / sessions / cache strategy / memory TTL / context compression (clear preserves memory, compaction callback, CCR persistence) / permissions / task routing / IM engine / OA data compatibility / server boot.

---

## 🛣️ Roadmap

- [ ] LDAP / 企业微信 / DingTalk SSO
- [ ] WebSocket real-time IM (currently polling)
- [ ] SQLite / PostgreSQL storage (currently single JSON)
- [ ] Multi-language UI (currently Chinese-first)

---

## 📄 License

[MIT](LICENSE)

---

*N.M — All your office work, in one conversation.*
