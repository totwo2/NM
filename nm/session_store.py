"""
harness/session_store.py — 会话历史持久化
按 session_id 保存对话消息（纯 user/assistant 文本轮次），
使 AI 助手跨请求保持上下文连续性（"帮我写通知"→"改成红色"能关联前文）。

存储: 单 JSON 文件 {session_id: {"messages": [...], "updated_at": ...}}
限制: 每会话最多保留 MAX_MESSAGES 条（防无限膨胀）
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

MAX_MESSAGES = 30  # 每会话保留最近 30 条文本消息（15 轮对话）
MAX_SESSIONS = 200  # 最多保留 200 个会话（防文件膨胀）


class SessionStore:
    """会话历史存储（线程安全 + 原子写）"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._load()

    # ---- 持久化 ----

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- 读写 ----

    def get_messages(self, session_id: str) -> list[dict]:
        """返回该会话的历史文本消息 [{role, content}, ...]"""
        with self._lock:
            s = self._data.get(session_id)
            if not s:
                return []
            return list(s.get("messages", []))

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息（自动裁剪）"""
        if not content.strip():
            return
        with self._lock:
            s = self._data.setdefault(session_id, {
                "messages": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            s["messages"].append({"role": role, "content": content})
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            # 裁剪
            if len(s["messages"]) > MAX_MESSAGES:
                s["messages"] = s["messages"][-MAX_MESSAGES:]
            self._prune_sessions()
            self._save()

    def clear(self, session_id: str) -> bool:
        with self._lock:
            existed = self._data.pop(session_id, None) is not None
            if existed:
                self._save()
            return existed

    def list_sessions(self, user_id: str = "") -> list[dict]:
        """列出会话（session_id 含 user_id 前缀时可按用户过滤）"""
        with self._lock:
            out = []
            for sid, s in self._data.items():
                if user_id and f"user:{user_id}" not in sid and not sid.startswith(user_id):
                    continue
                out.append({
                    "session_id": sid,
                    "message_count": len(s.get("messages", [])),
                    "updated_at": s.get("updated_at", ""),
                })
            out.sort(key=lambda x: x["updated_at"], reverse=True)
            return out

    def _prune_sessions(self):
        """超过 MAX_SESSIONS 时删除最旧的"""
        if len(self._data) <= MAX_SESSIONS:
            return
        ordered = sorted(
            self._data.items(),
            key=lambda kv: kv[1].get("updated_at", ""),
            reverse=True,
        )
        self._data = dict(ordered[:MAX_SESSIONS])