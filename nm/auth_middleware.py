"""
harness/auth_middleware.py — FastAPI 认证中间件
所有 /api/* 请求必须携带有效 Bearer token（除 /api/auth/login 等公开端点）。
- 从 token 解析出 user_id，写入 request.state.user_id
- 若请求 query 携带 user_id，必须与 token 身份一致，否则 403（防伪造身份）

用法（web/server.py）:
    from nm.auth_middleware import auth_middleware
    app.middleware("http")(auth_middleware)
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from nm.auth import get_session_store, extract_token

logger = logging.getLogger(__name__)

# 无需认证的公开路径前缀
PUBLIC_PREFIXES = (
    "/api/auth/login",
    "/api/auth/logout",
    "/static",
)

# 无需认证的开放路径
OPEN_PATHS = {
    "/api/health",
    "/api/version",
}


async def auth_middleware(request: Request, call_next):
    """FastAPI HTTP 中间件（async 签名）"""
    path = request.url.path

    # 公开端点放行
    if any(path.startswith(p) for p in PUBLIC_PREFIXES) or path in OPEN_PATHS:
        return await call_next(request)

    # 仅拦截 /api/* （静态资源与页面不需认证）
    if not path.startswith("/api/"):
        return await call_next(request)

    token = extract_token(request)
    if not token:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "未登录或登录已过期，请重新登录"},
        )

    user_id = get_session_store().verify(token)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "登录已过期，请重新登录"},
        )

    request.state.user_id = user_id

    # query 身份一致性校验：若请求带了 user_id 参数，必须等于 token 身份
    q_user = request.query_params.get("user_id")
    if q_user and q_user != user_id:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "身份校验失败：请求身份与登录身份不一致"},
        )

    return await call_next(request)