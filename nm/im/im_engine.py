"""
harness/im/im_engine.py — 内部通讯引擎（业务逻辑层）

职责：群组 CRUD、成员管理、消息发送/查询、未读统计。
存储委托给 IMStore。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from nm.im.im_store import IMStore, Group, Member, Message


class IMEngine:
    """内部通讯引擎"""

    def __init__(self, store: IMStore | None = None, store_path: str = "im_store.json"):
        self.store = store or IMStore(store_path)

    # ========================================================================
    # 群组管理
    # ========================================================================

    def create_group(self, name: str, created_by: str, description: str = "",
                     group_type: str = "department") -> Group:
        """创建群组，创建者自动成为 owner"""
        group = Group(
            id=_new_id("g"),
            name=name,
            description=description,
            group_type=group_type,
            created_by=created_by,
            created_at=_now(),
        )
        self.store.add_group(group)
        # 创建者自动加入
        self.add_member(group.id, created_by, role="owner")
        return group

    def get_group(self, group_id: str) -> Group | None:
        return self.store.get_group(group_id)

    def list_groups(self) -> list[Group]:
        return self.store.list_groups()

    def update_group(self, group_id: str, **kwargs) -> Group | None:
        return self.store.update_group(group_id, **kwargs)

    def delete_group(self, group_id: str, operator_id: str) -> bool:
        group = self.store.get_group(group_id)
        if not group:
            return False
        # 只有 owner 能删
        if group.created_by != operator_id:
            raise PermissionError("只有群主可删除群组")
        return self.store.delete_group(group_id)

    # ========================================================================
    # 成员管理
    # ========================================================================

    def add_member(self, group_id: str, user_id: str, role: str = "member",
                   adder_id: str = "") -> Member:
        """添加成员。adder_id 为空时系统操作。"""
        group = self.store.get_group(group_id)
        if not group:
            raise ValueError(f"群组 {group_id} 不存在")
        # 权限：owner/admin 可加人
        if adder_id:
            adder_role = self.store.get_member_role(adder_id, group_id)
            if adder_role not in ("owner", "admin"):
                raise PermissionError("只有群主/管理员可添加成员")
        member = Member(
            user_id=user_id,
            group_id=group_id,
            role=role,
            joined_at=_now(),
        )
        self.store.add_member(member)
        return member

    def remove_member(self, group_id: str, user_id: str, operator_id: str) -> bool:
        """移除成员。operator 为群主/admin，或被移除者自己可退出。"""
        group = self.store.get_group(group_id)
        if not group:
            raise ValueError(f"群组 {group_id} 不存在")
        # 自己退出
        if user_id == operator_id:
            return self.store.remove_member(user_id, group_id)
        # 他人移除：需 owner/admin
        op_role = self.store.get_member_role(operator_id, group_id)
        if op_role not in ("owner", "admin"):
            raise PermissionError("无权限移除成员")
        # owner 不能被移除（只能删群）
        target_role = self.store.get_member_role(user_id, group_id)
        if target_role == "owner":
            raise PermissionError("不能移除群主，请先转让群主")
        return self.store.remove_member(user_id, group_id)

    def get_members(self, group_id: str) -> list[Member]:
        return self.store.get_members(group_id)

    def get_user_groups(self, user_id: str) -> list[dict]:
        """用户所在的所有群组（带未读计数）"""
        members = self.store.get_user_groups(user_id)
        result = []
        for m in members:
            group = self.store.get_group(m.group_id)
            if group:
                unread = self.store.get_unread_count(user_id, m.group_id)
                result.append({
                    "group_id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "group_type": group.group_type,
                    "role": m.role,
                    "unread_count": unread,
                    "last_read_at": m.last_read_at,
                })
        return result

    # ========================================================================
    # 消息
    # ========================================================================

    def send_message(self, group_id: str, sender_id: str, content: str,
                     content_type: str = "text") -> Message:
        """发送消息。自动校验成员身份。"""
        if not self.store.is_member(sender_id, group_id):
            raise PermissionError(f"你不是群组 {group_id} 的成员，无法发送消息")
        message = Message(
            id=_new_id("m"),
            group_id=group_id,
            sender_id=sender_id,
            content=content,
            content_type=content_type,
            created_at=_now(),
        )
        self.store.add_message(message)
        return message

    def get_messages(self, group_id: str, user_id: str, limit: int = 50,
                     before_id: str = "") -> list[dict]:
        """获取群消息，自动标记已读。"""
        # 权限校验
        if not self.store.is_member(user_id, group_id):
            raise PermissionError("你不是该群成员")
        msgs = self.store.get_messages(group_id, limit=limit, before_id=before_id)
        # 标记已读
        self.store.mark_read(user_id, group_id)
        return [m.to_dict() for m in msgs]

    def update_message(self, message_id: str, sender_id: str, content: str) -> Message | None:
        """编辑消息（2 分钟内可改）。"""
        msg = self.store.get_message(message_id)
        if not msg:
            return None
        if msg.sender_id != sender_id:
            raise PermissionError("只能编辑自己的消息")
        # 2 分钟限制
        created = datetime.fromisoformat(msg.created_at)
        if (datetime.now(timezone.utc) - created).total_seconds() > 120:
            raise PermissionError("超过 2 分钟，无法编辑")
        return self.store.update_message(message_id, content=content, edited_at=_now())

    def delete_message(self, message_id: str, operator_id: str) -> bool:
        """删除消息。发送者或群主/admin 可删。"""
        msg = self.store.get_message(message_id)
        if not msg:
            return False
        # 发送者本人
        if msg.sender_id == operator_id:
            return self.store.delete_message(message_id)
        # 群主/admin
        role = self.store.get_member_role(operator_id, msg.group_id)
        if role in ("owner", "admin"):
            return self.store.delete_message(message_id)
        raise PermissionError("无权限删除该消息")

    def search_messages(self, group_id: str, keyword: str, limit: int = 20) -> list[dict]:
        """搜索群内消息。"""
        return [m.to_dict() for m in self.store.search_messages(group_id, keyword, limit)]

    # ========================================================================
    # 统计
    # ========================================================================

    def stats(self, user_id: str) -> dict:
        """用户通讯统计。"""
        groups = self.get_user_groups(user_id)
        total_unread = sum(g["unread_count"] for g in groups)
        return {
            "my_groups": len(groups),
            "total_unread": total_unread,
            "groups": groups,
        }

    def group_stats(self, group_id: str) -> dict:
        """群组统计。"""
        members = self.store.get_members(group_id)
        return {
            "member_count": len(members),
            "members": [m.to_dict() for m in members],
        }


# ============================================================================
# 工具函数
# ============================================================================

def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
