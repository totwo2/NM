from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Request
from pydantic import BaseModel
from typing import Optional

from .user_store import UserStore

import os
router = APIRouter()

_store: UserStore | None = None


def _get_store() -> UserStore:
    global _store
    if _store is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".harness", "users.json")
        _store = UserStore(path)
        # 首次启动预置演示用户
        try:
            if not os.path.exists(path):
                _store.seed_demo()
        except Exception:
            pass
    return _store


# ---- Models ----

class UserCreate(BaseModel):
    user_id: str
    name: str
    email: str = ""
    department: str = ""
    role: str = "staff"
    manager_id: Optional[str] = None
    avatar: str = ""


class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    department: Optional[str] = None
    role: Optional[str] = None
    manager_id: Optional[str] = None
    avatar: Optional[str] = None
    is_active: Optional[bool] = None


class LoginReq(BaseModel):
    user_id: str
    password: str = ""  # 演示版允许空密码直接登录（HARNESS_ALLOW_EMPTY_PASSWORD=1 时）


# ---- Routes ----

@router.get("/api/users")
def list_users(role: Optional[str] = None, department: Optional[str] = None):
    store = _get_store()
    users = store.list_users()
    if role:
        users = [u for u in users if u.get("role") == role]
    if department:
        users = [u for u in users if u.get("department") == department]
    return {"total": len(users), "items": [_safe_user(u) for u in users]}


