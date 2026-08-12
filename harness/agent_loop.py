"""
AgentLoop — Harness 主循环

核心流程:
  用户输入 → recall 记忆 → 注入 system prompt
  → while True:
      路由模型 → 调 LLM（流式）→ 解析响应
      → tool_use? 执行工具 → 防循环检测 → 回灌结果
      → end_turn? 退出循环 → ingest + distill 记忆
"""

import json
import logging
import time
from typing import Any, Callable

from .types import (
    Message, ContentBlock, ToolCall, ToolResult, LLMResponse,
    StopReason, PermissionDeniedError, ToolExecutionError,
)
from .config import HarnessConfig
from .context_manager import ContextManager
from .permission_checker import PermissionChecker
from .task_router import TaskRouter
from .tool_registry import ToolRegistry
from .memory.memory_adapter import MemoryAdapter

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Harness 主循环 — 把所有模块串起来

    使用:
        harness = AgentLoop(config, memory, task_router, tool_registry, ctx_mgr, perm_checker)
        response = harness.run("帮我写一份通知", user_id="zhangsan")
    """

    def __init__(
        self,
        config: HarnessConfig,
        memory: MemoryAdapter,
        task_router: TaskRouter,
        tool_registry: ToolRegistry,
        context_manager: ContextManager,
        permission_checker: PermissionChecker,
        on_text: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        session_store: Any | None = None,  # SessionStore 实例（可选）
    ):
        self.config = config
        self.memory = memory
        self.task_router = task_router
        self.tool_registry = tool_registry
        self.context = context_manager
        self.permission = permission_checker
        self.session_store = session_store

        # 回调
        self.on_text = on_text or (lambda t: None)
        self.on_tool_call = on_tool_call or (lambda n, a: None)
        self.on_tool_result = on_tool_result or (lambda n, r: None)

        # 运行时状态
        self._turn_count: int = 0
        self._session_id: str = ""
        self._user_id: str = ""
        self._executed_tools: list[dict] = []  # 本轮执行工具链（结构化返回用）

    # ========================================================================
    # 主入口
    # ========================================================================

    def run(
        self,
        user_input: str,
        user_id: str = "default",
        session_id: str | None = None,
        force_model: str | None = None,
    ) -> dict:
        """
        执行一次对话

        Args:
            user_input: 用户输入
            user_id: 用户 ID（用于分组/配额/路由）
            session_id: 会话 ID（用于记忆隔离）
            force_model: 强制使用指定模型（跳过路由）

        Returns:
            结构化结果: {text, tool_calls, usage, model, turns}
        """
        self._turn_count = 0
        self._user_id = user_id
        self._session_id = session_id or f"session:{user_id}:{int(time.time())}"
        self._executed_tools = []  # 重置工具追踪
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # 重置防循环追踪
        self.memory.reset_loop_tracker()

        # Step 1: 回灌记忆
        memory_text = self.memory.recall([self._session_id, f"user:{user_id}", "project:general"])
        self.context.set_memory(memory_text)
        self.context.clear()

        # Step 1.5: 恢复会话历史（跨请求上下文连续性）
        if self.session_store is not None:
            history = self.session_store.get_messages(self._session_id)
            for hmsg in history:
                if hmsg.get("role") == "user":
                    self.context.add_user(hmsg.get("content", ""))
                elif hmsg.get("role") == "assistant":
                    self.context.add_assistant(hmsg.get("content", ""))
            if history:
                logger.debug(f"Session {self._session_id}: restored {len(history)} history messages")

        # Step 2: 添加用户输入
        self.context.add_user(user_input)

        # Step 3: 工具能力描述
        tools_desc = self._build_tools_description()
        self.context.set_tools(tools_desc)

        # Step 4: 主循环
        final_response = ""
        model_name = "unknown"  # 初始化，防止异常时未赋值
        try:
            while self._should_continue():
                self._turn_count += 1

                # 路由 → 获取模型
                decision, client, model_name = self.task_router.route(
                    user_input=user_input if self._turn_count == 1 else "",
                    user_id=user_id,
                    force_model=force_model,
                )

                # 调用 LLM
                response = self._call_llm(client, model_name)

                # 记录用量（结构化追踪 + ModelManager 异步记录）
                if response.usage:
                    if isinstance(response.usage, dict):
                        up, uc = response.usage.get("prompt_tokens", 0) or 0, response.usage.get("completion_tokens", 0) or 0
                    else:
                        up, uc = response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0
                    total_usage["prompt_tokens"] += up
                    total_usage["completion_tokens"] += uc
                    total_usage["total_tokens"] += up + uc
                    try:
                        _, _, acct_id = self.task_router.mm.get_model(user_id, model_name)
                        self.task_router.mm.track_usage(
                            user_id=user_id,
                            account_id=acct_id or "",
                            model_name=model_name,
                            usage=response.usage,
                        )
                    except Exception:
                        pass  # 用量追踪失败不影响对话

                if response.stop_reason == StopReason.END_TURN:
                    final_response = response.text
                    break

                if response.stop_reason == StopReason.TOOL_USE:
                    # 执行工具（追踪执行链）
                    for tc in response.tool_calls:
                        self._execute_tool(tc)
                        self._executed_tools.append({
                            "name": tc.name,
                            "args": tc.arguments,
                            "result": self.context.messages[-1].content if self.context.messages else None,
                        })

                    # 继续循环
                    continue

                if response.stop_reason == StopReason.ERROR:
                    final_response = response.text or "模型调用出错，请重试"
                    break

                if response.stop_reason == StopReason.MAX_TOKENS:
                    # 继续循环让模型接着输出
                    continue

        except PermissionDeniedError as e:
            final_response = f"[权限拒绝] {e}"
            logger.warning(f"Permission denied: {e}")

        except Exception as e:
            logger.exception(f"AgentLoop error")
            final_response = f"[错误] {e}"

        # Step 5: 后处理 — 写入记忆 + 保存会话历史
        if final_response:
            self.memory.ingest("user", user_input, self._session_id)
            self.memory.ingest("assistant", final_response, self._session_id)
            self.memory.distill(self._session_id)

            # 保存会话历史（供下次 run 恢复上下文）
            if self.session_store is not None:
                self.session_store.append(self._session_id, "user", user_input)
                self.session_store.append(self._session_id, "assistant", final_response)

        return {
            "text": final_response,
            "tool_calls": self._executed_tools,
            "usage": total_usage,
            "model": model_name,
            "turns": self._turn_count,
        }

    # ========================================================================
    # LLM 调用
    # ========================================================================

    def _call_llm(self, client: Any, model_name: str) -> LLMResponse:
        """调用 LLM，处理流式响应和工具调用解析"""
        tools = self.tool_registry.get_openai_tools()

        try:
            if self.config.stream_output:
                return self._call_llm_streaming(client, model_name, tools)
            else:
                return self._call_llm_sync(client, model_name, tools)
        except Exception as e:
            logger.exception(f"LLM call failed: {model_name}")
            return LLMResponse(
                text=str(e),
                stop_reason=StopReason.ERROR,
                model=model_name,
            )

    def _call_llm_streaming(self, client: Any, model_name: str, tools: list[dict]) -> LLMResponse:
        """流式调用 LLM"""
        messages = self.context.to_openai_messages()

        # 创建流式请求
        kwargs = {
            "model": model_name,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        stream = client.chat.completions.create(**kwargs)

        # 累积响应
        text_parts: list[str] = []
        tool_calls_accum: dict[int, dict] = {}  # index → {id, name, arguments_str}
        usage: dict[str, int] = {}

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # 文本
            if delta.content:
                text_parts.append(delta.content)
                self.on_text(delta.content)

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments_str": "",
                        }
                    if tc.id:
                        tool_calls_accum[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_accum[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_accum[idx]["arguments_str"] += tc.function.arguments

            # usage（最后一个 chunk 通常有 usage）
            if hasattr(chunk, "usage") and chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens or 0,
                    "completion_tokens": chunk.usage.completion_tokens or 0,
                    "total_tokens": chunk.usage.total_tokens or 0,
                }

        # 解析工具调用（按 index 排序，兼容不连续 index）
        tool_calls = []
        for idx in sorted(tool_calls_accum.keys()):
            tc_data = tool_calls_accum[idx]
            try:
                args = json.loads(tc_data["arguments_str"]) if tc_data["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc_data["id"],
                name=tc_data["name"],
                arguments=args,
            ))

        text = "".join(text_parts)

        if tool_calls:
            stop_reason = StopReason.TOOL_USE
            # 将 tool_calls 添加到上下文中
            for tc in tool_calls:
                self.context.add_tool_call(tc.name, tc.id, tc.arguments)
        else:
            stop_reason = StopReason.END_TURN
            self.context.add_assistant(text)

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            model=model_name,
        )

    def _call_llm_sync(self, client: Any, model_name: str, tools: list[dict]) -> LLMResponse:
        """非流式调用 LLM"""
        messages = self.context.to_openai_messages()

        kwargs = {
            "model": model_name,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]

        text = choice.message.content or ""

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                tool_call = ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                tool_calls.append(tool_call)
                self.context.add_tool_call(tc.function.name, tc.id, args)

        if tool_calls:
            stop_reason = StopReason.TOOL_USE
        else:
            stop_reason = StopReason.END_TURN
            self.context.add_assistant(text)
            self.on_text(text)

        usage = {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            "total_tokens": resp.usage.total_tokens if resp.usage else 0,
        }

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            model=model_name,
        )

    # ========================================================================
    # 工具执行
    # ========================================================================

    def _execute_tool(self, tool_call: ToolCall) -> None:
        """执行工具调用"""
        self.on_tool_call(tool_call.name, tool_call.arguments)

        # 权限检查
        try:
            self.permission.check(tool_call.name, tool_call.arguments)
        except PermissionDeniedError:
            result = ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content="[权限拒绝] 当前权限模式下禁止执行此操作",
                is_error=True,
            )
            self.context.add_tool_result(result)
            return

        # 防循环检测
        if self.config.enable_anti_loop:
            if self.memory.detect_loop(tool_call.name, tool_call.arguments, self.config.anti_loop_threshold):
                result = ToolResult(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=f"[防循环拦截] 你已连续 {self.config.anti_loop_threshold} 次用相同参数调用 '{tool_call.name}'，请更换策略或结束任务",
                    is_error=True,
                )
                self.context.add_tool_result(result)
                return

        # 执行
        result = self.tool_registry.execute(tool_call)

        # 智能压缩工具返回（内容类型路由，>500 字符才压缩）
        if len(result.content) > 500:
            compressed = self.context.smart_compress_result(
                result.content, tool_call.name, tool_call.id
            )
            if compressed != result.content:
                logger.debug(f"Compressed {tool_call.name}: {len(result.content)} → {len(compressed)} chars")
                result = ToolResult(
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                    content=compressed,
                    is_error=result.is_error,
                )

        self.context.add_tool_result(result)
        self.on_tool_result(tool_call.name, result.content)

    # ========================================================================
    # 循环控制
    # ========================================================================

    def _should_continue(self) -> bool:
        """判断是否继续循环"""
        if self.config.max_turns > 0 and self._turn_count >= self.config.max_turns:
            logger.warning(f"Max turns ({self.config.max_turns}) reached")
            return False
        return True

    def _build_tools_description(self) -> str:
        """构建工具能力描述 — 渐进式加载: 只输出索引，LLM 按需调 get_tool_detail"""
        return self.tool_registry.get_index_text()
