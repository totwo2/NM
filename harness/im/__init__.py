"""
harness/im/ — 内部通讯（IM）模块

组件：
  im_store  — 存储层（单 JSON 文件，原子写）
  im_engine — 业务逻辑（群组/成员/消息/未读）
  im_api    — FastAPI 路由（14 个端点）
"""
from harness.im.im_store import IMStore, Group, Member, Message
from harness.im.im_engine import IMEngine

__all__ = ["IMStore", "IMEngine", "Group", "Member", "Message"]
