"""
Harness Web 后端 — FastAPI
提供 REST API + SSE 流式响应，对接 AgentLoop
"""

import json
import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Harness 依赖
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness.model_manager import ModelManager, Group
from harness.task_router import TaskRouter
from harness.config import HarnessConfig
from harness.context_manager import ContextManager
from harness.permission_checker import PermissionChecker
from harness.types import PermissionMode
from harness.tool_registry import ToolRegistry
from harness.memory.memory_adapter import MemoryAdapter
from harness.agent_loop import AgentLoop
from harness.self_evolution import SelfEvolution

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness.web")


# ============================================================================
# 配置
# ============================================================================

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(WORKSPACE, "web")
STATIC_DIR = os.path.join(WEB_DIR, "static")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    import os
    from harness.auth import init_auth
    # 初始化认证 session 存储（.harness/sessions.json）
    init_auth(os.path.join(WORKSPACE, ".harness", "sessions.json"))

    # 确保用户库存在（首次启动种子化，含演示密码 demo123）
    from harness.users.user_store import UserStore
    users_path = os.path.join(WORKSPACE, ".harness", "users.json")
    try:
        us = UserStore(users_path)
        if not os.path.exists(users_path):
            us.seed_demo()
            logger.info("Demo users seeded (password: demo123)")
    except Exception as e:
        logger.warning("User seed failed: %s", e)

    oa_path = os.path.join(WORKSPACE, ".harness", "oa_store.json")
    if not os.path.exists(oa_path):
        try:
            from harness.oa.oa_store import OAStore
            from harness.oa.workflow import WorkflowEngine
            from harness.im.im_store import IMStore
            from harness.im.im_engine import IMEngine
            from harness.oa.presets import register_all_presets
            oa_store = OAStore(oa_path)
            oa_engine = WorkflowEngine(oa_store)
            register_all_presets(oa_engine)
            im_path = os.path.join(WORKSPACE, ".harness", "im_store.json")
            im_engine = IMEngine(store_path=im_path)
            g1 = im_engine.create_group("研发部", "zhangsan", "研发部门沟通群", "department")
            g2 = im_engine.create_group("Alpha 项目", "zhangsan", "Alpha 项目攻坚组", "project")
            im_engine.add_member(g1.id, "lisi", role="member", adder_id="zhangsan")
            im_engine.add_member(g1.id, "wangwu", role="admin", adder_id="zhangsan")
            im_engine.add_member(g2.id, "lisi", role="member", adder_id="zhangsan")
            im_engine.send_message(g1.id, "zhangsan", "大家好，欢迎加入研发部群！")
            im_engine.send_message(g1.id, "lisi", "收到，有问题随时沟通")
            im_engine.send_message(g2.id, "zhangsan", "Alpha 项目本周五要 demo，大家准备一下")
            logger.info("Demo data initialized (OA templates + IM groups)")
        except Exception as e:
            logger.warning("Demo data init failed: %s", e)
    yield

app = FastAPI(title="AI Agent Harness", version="2.0.0", lifespan=lifespan)

