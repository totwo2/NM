"""
harness/auth.py — 认证与会话管理
PBKDF2 密码哈希 + 服务端 session token（内存 + JSON 持久化）
零第三方依赖，满足企业内部部署的基本安全要求。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, HTTPException

_ITERATIONS = 200_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32
DEFAULT_TTL_SECONDS = 24 * 3600  # session 有效期 24 小时


# ============================================================================
# 密码哈希（PBKDF2-HMAC-SHA256）
# ============================================================================

def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2 哈希，返回格式: pbkdf2$<iter>$<salt_hex>$<hash_hex>"""
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。stored 为 hash_password 的输出格式。"""
    if not stored:
        return False
    try:
        algo, iter_str, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# ============================================================================
# Session 存储
# ============================================================================

class SessionStore:
    """服务端 session: token -> {user_id, expires_at}，支持 JSON 持久化"""

    def __init__(self, path: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.path = path
        self.ttl = ttl_seconds
        self._sessions: dict[str, dict] = {}
        self._lock = threading.RLock()
        if path and os.path.exists(path):
            self._load()

    def create(self, user_id: str) -> str:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            self._sessions[token] = {
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": time.time() + self.ttl,
            }
            self._save()
        return token

    def verify(self, token: str) -> str | None:
        """校验 token，返回 user_id；无效/过期返回 None"""
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if time.time() > s.get("expires_at", 0):
                self._sessions.pop(token, None)
                self._save()
                return None
            return s.get("user_id")

    def revoke(self, token: str) -> bool:
        with self._lock:
            existed = self._sessions.pop(token, None) is not None
            if existed:
                self._save()
            return existed

    def _load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
            now = time.time()
            self._sessions = {k: v for k, v in raw.items() if v.get("expires_at", 0) > now}
        except Exception:
            self._sessions = {}

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._sessions, f)
            os.replace(tmp, self.path)
        except Exception:
            pass


# ============================================================================
# FastAPI 集成
# ============================================================================

_SESSION_STORE: SessionStore | None = None


def init_auth(path: str) -> SessionStore:
    """初始化全局 session store（web 启动时调用一次）"""
    global _SESSION_STORE
    if _SESSION_STORE is None:
        _SESSION_STORE = SessionStore(path=path)
    return _SESSION_STORE


def get_session_store() -> SessionStore:
    if _SESSION_STORE is None:
        raise RuntimeError("auth 未初始化: 请先调用 init_auth()")
    return _SESSION_STORE


def current_user(request: Request) -> str:
    """FastAPI 依赖：从请求上下文取已认证的 user_id（由认证中间件写入）"""
    uid = getattr(request.state, "user_id", "")
    if not uid:
        raise HTTPException(401, "未登录")
    return uid


def extract_token(request: Request) -> str:
    """从 Authorization header 提取 Bearer token"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""