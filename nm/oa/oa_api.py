"""
OA API 路由 — 挂载到 FastAPI app
提供审批/待办/已办/流程跟踪等全部接口
"""

import os
import logging
from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel

from .workflow import WorkflowEngine
from .oa_store import OAStore
from .presets import register_all_presets

logger = logging.getLogger("nm.oa.api")

# ============================================================================
# 模型
# ============================================================================

class ApprovalStartRequest(BaseModel):
    def_id: str
    title: str = ""
    initiator: str
    initiator_name: str = ""
    form_data: dict = {}
    urgent: bool = False


class ApprovalActionRequest(BaseModel):
    action: str          # approve | reject | delegate
    comment: str = ""
    delegate_to: str | None = None


# ============================================================================
# 全局引擎实例（lazy init）
# ============================================================================

_engine: WorkflowEngine | None = None


def get_engine(workspace_dir: str | None = None) -> WorkflowEngine:
    global _engine
    if _engine is not None:
        return _engine

    if workspace_dir is None:
        workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    store_path = os.path.join(workspace_dir, ".nm", "oa_store.json")
    store = OAStore(store_path)
    engine = WorkflowEngine(store)

    # 注册预设模板（首次启动）
    try:
        register_all_presets(engine)
    except Exception as e:
        logger.warning(f"Preset registration skipped (may already exist): {e}")

    _engine = engine
    return engine


# ============================================================================
# 挂载路由
# ============================================================================

