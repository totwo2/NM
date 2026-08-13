"""
预设 OA 流程模板
对齐致远 A8 常用模板，一行代码即可注册全部
"""

from .workflow import WorkflowEngine


def register_all_presets(engine: WorkflowEngine):
    """一键注册所有预设流程模板"""

    # ---- 请假审批 ----
    engine.register_def(
        id="leave_approval",
        name="请假审批",
        category="hr",
        icon="🏖️",
        description="年假/事假/病假/婚假/产假/陪产假/丧假",
        form_schema={
            "type": "object",
            "properties": {
                "请假类型": {"type": "string", "enum": ["年假", "事假", "病假", "婚假", "产假", "陪产假", "丧假"]},
                "开始日期": {"type": "string", "format": "date"},
                "结束日期": {"type": "string", "format": "date"},
                "天数": {"type": "number", "min": 0.5},
                "请假事由": {"type": "string"},
            },
            "required": ["请假类型", "开始日期", "结束日期", "天数", "请假事由"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice", "timeout_hours": 24},
            {"id": "step_2", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 24, "can_reject": True},
            {"id": "step_3", "name": "HR 备案", "assignee_type": "fixed", "assignee": "oa_admin", "can_reject": False},
        ],
    )

    # ---- 报销审批 ----
    engine.register_def(
        id="expense_approval",
        name="报销审批",
        category="finance",
        icon="💰",
        description="差旅费/招待费/办公用品/其他费用报销",
        form_schema={
            "type": "object",
            "properties": {
                "报销类型": {"type": "string", "enum": ["差旅费", "招待费", "办公用品", "培训费", "其他"]},
                "报销金额": {"type": "number", "min": 0},
                "报销事由": {"type": "string"},
                "明细列表": {"type": "string", "description": "逐项列出费用明细"},
            },
            "required": ["报销类型", "报销金额", "报销事由"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_2", "name": "财务审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 72},
            {"id": "step_3", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 48, "can_reject": True},
        ],
    )

    # ---- 合同审批 ----
    engine.register_def(
        id="contract_approval",
        name="合同审批",
        category="legal",
        icon="📝",
        description="采购合同/服务合同/合作框架协议/劳动合同",
        form_schema={
            "type": "object",
            "properties": {
                "合同类型": {"type": "string", "enum": ["采购合同", "服务合同", "合作协议", "劳动合同", "其他"]},
                "对方单位": {"type": "string"},
                "合同金额": {"type": "number", "min": 0},
                "合同摘要": {"type": "string"},
                "是否涉及付款": {"type": "boolean"},
            },
            "required": ["合同类型", "对方单位", "合同金额", "合同摘要"],
        },
        steps=[
            {"id": "step_1", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_2", "name": "法务审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 72},
            {"id": "step_3", "name": "财务审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 48},
            {"id": "step_4", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 48, "can_reject": True},
        ],
    )

    # ---- 用印审批 ----
    engine.register_def(
        id="seal_approval",
        name="用印审批",
        category="admin",
        icon="🔖",
        description="公章/合同章/法人章/财务章使用申请",
        form_schema={
            "type": "object",
            "properties": {
                "印章类型": {"type": "string", "enum": ["公章", "合同章", "法人章", "财务章", "部门章"]},
                "用印事由": {"type": "string"},
                "文件名称": {"type": "string"},
                "份数": {"type": "number", "min": 1},
            },
            "required": ["印章类型", "用印事由", "文件名称", "份数"],
        },
        steps=[
            {"id": "step_1", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 24},
            {"id": "step_2", "name": "行政办审核", "assignee_type": "fixed", "assignee": "admin_admin", "timeout_hours": 24},
        ],
    )

    # ---- 采购审批 ----
    engine.register_def(
        id="purchase_approval",
        name="采购审批",
        category="finance",
        icon="🛒",
        description="固定资产/办公用品/服务采购申请",
        form_schema={
            "type": "object",
            "properties": {
                "采购类型": {"type": "string", "enum": ["固定资产", "办公用品", "IT设备", "服务采购", "其他"]},
                "采购金额": {"type": "number", "min": 0},
                "采购事由": {"type": "string"},
                "供应商": {"type": "string"},
            },
            "required": ["采购类型", "采购金额", "采购事由"],
        },
        steps=[
            {"id": "step_1", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_2", "name": "财务审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 48},
            {"id": "step_3", "name": "采购部门审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 48},
            {"id": "step_4", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
        ],
    )

    # ---- 出差审批 ----
    engine.register_def(
        id="travel_approval",
        name="出差审批",
        category="hr",
        icon="✈️",
        description="国内/国际出差申请",
        form_schema={
            "type": "object",
            "properties": {
                "出差类型": {"type": "string", "enum": ["国内出差", "国际出差"]},
                "目的地": {"type": "string"},
                "出发日期": {"type": "string", "format": "date"},
                "返回日期": {"type": "string", "format": "date"},
                "天数": {"type": "number", "min": 0.5},
                "出差事由": {"type": "string"},
                "预计费用": {"type": "number", "min": 0},
            },
            "required": ["出差类型", "目的地", "出发日期", "返回日期", "出差事由"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_2", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_3", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 48, "can_reject": True},
        ],
    )

    # ---- 加班审批 ----
    engine.register_def(
        id="overtime_approval",
        name="加班审批",
        category="hr",
        icon="⏰",
        description="工作日加班/休息日加班/节假日加班申请",
        form_schema={
            "type": "object",
            "properties": {
                "加班类型": {"type": "string", "enum": ["工作日加班", "休息日加班", "节假日加班"]},
                "加班日期": {"type": "string", "format": "date"},
                "开始时间": {"type": "string"},
                "结束时间": {"type": "string"},
                "加班事由": {"type": "string"},
            },
            "required": ["加班类型", "加班日期", "开始时间", "结束时间", "加班事由"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice", "timeout_hours": 24},
            {"id": "step_2", "name": "HR 备案", "assignee_type": "fixed", "assignee": "oa_admin", "can_reject": False},
        ],
    )

    # ---- 转正审批 ----
    engine.register_def(
        id="regular_approval",
        name="转正审批",
        category="hr",
        icon="🎓",
        description="试用期员工转正申请",
        form_schema={
            "type": "object",
            "properties": {
                "员工姓名": {"type": "string"},
                "入职日期": {"type": "string", "format": "date"},
                "拟转正日期": {"type": "string", "format": "date"},
                "试用期评价": {"type": "string"},
            },
            "required": ["员工姓名", "入职日期", "拟转正日期", "试用期评价"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导评价", "assignee_type": "initiator_choice", "timeout_hours": 72},
            {"id": "step_2", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 72},
            {"id": "step_3", "name": "HR 审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 72},
            {"id": "step_4", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 72},
        ],
    )

    # ---- 离职审批 ----
    engine.register_def(
        id="resignation_approval",
        name="离职审批",
        category="hr",
        icon="👋",
        description="员工离职申请",
        form_schema={
            "type": "object",
            "properties": {
                "员工姓名": {"type": "string"},
                "最后工作日": {"type": "string", "format": "date"},
                "离职原因": {"type": "string"},
            },
            "required": ["员工姓名", "最后工作日", "离职原因"],
        },
        steps=[
            {"id": "step_1", "name": "直属领导审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_2", "name": "部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 48},
            {"id": "step_3", "name": "HR 面谈", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 72},
            {"id": "step_4", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 48, "can_reject": True},
        ],
    )

    # ---- 调岗审批 ----
    engine.register_def(
        id="transfer_approval",
        name="调岗审批",
        category="hr",
        icon="🔄",
        description="员工岗位调整申请",
        form_schema={
            "type": "object",
            "properties": {
                "员工姓名": {"type": "string"},
                "原部门": {"type": "string"},
                "原岗位": {"type": "string"},
                "目标部门": {"type": "string"},
                "目标岗位": {"type": "string"},
                "调整原因": {"type": "string"},
            },
            "required": ["员工姓名", "原部门", "目标部门", "目标岗位", "调整原因"],
        },
        steps=[
            {"id": "step_1", "name": "原部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 72},
            {"id": "step_2", "name": "目标部门负责人审批", "assignee_type": "initiator_choice", "timeout_hours": 72},
            {"id": "step_3", "name": "HR 审核", "assignee_type": "fixed", "assignee": "oa_admin", "timeout_hours": 72},
            {"id": "step_4", "name": "总经理审批", "assignee_type": "initiator_choice", "timeout_hours": 72},
        ],
    )
