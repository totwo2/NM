"""
harness/im/ — 内部通讯（IM）模块

数据模型 + 存储层，对标 OA 模块风格。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class Group:
    """群组"""
    id: str
    name: str
    description: str = ""
    group_type: str = "department"  # department / project / random
    created_by: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Group":
        return Group(**{k: d.get(k, "") for k in
                        ["id", "name", "description", "group_type", "created_by", "created_at"]})


@dataclass
class Member:
    """群组成员"""
    user_id: str
    group_id: str
    role: str = "member"  # owner / admin / member
    joined_at: str = ""
    last_read_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Member":
        return Member(**{k: d.get(k, "") for k in
                         ["user_id", "group_id", "role", "joined_at", "last_read_at"]})


@dataclass
class Message:
    """消息"""
    id: str
    group_id: str
    sender_id: str
    content: str
    content_type: str = "text"   # text / markdown / system
    created_at: str = ""
    edited_at: str = ""
    deleted_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Message":
        return Message(**dict(
            (k, d.get(k, "")) for k in
            ["id", "group_id", "sender_id", "content",
             "content_type", "created_at", "edited_at", "deleted_at"]
        ))


# ============================================================================
# 存储层（单 JSON 文件，原子写）
# ============================================================================

class IMStore:
    """单 JSON 文件存储，结构：
    {
      "groups": [...], "members": [...], "messages": [...]
    }
    """

    def __init__(self, path: str = "im_store.json"):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, list[dict]] = {"groups": [], "members": [], "messages": []}
        self._load()

    # ---- 原子写 ----
    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {"groups": [], "members": [], "messages": []}

    # ---- Groups ----
    def add_group(self, group: Group) -> Group:
        with self._lock:
            self._data["groups"].append(group.to_dict())
            self._save()
            return group

    def get_group(self, group_id: str) -> Group | None:
        for g in self._data["groups"]:
            if g["id"] == group_id:
                return Group.from_dict(g)
        return None

    def list_groups(self) -> list[Group]:
        return [Group.from_dict(g) for g in self._data["groups"]]

    def update_group(self, group_id: str, **kwargs) -> Group | None:
        with self._lock:
            for g in self._data["groups"]:
                if g["id"] == group_id:
                    g.update(kwargs)
                    self._save()
                    return Group.from_dict(g)
        return None

    def delete_group(self, group_id: str) -> bool:
        with self._lock:
            before = len(self._data["groups"])
            self._data["groups"] = [g for g in self._data["groups"] if g["id"] != group_id]
            # 级联清理成员 + 消息
            self._data["members"] = [m for m in self._data["members"] if m["group_id"] != group_id]
            self._data["messages"] = [m for m in self._data["messages"] if m["group_id"] != group_id]
            self._save()
            return len(self._data["groups"]) < before

    # ---- Members ----
    def add_member(self, member: Member) -> Member:
        with self._lock:
            # 去重
            self._data["members"] = [
                m for m in self._data["members"]
                if not (m["user_id"] == member.user_id and m["group_id"] == member.group_id)
            ]
            self._data["members"].append(member.to_dict())
            self._save()
            return member

    def remove_member(self, user_id: str, group_id: str) -> bool:
        with self._lock:
            before = len(self._data["members"])
            self._data["members"] = [
                m for m in self._data["members"]
                if not (m["user_id"] == user_id and m["group_id"] == group_id)
            ]
            self._save()
            return len(self._data["members"]) < before

    def get_members(self, group_id: str) -> list[Member]:
        return [Member.from_dict(m) for m in self._data["members"] if m["group_id"] == group_id]

    def get_user_groups(self, user_id: str) -> list[Member]:
        return [Member.from_dict(m) for m in self._data["members"] if m["user_id"] == user_id]

    def update_member(self, user_id: str, group_id: str, **kwargs) -> Member | None:
        with self._lock:
            for m in self._data["members"]:
                if m["user_id"] == user_id and m["group_id"] == group_id:
                    m.update(kwargs)
                    self._save()
                    return Member.from_dict(m)
        return None

    def is_member(self, user_id: str, group_id: str) -> bool:
        return any(m["user_id"] == user_id and m["group_id"] == group_id
                   for m in self._data["members"])

    def get_member_role(self, user_id: str, group_id: str) -> str:
        for m in self._data["members"]:
            if m["user_id"] == user_id and m["group_id"] == group_id:
                return m.get("role", "member")
        return ""

    # ---- Messages ----
    def add_message(self, message: Message) -> Message:
        with self._lock:
            self._data["messages"].append(message.to_dict())
            self._save()
            return message

    def get_messages(self, group_id: str, limit: int = 50, before_id: str = "") -> list[Message]:
        """获取群消息（倒序分页）。before_id 为空时取最新 limit 条。"""
        msgs = [m for m in self._data["messages"]
                if m["group_id"] == group_id and not m.get("deleted_at")]
        msgs.sort(key=lambda m: m["created_at"])

        if before_id:
            try:
                idx = next(i for i, m in enumerate(msgs) if m["id"] == before_id)
                msgs = msgs[:idx]
            except StopIteration:
                pass

        return [Message.from_dict(m) for m in msgs[-limit:]]

    def get_message(self, message_id: str) -> Message | None:
        for m in self._data["messages"]:
            if m["id"] == message_id:
                return Message.from_dict(m)
        return None

    def update_message(self, message_id: str, **kwargs) -> Message | None:
        with self._lock:
            for m in self._data["messages"]:
                if m["id"] == message_id:
                    m.update(kwargs)
                    self._save()
                    return Message.from_dict(m)
        return None

    def delete_message(self, message_id: str) -> bool:
        with self._lock:
            for m in self._data["messages"]:
                if m["id"] == message_id:
                    m["deleted_at"] = _now()
                    self._save()
                    return True
        return False

    def search_messages(self, group_id: str, keyword: str, limit: int = 20) -> list[Message]:
        results = []
        for m in self._data["messages"]:
            if m["group_id"] == group_id and not m.get("deleted_at"):
                if keyword in m["content"]:
                    results.append(Message.from_dict(m))
                    if len(results) >= limit:
                        break
        return results

    # ---- Stats ----
    def get_unread_count(self, user_id: str, group_id: str) -> int:
        member = next(
            (m for m in self._data["members"]
             if m["user_id"] == user_id and m["group_id"] == group_id),
            None
        )
        if not member:
            return 0
        last_read = member.get("last_read_at", "")
        return sum(
            1 for m in self._data["messages"]
            if m["group_id"] == group_id
            and m["sender_id"] != user_id
            and not m.get("deleted_at")
            and m["created_at"] > last_read
        )

    def mark_read(self, user_id: str, group_id: str) -> None:
        with self._lock:
            for m in self._data["members"]:
                if m["user_id"] == user_id and m["group_id"] == group_id:
                    m["last_read_at"] = _now()
                    self._save()
                    return


# ============================================================================
# 工具函数
# ============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
