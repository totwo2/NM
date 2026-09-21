# N.M — 企业智能办公助手

> 打工人自己的 AI 工作台：AI 助手 + 手搓 OA 审批 + 内部沟通，一个入口搞定全部办公操作。

**English version: [README.en.md](README.en.md)**

---

## ✨ 亮点 / Highlights

N.M 不是又一个大号 OA 系统，而是一个 **「AI 智能体办公入口」** —— 把企业办公里最常用的事，全部放进一个对话框：

```
🏢 一个登录 → 搞定一切
├── ✨ AI 助手       自然语言操作一切（发审批/查数据/写公文/发消息）
├── ✅ 审批中心       请假/报销/合同/用印 全流程审批 + 流程跟踪
├── 💬 部门群组       内部沟通（群组/消息/已读/编辑）
├── 🔍 查询中心       通讯录/制度/文档查询
└── ⚙️ 管理后台       IM/OA 管理、员工花名册导入、HR 同步
```

### 为什么值得看 / Why it's interesting

| 亮点 | 说明 |
|------|------|
| **🧠 窗口与记忆分离架构** | 上下文管理器（窗口内短时态）与记忆体（跨会话持久态）**解耦设计**：压缩时通过事件回调把被压掉的原文沉淀进记忆体，形成「压缩 → 回灌」闭环，换模型/重启后 CCR 存档可恢复 |
| **🔄 记忆系统** | 三层记忆（稳定轨迹/决策/事实）+ 提取-仲裁-蒸馏流水线 + TTL 过期 + 深度蒸馏 + 防循环检测 + 原始全量留存（transcript.jsonl） |
| **🤖 AgentLoop 主循环** | 记忆回灌 → 工具路由 → LLM 调用 → 工具执行 → 智能压缩 → 记忆沉淀，全流程可追踪 |
| **💰 省 token 设计** | 三层模型路由（本地/云端/强模型）+ 智能缓存（只缓存纯 LLM 生成）+ 上下文智能压缩（JSON/日志/文件列表/大段文本分类型压缩）+ 表单字段纯正则提取（零 LLM 调用） |
| **📋 零数据库 OA** | 单 JSON 文件工作流引擎：请假/报销/合同/用印/调岗等 10 种预设模板，状态机 draft→pending→completed/rejected/withdrawn，支持退回/转办/撤回，备份 = 复制目录 |
| **💬 内置 IM** | 群组管理、消息已读、2 分钟撤回；在 AI 对话框里说到"请假"等意图自动弹出审批卡片，审批结果通过 IM 私聊通知 |
| **🦾 自进化** | 从失败中自动提炼改进建议，Ratchet 验证器保证只升不降 |
| **🔐 认证安全** | PBKDF2 密码哈希 + Bearer Token 会话，防伪造身份 |
| **🪶 极轻部署** | 单文件前端 SPA + FastAPI 后端 + Docker 一键部署，企业 IT 零负担 |

---

## 🚀 快速开始 / Quick Start

### 本地运行 / Local

```bash
# 1. 克隆并安装
git clone https://github.com/totwo2/NM.git
cd NM
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置模型（二选一）
# 方式 A: 环境变量
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export NM_MODEL="gpt-4o-mini"

# 方式 B: 自动发现 ~/.qclaw/openclaw.json（已有配置则跳过）

# 3. 启动
./start.sh
# 或：python web/server.py
```

打开 http://localhost:8787 即可使用。

### Docker 部署 / Docker

```bash
docker build -t nm .
docker run -d -p 8787:8787 \
  -e OPENAI_API_KEY="sk-xxx" \
  -v nm-data:/app/.nm \
  nm
```

> 数据默认存储在 `./.nm/`（单 JSON 文件，备份 = 复制目录）。
> Data lives in `./.nm/` (single JSON files, backup = copy the folder).

### 演示账号 / Demo accounts

| 账号 / Account | 密码 / Password | 角色 / Role |
|------|------|------|
| `admin` | `demo123` | 超管 / Super admin |
| `im_admin` | `demo123` | IM 管理员 / IM admin |
| `oa_admin` | `demo123` | OA 管理员 / OA admin |
| `zhangsan` | `demo123` | 普通员工（研发部）/ Employee (R&D) |
| `lisi` | `demo123` | 经理（研发部）/ Manager (R&D) |
| `wangwu` | `demo123` | 员工（行政部）/ Employee (Admin) |

**默认密码仅供本地演示；对外网部署前必须修改所有账号密码**（Docker 默认绑定 0.0.0.0）。

---

## 🏗️ 架构 / Architecture

### 核心分层：窗口与记忆解耦 / Core layering: window vs memory

