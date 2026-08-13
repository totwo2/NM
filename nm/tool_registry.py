"""
工具注册表
注册、查找、执行工具的中央调度器
"""

import json
import logging
from typing import Any

from .types import Tool, ToolCall, ToolResult, ToolSchema, ToolExecutionError

logger = logging.getLogger(__name__)


def _format_tool_detail(detail: dict[str, Any] | None) -> str:
    if not detail:
        return "工具不存在"
    return json.dumps(detail, ensure_ascii=False, indent=2)


class ToolRegistry:
    """工具注册表 — 统一管理所有工具的注册、查找和执行"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._index_cache: str = ""  # 缓存工具索引文本（注册/卸载时失效）

    # ========================================================================
    # 注册
    # ========================================================================

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: callable,
        requires_approval: bool = False,
        timeout_seconds: int = 30,
    ) -> None:
        """注册一个工具"""
        schema = ToolSchema(
            name=name,
            description=description,
            parameters=parameters,
        )
        self._tools[name] = Tool(
            schema=schema,
            fn=fn,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
        )
        self._index_cache = ""  # 注册新工具，失效缓存
        logger.info(f"Tool registered: {name}")

    def unregister(self, name: str) -> None:
        """注销工具"""
        self._tools.pop(name, None)
        self._index_cache = ""  # 卸载工具，失效缓存
        logger.info(f"Tool unregistered: {name}")

    # ========================================================================
    # 查询
    # ========================================================================

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_all(self) -> list[Tool]:
        return list(self._tools.values())

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """生成 OpenAI function calling 格式的工具列表"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.schema.name,
                    "description": tool.schema.description,
                    "parameters": tool.schema.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def needs_approval(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool.requires_approval if tool else False

    # ========================================================================
    # 渐进式加载（Index First, Body on Demand）
    # ========================================================================

    def get_index(self) -> list[dict[str, str]]:
        """
        返回工具索引：只含名称和一句话描述。
        放入 system prompt，不占上下文。
        """
        return [
            {"name": t.schema.name, "brief": t.schema.description.split("。")[0].split("\n")[0][:80]}
            for t in self._tools.values()
        ]

    def get_detail(self, name: str) -> dict[str, Any] | None:
        """
        返回单个工具的完整 schema。
        Agent 判断需要某个工具时，通过 get_tool_detail 工具主动加载。
        """
        tool = self._tools.get(name)
        if not tool:
            return None
        return {
            "name": tool.schema.name,
            "description": tool.schema.description,
            "parameters": tool.schema.parameters,
            "requires_approval": tool.requires_approval,
        }

    def get_index_text(self) -> str:
        """工具索引文本，用于 system prompt（带缓存，注册/卸载时失效）"""
        if self._index_cache:
            return self._index_cache
        index = self.get_index()
        if not index:
            return ""
        lines = ["\n## 可用工具索引\n"]
        for i, item in enumerate(index, 1):
            lines.append(f"{i}. `{item['name']}` — {item['brief']}")
        lines.append(f"\n共 {len(index)} 个工具。获取完整用法请调用 `get_tool_detail`。")
        self._index_cache = "\n".join(lines)
        return self._index_cache

    # ========================================================================
    # 执行
    # ========================================================================

    def execute(self, call: ToolCall) -> ToolResult:
        """执行工具调用"""
        tool = self._tools.get(call.name)
        if not tool:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"错误: 未找到工具 '{call.name}'。可用工具: {list(self._tools.keys())}",
                is_error=True,
            )

        try:
            logger.info(f"Executing tool: {call.name}({call.arguments})")
            result = tool.fn(**call.arguments)
            result_str = str(result) if not isinstance(result, str) else result

            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=result_str,
            )

        except ToolExecutionError as e:
            logger.error(f"Tool error: {e}")
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=str(e),
                is_error=True,
            )
        except Exception as e:
            logger.exception(f"Unexpected error executing {call.name}")
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"工具 '{call.name}' 执行失败: {e}",
                is_error=True,
            )

    def execute_batch(self, calls: list[ToolCall]) -> list[ToolResult]:
        """批量执行工具调用"""
        return [self.execute(call) for call in calls]

    # ========================================================================
    # 工厂方法
    # ========================================================================

    def create_default(self, workspace_dir: str) -> "ToolRegistry":
        """创建包含默认工具的注册表"""
        from .tools.base_tools import create_base_tools
        from .tools.oa_tools import create_oa_tools

        registry = ToolRegistry()

        # 内置工具: get_tool_detail
        registry.register(
            name="get_tool_detail",
            description="获取指定工具的完整用法说明（参数、示例、注意事项）。当你需要了解某个不熟悉的工具的详细用法时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "工具名称"},
                },
                "required": ["tool_name"],
            },
            fn=lambda tool_name: _format_tool_detail(registry.get_detail(tool_name)),
            requires_approval=False,
            timeout_seconds=5,
        )

        # 基础工具
        for tool_def in create_base_tools(workspace_dir):
            registry.register(**tool_def)

        # OA 工具
        for tool_def in create_oa_tools(workspace_dir):
            registry.register(**tool_def)

        return registry
