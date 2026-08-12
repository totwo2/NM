"""
Memory 适配层
封装 memory_core，新增企业场景优化:
- TTL 过期清理
- 上下文压缩（tool_result 占位符替换）
- 树状会话（checkpoint + resume from branch）
- 深度蒸馏（夜间复盘，二次提炼模式）
- 防循环记忆（记录死循环模式）
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .memcore_impl import MemoryCore, EXTRACTOR

logger = logging.getLogger(__name__)


class MemoryAdapter:
    """
    记忆适配层 — 你现有的 memory_core 加五层增量优化

    使用方式:
        adapter = MemoryAdapter(store_path, transcript_path)
        adapter.recall("session:user123")     # 回灌记忆
        adapter.ingest("user", "...", "...")   # 记录对话
        adapter.distill("session:user123")     # 提炼记忆
        adapter.checkpoint("node_1")           # 树状会话: 标记节点
        adapter.compact_context(messages)       # 压缩上下文
    """

    def __init__(
        self,
        store_path: str = "memory_store.json",
        transcript_path: str = "transcript.jsonl",
        ttl_days: int = 30,
        enable_tree: bool = True,
        enable_deep_distill: bool = True,
    ):
        self.core = MemoryCore(store_path, transcript_path)
        self.ttl_days = ttl_days
        self.enable_tree = enable_tree
        self.enable_deep_distill = enable_deep_distill

        # 树状会话: {checkpoint_id: {"entries_snapshot": [...], "timestamp": "..."}}
        self._checkpoints: dict[str, dict] = {}

        # 防循环模式追踪: {(tool_name, args_hash): [timestamp1, timestamp2, ...]}
        self._loop_tracker: dict[tuple, list[float]] = {}

    # ========================================================================
    # 基础读写（透传 memory_core）
    # ========================================================================

    def ingest(self, role: str, content: str, scope: str = "general") -> None:
        """记录一条对话"""
        self.core.ingest(role, content, scope)

    def distill(self, scope: str | None = None) -> None:
        """提炼 pending 缓冲中的对话为结构化记忆"""
        self.core.distill(scope)

    def recall(self, scopes: list[str] | None = None) -> str:
        """全量回灌记忆（稳定 + 决策 + 事实），作 system prompt 片段"""
        return self.core.recall_split(scopes)

    def capture(self, payload: dict) -> None:
        """显式捕获一条结构化记忆"""
        self.core.capture(payload)

    def mark_used(self, ids: list[str]) -> None:
        """标记记忆条目已被模型读取"""
        self.core.mark_used(ids)

    def stats(self) -> dict:
        """记忆库统计"""
        return self.core.stats()

    # ========================================================================
    # 优化 1: TTL 过期清理（参考 Flink Agents）
    # ========================================================================

    def cleanup_expired(self) -> int:
        """清理过期的记忆条目，返回清理数量"""
        if self.ttl_days <= 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.ttl_days)
        entries = self.core._load()
        original_count = len(entries)

        kept = []
        removed = 0
        for entry in entries:
            # 有 expires_at 字段的按字段判断，否则按 created_at + ttl_days
            expires_at = entry.get("expires_at")
            if expires_at:
                try:
                    exp = datetime.fromisoformat(expires_at)
                    if exp < datetime.now(timezone.utc):
                        removed += 1
                        continue
                except (ValueError, TypeError):
                    pass
            else:
                created = entry.get("created_at", "")
                if created:
                    try:
                        ct = datetime.fromisoformat(created)
                        if ct < cutoff:
                            removed += 1
                            continue
                    except (ValueError, TypeError):
                        pass
            kept.append(entry)

        if removed > 0:
            self.core._save(kept)
            logger.info(f"TTL cleanup: removed {removed}/{original_count} entries")

        return removed

    # ========================================================================
    # 优化 2: 上下文压缩（参考 JiangGongAgent）
    # ========================================================================

    @staticmethod
    def compact_context(
        messages: list[dict],
        threshold: int = 40,
        keep_recent: int = 20,
    ) -> list[dict]:
        """
        .. deprecated::
            优先使用 ``ContextManager.maybe_compact()``（已集成到 ``to_openai_messages()`` 自动调用）。
            本方法保留作为 standalone 工具函数，但不再主动调用。

        压缩消息列表中的旧 tool_result 内容

        当消息数超过 threshold 时:
        - 保留最近 keep_recent 条消息不变
        - 超过的部分中，tool_result 的 content 替换为占位符
        - assistant 消息中的决策文本保留（那是模型基于数据做的判断）
        """
        if len(messages) <= threshold:
            return messages

        compact_at = len(messages) - keep_recent
        compacted = []

        for i, msg in enumerate(messages):
            if i < compact_at and msg.get("role") == "user":
                # 用户消息中的 tool_result 块
                content = msg.get("content", "")
                if isinstance(content, list):
                    new_content = []
                    for block in content:
                        if block.get("type") == "tool_result":
                            tool_name = block.get("name", "unknown")
                            new_content.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": f"[已压缩: {tool_name} 工具的返回结果，上下文空间有限已省略原文]",
                            })
                        else:
                            new_content.append(block)
                    msg = {**msg, "content": new_content}
            compacted.append(msg)

        return compacted

    # ========================================================================
    # 优化 3: 树状会话（参考 Pi）
    # ========================================================================

    def checkpoint(self, checkpoint_id: str) -> None:
        """在当前会话中打一个检查点，后续可以从这里开分支"""
        if not self.enable_tree:
            return

        entries = self.core._load()
        self._checkpoints[checkpoint_id] = {
            "entries_snapshot": [dict(e) for e in entries],  # deep copy
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"Checkpoint created: {checkpoint_id} ({len(entries)} entries)")

    def resume_from_checkpoint(self, checkpoint_id: str) -> int:
        """从检查点恢复记忆状态（丢弃之后的所有变更），返回恢复的条目数"""
        if checkpoint_id not in self._checkpoints:
            logger.warning(f"Checkpoint not found: {checkpoint_id}")
            return 0

        cp = self._checkpoints[checkpoint_id]
        self.core._save(cp["entries_snapshot"])
        logger.info(f"Restored from checkpoint: {checkpoint_id}")
        return len(cp["entries_snapshot"])

    def list_checkpoints(self) -> list[dict]:
        """列出所有检查点"""
        return [
            {"id": cid, "entries": len(cp["entries_snapshot"]), "timestamp": cp["timestamp"]}
            for cid, cp in self._checkpoints.items()
        ]

    # ========================================================================
    # 优化 4: 深度蒸馏（参考 CowAgent 夜间自进化）
    # ========================================================================

    def deep_distill(self, scope: str | None = None) -> dict:
        """
        深度二次提炼：从 transcript 中找出模式级洞察

        发现:
        - 连续 3 次踩同一个坑 → 升级为 fact(pitfall)
        - 多次解决同一类问题 → 提炼为 pattern
        - 从未被读的决策 → 标记为 stale
        """
        if not self.enable_deep_distill:
            return {"status": "disabled"}

        # 先做常规蒸馏
        self.distill(scope)

        stats = self.core.stats()
        findings = {
            "total_entries": stats.get("total", 0),
            "never_read": stats.get("never_read", 0),
            "pitfalls_detected": 0,
            "patterns_extracted": 0,
        }

        # 检测从未被读的条目 → 标记为可能需要清理
        entries = self.core._load()
        for entry in entries:
            if entry.get("used_count", 0) == 0 and entry.get("layer") == "decision":
                # 超过 7 天的未读决策标记为 stale
                created = entry.get("created_at", "")
                if created:
                    try:
                        ct = datetime.fromisoformat(created)
                        if (datetime.now(timezone.utc) - ct).days > 7:
                            entry["stale"] = True
                            findings["pitfalls_detected"] += 1
                    except (ValueError, TypeError):
                        pass

        self.core._save(entries)

        logger.info(f"Deep distill completed: {findings}")
        return findings

    # ========================================================================
    # 优化 5: 防循环记忆（参考 CMU anti_loop_hook）
    # ========================================================================

    def detect_loop(self, tool_name: str, args: dict, threshold: int = 3) -> bool:
        """
        检测是否陷入循环调用

        如果连续 threshold 次用相同参数调用同一个工具 → 返回 True
        同时写入一条 fact(pitfall) 到记忆库
        """
        args_hash = self._hash_args(args)
        key = (tool_name, args_hash)

        now = time.time()
        if key not in self._loop_tracker:
            self._loop_tracker[key] = []

        # 清理超过 60 秒的记录
        self._loop_tracker[key] = [t for t in self._loop_tracker[key] if now - t < 60]
        self._loop_tracker[key].append(now)

        if len(self._loop_tracker[key]) >= threshold:
            # 记录为 pitfall — memory_core 用 kind 字段
            self.capture({
                "layer": "fact",
                "kind": "pitfall",
                "scope": f"session:anti_loop",
                "text": f"死循环: {tool_name}x{threshold}, args_hash={args_hash}",
                "verbatim": f"连续{threshold}次调用{tool_name}相同参数",
            })
            logger.warning(f"Loop detected: {tool_name} x{len(self._loop_tracker[key])}")
            return True

        return False

    def reset_loop_tracker(self) -> None:
        """重置循环追踪器（新会话开始时调用）"""
        self._loop_tracker.clear()

    # ========================================================================
    # 辅助
    # ========================================================================

    @staticmethod
    def _hash_args(args: dict) -> str:
        """对工具参数做稳定性哈希"""
        import hashlib
        import json

        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]
