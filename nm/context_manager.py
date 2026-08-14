"""
上下文管理器
管理对话消息列表、系统提示词组装、上下文压缩

压缩策略（参考 Headroom 59.5K星 — 内容类型路由压缩）:
- JSON/结构化结果 → 统计压缩: 保留异常值，重复记录只留样本
- 代码/日志输出 → 错误聚焦: 保留 error/warning 行，其余压缩
- 大段文本 → 摘要截断: 保留首尾 + 关键词
- 文件列表 → 去重计数
"""

import json
import logging
import os
import re
from typing import Any, Callable

from .types import Message, ContentBlock, ToolResult

logger = logging.getLogger(__name__)


class ContextManager:
    """
    管理 AgentLoop 的消息上下文

    职责:
    - 维护 messages 列表（system + user + assistant + tool_result）
    - 组装 system prompt（注入 memory + workspace 信息）
    - 上下文压缩（超过阈值时替换旧的 tool_result）
    - Token 预算估算
    """

    def __init__(
        self,
        system_prompt_template: str,
        workspace_dir: str,
        compaction_threshold: int = 40,
        keep_recent: int = 20,
        output_style_rules: str = "",
        on_compaction: Callable[[list[dict]], None] | None = None,
        archive_path: str | None = None,
    ):
        self.system_prompt_template = system_prompt_template
        self.workspace_dir = workspace_dir
        self.compaction_threshold = compaction_threshold
        self.keep_recent = keep_recent
        self.output_style_rules = output_style_rules
        # 压缩回调: 中间层 → 记忆体 的解耦事件通道。
        # 每次发生上下文压缩时触发，回调收到被压缩的 tool_result 原文列表
        # ({"tool_name", "tool_id", "original", "placeholder"})，由调用方决定
        # 是否沉淀为记忆（如 AgentLoop 注入 MemoryAdapter 做提取）。
        self.on_compaction = on_compaction
        # CCR 存档持久化路径（默认 workspace/.nm/compression_archive.json）
        self.archive_path = archive_path or os.path.join(
            workspace_dir, ".nm", "compression_archive.json"
        )

        self.messages: list[Message] = []
        self._memory_context: str = ""
        self._tools_context: str = ""
        # 压缩存档: tool_call_id → 原始完整内容（CCR 可恢复机制，落盘持久化）
        self._compression_archive: dict[str, str] = {}
        self._load_archive()

    # ========================================================================
    # 消息操作
    # ========================================================================

    def set_memory(self, memory_text: str) -> None:
        """设置记忆回灌文本（在 system prompt 中注入）"""
        self._memory_context = memory_text

    def set_tools(self, tools_desc: str) -> None:
        """设置工具能力描述"""
        self._tools_context = tools_desc

    def build_system(self) -> Message:
        """构建系统消息"""
        prompt = self.system_prompt_template.format(
            workspace_dir=self.workspace_dir,
            memory_context=self._memory_context or "(无历史记忆)",
            output_style_rules=self.output_style_rules or "",
        )

        if self._tools_context:
            prompt += f"\n{self._tools_context}"

        return Message.system(prompt)

    def add_user(self, text: str) -> None:
        self.messages.append(Message.user(text))

    def add_assistant(self, text: str | None = None) -> None:
        msg = Message.assistant(text)
        # 如果没有文本内容但有后续的 tool_calls，等 tool_calls 处理
        if text:
            # 已有文本，直接添加
            pass
        self.messages.append(msg)

    def add_tool_call(self, name: str, tool_id: str, arguments: dict[str, Any]) -> None:
        """在最近的 assistant 消息中添加 tool_use 块"""
        if not self.messages or self.messages[-1].role != "assistant":
            # 创建一个新的 assistant 消息
            self.messages.append(Message(role="assistant", content=[]))

        self.messages[-1].content.append(
            ContentBlock(
                type="tool_use",
                tool_name=name,
                tool_id=tool_id,
                tool_input=arguments,
            )
        )

    def add_tool_result(self, result: ToolResult) -> None:
        """添加工具执行结果"""
        self.messages.append(
            Message(
                role="tool",
                content=[
                    ContentBlock(
                        type="tool_result",
                        text=result.content,
                        tool_id=result.tool_call_id,
                        tool_name=result.name,
                    )
                ],
            )
        )

    def add_system_reminder(self, text: str) -> None:
        """添加系统提醒（如 anti-loop 提示）"""
        self.messages.append(Message(role="user", content=[ContentBlock(type="text", text=text)]))

    # ========================================================================
    # 压缩
    # ========================================================================

    def _load_archive(self) -> None:
        """从磁盘加载 CCR 存档（进程重启/换模型后仍可恢复原文）"""
        try:
            with open(self.archive_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._compression_archive = data
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning(f"Failed to load compression archive: {self.archive_path}")

    def _save_archive(self) -> None:
        """把 CCR 存档写入磁盘（原子写）"""
        try:
            os.makedirs(os.path.dirname(self.archive_path) or ".", exist_ok=True)
            tmp = self.archive_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._compression_archive, f, ensure_ascii=False)
            os.replace(tmp, self.archive_path)
        except Exception:
            logger.warning(f"Failed to save compression archive: {self.archive_path}")

    def maybe_compact(self) -> bool:
        """如果消息数超过阈值，执行上下文压缩。返回是否执行了压缩

        压缩时通过 on_compaction 回调把被压缩的 tool_result 原文
        (含 tool_name/tool_id) 交给记忆体沉淀——这是"压缩 → 回灌"
        闭环的事件通道，ContextManager 不关心记忆体怎么用这些内容。
        """
        if len(self.messages) <= self.compaction_threshold:
            return False

        compact_at = len(self.messages) - self.keep_recent
        compacted_count = 0
        compacted_items: list[dict] = []

        for i in range(compact_at):
            msg = self.messages[i]
            for block in msg.content:
                if block.type == "tool_result" and block.text:
                    # CCR: 先存档原始内容（供 retrieve_original 恢复）
                    tool_id = getattr(block, "tool_id", "") or getattr(block, "tool_use_id", "")
                    if tool_id:
                        self._compression_archive[tool_id] = block.text
                    # 保留工具名，替换内容
                    tool_name = getattr(block, "tool_name", "") or "unknown"
                    placeholder = f"[已压缩: {tool_name} 工具的返回结果，上下文空间有限已省略原文]"
                    block.text = placeholder
                    compacted_items.append({
                        "tool_name": tool_name,
                        "tool_id": tool_id,
                        "original": self._compression_archive.get(tool_id, ""),
                        "placeholder": placeholder,
                    })
                    compacted_count += 1

        if compacted_count > 0:
            logger.info(f"Context compacted: {compacted_count} tool_results replaced")
            self._save_archive()  # CCR 落盘：重启/换模型后可恢复原文
            if self.on_compaction and compacted_items:
                try:
                    self.on_compaction(compacted_items)
                except Exception:
                    logger.exception("on_compaction callback failed (memory sink skipped)")

        return compacted_count > 0

    # ========================================================================
    # OpenAI 格式转换
    # ========================================================================

    def retrieve_original(self, tool_call_id: str) -> str | None:
        """取回被压缩的 tool_result 原文（CCR 可恢复机制）"""
        return self._compression_archive.get(tool_call_id)

    def to_openai_messages(self) -> list[dict[str, Any]]:
        """将消息列表转换为 OpenAI API 格式"""
        # 压缩检查
        self.maybe_compact()

        # 构建完整消息列表
        full_messages = [self._msg_to_openai(self.build_system())]
        full_messages.extend(self._msg_to_openai(m) for m in self.messages)

        return full_messages

    def _msg_to_openai(self, msg: Message) -> dict[str, Any]:
        """单条消息转换为 OpenAI 格式"""
        if not msg.content:
            return {"role": msg.role, "content": ""}

        # 检查是否有多模态内容
        has_tool_use = any(b.type == "tool_use" for b in msg.content)
        has_tool_result = any(b.type == "tool_result" for b in msg.content)
        has_only_text = all(b.type == "text" for b in msg.content)

        if has_only_text and len(msg.content) == 1:
            return {"role": msg.role, "content": msg.content[0].text or ""}

        if has_tool_use:
            # assistant 消息带 tool_calls
            text_parts = []
            tool_calls = []
            for block in msg.content:
                if block.type == "text" and block.text:
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append({
                        "id": block.tool_id or "",
                        "type": "function",
                        "function": {
                            "name": block.tool_name or "",
                            "arguments": json.dumps(block.tool_input or {}, ensure_ascii=False),
                        },
                    })

            result = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                result["tool_calls"] = tool_calls
            return result

        if has_tool_result:
            # tool role — only one result per message
            block = msg.content[0]
            return {
                "role": "tool",
                "tool_call_id": block.tool_id or "",
                "content": block.text or "",
            }

        # 纯文本多块
        text = " ".join(b.text or "" for b in msg.content if b.text)
        return {"role": msg.role, "content": text}

    # ========================================================================
    # Token 估算
    # ========================================================================

    def estimate_tokens(self) -> int:
        """粗略估计当前上下文 token 数（中文 1 字 ≈ 1.5 token，英文 1 词 ≈ 1.3 token）"""
        total = 0
        for msg in self.messages:
            for block in msg.content:
                if block.text:
                    # 简单估算
                    total += len(block.text) * 1.2
                elif block.tool_input:
                    import json
                    total += len(json.dumps(block.tool_input, ensure_ascii=False)) * 0.8
        return int(total)

    def clear(self) -> None:
        """清空窗口内消息列表。

        注意：只清 messages（窗口态），保留 memory/tools 上下文。
        AgentLoop 每次 run() 先 clear() 再 set_memory()，
        若这里把 _memory_context 一起清掉，回灌记忆会在进入 system prompt 前丢失。
        """
        self.messages.clear()

    # ========================================================================
    # 智能压缩（内容类型路由 — 参考 Headroom 思路自研实现）
    # ========================================================================

    def smart_compress_result(self, text: str, tool_name: str = "", tool_id: str = "") -> str:
        """
        对工具返回内容做内容类型感知的智能压缩

        策略:
        - JSON/结构化 → 采样: 保留前N条 + 后N条 + 异常值
        - 代码/日志 → 过滤: 保留 error/warning/exception 行
        - 文件列表 → 统计: 总数 + 去重后的文件名
        - 大段文本 → 截断: 保留首尾，中间省略
        """
        if len(text) <= 500:
            return text

        # 存档原始内容（CCR: 可恢复）
        if tool_id:
            self._compression_archive[tool_id] = text

        # 类型路由
        if self._is_json_output(text):
            return self._compress_json(text)
        if self._is_log_output(text):
            return self._compress_log(text)
        if self._is_file_list(text):
            return self._compress_file_list(text)

        return self._compress_text(text)

    @staticmethod
    def _is_json_output(text: str) -> bool:
        stripped = text.strip()
        return (stripped.startswith("[") and stripped.endswith("]")) or \
               (stripped.startswith("{") and stripped.endswith("}"))

    @staticmethod
    def _is_log_output(text: str) -> bool:
        log_patterns = ["ERROR", "WARN", "INFO ", "DEBUG", "TRACE",
                        "Exception", "Traceback", "error:", "warning:"]
        lines = text.split("\n")
        hits = sum(1 for line in lines[:20] if any(p in line for p in log_patterns))
        return hits >= 3

    @staticmethod
    def _is_file_list(text: str) -> bool:
        """检测是否为 ls/find/grep 输出的文件列表"""
        lines = [l for l in text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return False
        # 检查每行是否像文件路径（含 / 或 .ext）
        path_like = sum(1 for l in lines if "/" in l or "." in l.split()[-1])
        return path_like / len(lines) > 0.6

    @staticmethod
    def _compress_json(text: str) -> str:
        """JSON 压缩: 保留前3条+后2条+总数"""
        lines = text.strip().split("\n")
        total = len(lines)
        if total <= 20:
            return text

        head = lines[:3]
        tail = lines[-2:]
        sample = "\n".join(head + [f"  ...(共 {total - 5} 条省略)..."] + tail)
        return f"[已压缩: 共 {total} 条记录，以下为前后样本]\n{sample}"

    @staticmethod
    def _compress_log(text: str) -> str:
        """日志压缩: 保留错误行 + 最后10行"""
        lines = text.split("\n")
        error_lines = []
        other_lines = []

        for line in lines:
            if re.search(r"(ERROR|WARN|Exception|Traceback|error:|warning:|FAILED|FATAL)", line):
                error_lines.append(line)
            else:
                other_lines.append(line)

        result = []
        if error_lines:
            result.append(f"[错误/警告: {len(error_lines)} 行]")
            result.extend(error_lines[:20])  # 最多20行错误

        if other_lines:
            result.append(f"\n[其他日志: {len(other_lines)} 行，显示最后10行]")
            result.extend(other_lines[-10:])

        return "\n".join(result) if result else "\n".join(lines[-30:])

    @staticmethod
    def _compress_file_list(text: str) -> str:
        """文件列表压缩: 去重+统计"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        unique = list(dict.fromkeys(lines))  # 保持顺序去重

        if len(unique) <= 15:
            return f"[共 {len(unique)} 个文件]\n" + "\n".join(unique)

        # 超过15个: 显示前10 + 后5 + 计数
        return (
            f"[共 {len(unique)} 个文件，去重后显示样本]\n"
            + "\n".join(unique[:10])
            + f"\n  ...({len(unique) - 15} 个省略)..."
            + "\n" + "\n".join(unique[-5:])
        )

    @staticmethod
    def _compress_text(text: str) -> str:
        """通用文本压缩: 保留首尾，中间省略"""
        lines = text.split("\n")
        if len(lines) <= 30:
            return text

        return (
            "\n".join(lines[:10])
            + f"\n  ...({len(lines) - 20} 行省略)..."
            + "\n" + "\n".join(lines[-10:])
        )

    def get_compression_stats(self) -> dict:
        """压缩统计"""
        total = sum(len(v) for v in self._compression_archive.values())
        return {
            "archived_items": len(self._compression_archive),
            "archived_total_chars": total,
        }
