# N.M — 企业智能办公助手

> 打工人自己的 AI 工作台：AI 助手 + 手搓 OA 审批 + 内部沟通，一个入口搞定全部办公操作。

N.M 是一个**面向办公室普通人群**的 AI 智能体产品。它把"AI 对话"与"企业内部办公系统"深度集成——你可以直接用自然语言发起审批、查数据、写公文、和同事沟通，而不需要打开五六个不同的系统。

```
🏢 一个登录 → 搞定一切
├── ✨ AI 助手       自然语言操作一切（发审批/查数据/写公文/发消息）
├── ✅ 审批中心       请假/报销/合同/用印 全流程审批 + 流程跟踪
├── 💬 部门群组       内部沟通（群组/消息/已读/编辑）
├── 🔍 查询中心       通讯录/制度/文档查询
└── ⚙️ 管理后台       IM/OA 管理、员工花名册导入、HR 同步
```

## ✨ 核心特性

| 模块 | 说明 |
|------|------|
| **AI 智能体** | AgentLoop 主循环 + 智能模型路由（复杂度/领域/PII 检测） |
| **记忆系统** | 三层记忆（稳定/决策/事实）+ TTL 过期 + 深度蒸馏 + 防循环 |
| **会话连续** | 跨请求上下文保持——"帮我写通知" → "改成红色" AI 完全记得前文 |
| **OA 审批** | 零数据库工作流引擎：请假/报销/合同/用印/调岗 10 种预设模板 |
| **内部沟通** | 群组管理、成员权限、消息已读、2 分钟撤回 |
| **自进化** | 从失败中提炼改进建议，Ratchet 验证器防止退化 |
| **认证安全** | PBKDF2 密码哈希 + Bearer Token 会话，防伪造身份 |
| **成本控制** | 三层模型路由 + 用量追踪 + 费用报表 + 智能缓存（只缓存纯 LLM 生成） |

## 🚀 快速开始

### 本地运行

```bash
# 1. 克隆并安装
git clone <your-repo-url>
cd nm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置模型（二选一）
# 方式 A: 环境变量
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export HARNESS_MODEL="gpt-4o-mini"

# 方式 B: 自动发现 ~/.qclaw/openclaw.json（已有配置则跳过）

# 3. 启动
./start.sh
# 或: python web/server.py
```

打开 http://localhost:8787 即可使用。

### Docker 部署

```bash
docker build -t nm .
docker run -d -p 8787:8787 \
  -e OPENAI_API_KEY="sk-xxx" \
  -v nm-data:/app/.harness \
  nm
```

> 数据默认存储在 `./.harness/`（单 JSON 文件，备份 = 复制目录）。

### 演示账号

| 账号 | 密码 | 角色 |
|------|------|------|
| `admin` | `demo123` | 超管 |
| `im_admin` | `demo123` | IM 管理员 |
| `oa_admin` | `demo123` | OA 管理员 |
| `zhangsan` | `demo123` | 普通员工（研发部） |
| `lisi` | `demo123` | 经理（研发部） |
| `wangwu` | `demo123` | 员工（行政部） |

## 🏗️ 架构

```
┌─────────────────────────────────────────────────┐
│             Web 前端 (web/static/index.html)     │
└──────────────────┬──────────────────────────────┘
                   │ REST + SSE
┌──────────────────▼──────────────────────────────┐
│             FastAPI 服务 (web/server.py)         │
│  ┌──────────┐ ┌──────────┐ ┌─────────────────┐  │
│  │ 认证中间件 │ │ LLM 缓存  │ │ 会话历史持久化    │  │
│  └──────────┘ └──────────┘ └─────────────────┘  │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│               AgentLoop 主循环                    │
│  ┌──────────┐ ┌──────────┐ ┌─────────────────┐  │
│  │TaskRouter │ │ContextMgr│ │   Permission    │  │
│  │ 三层模型   │ │ 上下文压缩 │ │   权限检查      │  │
│  └──────────┘ └──────────┘ └─────────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌─────────────────┐  │
│  │ModelMgr  │ │ Memory   │ │   SelfEvolution  │  │
│  │ 账号池/配额│ │ 记忆系统  │ │    自进化        │  │
│  └──────────┘ └──────────┘ └─────────────────┘  │
└───────┬─────────────────────────────────────────┘
        ▼
┌───────────────┬───────────────┬───────────────┐
│  OA 工作流引擎  │   IM 沟通引擎   │  用户/组织     │
│ 审批/流转/跟踪  │ 群组/消息/已读  │ 花名册/权限    │
└───────────────┴───────────────┴───────────────┘
```

### 目录结构

```
nm/
├── harness/              # 核心引擎
│   ├── agent_loop.py     #   Agent 主循环
│   ├── task_router.py    #   智能模型路由
│   ├── model_manager.py  #   模型账号/配额/费用
│   ├── context_manager.py#   上下文管理/压缩
│   ├── memory/           #   记忆系统（TTL/蒸馏/防循环）
│   ├── session_store.py  #   会话历史持久化
│   ├── auth.py           #   密码哈希 + Session
│   ├── auth_middleware.py#   认证中间件
│   ├── oa/               #   OA 工作流引擎（表单/状态机/审批）
│   ├── im/               #   内部沟通引擎
│   └── users/            #   用户/组织/HR 字段
├── web/
│   ├── server.py         #   FastAPI 后端
│   └── static/index.html #   前端（单文件 SPA）
├── tests/                # 67 个单元测试
├── Dockerfile
├── start.sh
└── requirements.txt
```

## ⚙️ 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HARNESS_PORT` | `8787` | Web 服务端口 |
| `OPENAI_API_KEY` | - | OpenAI 兼容 API Key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API 端点（可指向任意 OpenAI 兼容网关） |
| `HARNESS_MODEL` | `gpt-4o-mini` | 默认模型 |
| `HARNESS_MAX_TURNS` | `50` | 单次对话最大轮数 |
| `HARNESS_MEMORY_TTL_DAYS` | `30` | 记忆过期天数 |
| `HARNESS_ALLOW_EMPTY_PASSWORD` | `0` | 演示模式允许空密码（设为 1 开启） |

> 未配置 `OPENAI_API_KEY` 时，服务会自动发现 `~/.qclaw/openclaw.json`（QClaw 网关配置）。

## 🧪 测试

```bash
pytest            # 67 个测试全绿
```

覆盖：认证/会话/缓存策略/记忆 TTL/权限检查/任务路由/IM 引擎/OA 数据兼容/服务启动。

## 🛣️ Roadmap

- [ ] LDAP / 企业微信 / 钉钉 SSO 对接
- [ ] WebSocket 实时 IM 推送（当前为轮询）
- [ ] SQLite / PostgreSQL 存储（当前单 JSON 文件）
- [ ] 多语言（当前中文优先）

## 📄 License

[MIT](LICENSE)

---

**N.M** — 让打工人把所有办公操作，放进一个对话框。