def mount_oa_routes(app: FastAPI, workspace_dir: str | None = None):
    """将 OA API 路由挂载到 FastAPI 应用"""

    engine = get_engine(workspace_dir)

    # ---- 数据看板 ----
    @app.get("/api/oa/stats")
    async def oa_stats(user_id: str = "default"):
        """首页 OA 统计数字"""
        stats = engine.stats(user_id)
        store_stats = engine.store.stats()
        return {
            **stats,
            "store": store_stats,
        }

    # ---- 智能预检（AI 自动核算假期余额 + 搭配建议）----
    @app.post("/api/oa/precheck")
    async def precheck(body: dict):
        """审批前智能预检
        body: {user_id, def_id, form_data: {请假类型, 天数, ...}}
        返回: {pass, reason, available, suggestions, recommended}
        """
        user_id = body.get("user_id", "")
        def_id = body.get("def_id", "")
        form_data = body.get("form_data", {})
        if not user_id or not def_id:
            raise HTTPException(400, "缺少 user_id 或 def_id")

        # 加载用户 HR 数据
        from nm.users.user_store import UserStore
        users_path = os.path.normpath(os.path.join(os.path.dirname(engine.store.path), "users.json"))
        user_store = UserStore(users_path)
        user = user_store.get(user_id)
        if not user:
            return {"pass": False, "reason": "用户不存在", "available": {}, "suggestions": []}

        def _f(name):
            return float(user.get(name, 0))

        available = {
            "annual_leave": _f("annual_leave"),
            "parental_leave": _f("parental_leave"),
            "personal_leave": _f("personal_leave"),
        }

        # 仅请假模板做余额校验
        if def_id not in ("leave_approval", "business_trip", "overtime_approval"):
            return {"pass": True, "reason": "", "available": available, "suggestions": [], "recommended": None}

        days = float(form_data.get("天数", 0))
        if days <= 0:
            return {"pass": True, "reason": "", "available": available, "suggestions": [], "recommended": None}

        # 年假 + 育儿假 混合搭配算法
        annual = available["annual_leave"]
        parental = available["parental_leave"]
        personal = available["personal_leave"]
        total = annual + parental + personal

        if total < days:
            return {
                "pass": False,
                "reason": f"余额不足，年假剩{annual}天+育儿假剩{parental}天+事假剩{personal}天，共{total}天，请{days}天还差{days - total}天",
                "available": available,
                "suggestions": [],
                "recommended": None,
            }

        # 生成所有可行组合（年假 x 育儿假 x 事假，x 表示用了多少天）
        suggestions = []
        for a_use in range(int(min(annual, days)) + 1):
            for p_use in range(int(min(parental, days - a_use)) + 1):
                remain = days - a_use - p_use
                if remain < 0:
                    continue
                pe_use = remain  # 事假补足
                if pe_use > personal:
                    continue
                label = "".join(filter(None, [
                    f"年假{a_use}天" if a_use else None,
                    f"+育儿假{p_use}天" if p_use else None,
                    f"+事假{pe_use}天" if pe_use else None,
                ]))
                suggestions.append({
                    "combo": label,
                    "annual": a_use,
                    "parental": p_use,
                    "personal": pe_use,
                    "remaining_after": {
                        "annual_leave": round(annual - a_use, 1),
                        "parental_leave": round(parental - p_use, 1),
                        "personal_leave": round(personal - pe_use, 1),
                    }
                })

        # 排序：优先年假多的（年假有年度截止风险，优先消）
        suggestions.sort(key=lambda s: (-s["annual"], s["combo"]))
        recommended = suggestions[0]["combo"] if suggestions else None

        return {
            "pass": True,
            "reason": "",
            "requested_days": days,
            "available": available,
            "suggestions": suggestions[:3],  # 最多返回 3 个方案
            "recommended": recommended,
        }

    # ---- 流程模板 ----
    @app.get("/api/oa/defs")
    async def list_defs(category: str | None = None):
        """获取可用流程模板"""
        defs = engine.list_defs(category)
        return {
            "total": len(defs),
            "items": [
                {
                    "id": d.id,
                    "name": d.name,
                    "category": d.category,
                    "icon": d.icon,
                    "description": d.description,
                    "form_schema": d.form_schema,
                    "step_count": len(d.steps),
                    "steps": [
                        {"id": s.id, "name": s.name, "assignee_type": s.assignee_type}
                        for s in d.steps
                    ],
                }
                for d in defs
            ],
        }

    @app.get("/api/oa/defs/{def_id}")
    async def get_def(def_id: str):
        """获取单个流程模板详情"""
        d = engine.get_def(def_id)
        if not d:
            raise HTTPException(404, "模板不存在")
        return {
            "id": d.id,
            "name": d.name,
            "category": d.category,
            "icon": d.icon,
            "description": d.description,
            "form_schema": d.form_schema,
            "steps": [
                {"id": s.id, "name": s.name, "assignee_type": s.assignee_type,
                 "can_reject": s.can_reject, "can_delegate": s.can_delegate,
                 "timeout_hours": s.timeout_hours}
                for s in d.steps
            ],
        }

    # ---- 发起审批 ----
    @app.post("/api/oa/start")
    async def start_approval(req: ApprovalStartRequest):
        """发起一个新审批"""
        try:
            inst = engine.start(
                def_id=req.def_id,
                initiator=req.initiator,
                initiator_name=req.initiator_name,
                title=req.title,
                form_data=req.form_data,
                urgent=req.urgent,
            )
            return {"status": "ok", "instance_id": inst.id, "statusCode": inst.status}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/oa/submit/{inst_id}")
    async def submit_approval(inst_id: str, user_id: str = "default"):
        """发起人提交草稿"""
        try:
            inst = engine.submit(inst_id, user_id)
            return {"status": "ok", "instance_id": inst.id, "statusCode": inst.status}
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---- 审批操作 ----
    @app.post("/api/oa/approve/{inst_id}")
    async def approve(inst_id: str, req: ApprovalActionRequest, user_id: str = "default"):
        """同意/退回/转办"""
        try:
            action = req.action
            if action == "approve":
                inst = engine.approve(inst_id, user_id, req.comment or "同意")
            elif action == "reject":
                inst = engine.reject(inst_id, user_id, req.comment or "退回")
            elif action == "delegate":
                if not req.delegate_to:
                    raise HTTPException(400, "转办需要指定 delegate_to")
                inst = engine.delegate(inst_id, user_id, req.delegate_to, req.comment)
            else:
                raise HTTPException(400, f"无效操作: {action}")
            return {"status": "ok", "instance_id": inst.id, "statusCode": inst.status}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/oa/withdraw/{inst_id}")
    async def withdraw(inst_id: str, user_id: str = "default"):
        """发起人撤回"""
        try:
            inst = engine.withdraw(inst_id, user_id)
            return {"status": "ok", "instance_id": inst.id, "statusCode": inst.status}
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---- 查询 ----
    @app.get("/api/oa/pending")
    async def get_pending(user_id: str = "default"):
        """待我审批"""
        items = engine.get_pending(user_id)
        return {"total": len(items), "items": items}

    @app.get("/api/oa/started")
    async def get_my_started(user_id: str = "default"):
        """我发起的"""
        items = engine.get_my_started(user_id)
        return {"total": len(items), "items": items}

    @app.get("/api/oa/history")
    async def get_history(user_id: str = "default"):
        """我处理过的（已办）"""
        items = engine.get_history(user_id)
        return {"total": len(items), "items": items}

    @app.get("/api/oa/drafts")
    async def get_drafts(user_id: str = "default"):
        """我的草稿"""
        items = engine.get_my_drafts(user_id)
        return {"total": len(items), "items": items}

    @app.get("/api/oa/track/{inst_id}")
    async def get_track(inst_id: str):
        """流程跟踪"""
        track = engine.get_track(inst_id)
        if not track:
            raise HTTPException(404, "审批实例不存在")
        return track

    # ---- 兼容旧 API（渐进式迁移） ----
    @app.get("/api/approvals")
    async def legacy_approvals(user_id: str = "default"):
        """兼容旧 /api/approvals 接口"""
        pending = engine.get_pending(user_id)
        return {"total": len(pending), "items": pending}