# ============================================================================
# 认证中间件：所有 /api/* 请求需 Bearer token（登录接口除外）
# ============================================================================
from harness.auth_middleware import auth_middleware
app.middleware("http")(auth_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ============================================================================
# 共享 LLM 响应缓存（动态积累，多用户共享，零 token 复用）
# ============================================================================
import hashlib
from collections import defaultdict

LLM_CACHE_FILE = os.path.join(WORKSPACE, ".harness", "llm_cache.json")
_llm_cache: dict[str, dict] = {}  # key=sha1(msg.lower()), value={text, ts, hits}
_llm_cache_ttl = 24 * 3600  # 24h
_llm_cache_hits = defaultdict(int)


def _load_llm_cache():
    global _llm_cache
    try:
        with open(LLM_CACHE_FILE) as f:
            raw = json.load(f)
            _llm_cache = {k: v for k, v in raw.items() if time.time() - v.get("ts", 0) < _llm_cache_ttl}
            for v in _llm_cache.values():
                _llm_cache_hits[v["text"]] += v.get("hits", 0)
    except Exception:
        _llm_cache = {}


def _save_llm_cache():
    os.makedirs(os.path.dirname(LLM_CACHE_FILE), exist_ok=True)
    with open(LLM_CACHE_FILE, "w") as f:
        json.dump(_llm_cache, f, ensure_ascii=False)


def _cache_key(msg: str, user_id: str = "default") -> str:
    """缓存 key 包含 user_id，避免不同用户共享缓存"""
    raw = f"{user_id}|{msg.strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()


# 执行了这些工具的结果属于动态业务操作，绝不缓存
_NON_CACHEABLE_TOOL_PREFIXES = (
    "oa_", "im_", "send_", "approve", "reject", "submit", "withdraw",
    "bash", "write_file", "edit_file", "create_", "delete_", "update_",
    "start_", "generate_", "checkpoint", "resume", "deduct",
)


def _is_cacheable_result(result: dict) -> bool:
    """
    判断 AgentLoop 结果是否可缓存。
    原则: 只要是"纯 LLM 生成"（无工具调用）才可缓存；
    任何执行了工具（尤其动态业务操作）的响应不可缓存，避免返回过期数据。
    """
    # 无文本不可缓存
    if not result.get("text"):
        return False
    # 执行过工具调用 → 不可缓存（可能包含动态数据或已产生副作用）
    tool_calls = result.get("tool_calls") or []
    if tool_calls:
        return False
    # 兜底：即使 tool_calls 为空，但模型名未知/错误也不缓存
    if result.get("model") == "unknown":
        return False
    return True


@app.get("/api/cache")
def get_cache(q: str = "", user_id: str = "default"):
    """查缓存：命中返回 {hit: true, text}，否则 {hit: false}"""
    if not q:
        return {"hit": False}
    k = _cache_key(q, user_id)
    e = _llm_cache.get(k)
    if e and time.time() - e["ts"] < _llm_cache_ttl:
        _llm_cache_hits[e["text"]] += 1
        e["hits"] = e.get("hits", 0) + 1
        e["last_used"] = time.time()
        return {"hit": True, "text": e["text"], "hits": e["hits"]}
    return {"hit": False}


@app.post("/api/cache")
def save_cache(body: dict):
    """写缓存：body = {q: str, text: str, user_id: str}"""
    q = (body.get("q") or "").strip()
    text = (body.get("text") or "").strip()
    user_id = body.get("user_id") or "default"
    if not q or not text:
        return {"ok": False}
    k = _cache_key(q, user_id)
    existing = _llm_cache.get(k)
    _llm_cache[k] = {
        "text": text,
        "ts": time.time(),
        "hits": (existing.get("hits", 0) if existing else 0),
        "last_used": time.time(),
    }
    # 异步持久化（不阻塞响应）
    try:
        _save_llm_cache()
    except Exception:
        pass
    return {"ok": True, "key": k}


@app.get("/api/cache/stats")
def cache_stats():
    """缓存统计（Dashboard 用）"""
    now = time.time()
    active = [e for e in _llm_cache.values() if now - e.get("ts", 0) < _llm_cache_ttl]
    return {
        "total_entries": len(active),
        "total_hits": sum(e.get("hits", 0) for e in active),
        "saved_tokens": sum(e.get("hits", 0) * 1200 for e in active),  # 粗略估算
    }


# 启动时加载 LLM 响应缓存
_load_llm_cache()


# ============================================================================
# 统一 API 响应 envelope
# ============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常拦截：统一返回 {ok: false, error: str}"""
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": str(exc) or "服务器内部错误"},
    )


# ============================================================================
# Pydantic models
# ============================================================================

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    session_id: str | None = None
    force_model: str | None = None





# ============================================================================
# Harness 实例（lazy init）
# ============================================================================

_harness: AgentLoop | None = None
_memory: MemoryAdapter | None = None
_model_manager: ModelManager | None = None
_evolver: SelfEvolution | None = None
_tool_registry: ToolRegistry | None = None
_router: TaskRouter | None = None
_session_store: Any | None = None


def get_session_store() -> Any:
    """获取全局会话历史存储（lazy init）"""
    global _session_store
    if _session_store is None:
        from harness.session_store import SessionStore
        _session_store = SessionStore(os.path.join(WORKSPACE, ".harness", "sessions_hist.json"))
    return _session_store


