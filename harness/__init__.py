"""
N.M — 企业智能办公助手

取百家之长:
- vLLM Semantic Router   → 智能路由架构
- LLMRouter (UIUC)       → 16+ 路由策略
- Portkey AI Gateway     → 生产可靠性
- Pi (7.7万星)           → 极简设计哲学 + 渐进式工具加载
- PenguinHarness          → 自进化流水线
- Darwin.skill            → 棘轮验证 + 独立评分
- SESA / VeriSkill (北大) → 失败蒸馏 + 责任归因
- JiangGongAgent         → 完整教学参考
- memory_core (自研)     → 分层持久记忆
- CowAgent + Flink       → 记忆自进化

用法:
    python -m harness.cli                    # 交互模式
    python -m harness.cli -o "帮我写一份通知"  # 单次模式
"""

from .agent_loop import AgentLoop
from .config import HarnessConfig
from .context_manager import ContextManager
from .model_manager import ModelManager
from .permission_checker import PermissionChecker
from .task_router import TaskRouter
from .tool_registry import ToolRegistry
from .memory.memory_adapter import MemoryAdapter
from .self_evolution import SelfEvolution, FailureClassifier, RatchetVerifier

__version__ = "1.2.0"
__all__ = [
    "AgentLoop",
    "HarnessConfig",
    "ContextManager",
    "ModelManager",
    "PermissionChecker",
    "TaskRouter",
    "ToolRegistry",
    "MemoryAdapter",
    "SelfEvolution",
    "FailureClassifier",
    "RatchetVerifier",
]