```
┌────────────────────────────────────────────────────────────┐
│                     Web 前端 (单文件 SPA)                     │
└──────────────────────────┬─────────────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼─────────────────────────────────┐
│                  FastAPI 服务 (web/server.py)               │
│  认证中间件 · LLM 缓存 · 会话历史持久化 · 用量追踪               │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│                    AgentLoop 主循环                          │
│                                                            │
│  ┌─────────────── 中间层 ContextManager ───────────────┐   │
│  │ 窗口内消息管理 · system prompt 组装 · token 预算       │   │
│  │ 智能压缩: tool_result → 占位符 + CCR 存档(落盘)        │   │
│  │ 压缩事件 on_compaction ──┐                          │   │
│  └──────────────────────────┼──────────────────────────┘   │
│                             │ 解耦回调（不感知记忆体）        │
│                             ▼                             │
│  ┌─────────────── 记忆体 MemoryCore ──────────────────┐   │
│  │ 提取(Extractor) → 仲裁(Arbiter) → 蒸馏(Distiller)   │   │
│  │ 持久化: memory_store.json + transcript.jsonl        │   │
│  │ 回灌: recall_split → stable(轨迹常驻) + volatile    │   │
│  └───────────────▲────────────────────────────────────┘   │
│                  │ recall 回灌 → set_memory                │
│  TaskRouter(三层模型) · ModelManager(账号池) · Permission  │
│  SelfEvolution(自进化) · SessionStore(跨请求会话)           │
└───────────┬────────────────────────────────────────────────┘
            ▼
┌───────────────┬───────────────┬──────────────────────────┐
│  OA 工作流引擎  │   IM 沟通引擎   │  用户/组织/权限            │
│ 审批/流转/跟踪  │ 群组/消息/已读  │ 花名册/认证/HR 字段        │
└───────────────┴───────────────┴──────────────────────────┘
```

**窗口与记忆为什么分开 / Why window and memory are separate:**

- **中间层 ContextManager** 只管「这轮对话窗口内」：消息列表、压缩、system prompt 组装。它是易失的、进程内的、与服务同生命周期的。
- **记忆体 MemoryCore** 只管「跨会话的长期记忆」：提取、冲突仲裁、蒸馏、持久化。它是落盘的、永久的、独立于任何一次对话的。
- 两者的唯一接触点是 AgentLoop：`clear() → recall() → set_memory()` 回灌记忆；压缩发生时通过 `on_compaction` 事件把被压掉的原文沉淀进记忆体——**中间层只发事件，不感知记忆体；记忆体只收事件，不感知窗口**。

### 记忆链路 / Memory pipeline

```
用户输入 → AgentLoop.run()
         → context.clear()                      # 清窗口（保留记忆态）
         → memory.recall(scope)                 # 回灌稳定+易变记忆
         → context.set_memory(记忆文本)          # 注入 system prompt
         → 循环: 路由 → LLM → 工具执行 → 智能压缩
         → 压缩事件 on_compaction → memory.ingest + distill   # 沉淀
         → memory.ingest(user+assistant) + distill            # 每轮沉淀
         → session_store 保存会话历史（跨请求连续性，独立链路）
```

### 目录结构 / Directory layout

```
nm/
├── nm/                  # 核心引擎
│   ├── agent_loop.py        #   Agent 主循环（组装点，唯一的耦合处）
│   ├── context_manager.py   #   中间层：窗口管理/压缩/CCR/事件回调
│   ├── memory/              #   记忆体：提取/仲裁/蒸馏/持久化
│   │   ├── memcore_impl.py  #     MemoryCore 门面（含提取/仲裁/蒸馏）
│   │   └── memory_adapter.py#     适配层（TTL/防循环/深度蒸馏）
│   ├── task_router.py       #   智能模型路由（本地/云端/强模型）
│   ├── model_manager.py     #   模型账号/配额/费用
│   ├── session_store.py     #   会话历史持久化（跨请求）
│   ├── auth.py              #   密码哈希 + Session
│   ├── auth_middleware.py   #   认证中间件
│   ├── oa/                  #   OA 工作流引擎（表单/状态机/审批）
│   ├── im/                  #   内部沟通引擎
│   └── users/               #   用户/组织/HR 字段
├── web/
│   ├── server.py            #   FastAPI 后端
│   └── static/index.html    #   前端（单文件 SPA）
├── tests/                   # 70 个单元测试
├── Dockerfile
├── start.sh
└── requirements.txt
```

---

## ⚙️ 配置 / Configuration

| 环境变量 / Env var | 默认值 / Default | 说明 / Description |
|----------|--------|------|
| `NM_PORT` | `8787` | Web 服务端口 / Web port |
| `OPENAI_API_KEY` | - | OpenAI 兼容 API Key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API 端点（可指向任意 OpenAI 兼容网关） |
| `NM_MODEL` | `gpt-4o-mini` | 默认模型 / Default model |
| `NM_MAX_TURNS` | `50` | 单次对话最大轮数 / Max turns per conversation |
| `NM_MEMORY_TTL_DAYS` | `30` | 记忆过期天数（0=永不过期）/ Memory TTL days (0=never) |
| `NM_ALLOW_EMPTY_PASSWORD` | `0` | 演示模式允许空密码 / Allow empty passwords in demo |

> 未配置 `OPENAI_API_KEY` 时，服务会自动发现 `~/.qclaw/openclaw.json`（QClaw 网关配置）。
> Without `OPENAI_API_KEY`, the server auto-discovers `~/.qclaw/openclaw.json`.

---

## 🧪 测试 / Tests

```bash
pytest            # 70 个测试全绿 / 70 tests all green
```

覆盖 / Coverage：认证 / 会话 / 缓存策略 / 记忆 TTL / 上下文压缩（含 clear 保留记忆、压缩事件回调、CCR 持久化）/ 权限检查 / 任务路由 / IM 引擎 / OA 数据兼容 / 服务启动。

---

## 🛣️ Roadmap

- [ ] LDAP / 企业微信 / 钉钉 SSO 对接
- [ ] WebSocket 实时 IM 推送（当前为轮询）
- [ ] SQLite / PostgreSQL 存储（当前单 JSON 文件）
- [ ] 多语言 UI（当前中文优先）

---

## 📄 License

[MIT](LICENSE)

---

**N.M** — 让打工人把所有办公操作，放进一个对话框。
*All your office work, in one conversation.*