def get_harness() -> AgentLoop:
    global _harness, _memory, _model_manager, _evolver, _tool_registry, _router

    if _harness is not None:
        return _harness

    # 初始化 ModelManager
    mm = ModelManager()
    _model_manager = mm

    # 配置模型（从环境变量或 qclaw 自动发现）
    _setup_models(mm)

    # Memory
    mem = MemoryAdapter(
        os.path.join(WORKSPACE, ".harness", "web_mem.json"),
        os.path.join(WORKSPACE, ".harness", "web_trans.jsonl"),
        ttl_days=30,
    )
    _memory = mem

    # Tools
    tools = ToolRegistry().create_default(WORKSPACE)
    _tool_registry = tools

    # Config
    config = HarnessConfig(workspace_dir=WORKSPACE, permission_mode=PermissionMode.AUTO, web_mode=True, output_style="full")

    # Context
    ctx = ContextManager(config.system_prompt_template, WORKSPACE, output_style_rules=config.output_style_rules)

    # Permission
    perm = PermissionChecker(mode=PermissionMode.AUTO, web_mode=True)

    # TaskRouter — 本地优先云端兜底，三层模型分离
    # 从 ModelManager 取 default 组配置；若 provider 模型名不同，用组内实际第一个模型
    default_group = mm._groups.get("default")
    if default_group:
        group_models = default_group.assigned_accounts and [
            m for acct in default_group.assigned_accounts
            for m in mm._clients.get(acct, {})
        ] or []
        first_actual = group_models[0] if group_models else None
        local_m = getattr(default_group, 'local_model', None) or first_actual or "qwen3-30b-local"
        cloud_m = getattr(default_group, 'cloud_model', None) or first_actual or "gpt-4o-mini"
        strong_m = getattr(default_group, 'cloud_strong_model', None) or first_actual or "gpt-4o"
    else:
        local_m, cloud_m, strong_m = "qwen3-30b-local", "gpt-4o-mini", "gpt-4o"
    router = TaskRouter(mm, local_model=local_m, cloud_model=cloud_m, cloud_strong_model=strong_m)
    _router = router

    # AgentLoop
    harness = AgentLoop(
        config=config, memory=mem, task_router=router,
        tool_registry=tools, context_manager=ctx, permission_checker=perm,
        session_store=get_session_store(),
    )
    _harness = harness

    # SelfEvolution
    _evolver = SelfEvolution(mem, router, tools, ctx)

    # 对所有未分配的用户，自动落到默认组
    mm.assign_user_to_group("default", "default")
    # 注册一个 fallback 回调
    mm._fallback_user = "default"

    logger.info("Harness initialized (web mode)")
    return harness


# --- Monkey-patch: ModelManager 对 web 用户自动 fallback ---
_original_get_model = ModelManager.get_model

def _web_get_model(self, user_id, model_name=None):
    if user_id not in self._user_groups:
        logger.debug(f"Auto-assigning user '{user_id}' to default group")
        self._user_groups[user_id] = "default"
    return _original_get_model(self, user_id, model_name)

ModelManager.get_model = _web_get_model


def _setup_models(mm: ModelManager) -> None:
    """配置模型 — 优先环境变量，其次 qclaw 自动发现"""
    import openai

    # 方法1: 环境变量
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("HARNESS_MODEL", "gpt-4o-mini")

    if api_key:
        mm.register_provider("cloud", "cloud", base_url, api_key, [model])
        mm.add_account("default", "cloud", api_key, [model])
        mm._clients["default"] = {
            model: openai.OpenAI(base_url=base_url, api_key=api_key)
        }
        mm.add_group("default", "默认组", ["default"], default_model=model)
        mm.assign_user_to_group("default", "default")
        return

    # 方法2: qclaw 自动发现
    qclaw_path = os.path.expanduser("~/.qclaw/openclaw.json")
    if os.path.exists(qclaw_path):
        with open(qclaw_path) as f:
            qclaw = json.load(f)

        providers = qclaw.get("models", {}).get("providers", {})
        for name, cfg in providers.items():
            api_key = cfg.get("apiKey", "")
            base_url = cfg.get("baseUrl", "")
            models_list = [m["id"] for m in cfg.get("models", [])]
            if api_key and base_url and models_list:
                mm.register_provider(name, "cloud", base_url, api_key, models_list)
                mm.add_account(name, name, api_key, models_list)
                mm._clients[name] = {
                    m: openai.OpenAI(base_url=base_url, api_key=api_key)
                    for m in models_list
                }

        # 使用第一个可用 provider
        first = next(iter(mm._clients), None)
        if first:
            first_model = next(iter(mm._clients[first]))
            mm.add_group("default", "默认组", [first], default_model=first_model)
            mm.assign_user_to_group("default", "default")
            logger.info(f"Auto-configured default model: {first}/{first_model}")
            return

    raise RuntimeError(
        "No model configured. Set OPENAI_API_KEY or ensure qclaw is configured with at least one provider."
    )


# ============================================================================
# 页面路由
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """首页门户"""
    path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h1>Harness Web</h1><p>Frontend not found. Place index.html in web/static/</p>")


# ============================================================================
# Chat API
# ============================================================================

