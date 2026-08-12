"""
OA 工作流引擎核心

=== 数据模型 ===

WorkflowDef: 流程模板定义
  - id, name, category, form_schema, steps
  - 一条模板 = 一张表单结构 + 一串审批节点

WorkflowStep: 审批节点
  - name, assignee_type (fixed/role/initiator_choice/prev_choice)
  - assignee, can_reject, can_delegate, timeout_hours

WorkflowInstance: 流程实例（每次发起一个）
  - 绑定哪个 def → 填了什么表单 → 当前卡在哪一步 → 历史记录

StepRecord: 每一步审批的处理记录
  - 谁处理的、什么动作（approve/reject/delegate/withdraw）、什么意见、什么时间

=== 状态机 ===

  draft → pending → (每步: pending → approved → 下一步)
                                    → rejected → 退回发起人
                                    → delegated → 转给新审批人（同一步）
                                    → timeout → 提醒
  → completed → archived

=== 设计原则 ===

- 零外部数据库：所有数据存单个 JSON 文件
- 对齐致远习惯：发起/待办/已办/我的发起/流程跟踪 五大入口
- 低耦合：引擎不依赖 harness 其他模块，可独立使用
- 易维护：一个 IT 在配置里加一条 def，全员即可使用
"""

import json
import os
import threading
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

@contextmanager
def _NullCtx():
    yield

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class WorkflowStep:
    """审批节点定义"""
    id: str                                          # "step_1"
    name: str                                        # "直属领导审批"
    assignee_type: str = "initiator_choice"          # fixed | role | initiator_choice | prev_choice
    assignee: str | None = None                      # 指定人/角色 user_id
    can_reject: bool = True                          # 是否可以退回
    can_delegate: bool = True                        # 是否可以转办
    timeout_hours: int = 0                           # 超时自动提醒（0=不超时）
    order: int = 0                                   # 排序序号


@dataclass
class WorkflowDef:
    """流程模板定义"""
    id: str                                          # "leave_approval"
    name: str                                        # "请假审批"
    category: str = "hr"                             # hr | finance | admin | contract | general
    description: str = ""
    icon: str = "📋"
    form_schema: dict = field(default_factory=dict)  # JSON Schema 定义表单字段
    steps: list[WorkflowStep] = field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class StepRecord:
    """审批节点处理记录"""
    step_id: str
    step_name: str
    assignee: str                                    # 审批人
    action: str                                      # approve | reject | delegate | withdraw | timeout
    comment: str = ""
    delegate_to: str | None = None                   # 转办目标
    timestamp: str = ""


@dataclass
class WorkflowInstance:
    """流程实例（一次审批的"活档案"）"""
    id: str                                          # 流水号，如 "OA-20260811-0001"
    def_id: str                                      # 引用 WorkflowDef.id
    title: str                                       # 显示标题，如 "王芳的年假申请"
    initiator: str                                   # 发起人 user_id
    initiator_name: str = ""
    form_data: dict = field(default_factory=dict)    # 表单填写内容
    status: str = "draft"                            # draft | pending | approved | rejected | withdrawn | completed | archived
    current_step: int = 0                            # 当前在第几步（从 0 开始）
    history: list[StepRecord] = field(default_factory=list)
    urgent: bool = False
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class StateMachine:
    """OA 流程状态机

    流转规则：
      draft → pending (发起人提交)
      pending: current_step 指向某一步
        → approved (当前审批人同意，current_step += 1)
        → rejected (退回发起人，status → rejected)
        → delegated (转办，assignee 变，step 不变)
      pending 且 current_step >= len(steps) → completed
      completed → archived (可选)
    """

    VALID_TRANSITIONS = {
        "draft":      ["pending", "withdrawn"],
        "pending":    ["approved", "rejected", "delegated", "withdrawn", "completed", "timeout"],
        "approved":   [],                # 中间状态，立即推进到下一步
        "rejected":   ["draft"],         # 退回后发起人可重新编辑
        "delegated":  [],                # 中间状态，underlying status 不变
        "completed":  ["archived", "rejected"],  # 完成后也可以撤回
        "archived":   [],
        "withdrawn":  [],
        "timeout":    [],                # 超时提醒，不改变状态
    }

    @staticmethod
    def can_transition(current_status: str, target_action: str) -> bool:
        return target_action in StateMachine.VALID_TRANSITIONS.get(current_status, [])

    @staticmethod
    def apply(instance: WorkflowInstance, action: str, **kwargs) -> WorkflowInstance:
        """对实例执行一个状态转换，返回修改后的实例"""
        if not StateMachine.can_transition(instance.status, action):
            raise ValueError(f"无效的状态转换: {instance.status} → {action}")

        now = datetime.now(timezone.utc).isoformat()

        if action == "pending":
            # draft → pending: 发起人提交
            if instance.status == "draft":
                instance.status = "pending"
                instance.current_step = 0
                instance.created_at = instance.created_at or now

        elif action == "approved":
            # 当前步骤审批通过，推进到下一步
            step = kwargs.get("step_record")
            if step:
                instance.history.append(step)
            instance.current_step += 1
            instance.updated_at = now

            # 检查是否所有步骤都通过了
            if instance.current_step >= len(kwargs.get("steps", [])):
                instance.status = "completed"
                instance.completed_at = now

        elif action == "rejected":
            # 退回发起人
            step = kwargs.get("step_record")
            if step:
                instance.history.append(step)
            instance.status = "rejected"
            instance.updated_at = now

        elif action == "delegated":
            # 转办：同一步骤换审批人，不改变 current_step
            step = kwargs.get("step_record")
            if step:
                instance.history.append(step)
            instance.updated_at = now

        elif action == "withdrawn":
            # 发起人撤回
            instance.status = "withdrawn"
            instance.updated_at = now

        elif action == "archived":
            instance.status = "archived"
            instance.updated_at = now

        elif action == "timeout":
            # 超时不改变状态，只记录
            instance.updated_at = now

        return instance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_dt(iso_str: str) -> str:
    """将 ISO 时间戳格式化为可读的北京时间"""
    try:
        dt = datetime.fromisoformat(iso_str)
        dt = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return iso_str[:16] if iso_str else ""


