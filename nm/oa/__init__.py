"""
OA 工作流引擎
表单 → 审批流 → 状态机 → 归档
零外部数据库，单 JSON 文件存储
"""
from .workflow import WorkflowEngine, WorkflowDef, WorkflowStep, WorkflowInstance, StepRecord, StateMachine
from .oa_store import OAStore
from .presets import register_all_presets
from .oa_api import mount_oa_routes, mount_oa_admin_routes

__all__ = [
    "WorkflowEngine", "WorkflowDef", "WorkflowStep", "WorkflowInstance", "StepRecord", "StateMachine",
    "OAStore", "register_all_presets", "mount_oa_routes", "mount_oa_admin_routes",
]