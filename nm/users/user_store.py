import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class User:
    id: str
    name: str
    email: str = ""
    department: str = ""
    role: str = "staff"  # admin / manager / staff
    manager_id: Optional[str] = None
    avatar: str = ""
    is_active: bool = True
    password_hash: str = ""  # PBKDF2 哈希（auth.hash_password 生成）
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # HR 字段（OA precheck 用）
    gender: str = ""  # 男/女
    age: int = 0
    phone: str = ""
    ins_years: int = 0  # 参保年限
    annual_leave: float = 0.0  # 年假余额（天）
    parental_leave: float = 0.0  # 育儿假余额（天）
    personal_leave: float = 0.0  # 事假余额（天，通常很大）
    position: str = ""  # 职位
    seat: str = ""  # 座位号


class UserStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self._data: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {"users": {}}
        else:
            self._data = {"users": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def list_users(self) -> list[dict]:
        with self.lock:
            return [User(**{**u, "id": k}).__dict__ for k, u in self._data.get("users", {}).items()]

    def get(self, user_id: str) -> Optional[dict]:
        with self.lock:
            u = self._data.get("users", {}).get(user_id)
            if not u:
                return None
            return User(**{**u, "id": user_id}).__dict__

    def create(self, user_id: str, name: str, **kwargs) -> dict:
        with self.lock:
            if user_id in self._data.get("users", {}):
                raise ValueError(f"User {user_id} already exists")
            user = User(id=user_id, name=name, **kwargs)
            self._data.setdefault("users", {})[user_id] = {k: v for k, v in user.__dict__.items() if k != "id"}
            self._save()
            return user.__dict__

    def update(self, user_id: str, **kwargs) -> Optional[dict]:
        with self.lock:
            users = self._data.setdefault("users", {})
            if user_id not in users:
                return None
            users[user_id].update({k: v for k, v in kwargs.items() if k != "id"})
            self._save()
            return User(**{**users[user_id], "id": user_id}).__dict__

    def delete(self, user_id: str) -> bool:
        with self.lock:
            users = self._data.setdefault("users", {})
            if user_id not in users:
                return False
            del users[user_id]
            self._save()
            return True

    def seed_demo(self, passwords: dict[str, str] | None = None):
        """预置演示用户，可传入 {user_id: password} 指定密码（默认 demo123）"""
        from nm.auth import hash_password

        default_pw = (passwords or {}).get("__default__", "demo123")
        with self.lock:
            users = {
                # 研发部：lisi 是经理，zhangsan 汇报给 lisi；wangerma 汇报给 zhangsan
                "lisi": {"name": "李四", "email": "lisi@company.com", "department": "研发部", "role": "manager", "manager_id": "admin", "avatar": "👩‍💼",
                    "gender": "女", "age": 35, "phone": "13800138001", "ins_years": 10,
                    "annual_leave": 10.0, "parental_leave": 0.0, "personal_leave": 99.0,
                    "position": "研发经理", "seat": "A-301"},
                "zhangsan": {"name": "张三", "email": "zhangsan@company.com", "department": "研发部", "role": "staff", "manager_id": "lisi", "avatar": "👨‍💼",
                    "gender": "男", "age": 36, "phone": "13999999999", "ins_years": 10,
                    "annual_leave": 10.0, "parental_leave": 10.0, "personal_leave": 99.0,
                    "position": "高级工程师", "seat": "2-14"},
                "wangerma": {"name": "王二麻", "email": "wangerma@company.com", "department": "研发部", "role": "staff", "manager_id": "zhangsan", "avatar": "👨‍💻",
                    "gender": "男", "age": 28, "phone": "13999999998", "ins_years": 5,
                    "annual_leave": 5.0, "parental_leave": 0.0, "personal_leave": 99.0,
                    "position": "初级工程师", "seat": "2-16"},
                # 行政部：oa_admin 兼任经理；wangwu 是员工
                "oa_admin": {"name": "OA管理员", "email": "oa_admin@company.com", "department": "行政部", "role": "manager", "manager_id": "admin", "avatar": "📋",
                    "gender": "女", "age": 32, "phone": "13800138002", "ins_years": 8,
                    "annual_leave": 8.0, "parental_leave": 5.0, "personal_leave": 99.0,
                    "position": "行政经理", "seat": "B-201"},
                "wangwu": {"name": "王五", "email": "wangwu@company.com", "department": "行政部", "role": "staff", "manager_id": "oa_admin", "avatar": "👨‍💻",
                    "gender": "男", "age": 30, "phone": "13999999997", "ins_years": 7,
                    "annual_leave": 7.0, "parental_leave": 0.0, "personal_leave": 99.0,
                    "position": "行政专员", "seat": "B-205"},
                # IM 管理员
                "im_admin": {"name": "IM管理员", "email": "im_admin@company.com", "department": "信息中心", "role": "staff", "manager_id": "admin", "avatar": "💬",
                    "gender": "男", "age": 29, "phone": "13999999996", "ins_years": 6,
                    "annual_leave": 6.0, "parental_leave": 0.0, "personal_leave": 99.0,
                    "position": "运维工程师", "seat": "C-101"},
                # 超管
                "admin": {"name": "超管", "email": "admin@company.com", "department": "信息中心", "role": "admin", "manager_id": None, "avatar": "👑",
                    "gender": "男", "age": 40, "phone": "13999999995", "ins_years": 15,
                    "annual_leave": 15.0, "parental_leave": 0.0, "personal_leave": 99.0,
                    "position": "技术总监", "seat": "C-801"},
            }
            self._data["users"] = {}
            for uid, u in users.items():
                u["password_hash"] = hash_password((passwords or {}).get(uid, default_pw))
                self._data["users"][uid] = u
            self._save()

    def get_role(self, user_id: str) -> str:
        """获取用户角色。超管 admin 自动拥有 im_admin + oa_admin 权限。"""
        u = self.get(user_id)
        if not u:
            return "guest"
        return u.get("role", "staff")

    def has_im_admin(self, user_id: str) -> bool:
        """是否有 IM 管理权限（admin 或 im_admin）"""
        role = self.get_role(user_id)
        return role in ("admin", "im_admin")

    def has_oa_admin(self, user_id: str) -> bool:
        """是否有 OA 管理权限（admin 或 oa_admin）"""
        role = self.get_role(user_id)
        return role in ("admin", "oa_admin")

    def deduct_leave(self, user_id: str, leave_type: str, days: float) -> tuple[bool, str]:
        """扣减假期余额。返回 (成功, 消息)。"""
        with self.lock:
            users = self._data.setdefault("users", {})
            u = users.get(user_id)
            if not u:
                return False, "用户不存在"
            field_map = {
                "年假": "annual_leave",
                "育儿假": "parental_leave",
                "事假": "personal_leave",
            }
            field = field_map.get(leave_type)
            if not field:
                return False, f"未知假期类型: {leave_type}"
            current = u.get(field, 0)
            if current < days:
                return False, f"{leave_type}余额不足（剩余 {current} 天，需要 {days} 天）"
            u[field] = round(current - days, 1)
            self._save()
            return True, f"扣减成功，剩余 {u[field]} 天"
