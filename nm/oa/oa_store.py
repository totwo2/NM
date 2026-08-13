"""
OA 存储层 — 单 JSON 文件持久化
零外部数据库依赖，备份 = cp，迁移 = scp
"""

import json
import os
import threading
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from .workflow import WorkflowDef, WorkflowInstance, WorkflowStep, StepRecord

logger = logging.getLogger(__name__)


def _deserialize_def(data: dict) -> WorkflowDef:
    """从 JSON dict 反序列化为 WorkflowDef"""
    steps = [
        WorkflowStep(**{k: v for k, v in s.items() if k in WorkflowStep.__dataclass_fields__})
        for s in data.get("steps", [])
    ]
    data = {k: v for k, v in data.items() if k != "steps"}
    return WorkflowDef(steps=steps, **data)


def _deserialize_instance(data: dict) -> WorkflowInstance:
    """从 JSON dict 反序列化为 WorkflowInstance"""
    history = []
    for r in data.get("history", []):
        # 兼容旧数据：缺失字段给默认值
        clean = {k: v for k, v in r.items() if k in StepRecord.__dataclass_fields__}
        clean.setdefault("step_name", clean.get("step_id", ""))
        clean.setdefault("assignee", "")
        clean.setdefault("action", "approved")
        clean.setdefault("comment", "")
        clean.setdefault("timestamp", "")
        history.append(StepRecord(**clean))
    data = {k: v for k, v in data.items() if k != "history"}
    return WorkflowInstance(history=history, **data)


def _to_dict(obj: Any) -> dict:
    """将 dataclass 实例转为可序列化的 dict"""
    if is_dataclass(obj) and not isinstance(obj, type):
        d = asdict(obj)
        # 处理 nested dataclass
        for k, v in list(d.items()):
            if is_dataclass(v) and not isinstance(v, type):
                d[k] = asdict(v)
        return d
    return obj


class OAStore:
    """OA 数据存储（单 JSON 文件）

    结构:
    {
        "defs": [...],         // 流程模板定义
        "instances": [...],    // 流程实例
        "seq": 1               // 流水号（自动递增）
    }
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict = {"defs": [], "instances": [], "seq": 1}
        self._load()

    # ---- 持久化 ----

    def _load(self):
        """从文件加载，损坏/不存在当空"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {"defs": [], "instances": [], "seq": 1}
        # 确保 key 存在
        for key in ("defs", "instances", "seq"):
            if key not in self._data:
                self._data[key] = [] if key != "seq" else 1

    def _save(self):
        """原子写（tmp + os.replace，进程崩溃不留半写文件）"""
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 流程模板 ----

    def get_def(self, def_id: str) -> WorkflowDef | None:
        with self._lock:
            self._load()  # 多实例共享时防止陈旧
            for d in self._data["defs"]:
                if d.get("id") == def_id:
                    return _deserialize_def(d)
        return None

    def list_defs(self) -> list[WorkflowDef]:
        with self._lock:
            self._load()
            return [_deserialize_def(d) for d in self._data["defs"]]

    def save_def(self, wdef: WorkflowDef):
        with self._lock:
            self._load()
            data = _to_dict(wdef)
            # 更新或新增
            for i, d in enumerate(self._data["defs"]):
                if d.get("id") == wdef.id:
                    self._data["defs"][i] = data
                    self._save()
                    return
            self._data["defs"].append(data)
            self._save()

    def delete_def(self, def_id: str) -> bool:
        with self._lock:
            self._load()
            for i, d in enumerate(self._data["defs"]):
                if d.get("id") == def_id:
                    self._data["defs"].pop(i)
                    self._save()
                    return True
        return False

    # ---- 流程实例 ----

    def get_instance(self, inst_id: str) -> WorkflowInstance | None:
        with self._lock:
            self._load()
            for d in self._data["instances"]:
                if d.get("id") == inst_id:
                    return _deserialize_instance(d)
        return None

    def list_instances(
        self,
        status: str | None = None,
        initiator: str | None = None,
        def_id: str | None = None,
    ) -> list[WorkflowInstance]:
        with self._lock:
            self._load()
            result = []
            for d in self._data["instances"]:
                if status and d.get("status") != status:
                    continue
                if initiator and d.get("initiator") != initiator:
                    continue
                if def_id and d.get("def_id") != def_id:
                    continue
                result.append(_deserialize_instance(d))
            return result

    def save_instance(self, inst: WorkflowInstance):
        with self._lock:
            self._load()
            data = _to_dict(inst)
            for i, d in enumerate(self._data["instances"]):
                if d.get("id") == inst.id:
                    self._data["instances"][i] = data
                    self._save()
                    return
            self._data["instances"].append(data)
            self._save()

    def delete_instance(self, inst_id: str) -> bool:
        with self._lock:
            self._load()
            for i, d in enumerate(self._data["instances"]):
                if d.get("id") == inst_id:
                    self._data["instances"].pop(i)
                    self._save()
                    return True
        return False

    # ---- 流水号 ----

    def next_seq(self) -> int:
        with self._lock:
            self._load()
            seq = self._data["seq"]
            self._data["seq"] += 1
            self._save()
            return seq

    # ---- 运维 ----

    def stats(self) -> dict:
        with self._lock:
            self._load()
            instances = self._data["instances"]
            by_status = {}
            for i in instances:
                s = i.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1
            return {
                "total_defs": len(self._data["defs"]),
                "total_instances": len(instances),
                "by_status": by_status,
            }

    def reset(self):
        """清空所有数据（测试用）"""
        with self._lock:
            self._data = {"defs": [], "instances": [], "seq": 1}
            self._save()