# ============================================================================
# OA 流程模板管理（管理员功能）
# ============================================================================

def mount_oa_admin_routes(app):
    """挂载 OA 管理员专用路由（流程模板 CRUD）"""
    
    from nm.users.user_store import UserStore
    import csv, io
    from openpyxl import load_workbook
    
    # 全局用户存储（懒加载）
    _user_store = None
    def get_users():
        nonlocal _user_store
        if _user_store is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".nm", "users.json")
            _user_store = UserStore(path)
        return _user_store
    
    def get_engine():
        return _engine
    
    @app.post("/api/oa/defs/import/preview")
    async def import_def_preview(file: "UploadFile"):
        """解析流程模板文件（CSV/Excel）预览。期望列：id, name, category, steps(分号分隔)、form_fields(JSON)。"""
        content = await file.read()
        name = file.filename or ""
        rows = []
        headers = []
        try:
            if name.endswith(('.xlsx', '.xlsm')):
                wb = load_workbook(io.BytesIO(content), data_only=True)
                ws = wb.active
                headers = [str(c.value or '').strip() for c in ws[1]]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if any(v not in (None, '') for v in row):
                        rows.append({headers[i] if i < len(headers) else f'col{i}': (v or '') for i, v in enumerate(row)})
            else:
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
        
        # 字段映射
        field_map = {
            'id': ['id', '模板ID', '流程ID', 'code'],
            'name': ['name', '模板名称', '流程名称', 'title'],
            'category': ['category', '分类', '类别', 'type'],
            'icon': ['icon', '图标'],
            'description': ['description', '说明', '描述'],
            'steps': ['steps', '审批节点', '审批人', 'approvers', '审批流程'],
            'form_fields': ['form_fields', '表单字段', 'fields', 'form'],
        }
        detected = {}
        for target, candidates in field_map.items():
            for h in headers:
                if h in candidates or h.lower() in [c.lower() for c in candidates]:
                    detected[target] = h
                    break
        
        # 预览时把 steps/form_fields 解析出来方便管理员确认
        preview = []
        for r in rows[:10]:
            item = dict(r)
            if 'steps' in detected and detected['steps'] in r and r[detected['steps']]:
                raw = r[detected['steps']]
                item['_parsed_steps'] = [s.strip() for s in str(raw).replace('；', ';').split(';') if s.strip()]
            if 'form_fields' in detected and detected['form_fields'] in r and r[detected['form_fields']]:
                item['_parsed_form_count'] = len([f for f in str(r[detected['form_fields']]).split(',') if f.strip()])
            preview.append(item)
        
        return {"filename": name, "total_rows": len(rows), "headers": headers, "field_mapping": detected, "preview": preview}
    
    
    class DefImportConfirm(BaseModel):
        rows: list[dict]
        field_mapping: dict
        default_assignee: str = "manager"  # 节点默认审批人
    
    
    @app.post("/api/oa/defs/import/commit")
    async def import_def_commit(req: DefImportConfirm, user_id: str = "default"):
        """提交流程模板导入。需要 OA 管理员权限。"""
        users = get_users()
        if not users.has_oa_admin(user_id):
            raise HTTPException(403, "需要 OA 管理员权限")
        engine = get_engine()
        fm = req.field_mapping
        if 'id' not in fm or 'name' not in fm:
            raise HTTPException(400, "至少需要 id 和 name 字段")
        
        created, skipped, errors = [], [], []
        for i, row in enumerate(req.rows):
            try:
                did = str(row.get(fm['id'], '')).strip()
                if not did:
                    skipped.append({'row': i, 'reason': 'id 为空'}); continue
                name = str(row.get(fm['name'], '')).strip()
                if not name:
                    skipped.append({'row': i, 'reason': 'name 为空'}); continue
                if engine.get_def(did):
                    skipped.append({'row': i, 'reason': f'模板 {did} 已存在'}); continue
                
                # 解析 steps
                steps = []
                if 'steps' in fm and fm['steps']:
                    raw = str(row.get(fm['steps'], ''))
                    step_names = [s.strip() for s in raw.replace('；', ';').split(';') if s.strip()]
                    for j, sname in enumerate(step_names):
                        steps.append({
                            "id": f"step_{j+1}",
                            "name": sname,
                            "assignee_type": req.default_assignee if req.default_assignee in ("initiator_choice", "fixed") else "initiator_choice",
                        })
                if not steps:
                    steps = [{"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice"}]
                
                # 解析 form_fields
                form_schema = {"type": "object", "properties": {}, "required": []}
                if 'form_fields' in fm and fm['form_fields']:
                    raw = str(row.get(fm['form_fields'], ''))
                    fields = [f.strip() for f in raw.replace('；', ',').split(',') if f.strip()]
                    for f in fields:
                        form_schema["properties"][f] = {"type": "string"}
                        form_schema["required"].append(f)
                
                engine.register_def(
                    id=did, name=name,
                    category=str(row.get(fm.get('category', ''), 'admin')) if 'category' in fm else 'admin',
                    icon=str(row.get(fm.get('icon', ''), '📋')) if 'icon' in fm else '📋',
                    description=str(row.get(fm.get('description', ''), '')) if 'description' in fm else '',
                    form_schema=form_schema, steps=steps,
                )
                created.append(did)
            except Exception as e:
                errors.append({'row': i, 'reason': str(e)})
        
        return {"created": len(created), "skipped": len(skipped), "errors": len(errors), "details": {"created": created, "skipped": skipped, "errors": errors}}
    
    
    @app.delete("/api/oa/defs/{def_id}")
    async def delete_def(def_id: str, user_id: str = "default"):
        """删除流程模板。需要 OA 管理员权限。"""
        users = get_users()
        if not users.has_oa_admin(user_id):
            raise HTTPException(403, "需要 OA 管理员权限")
        engine = get_engine()
        ok = engine.delete_def(def_id)
        if not ok:
            raise HTTPException(404, "模板不存在")
        return {"ok": True, "deleted": def_id}
    
    
    @app.get("/api/oa/defs-export")
    async def export_defs():
        """导出所有模板为 CSV 格式。"""
        import io
        engine = get_engine()
        defs = engine.list_defs()
        output = io.StringIO()
        if not defs:
            output.write("id,name,category,description,steps\n")
        else:
            output.write("id,name,category,description,steps\n")
            for d in defs:
                # d 可能是 dataclass 或 dict
                if hasattr(d, 'steps'):
                    steps = ';'.join((s.get('name', '') if isinstance(s, dict) else getattr(s, 'name', '')) for s in d.steps)
                    desc = (getattr(d, 'description', '') or '').replace('"', '""')
                    output.write(f"{d.id},{d.name},{getattr(d, 'category', '')},\"{desc}\",{steps}\n")
                else:
                    steps = ';'.join(s.get('name', '') for s in d.get('steps', []))
                    desc = (d.get('description') or '').replace('"', '""')
                    output.write(f"{d['id']},{d['name']},{d.get('category','')},\"{desc}\",{steps}\n")
        from fastapi.responses import Response
        return Response(content=output.getvalue().encode('utf-8-sig'), 
                       media_type='text/csv',
                       headers={'Content-Disposition': 'attachment; filename=oa_defs.csv'})


# 需要导入
from fastapi import UploadFile  # noqa: E402