# ============================================================================
# 工作流引擎
# ============================================================================

class WorkflowEngine:
    """OA 工作流引擎

    使用方式:
        store = OAStore(".harness/oa_store.json")
        engine = WorkflowEngine(store)

        # 发起审批
        inst = engine.start(def_id="leave_approval", initiator="wang",
                           initiator_name="王主任", form_data={"days": 3, "reason": "探亲"})

        # 审批
        engine.approve(inst.id, user_id="zhang", comment="同意")
        engine.reject(inst.id, user_id="zhang", comment="材料不全")

        # 查询
        pending = engine.get_pending("zhang")     # 待我审批的
        my_started = engine.get_my_started("wang") # 我发起的
        history = engine.get_history("wang")      # 我处理过的
    """

    def __init__(self, store: "OAStore"):
        self.store = store
        self._lock = threading.RLock()

    # ---- 流程模板管理 ----

    def register_def(self, **kwargs) -> WorkflowDef:
        """注册一个流程模板"""
        kwargs.setdefault("id", kwargs.get("name"))
        kwargs.setdefault("created_at", _now())
        kwargs.setdefault("updated_at", _now())

        # 解析 steps
        steps_raw = kwargs.pop("steps", [])
        steps = []
        for i, s in enumerate(steps_raw):
            steps.append(WorkflowStep(
                id=s.get("id", f"step_{i+1}"),
                name=s.get("name", f"步骤{i+1}"),
                assignee_type=s.get("assignee_type", "initiator_choice"),
                assignee=s.get("assignee"),
                can_reject=s.get("can_reject", True),
                can_delegate=s.get("can_delegate", True),
                timeout_hours=s.get("timeout_hours", 0),
                order=s.get("order", i + 1),
            ))

        def_obj = WorkflowDef(steps=steps, **kwargs)
        self.store.save_def(def_obj)
        return def_obj

    def get_def(self, def_id: str) -> WorkflowDef | None:
        return self.store.get_def(def_id)

    def list_defs(self, category: str | None = None) -> list[WorkflowDef]:
        defs = self.store.list_defs()
        if category:
            defs = [d for d in defs if d.category == category]
        return sorted(defs, key=lambda d: d.updated_at, reverse=True)

    def delete_def(self, def_id: str) -> bool:
        """删除流程模板。仅当没有任何进行中的实例时才允许删除（防止悬挂引用）。"""
        with self.store.lock if hasattr(self.store, 'lock') else _NullCtx():
            insts = self.store.list_instances()
            active = [i for i in insts if i.get('def_id') == def_id and i.get('status') in ('pending', 'draft')]
            if active:
                return False
            return self.store.delete_def(def_id)

    # ---- 发起审批 ----

    def start(
        self,
        def_id: str,
        initiator: str,
        initiator_name: str = "",
        title: str = "",
        form_data: dict | None = None,
        urgent: bool = False,
    ) -> WorkflowInstance:
        """发起一个审批流程"""
        with self._lock:
            wdef = self.get_def(def_id)
            if not wdef:
                raise ValueError(f"流程模板不存在: {def_id}")
            if not wdef.enabled:
                raise ValueError(f"流程模板已停用: {def_id}")
            if not wdef.steps:
                raise ValueError(f"流程模板没有配置审批节点: {def_id}")

            # 生成流水号
            seq = self.store.next_seq()
            now_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            inst_id = f"OA-{now_str}-{seq:04d}"

            inst = WorkflowInstance(
                id=inst_id,
                def_id=def_id,
                title=title or f"{initiator_name}的{wdef.name}",
                initiator=initiator,
                initiator_name=initiator_name,
                form_data=form_data or {},
                status="draft",
                current_step=0,
                urgent=urgent,
                created_at=_now(),
                updated_at=_now(),
            )

            # 自动路由：若第一步是 initiator_choice 且未指定审批人，从发起人的 manager_id 自动填入
            self._auto_route_first_step(inst, wdef)

            self.store.save_instance(inst)
            logger.info(f"审批发起: {inst_id} ({inst.title}) by {initiator}")
            return inst

    def _auto_route_first_step(self, inst, wdef):
        """自动路由第一步审批人：若 initiator_choice 且未指定，从发起人 manager 自动填入"""
        if not wdef.steps or inst.current_step >= len(wdef.steps):
            return
        first_step = wdef.steps[0]
        if first_step.assignee_type != "initiator_choice":
            return
        key = f"_assignee_{first_step.id}"
        if inst.form_data.get(key):
            return  # 已显式指定
        # 懒加载 UserStore 避免循环依赖
        try:
            from harness.users.user_store import UserStore
            import os
            user_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".harness", "users.json")
            store = UserStore(path=user_path)
            store.seed_demo()  # 确保有种子用户
            initiator = store.get(inst.initiator)
            if initiator and initiator.get("manager_id"):
                inst.form_data[key] = initiator["manager_id"]
                logger.info(f"自动路由 {inst.id} 第一步审批人 → {initiator['manager_id']}（{initiator.get('name','?')} 的直属上级）")
            else:
                logger.info(f"自动路由跳过：{inst.initiator} 无 manager（可选 fallback 到 admin）")
        except Exception as e:
            logger.warning(f"自动路由失败（不影响发起）: {e}")

    def submit(self, inst_id: str, user_id: str) -> WorkflowInstance:
        """发起人提交（draft → pending）"""
        inst = self._get_and_check(inst_id, user_id, require_initiator=True)
        wdef = self.get_def(inst.def_id)

        # 提交前再试一次自动路由（弥补 start 后才设置 manager 的场景）
        self._auto_route_first_step(inst, wdef)

        # 若第一步审批人仍待指定，报明确错
        if wdef and wdef.steps and wdef.steps[0].assignee_type == "initiator_choice":
            key = f"_assignee_{wdef.steps[0].id}"
            if not inst.form_data.get(key):
                raise ValueError(
                    f"第一步审批人未指定。发起人 {inst.initiator} 未配置 manager_id，"
                    f"且 start() 时未传 _assignee_{wdef.steps[0].id}。"
                    f"请在 form_data 中传 {key}: '<user_id>'"
                )

        StateMachine.apply(inst, "pending", steps=wdef.steps if wdef else [])
        inst.updated_at = _now()
        self.store.save_instance(inst)
        return inst

    # ---- 审批操作 ----

    def approve(self, inst_id: str, user_id: str, comment: str = "同意") -> WorkflowInstance:
        """同意当前步骤"""
        inst, step = self._get_current_step(inst_id, user_id)
        wdef = self.get_def(inst.def_id)

        record = StepRecord(
            step_id=step.id,
            step_name=step.name,
            assignee=user_id,
            action="approve",
            comment=comment,
            timestamp=_now(),
        )

        StateMachine.apply(inst, "approved", step_record=record, steps=wdef.steps if wdef else [])
        self.store.save_instance(inst)
        logger.info(f"审批通过: {inst_id} step={step.name} by {user_id}")

        # 审批完成（全流程结束）→ 扣减假期余额
        if inst.status == "completed":
            try:
                from harness.users.user_store import UserStore
                import os
                user_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".harness", "users.json")
                store = UserStore(path=user_path)
                leave_type = inst.form_data.get("请假类型", "")
                days = float(inst.form_data.get("天数", 0))
                # 支持混合类型（如 "年假+育儿假混合" 存在备注中）
                if leave_type and days > 0:
                    ok, msg = store.deduct_leave(inst.initiator, leave_type, days)
                    logger.info(f"扣减假期: {inst.initiator} {leave_type}{days}天 → {msg}")
            except Exception as e:
                logger.warning(f"扣减假期失败: {e}")

        # 走到下一步后，若下一步是 initiator_choice 且未指定，自动以“上一步审批人”的 manager 填入
        if inst.status == "pending" and wdef and inst.current_step < len(wdef.steps):
            next_step = wdef.steps[inst.current_step]
            if next_step.assignee_type == "initiator_choice":
                key = f"_assignee_{next_step.id}"
                if not inst.form_data.get(key):
                    try:
                        from harness.users.user_store import UserStore
                        import os
                        user_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".harness", "users.json")
                        store = UserStore(path=user_path)
                        store.seed_demo()
                        approver = store.get(user_id)
                        if approver and approver.get("manager_id"):
                            inst.form_data[key] = approver["manager_id"]
                            self.store.save_instance(inst)
                            logger.info(f"自动路由 {inst.id} 下一步 {next_step.name} → {approver['manager_id']}")
                    except Exception as e:
                        logger.warning(f"下一步自动路由失败: {e}")

        return inst

    def reject(self, inst_id: str, user_id: str, comment: str = "退回") -> WorkflowInstance:
        """退回发起人"""
        inst, step = self._get_current_step(inst_id, user_id)
        if not step.can_reject:
            raise ValueError(f"当前步骤不允许退回")

        record = StepRecord(
            step_id=step.id,
            step_name=step.name,
            assignee=user_id,
            action="reject",
            comment=comment,
            timestamp=_now(),
        )

        StateMachine.apply(inst, "rejected", step_record=record)
        self.store.save_instance(inst)
        logger.info(f"审批退回: {inst_id} step={step.name} by {user_id}")
        return inst

    def delegate(self, inst_id: str, user_id: str, delegate_to: str, comment: str = "转办") -> WorkflowInstance:
        """转办给其他人"""
        inst, step = self._get_current_step(inst_id, user_id)
        if not step.can_delegate:
            raise ValueError(f"当前步骤不允许转办")

        record = StepRecord(
            step_id=step.id,
            step_name=step.name,
            assignee=user_id,
            action="delegate",
            comment=comment,
            delegate_to=delegate_to,
            timestamp=_now(),
        )

        # 转办：修改当前步骤的 assignee
        step._delegated_to = delegate_to

        StateMachine.apply(inst, "delegated", step_record=record)
        self.store.save_instance(inst)
        logger.info(f"审批转办: {inst_id} {user_id} → {delegate_to}")
        return inst

    def withdraw(self, inst_id: str, user_id: str) -> WorkflowInstance:
        """发起人撤回"""
        inst = self._get_and_check(inst_id, user_id, require_initiator=True)
        if inst.status not in ("draft", "pending"):
            raise ValueError(f"当前状态不允许撤回: {inst.status}")

        StateMachine.apply(inst, "withdrawn")
        self.store.save_instance(inst)
        logger.info(f"审批撤回: {inst_id} by {user_id}")
        return inst

    # ---- 查询 ----

    def get_pending(self, user_id: str) -> list[dict]:
        """待我审批的列表（对齐致远"待办"）"""
        instances = self.store.list_instances(status="pending")
        result = []
        for inst in instances:
            wdef = self.get_def(inst.def_id)
            if not wdef or inst.current_step >= len(wdef.steps):
                continue
            current_step = wdef.steps[inst.current_step]
            if self._is_assignee(current_step, user_id, inst):
                result.append(self._format_instance_card(inst, wdef, current_step))
        return sorted(result, key=lambda x: (not x["urgent"], x["updated_at"]), reverse=False)

    def get_my_started(self, user_id: str) -> list[dict]:
        """我发起的列表（对齐致远"我的发起"）"""
        instances = self.store.list_instances(initiator=user_id)
        result = []
        for inst in instances:
            wdef = self.get_def(inst.def_id)
            result.append(self._format_instance_card(inst, wdef))
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)

    def get_history(self, user_id: str) -> list[dict]:
        """我处理过的审批（对齐致远"已办"）"""
        all_instances = self.store.list_instances()
        result = []
        for inst in all_instances:
            for record in inst.history:
                if record.assignee == user_id:
                    wdef = self.get_def(inst.def_id)
                    result.append(self._format_instance_card(inst, wdef))
                    break
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)

    def get_my_drafts(self, user_id: str) -> list[dict]:
        """我的草稿"""
        instances = self.store.list_instances(status="draft", initiator=user_id)
        result = []
        for inst in instances:
            wdef = self.get_def(inst.def_id)
            result.append(self._format_instance_card(inst, wdef))
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)

    def get_track(self, inst_id: str) -> dict | None:
        """流程跟踪（对齐致远的"流程跟踪图"）"""
        inst = self.store.get_instance(inst_id)
        if not inst:
            return None
        wdef = self.get_def(inst.def_id)
        if not wdef:
            return None

        # 构建步骤状态列表
        step_statuses = []
        for i, step in enumerate(wdef.steps):
            s = {
                "order": i + 1,
                "step_id": step.id,
                "name": step.name,
                "status": "waiting",        # waiting | current | done | rejected
                "handler": None,
                "handled_at": None,
                "comment": None,
                "action": None,
            }

            if i < inst.current_step:
                # 已经过的步骤
                record = self._find_record(inst, step.id)
                s["status"] = "done"
                s["handler"] = record.assignee if record else None
                s["handled_at"] = _fmt_dt(record.timestamp) if record else None
                s["action"] = record.action if record else "approved"
                s["comment"] = record.comment if record else None
            elif i == inst.current_step and inst.status == "pending":
                s["status"] = "current"
                s["handler"] = self._resolve_current_assignee(inst, wdef, step)
            elif i == inst.current_step and inst.status == "rejected":
                # 当前步被退回
                record = self._find_record(inst, step.id)
                s["status"] = "rejected"
                s["handler"] = record.assignee if record else None
                s["handled_at"] = _fmt_dt(record.timestamp) if record else None
                s["action"] = "rejected"
                s["comment"] = record.comment if record else None

            step_statuses.append(s)

        return {
            "instance_id": inst.id,
            "title": inst.title,
            "def_name": wdef.name,
            "status": inst.status,
            "initiator": inst.initiator_name or inst.initiator,
            "form_data": inst.form_data,
            "steps": step_statuses,
            "created_at": _fmt_dt(inst.created_at),
            "completed_at": _fmt_dt(inst.completed_at) if inst.completed_at else None,
        }

    def get_instance(self, inst_id: str) -> WorkflowInstance | None:
        return self.store.get_instance(inst_id)

    # ---- 统计 ----

    def stats(self, user_id: str) -> dict:
        """个人统计（首页小红圈数字用）"""
        return {
            "pending_approvals": len(self.get_pending(user_id)),
            "my_drafts": len(self.get_my_drafts(user_id)),
            "my_started_today": sum(
                1 for i in self.get_my_started(user_id)
                if i["created_at"] and _now()[:10] in i["created_at"]
            ),
        }

    # ---- 内部辅助 ----

    def _get_and_check(self, inst_id: str, user_id: str, require_initiator: bool = False) -> WorkflowInstance:
        inst = self.store.get_instance(inst_id)
        if not inst:
            raise ValueError(f"审批实例不存在: {inst_id}")
        if require_initiator and inst.initiator != user_id:
            raise ValueError(f"只有发起人可以操作")
        return inst

    def _get_current_step(self, inst_id: str, user_id: str) -> tuple[WorkflowInstance, WorkflowStep]:
        inst = self.store.get_instance(inst_id)
        if not inst:
            raise ValueError(f"审批实例不存在: {inst_id}")
        if inst.status != "pending":
            raise ValueError(f"审批不处于待审批状态: {inst.status}")

        wdef = self.get_def(inst.def_id)
        if not wdef or inst.current_step >= len(wdef.steps):
            raise ValueError(f"没有待处理的审批步骤")

        step = wdef.steps[inst.current_step]
        if not self._is_assignee(step, user_id, inst):
            # 检查是否是转办后的审批人
            last_record = self._find_record(inst, step.id)
            if last_record and last_record.action == "delegate" and last_record.delegate_to == user_id:
                # 将转办目标记录到 step 上
                step._delegated_to = user_id
            else:
                raise ValueError(f"你不是当前步骤的审批人")

        return inst, step

    def _is_assignee(self, step: WorkflowStep, user_id: str, inst: WorkflowInstance) -> bool:
        """判断一个用户是否是某步骤的审批人"""
        # 转办后的目标
        if getattr(step, "_delegated_to", None) == user_id:
            return True

        if step.assignee_type == "fixed":
            return step.assignee == user_id
        elif step.assignee_type == "role":
            # 目前简化为直接匹配，后续可接入角色系统
            return step.assignee == user_id
        elif step.assignee_type == "initiator_choice":
            # 发起人选的人，存在 form_data 中
            choice = inst.form_data.get(f"_assignee_{step.id}")
            return choice == user_id
        elif step.assignee_type == "prev_choice":
            # 上一步审批人选的人
            if inst.current_step > 0:
                prev_choice = inst.form_data.get(f"_assignee_{wdef.steps[inst.current_step-1].id}")
                return prev_choice == user_id
        return False

    def _resolve_current_assignee(self, inst: WorkflowInstance, wdef: WorkflowDef, step: WorkflowStep | None = None) -> str | None:
        """获取当前步骤的审批人名称"""
        if inst.current_step >= len(wdef.steps):
            return None
        if step is None:
            step = wdef.steps[inst.current_step]

        # 检查是否有转办
        last_record = self._find_record(inst, step.id)
        if last_record and last_record.action == "delegate" and last_record.delegate_to:
            return last_record.delegate_to

        if step.assignee_type in ("fixed", "role"):
            return step.assignee
        elif step.assignee_type == "initiator_choice":
            return inst.form_data.get(f"_assignee_{step.id}", "待指定")
        return "待指定"

    def _find_record(self, inst: WorkflowInstance, step_id: str) -> StepRecord | None:
        """在 history 中查找某一步的最新记录"""
        for record in reversed(inst.history):
            if record.step_id == step_id:
                return record
        return None

    def _format_instance_card(self, inst: WorkflowInstance, wdef: WorkflowDef | None, current_step: WorkflowStep | None = None) -> dict:
        """格式化实例为前端卡片数据"""
        status_label = {
            "draft": "草稿",
            "pending": "审批中",
            "approved": "已通过",
            "rejected": "已退回",
            "withdrawn": "已撤回",
            "completed": "已完成",
            "archived": "已归档",
        }

        card = {
            "id": inst.id,
            "def_id": inst.def_id,
            "title": inst.title,
            "category": wdef.category if wdef else "general",
            "category_name": wdef.name if wdef else "",
            "icon": wdef.icon if wdef else "📋",
            "initiator": inst.initiator_name or inst.initiator,
            "status": inst.status,
            "status_label": status_label.get(inst.status, inst.status),
            "urgent": inst.urgent,
            "current_step_name": current_step.name if current_step else None,
            "current_assignee": self._resolve_current_assignee(inst, wdef, current_step) if wdef else None,
            "total_steps": len(wdef.steps) if wdef else 0,
            "current_step": inst.current_step,
            "form_data": inst.form_data,
            "created_at": _fmt_dt(inst.created_at),
            "updated_at": _fmt_dt(inst.updated_at),
        }

        # 摘要信息（根据 category 提取关键字段）
        if inst.form_data:
            card["summary"] = self._extract_summary(inst, wdef)

        return card

    def _extract_summary(self, inst: WorkflowInstance, wdef: WorkflowDef | None) -> str:
        """从表单数据提取一句话摘要"""
        fd = inst.form_data
        if inst.def_id == "leave_approval":
            days = fd.get("days", fd.get("请假天数", ""))
            reason = fd.get("reason", fd.get("请假事由", ""))
            return f"请假 {days} 天 · {reason}" if days else reason or ""
        elif inst.def_id == "expense_approval":
            amount = fd.get("amount", fd.get("报销金额", ""))
            desc = fd.get("description", fd.get("报销事由", ""))
            return f"报销 ¥{amount} · {desc}" if amount else desc or ""
        elif inst.def_id == "contract_approval":
            amount = fd.get("amount", fd.get("合同金额", ""))
            party = fd.get("party", fd.get("对方单位", ""))
            return f"合同 ¥{amount} · {party}" if amount and party else ""
        elif inst.def_id == "seal_approval":
            purpose = fd.get("purpose", fd.get("用印事由", ""))
            return purpose or ""
        return ""
