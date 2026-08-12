"""
harness/im/im_api.py — FastAPI 路由

端点：
  POST /api/im/groups            — 创建群组
  GET  /api/im/groups            — 我的群组列表（带未读）
  GET  /api/im/groups/{gid}      — 群组详情
  PUT  /api/im/groups/{gid}      — 修改群组名称/描述
  DELETE /api/im/groups/{gid}    — 删除群组
  POST /api/im/groups/{gid}/members  — 添加成员
  DELETE /api/im/groups/{gid}/members/{uid}  — 移除成员
  GET  /api/im/groups/{gid}/members  — 成员列表
  POST /api/im/groups/{gid}/messages — 发送消息
  GET  /api/im/groups/{gid}/messages — 获取消息（分页）
  PUT  /api/im/messages/{mid}     — 编辑消息
  DELETE /api/im/messages/{mid}   — 删除消息
  GET  /api/im/search             — 搜索消息
  GET  /api/im/stats              — 个人统计
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("harness.im")

# ---- 延迟初始化（避免启动时 import 循环）----
_engine: Any = None


def get_engine() -> Any:
    global _engine
    if _engine is None:
        from harness.im.im_engine import IMEngine
        import os
        store_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".harness", "im_store.json")
        _engine = IMEngine(store_path=store_path)
        # 初始化演示数据（首次启动）
        try:
            if not os.path.exists(store_path):
                g1 = _engine.create_group("研发部", "zhangsan", "研发部门沟通群", "department")
                g2 = _engine.create_group("Alpha 项目", "zhangsan", "Alpha 项目攻坚组", "project")
                _engine.add_member(g1.id, "lisi", role="member", adder_id="zhangsan")
                _engine.add_member(g1.id, "wangwu", role="admin", adder_id="zhangsan")
                _engine.add_member(g2.id, "lisi", role="member", adder_id="zhangsan")
                _engine.send_message(g1.id, "zhangsan", "大家好，欢迎加入研发部群！")
                _engine.send_message(g1.id, "lisi", "收到，有问题随时沟通")
                _engine.send_message(g2.id, "zhangsan", "Alpha 项目本周五要 demo，大家准备一下")
                logger.info("Demo IM data initialized")
        except Exception as e:
            logger.warning("Demo IM data init failed: %s", e)
    return _engine


# ---- Pydantic 模型 ----
class CreateGroupReq(BaseModel):
    name: str
    description: str = ""
    group_type: str = "department"
    member_ids: list[str] = []


class AddMemberReq(BaseModel):
    user_id: str
    role: str = "member"


class SendMessageReq(BaseModel):
    sender_id: str
    content: str
    content_type: str = "text"


class UpdateGroupReq(BaseModel):
    name: str | None = None
    description: str | None = None


# ---- 路由注册（由 web/server.py 调用）----
def mount_im_routes(app: FastAPI, workspace_dir: str | None = None) -> None:
    """将 IM 路由挂载到 FastAPI app。"""

    @app.post("/api/im/groups")
    async def create_group(req: CreateGroupReq, user_id: str = Query("default")):
        """创建群组（user_id 为群主）。"""
        engine = get_engine()
        try:
            group = engine.create_group(
                name=req.name,
                created_by=user_id,
                description=req.description,
                group_type=req.group_type,
            )
            # 批量添加初始成员
            for uid in req.member_ids:
                if uid != user_id:
                    engine.add_member(group.id, uid, role="member", adder_id=user_id)
            return {"id": group.id, "name": group.name, "created_at": group.created_at}
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/im/groups")
    async def list_my_groups(user_id: str = Query("default")):
        """我的群组列表（带未读计数）。"""
        engine = get_engine()
        return engine.get_user_groups(user_id)

    @app.get("/api/im/groups/{group_id}")
    async def get_group_detail(group_id: str):
        """群组详情。"""
        engine = get_engine()
        group = engine.get_group(group_id)
        if not group:
            raise HTTPException(404, "群组不存在")
        members = engine.get_members(group_id)
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "group_type": group.group_type,
            "created_by": group.created_by,
            "created_at": group.created_at,
            "member_count": len(members),
        }

    @app.put("/api/im/groups/{group_id}")
    async def update_group(group_id: str, req: UpdateGroupReq, user_id: str = Query("default")):
        """修改群组（仅 owner/admin）。"""
        engine = get_engine()
        role = engine.store.get_member_role(user_id, group_id)
        if role not in ("owner", "admin"):
            raise HTTPException(403, "仅群主/管理员可修改群组")
        kwargs: dict = {}
        if req.name is not None:
            kwargs["name"] = req.name
        if req.description is not None:
            kwargs["description"] = req.description
        group = engine.update_group(group_id, **kwargs)
        if not group:
            raise HTTPException(404, "群组不存在")
        return {"id": group.id, "name": group.name}

    @app.delete("/api/im/groups/{group_id}")
    async def delete_group(group_id: str, user_id: str = Query("default")):
        """删除群组（仅 owner）。"""
        engine = get_engine()
        ok = engine.delete_group(group_id, user_id)
        if not ok:
            raise HTTPException(404, "群组不存在或无权限")
        return {"ok": True}

    @app.post("/api/im/groups/{group_id}/members")
    async def add_member(group_id: str, req: AddMemberReq, user_id: str = Query("default")):
        """添加成员。"""
        engine = get_engine()
        try:
            member = engine.add_member(group_id, req.user_id, role=req.role, adder_id=user_id)
            return {"user_id": member.user_id, "role": member.role, "joined_at": member.joined_at}
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/im/groups/{group_id}/members/{target_uid}")
    async def remove_member(group_id: str, target_uid: str, user_id: str = Query("default")):
        """移除成员（自己退出或 owner/admin 移除）。"""
        engine = get_engine()
        try:
            ok = engine.remove_member(group_id, target_uid, user_id)
            if not ok:
                raise HTTPException(404, "成员不存在")
            return {"ok": True}
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/im/groups/{group_id}/members")
    async def list_members(group_id: str):
        """群组成员列表。"""
        engine = get_engine()
        if not engine.get_group(group_id):
            raise HTTPException(404, "群组不存在")
        members = engine.get_members(group_id)
        return [m.to_dict() for m in members]

    @app.post("/api/im/groups/{group_id}/messages")
    async def send_message(group_id: str, req: SendMessageReq):
        """发送消息。"""
        engine = get_engine()
        try:
            msg = engine.send_message(group_id, req.sender_id, req.content, req.content_type)
            return msg.to_dict()
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/im/groups/{group_id}/messages")
    async def get_messages(
        group_id: str,
        user_id: str = Query("default"),
        limit: int = Query(50, ge=1, le=100),
        before_id: str = Query(""),
    ):
        """获取消息（自动标记已读）。"""
        engine = get_engine()
        try:
            return engine.get_messages(group_id, user_id, limit=limit, before_id=before_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))

    @app.put("/api/im/messages/{message_id}")
    async def update_message(message_id: str, sender_id: str = Query("default"),
                             content: str = Query("")):
        """编辑消息（2 分钟内）。"""
        engine = get_engine()
        msg = engine.update_message(message_id, sender_id, content)
        if not msg:
            raise HTTPException(404, "消息不存在")
        return msg.to_dict()

    @app.delete("/api/im/messages/{message_id}")
    async def delete_message(message_id: str, user_id: str = Query("default")):
        """删除消息。"""
        engine = get_engine()
        ok = engine.delete_message(message_id, user_id)
        if not ok:
            raise HTTPException(404, "消息不存在或无权限")
        return {"ok": True}

    @app.get("/api/im/search")
    async def search(keyword: str = Query(...), group_id: str = Query(...),
                     limit: int = Query(20, ge=1, le=50)):
        """搜索消息。"""
        engine = get_engine()
        return engine.search_messages(group_id, keyword, limit)

    @app.get("/api/im/stats")
    async def my_stats(user_id: str = Query("default")):
        """个人通讯统计。"""
        engine = get_engine()
        return engine.stats(user_id)

    logger.info("IM routes mounted at /api/im")