@app.get("/api/chat/stream")
async def chat_stream(message: str, user_id: str = "default", session_id: str | None = None):
    """SSE 流式聊天（透明缓存层：命中直接返回，未命中走 AgentLoop 并写缓存）"""
    async def event_stream() -> AsyncGenerator[str, None]:
        # ============================================================
        # 透明缓存拦截（放在 TaskRouter 之前，不污染业务代码）
        # ============================================================
        cache_key = _cache_key(message, user_id)
        cached = _llm_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < _llm_cache_ttl:
            cached["hits"] = cached.get("hits", 0) + 1
            cached["last_used"] = time.time()
            _llm_cache_hits[cached["text"]] += 1
            # 直接返回缓存响应（零 token，不走 AgentLoop）
            yield f"data: {json.dumps({'type': 'text', 'content': cached['text']})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'content': cached['text']})}\n\n"
            # 异步持久化命中统计
            try:
                _save_llm_cache()
            except Exception:
                pass
            return
        # ============================================================
        # 未命中：正常走 AgentLoop
        # ============================================================
        queue: list[str] = []

        def on_text(text: str):
            queue.append(json.dumps({"type": "text", "content": text}))

        def on_tool(name: str, args: dict):
            queue.append(json.dumps({"type": "tool", "name": name, "args": args}))

        try:
            harness = get_harness()
            # 注入回调
            harness.on_text = on_text
            harness.on_tool_call = on_tool
            harness.on_tool_result = lambda n, r: None

            # 在线程中运行 AgentLoop（因为是同步的）
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: harness.run(message, user_id=user_id, session_id=session_id)
            )

            # 发送缓冲事件
            for event in queue:
                yield f"data: {event}\n\n"
                await asyncio.sleep(0.01)

            # 结构化 done 事件（含 tool_calls + usage）
            yield f"data: {json.dumps({'type': 'done', 'content': result.get('text',''), 'tool_calls': result.get('tool_calls',[]), 'usage': result.get('usage',{}), 'turns': result.get('turns',0)})}\n\n"

            # 写缓存（仅纯 LLM 生成可缓存；执行过工具调用的结果绝不缓存）
            if _is_cacheable_result(result):
                k = cache_key
                _llm_cache[k] = {
                    "text": result.get("text"),
                    "ts": time.time(),
                    "hits": 0,
                    "last_used": time.time(),
                    "user_id": user_id,
                }
                try:
                    _save_llm_cache()
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Chat error")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """同步聊天（非流式，含透明缓存）"""
    # 先查缓存（不经过 AgentLoop，零 token）
    cache_key = _cache_key(req.message, req.user_id)
    cached = _llm_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < _llm_cache_ttl:
        cached["hits"] = cached.get("hits", 0) + 1
        _llm_cache_hits[cached["text"]] += 1
        try:
            _save_llm_cache()
        except Exception:
            pass
        return {
            "response": cached["text"],
            "usage": {"cached": True, "saved_tokens": len(cached["text"]) * 2},
        }
    
    # 未命中：正常走 AgentLoop
    harness = get_harness()
    result = harness.run(req.message, user_id=req.user_id, session_id=req.session_id, force_model=req.force_model)

    # 用量统计
    report = _model_manager.get_usage_report(user_id=req.user_id)
    # 写缓存（仅纯 LLM 生成可缓存；执行过工具调用的结果绝不缓存）
    if _is_cacheable_result(result):
        k = cache_key
        _llm_cache[k] = {
            "text": result["text"],
            "ts": time.time(),
            "hits": 0,
            "last_used": time.time(),
            "user_id": req.user_id,
        }
        try:
            _save_llm_cache()
        except Exception:
            pass
    return {
        "response": result.get("text", ""),
        "tool_calls": result.get("tool_calls", []),
        "usage": {
            "total_calls": report["total_calls"],
            "total_tokens": report["total_tokens"],
            "total_cost": report["total_cost"],
            "prompt_tokens": result.get("usage", {}).get("prompt_tokens", 0),
            "completion_tokens": result.get("usage", {}).get("completion_tokens", 0),
        },
        "turns": result.get("turns", 0),
    }


# ============================================================================
# OA 工作流引擎（真实审批系统）
# ============================================================================

from harness.oa.oa_api import mount_oa_routes
from harness.im.im_api import mount_im_routes
from harness.users.user_api import mount_user_routes

mount_oa_routes(app, WORKSPACE)
mount_im_routes(app, WORKSPACE)
mount_user_routes(app)
from harness.oa import mount_oa_admin_routes
mount_oa_admin_routes(app)





# ============================================================================
# Dashboard / 首页数据
# ============================================================================

@app.get("/api/dashboard")
async def get_dashboard(user_id: str = "default"):
    """首页门户数据"""
    harness = get_harness()

    # 用户信息（问候语用真实姓名）
    user_name = user_id
    try:
        from harness.users.user_store import UserStore
        user_path = os.path.join(WORKSPACE, ".harness", "users.json")
        u = UserStore(user_path).get(user_id)
        if u and u.get("name"):
            user_name = u["name"]
    except Exception:
        pass

    # 用量
    report = _model_manager.get_usage_report(user_id=user_id) if _model_manager else {}

    # 记忆统计
    mem_stats = _memory.stats() if _memory else {}

    # OA 统计数据（真实 OA 引擎）
    try:
        from harness.oa.oa_api import get_engine
        oa_engine = get_engine()
        oa_stats = oa_engine.stats(user_id)
        pending_details = oa_engine.get_pending(user_id)
    except Exception:
        oa_stats = {"pending_approvals": 0, "my_drafts": 0, "my_started_today": 0}
        pending_details = []

    return {
        "user_name": user_name,
        "greeting": _get_greeting(),
        "pending_approvals": oa_stats["pending_approvals"],
        "pending_todos": oa_stats.get("my_drafts", 0),
        "urgent_approvals": sum(1 for a in pending_details if a.get("urgent")),
        "pending_list": pending_details[:10],  # 首页直接展示前 10 条待审批
        "usage": {
            "today_calls": report["total_calls"],
            "today_tokens": report["total_tokens"],
            "this_month_cost": report["total_cost"],
        },
        "cache": cache_stats(),  # 共享 LLM 缓存统计
        "memory": {
            "total_entries": mem_stats.get("total", 0),
            "active_decisions": mem_stats.get("active_decisions", 0),
        },
        "schedule": [
            {"time": "09:00", "title": "部门周会", "room": "A301"},
            {"time": "14:00", "title": "客户来访", "room": "贵宾室"},
            {"time": "16:00", "title": "Q2 财报审阅", "room": "线上"},
        ],
    }


# ============================================================================
# AI 晨间简报生成
# ============================================================================

@app.get("/api/morning-brief")
async def morning_brief(user_id: str = "default"):
    """AI 生成晨间简报"""
    harness = get_harness()

    # 从记忆库收集最近信息
    mem_text = _memory.recall([f"user:{user_id}", "project:general"]) if _memory else ""

    prompt = f"""请生成今日晨间简报，包含:
1. 今日待办摘要（从以下记忆中提取）
2. 待审批事项提醒
3. 今日日程预览

记忆内容:
{mem_text[:2000] if mem_text else "(无历史记忆)"}"""

    harness = get_harness()
    raw = harness.run(prompt, user_id=user_id)
    result = raw.get("text", raw) if isinstance(raw, dict) else raw
    return {"brief": result}


# ============================================================================
# 文档生成 API
# ============================================================================

@app.post("/api/documents/generate")
async def generate_document(
    doc_type: str = "docx",
    template: str = "",
    content: str = "",
    user_id: str = "default",
):
    """生成文档（走 AgentLoop → OA 工具）"""
    harness = get_harness()
    prompt = f"使用模板 {template} 生成一份{doc_type}文档，内容: {content}"
    raw = harness.run(prompt, user_id=user_id)
    result = raw.get("text", raw) if isinstance(raw, dict) else raw
    return {"result": result, "turns": raw.get("turns", 1) if isinstance(raw, dict) else 1}


# ============================================================================
# 进化报告
# ============================================================================

@app.get("/api/evolution/report")
async def evolution_report():
    """获取自进化报告"""
    global _evolver
    if not _evolver:
        harness = get_harness()  # 确保初始化
    return _evolver.get_evolution_report() if _evolver else {"status": "not_initialized"}


@app.post("/api/evolution/run")
async def trigger_evolution(user_id: str = "default"):
    """手动触发一轮自进化"""
    global _evolver
    harness = get_harness()
    result = _evolver.evolve(user_id=user_id)
    return result


# ============================================================================
# 辅助
# ============================================================================

def _get_greeting() -> str:
    """根据时间生成问候语"""
    hour = time.localtime().tm_hour
    if hour < 9:
        return "早上好"
    elif hour < 12:
        return "上午好"
    elif hour < 14:
        return "中午好"
    elif hour < 18:
        return "下午好"
    return "晚上好"


# ============================================================================
# 启动
# ============================================================================

def main_entry():
    """pip entry point: harness-web"""
    import uvicorn

    port = int(os.getenv("HARNESS_PORT", "8787"))
    logger.info(f"Starting Harness Web on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main_entry()
