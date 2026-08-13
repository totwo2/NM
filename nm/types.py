"""
Harness 核心类型定义
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================================
# 权限模式
# ============================================================================

class PermissionMode(Enum):
    AUTO = "auto"       # 自动执行所有操作
    ASK = "ask"         # 执行前询问用户
    STRICT = "strict"   # 拒绝所有危险操作


# ============================================================================
# 路由信号
# ============================================================================

class Complexity(Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class Domain(Enum):
    DOCUMENT = "document"   # 公文/文档
    DATA = "data"           # 数据分析
    CODE = "code"           # 代码
    GENERAL = "general"     # 通用问答


# ============================================================================
# 消息类型
# ============================================================================

@dataclass
class ContentBlock:
    """消息内容块（支持多模态）"""
    type: str  # "text" | "tool_use" | "tool_result"
    text: str | None = None
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input: dict[str, Any] | None = None


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: list[ContentBlock] = field(default_factory=list)

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls(role="system", content=[ContentBlock(type="text", text=text)])

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls(role="user", content=[ContentBlock(type="text", text=text)])

    @classmethod
    def assistant(cls, text: str | None = None) -> "Message":
        blocks = []
        if text:
            blocks.append(ContentBlock(type="text", text=text))
        return cls(role="assistant", content=blocks)


@dataclass
class ToolCall:
    """工具调用请求"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


# ============================================================================
# LLM 响应
# ============================================================================

class StopReason(Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    ERROR = "error"


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


# ============================================================================
# 工具定义
# ============================================================================

@dataclass
class ToolSchema:
    """工具的 OpenAI function 定义"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class Tool:
    """工具体：定义 + 执行函数"""
    schema: ToolSchema
    fn: callable
    requires_approval: bool = False  # 是否需要用户确认
    timeout_seconds: int = 30


# ============================================================================
# 路由相关
# ============================================================================

@dataclass
class TaskSignal:
    """从用户输入中提取的路由信号"""
    complexity: Complexity = Complexity.MEDIUM
    domain: Domain = Domain.GENERAL
    has_pii: bool = False
    estimated_tokens: int = 0
    needs_tools: bool = False
    needs_reasoning: bool = False
    confidence: float = 0.5  # 信号置信度


@dataclass
class RoutingDecision:
    """路由决策"""
    model_name: str       # e.g. "gpt-4o", "qwen3-30b-local"
    provider: str         # "local" | "cloud"
    reason: str           # 决策理由（可审计）
    fallback_model: str | None = None  # 兜底模型
    cost_estimate: float = 0.0


# ============================================================================
# 模型管理
# ============================================================================

@dataclass
class ProviderConfig:
    """模型供应商配置"""
    name: str                    # "openai", "anthropic", "local_qwen"
    provider_type: str           # "local" | "cloud"
    base_url: str                # API 端点
    api_key: str = ""
    models: list[str] = field(default_factory=list)  # 支持的模型列表
    default_model: str = ""


@dataclass
class Account:
    """API 账号"""
    id: str
    provider: str          # 关联 ProviderConfig.name
    api_key: str
    is_active: bool = True
    total_tokens_used: int = 0
    cost_accumulated: float = 0.0
    last_used_at: str = ""


@dataclass
class Group:
    """用户组/部门"""
    id: str
    name: str
    assigned_accounts: list[str] = field(default_factory=list)  # Account.id 列表
    quota_daily_tokens: int = 0    # 0 = 不限
    quota_monthly_tokens: int = 0  # 0 = 不限
    default_model: str = ""        # 默认模型名


@dataclass
class UsageRecord:
    """用量记录"""
    timestamp: str
    user_id: str
    account_id: str
    model_name: str
    task_complexity: str
    task_domain: str
    tokens_in: int
    tokens_out: int
    cost: float
    routing_reason: str


# ============================================================================
# 工具执行异常
# ============================================================================

class ToolExecutionError(Exception):
    """工具执行错误"""
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"[{tool_name}] {message}")


class PermissionDeniedError(Exception):
    """权限拒绝"""
    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        super().__init__(f"Permission denied for {tool_name}: {reason}")