@router.get("/api/users/me")
def get_me(user_id: str = Query(...)):
    store = _get_store()
    u = store.get(user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return _safe_user(u)


@router.get("/api/users/{user_id}")
def get_user(user_id: str):
    store = _get_store()
    u = store.get(user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return _safe_user(u)


@router.post("/api/users")
def create_user(req: UserCreate):
    store = _get_store()
    try:
        u = store.create(req.user_id, req.name, email=req.email, department=req.department,
                         role=req.role, manager_id=req.manager_id, avatar=req.avatar)
        return _safe_user(u)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/api/users/{user_id}")
def update_user(user_id: str, req: UserUpdate):
    store = _get_store()
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    if not data:
        return _safe_user(store.get(user_id) or {})
    u = store.update(user_id, **data)
    if not u:
        raise HTTPException(404, "User not found")
    return _safe_user(u)


@router.delete("/api/users/{user_id}")
def delete_user(user_id: str):
    store = _get_store()
    ok = store.delete(user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


def _safe_user(u: dict) -> dict:
    """剔除敏感字段（password_hash）后的用户对象，用于 API 返回"""
    return {k: v for k, v in u.items() if k != "password_hash"}


@router.post("/api/auth/login")
def login(req: LoginReq, request: Request):
    import os
    store = _get_store()
    u = store.get(req.user_id)
    if not u or not u.get("is_active", True):
        raise HTTPException(401, "用户名或密码错误")

    # 空密码快速登录开关（默认关闭；演示环境可设 HARNESS_ALLOW_EMPTY_PASSWORD=1）
    allow_empty = os.getenv("HARNESS_ALLOW_EMPTY_PASSWORD", "0") == "1"
    stored_hash = u.get("password_hash", "")
    if req.password:
        from harness.auth import verify_password
        if not verify_password(req.password, stored_hash):
            raise HTTPException(401, "用户名或密码错误")
    elif allow_empty and not stored_hash:
        pass  # 仅当用户无密码哈希时可空密码登录
    elif allow_empty:
        from harness.auth import verify_password
        if not verify_password("", stored_hash):
            raise HTTPException(401, "用户名或密码错误")
    else:
        raise HTTPException(401, "请输入密码")

    # 签发 session token
    from harness.auth import get_session_store
    token = get_session_store().create(req.user_id)
    return {"user": _safe_user(u), "token": token}


@router.post("/api/auth/logout")
def logout(request: Request):
    from harness.auth import get_session_store, extract_token
    token = extract_token(request)
    if token:
        get_session_store().revoke(token)
    return {"ok": True}


@router.get("/api/auth/me")
def auth_me(request: Request):
    """恢复会话：根据 token 返回当前用户（配合前端刷新页面自动登录）"""
    uid = getattr(request.state, "user_id", "")
    if not uid:
        raise HTTPException(401, "未登录")
    store = _get_store()
    u = store.get(uid)
    if not u:
        raise HTTPException(401, "用户不存在")
    return {"user": _safe_user(u)}


@router.get("/api/departments")
def list_departments():
    store = _get_store()
    users = store.list_users()
    depts = {}
    for u in users:
        d = u.get("department", "").strip()
        if not d:
            continue
        if d not in depts:
            depts[d] = {"name": d, "head": None, "count": 0}
        depts[d]["count"] += 1
        if u.get("role") == "manager" and depts[d]["head"] is None:
            depts[d]["head"] = u.get("name")
    return {"total": len(depts), "items": list(depts.values())}


def mount_user_routes(app):
    app.include_router(router)


# ---- 用户批量导入（CSV/Excel） ----

import csv
import io

@router.post("/api/users/import/preview")
async def import_preview(file: UploadFile = File(...)):
    """解析上传文件并预览（不入库），返回预览数据 + 检测到的列字段。
    支持格式：CSV / Excel(.xlsx) / TSV
    """
    content = await file.read()
    name = file.filename or ""
    rows = []
    headers = []
    
    try:
        if name.endswith(('.xlsx', '.xlsm')):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(content), data_only=True)
            ws = wb.active
            headers = [str(c.value or '').strip() for c in ws[1]]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if any(v not in (None, '') for v in row):
                    rows.append({headers[i] if i < len(headers) else f'col{i}': (v or '') for i, v in enumerate(row)})
        else:
            # CSV / TSV
            text = content.decode('utf-8-sig', errors='replace')
            sniffer = csv.Sniffer()
            try:
                dialect = sniffer.sniff(text[:4096])
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            for r in reader:
                rows.append({k.strip(): (v or '').strip() for k, v in r.items()})
    except Exception as e:
        raise HTTPException(400, f"解析失败: {str(e)}")
    
    # 字段名映射（兼容常见 HR 系统导出格式）
    field_map = {
        'user_id': ['user_id', 'id', '工号', '账号', 'username', 'login', 'employee_id', '员工编号'],
        'name': ['name', '姓名', '员工姓名', 'full_name', 'real_name'],
        'email': ['email', '邮箱', 'mail', 'e_mail'],
        'department': ['department', '部门', 'dept', 'org'],
        'role': ['role', '角色', '权限', 'position'],
        'manager_id': ['manager_id', '上级', '主管', 'manager', 'supervisor'],
        'avatar': ['avatar', '头像'],
    }
    detected = {}
    for target, candidates in field_map.items():
        for h in headers:
            if h in candidates or h.lower() in [c.lower() for c in candidates]:
                detected[target] = h
                break
    
    return {
        "filename": name,
        "total_rows": len(rows),
        "headers": headers,
        "field_mapping": detected,
        "preview": rows[:10],
    }


class ImportConfirmReq(BaseModel):
    rows: list[dict]
    field_mapping: dict


@router.post("/api/users/import/commit")
def import_commit(req: ImportConfirmReq, user_id: str = Query(...)):
    """根据字段映射提交导入。只有 IM 管理员 / 超管可调用。"""
    store = _get_store()
    if not store.has_im_admin(user_id):
        raise HTTPException(403, "需要 IM 管理员权限")
    
    fm = req.field_mapping
    if 'user_id' not in fm or 'name' not in fm:
        raise HTTPException(400, "至少需要 user_id 和 name 字段")
    
    created = []
    updated = []
    skipped = []
    errors = []
    
    for i, row in enumerate(req.rows):
        try:
            uid = str(row.get(fm['user_id'], '')).strip()
            if not uid:
                skipped.append({'row': i, 'reason': 'user_id 为空'})
                continue
            name = str(row.get(fm['name'], '')).strip()
            if not name:
                skipped.append({'row': i, 'reason': 'name 为空'})
                continue
            
            data = {'name': name}
            for f in ['email', 'department', 'role', 'manager_id', 'avatar']:
                if f in fm and fm[f]:
                    v = str(row.get(fm[f], '')).strip()
                    if v:
                        data[f] = v
            
            existing = store.get(uid)
            if existing:
                store.update(uid, **data)
                updated.append(uid)
            else:
                store.create(uid, **data)
                created.append(uid)
        except Exception as e:
            errors.append({'row': i, 'reason': str(e)})
    
    return {
        'created': len(created),
        'updated': len(updated),
        'skipped': len(skipped),
        'errors': len(errors),
        'details': {'created': created, 'updated': updated, 'skipped': skipped, 'errors': errors},
    }


# ---- HR 系统对接（Webhook 推送） ----

@router.post("/api/users/hr/sync")
def hr_sync(req: ImportConfirmReq, user_id: str = Query(...)):
    """从外部 HR 系统同步用户（同样走字段映射）。
    区别于 import：批量 upsert + 输出差异（新增/离职/更新）。
    """
    store = _get_store()
    if not store.has_im_admin(user_id):
        raise HTTPException(403, "需要 IM 管理员权限")
    
    fm = req.field_mapping
    if 'user_id' not in fm:
        raise HTTPException(400, "至少需要 user_id 字段")
    
    incoming_ids = set()
    synced = []
    for row in req.rows:
        uid = str(row.get(fm['user_id'], '')).strip()
        if not uid:
            continue
        incoming_ids.add(uid)
        data = {}
        if 'name' in fm and fm['name']:
            data['name'] = str(row.get(fm['name'], '')).strip()
        for f in ['email', 'department', 'role', 'manager_id']:
            if f in fm and fm[f]:
                v = str(row.get(fm[f], '')).strip()
                if v:
                    data[f] = v
        existing = store.get(uid)
        if existing:
            store.update(uid, **data)
        else:
            if not data.get('name'):
                data['name'] = uid
            store.create(uid, **data)
        synced.append(uid)
    
    # 检测离职用户（在系统但不在本次同步中）
    all_ids = set(u['id'] for u in store.list_users())
    # 演示账号不标记为离职
    PRESERVED = {'admin', 'im_admin', 'oa_admin', 'zhangsan', 'lisi', 'wangwu'}
    departed = [uid for uid in all_ids - incoming_ids if uid not in PRESERVED]
    for uid in departed:
        store.update(uid, is_active=False)
    
    return {
        'synced': len(synced),
        'departed_marked_inactive': len(departed),
        'departed_list': departed,
    }



@router.get("/api/auth/permissions")
def get_permissions(user_id: str = Query(...)):
    """获取当前用户的权限"""
    store = _get_store()
    u = store.get(user_id)
    if not u:
        raise HTTPException(404, "User not found")
    role = store.get_role(user_id)
    return {
        "user_id": user_id,
        "role": role,
        "permissions": {
            "im_admin": store.has_im_admin(user_id),
            "oa_admin": store.has_oa_admin(user_id),
            "is_super_admin": role == "admin",
        }
    }
