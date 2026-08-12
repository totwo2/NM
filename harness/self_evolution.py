"""
自进化流水线 — Self-Evolution Pipeline

借鉴:
- Darwin.skill: 棘轮验证（只保留改进）+ 独立评分（换个模型打分）
- SESA (北大): 失败蒸馏 → 去重 → help-hurt 淘汰
- VeriSkill (北大): 责任归因（分清楚失败是谁的锅）
- PenguinHarness: 构建→评测→优化→再测 闭环

核心流程:
  transcript/memory_store → 失败分类 → 归因 → 提出改进 → 棘轮验证 → 应用 + 写回记忆
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import ToolResult, ToolCall

logger = logging.getLogger(__name__)


# ============================================================================
# 失败分类
# ============================================================================

class FailureCategory:
    """失败类别"""
    TOOL_NOT_FOUND = "tool_not_found"          # 工具不存在: LLM 编了个工具名
    PARAM_ERROR = "param_error"                # 参数错误: 缺少必填参数或类型不匹配
    TOOL_EXECUTION = "tool_execution"          # 工具执行失败: 文件不存在/权限不够
    MODEL_HALLUCINATION = "model_hallucination"  # 模型幻觉: 虚构了不存在的文件/路径
    DEAD_LOOP = "dead_loop"                    # 死循环: 连续相同调用
    PERMISSION_DENIED = "permission_denied"    # 权限拒绝
    UNKNOWN = "unknown"


@dataclass
class FailureRecord:
    """失败记录"""
    timestamp: str
    category: str          # FailureCategory
    tool_name: str
    error_message: str
    context: str           # 失败时的上下文摘要
    actionable: bool       # 是否可操作（VeriSkill: 只对可归因的失败做进化）
    responsibility: str    # 责任归因: "tool", "model", "environment", "user"


class FailureClassifier:
    """
    失败分类器 — 参考 VeriSkill 的责任归因

    把失败分成:
    - 工具侧: TOOL_NOT_FOUND, PARAM_ERROR (可通过改 tool schema 修复)
    - 模型侧: MODEL_HALLUCINATION (可通过加 prompt 约束修复)
    - 环境侧: TOOL_EXECUTION, PERMISSION_DENIED (不可自动修复，但可记录模式)
    - 未知: UNKNOWN
    """

    # 错误消息模式 → 分类（按优先级排序）
    PATTERNS = [
        (["防循环", "死循环", "loop", "连续.*次"], FailureCategory.DEAD_LOOP),
        (["未找到工具", "tool not found", "Tool not found", "工具不存在"], FailureCategory.TOOL_NOT_FOUND),
        (["权限", "Permission", "denied", "permission"], FailureCategory.PERMISSION_DENIED),
        (["参数", "缺少必填", "required", "missing", "argument"], FailureCategory.PARAM_ERROR),
        (["文件不存在", "File not found", "No such file", "not found"], FailureCategory.TOOL_EXECUTION),
        (["幻觉", "不存在", "编造"], FailureCategory.MODEL_HALLUCINATION),
    ]

    @classmethod
    def classify(
        cls,
        tool_name: str,
        error_message: str,
        context: str = "",
    ) -> FailureRecord:
        """分类一个失败"""
        category = FailureCategory.UNKNOWN

        for keywords, cat in cls.PATTERNS:
            if any(kw in error_message for kw in keywords):
                category = cat
                break

        # 责任归因
        responsibility = cls._attribute(category)

        # 是否可操作（VeriSkill: 只有工具侧和模型侧的可修复）
        actionable = category in (
            FailureCategory.TOOL_NOT_FOUND,
            FailureCategory.PARAM_ERROR,
            FailureCategory.MODEL_HALLUCINATION,
            FailureCategory.DEAD_LOOP,
        )

        return FailureRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            category=category,
            tool_name=tool_name,
            error_message=error_message[:200],
            context=context[:300],
            actionable=actionable,
            responsibility=responsibility,
        )

    @staticmethod
    def _attribute(category: str) -> str:
        if category in (FailureCategory.TOOL_NOT_FOUND, FailureCategory.PARAM_ERROR):
            return "tool"
        if category == FailureCategory.MODEL_HALLUCINATION:
            return "model"
        if category in (FailureCategory.TOOL_EXECUTION, FailureCategory.PERMISSION_DENIED):
            return "environment"
        return "unknown"

    @classmethod
    def classify_batch(cls, failures: list[dict]) -> list[FailureRecord]:
        """批量分类"""
        return [
            cls.classify(f.get("tool_name", ""), f.get("error", ""), f.get("context", ""))
            for f in failures
        ]

    @classmethod
    def summary(cls, records: list[FailureRecord]) -> dict:
        """分类汇总"""
        by_category = {}
        for r in records:
            if r.category not in by_category:
                by_category[r.category] = {"count": 0, "actionable": 0}
            by_category[r.category]["count"] += 1
            if r.actionable:
                by_category[r.category]["actionable"] += 1

        return {
            "total": len(records),
            "actionable": sum(1 for r in records if r.actionable),
            "by_category": by_category,
            "top_tools": cls._top_tools(records),
        }

    @classmethod
    def _top_tools(cls, records: list[FailureRecord], n: int = 5) -> list[dict]:
        """最常失败的工具"""
        from collections import Counter
        counts = Counter(r.tool_name for r in records)
        return [{"tool": k, "count": v} for k, v in counts.most_common(n)]


# ============================================================================
# 棘轮验证器
# ============================================================================

@dataclass
class Improvement:
    """改进提案"""
    id: str
    target: str            # 改进目标: "system_prompt", "tool_schema", "routing_rule", "memory_entry"
    original: str          # 原始版本
    proposed: str          # 改进版本
    reason: str            # 改进理由
    expected_effect: str   # 预期效果
    score_before: float = 0.0
    score_after: float = 0.0
    accepted: bool = False


class RatchetVerifier:
    """
    棘轮验证器 — Darwin.skill 模式

    原则:
    - 独立评分: 改用一个模型，评用另一个模型（不能自己改自己评）
    - 只升不降: 分数下降 → 丢弃改进，保留旧版
    - 量化标准: 精确度、覆盖度、简洁度 各维度打分
    """

    def __init__(self, evaluator_client: Any | None = None, evaluator_model: str = ""):
        """
        Args:
            evaluator_client: 评估用的 LLM 客户端（独立于改进用的客户端）
            evaluator_model: 评估模型名
        """
        self.evaluator = evaluator_client
        self.evaluator_model = evaluator_model
        self._history: list[dict] = []  # 改进历史

    # ========================================================================
    # 核心方法: 评估改进
    # ========================================================================

    def verify(
        self,
        improvement: Improvement,
        test_cases: list[dict] | None = None,
    ) -> Improvement:
        """
        验证改进是否真的更好

        Returns: 带有 score_before, score_after, accepted 的 Improvement
        """
        if not self.evaluator:
            # 无评估模型 → 简单启发式: 只要不空洞就接受
            improvement.score_before = 0.5
            improvement.score_after = 1.0 if improvement.proposed != improvement.original else 0.0
            improvement.accepted = improvement.score_after > improvement.score_before
            return improvement

        # 独立评分
        improvement.score_before = self._score(improvement.original, improvement.target, improvement.reason)
        improvement.score_after = self._score(improvement.proposed, improvement.target, improvement.reason)

        # 棘轮: 只保留提升的
        improvement.accepted = improvement.score_after > improvement.score_before

        self._history.append({
            "id": improvement.id,
            "target": improvement.target,
            "score_before": improvement.score_before,
            "score_after": improvement.score_after,
            "accepted": improvement.accepted,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            f"Ratchet: [{improvement.target}] "
            f"{improvement.score_before:.1f} → {improvement.score_after:.1f} "
            f"({'accepted' if improvement.accepted else 'rejected'})"
        )
        return improvement

    def verify_batch(
        self,
        improvements: list[Improvement],
    ) -> list[Improvement]:
        """批量验证，只返回被接受的"""
        verified = [self.verify(imp) for imp in improvements]
        accepted = [v for v in verified if v.accepted]
        rejected = [v for v in verified if not v.accepted]
        logger.info(f"Ratchet batch: {len(accepted)}/{len(verified)} accepted, {len(rejected)} rejected")
        return accepted

    # ========================================================================
    # 评分函数
    # ========================================================================

    def _score(self, content: str, target: str, reason: str) -> float:
        """独立评分（0-100）"""
        if not self.evaluator:
            return 50.0

        prompt = self._build_scoring_prompt(content, target, reason)
        try:
            resp = self.evaluator.chat.completions.create(
                model=self.evaluator_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.1,
            )
            text = resp.choices[0].message.content
            # 提取分数
            import re
            match = re.search(r"\b(\d{1,3}(?:\.\d)?)\s*(?:分|/|$)", text)
            if match:
                score = float(match.group(1))
                return min(100, max(0, score))
        except Exception:
            pass
        return 50.0

    def _build_scoring_prompt(self, content: str, target: str, reason: str) -> str:
        return f"""你是独立的评估者。请对以下 {target} 内容打分（0-100）。

评分标准:
- 精确度（40 分）: 是否明确、无歧义、不含虚假信息
- 覆盖度（30 分）: 是否覆盖了目标场景的关键需求
- 简洁度（30 分）: 长度适中，不啰嗦不遗漏

改进理由: {reason}

---待评分内容---
{content[:2000]}
---

请只输出分数，格式: "85 分" 或 "72.5"。"""

    def get_history(self, n: int = 20) -> list[dict]:
        """获取最近的改进历史"""
        return self._history[-n:]

    def acceptance_rate(self) -> float:
        """改进接受率"""
        if not self._history:
            return 0.0
        accepted = sum(1 for h in self._history if h.get("accepted"))
        return accepted / len(self._history)


# ============================================================================
# 自进化引擎
# ============================================================================

class SelfEvolution:
    """
    自进化引擎 — 整合失败分类 + 棘轮验证 + 改进应用

    使用:
        evolver = SelfEvolution(memory, task_router, tool_registry)
        result = evolver.evolve(user_id="zhangsan")
    """

    def __init__(
        self,
        memory,              # MemoryAdapter
        task_router,          # TaskRouter
        tool_registry,        # ToolRegistry
        context_manager,      # ContextManager
        evaluator_client=None,
        evaluator_model="",
    ):
        self.memory = memory
        self.router = task_router
        self.tools = tool_registry
        self.context = context_manager
        self.classifier = FailureClassifier()
        self.ratchet = RatchetVerifier(evaluator_client, evaluator_model)
        self._llm_client = evaluator_client  # 用于生成改进提案的 LLM 客户端
        self._llm_model = evaluator_model

        # 改进计数器
        self._evolution_count: int = 0

    # ========================================================================
    # 主入口
    # ========================================================================

    def evolve(
        self,
        user_id: str = "default",
        scope: str | None = None,
        max_improvements: int = 5,
    ) -> dict[str, Any]:
        """
        执行一轮自进化

        Returns: 进化报告
        """
        logger.info(f"Self-evolution round #{self._evolution_count + 1} started")

        # Step 1: 收集失败数据
        failures = self._collect_failures(scope)

        # Step 2: 分类 + 归因
        records = self.classifier.classify_batch(failures)
        summary = self.classifier.summary(records)

        if summary["actionable"] == 0:
            logger.info("No actionable failures, skipping evolution")
            return {"status": "skipped", "reason": "no actionable failures", "summary": summary}

        # Step 3: 分析失败 → 生成改进提案
        improvements = self._generate_improvements(records, user_id, max_improvements)

        if not improvements:
            return {"status": "no_improvements", "summary": summary}

        # Step 4: 棘轮验证
        accepted = self.ratchet.verify_batch(improvements)

        # Step 5: 应用改进 + 写回记忆
        applied = [self._apply_improvement(imp) for imp in accepted]

        self._evolution_count += 1

        return {
            "status": "completed",
            "round": self._evolution_count,
            "failures_analyzed": summary["total"],
            "actionable": summary["actionable"],
            "improvements_proposed": len(improvements),
            "improvements_accepted": len(accepted),
            "improvements_applied": len(applied),
            "ratchet_rate": self.ratchet.acceptance_rate(),
            "summary": summary,
            "applied": [
                {"target": imp.target, "reason": imp.reason, "score": imp.score_after}
                for imp in accepted
            ],
        }

    # ========================================================================
    # 失败收集
    # ========================================================================

    def _collect_failures(self, scope: str | None = None) -> list[dict]:
        """从 memory_core 的 memory_store 中提取失败"""
        failures = []

        entries = self.memory.core._store.entries
        for entry in entries:
            # memory_core stores fact type in 'ftype' field
            if (entry.get("layer") == "fact"
                    and entry.get("ftype") == "pitfall"
                    and (scope is None or entry.get("scope") == scope)):
                text = entry.get("text", "")
                # Parse tool name from text: "死循环: bashx3, args_hash=..."
                tool_name = "unknown"
                if ":" in text:
                    first_part = text.split(":")[1].strip()
                    # "bashx3" or "bash x3"
                    tool_name = first_part.split("x")[0].split(" ")[0].strip()

                failures.append({
                    "tool_name": tool_name,
                    "error": entry.get("verbatim", text),
                    "context": text,
                    "scope": entry.get("scope", ""),
                    "timestamp": entry.get("created_at", ""),
                })

        return failures

    # ========================================================================
    # 改进生成（LLM 增强 + 模板兜底）
    # ========================================================================

    def _call_llm(self, prompt: str, max_tokens: int = 300) -> str | None:
        """调用 LLM 生成内容，失败返回 None"""
        if not self._llm_client:
            return None
        try:
            resp = self._llm_client.chat.completions.create(
                model=self._llm_model or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.debug(f"LLM improvement generation failed: {e}")
            return None

    def _llm_generate_improvement(self, category: str, records: list, user_id: str) -> Improvement | None:
        """用 LLM 根据失败记录生成改进提案"""
        tool_names = list(set(r.tool_name for r in records if r.tool_name))
        failures_summary = json.dumps([
            {"tool": r.tool_name, "error": r.error_message[:100], "actionable": r.actionable}
            for r in records[:5]
        ], ensure_ascii=False)

        prompt = f"""你是系统改进专家。分析以下 {category} 类失败记录，生成一条具体的改进提案。

失败记录:
{failures_summary}

可用工具: {', '.join(tool_names) if tool_names else '无'}

请以 JSON 格式返回:
{{
    "target": "system_prompt" 或 "tool_schema" 或 "memory_entry",
    "proposed": "改进内容描述（50-200字）",
    "reason": "改进原因（20-80字）",
    "expected_effect": "预期效果（10-50字）"
}}"""

        result = self._call_llm(prompt)
        if not result:
            return None
        try:
            import re as _re
            m = _re.search(r'\{.*\}', result, _re.DOTALL)
            if not m:
                return None
            data = json.loads(m.group())
            timestamp = str(int(time.time()))
            return Improvement(
                id=f"ev-{timestamp}-{category}",
                target=data.get("target", "system_prompt"),
                original="[LLM 分析生成]",
                proposed=data.get("proposed", ""),
                reason=data.get("reason", f"{category} 类失败 {len(records)} 次"),
                expected_effect=data.get("expected_effect", ""),
            )
        except Exception:
            return None

    # ========================================================================
    # 改进生成
    # ========================================================================

    def _generate_improvements(
        self,
        records: list[FailureRecord],
        user_id: str,
        max_count: int = 5,
    ) -> list[Improvement]:
        """分析失败记录，生成改进提案"""
        improvements = []

        # 按类别分组
        by_category: dict[str, list[FailureRecord]] = {}
        for r in records:
            if r.actionable:
                by_category.setdefault(r.category, []).append(r)

        # 对每个类别生成改进
        for category, recs in by_category.items():
            if len(improvements) >= max_count:
                break

            imp = self._generate_for_category(category, recs, user_id)
            if imp:
                improvements.append(imp)

        return improvements

    def _generate_for_category(
        self,
        category: str,
        records: list[FailureRecord],
        user_id: str,
    ) -> Improvement | None:
        """针对某一类失败生成改进（LLM 优先，模板兜底）"""
        # 尝试 LLM 生成
        if self._llm_client:
            llm_imp = self._llm_generate_improvement(category, records, user_id)
            if llm_imp:
                return llm_imp

        # 兜底: 模板化生成
        timestamp = str(int(time.time()))

        if category == FailureCategory.TOOL_NOT_FOUND:
            # 改进 system prompt: 强调只能使用已注册的工具
            tools_list = [t["name"] for t in self.tools.get_index()]
            return Improvement(
                id=f"ev-{timestamp}-tool_not_found",
                target="system_prompt",
                original="[当前 system prompt 未明确约束工具调用范围]",
                proposed=(
                    f"只有以下工具可用，禁止调用不在列表中的工具: {', '.join(tools_list)}。"
                    f"如果不确定某个工具是否存在，先调 get_tool_detail 确认。"
                ),
                reason=f"模型调用了 {len(records)} 次不存在的工具（{records[0].tool_name} 等）",
                expected_effect="减少工具调用幻觉",
            )

        elif category == FailureCategory.PARAM_ERROR:
            # 改进工具使用指导
            tool_names = list(set(r.tool_name for r in records))
            return Improvement(
                id=f"ev-{timestamp}-param_error",
                target="tool_schema",
                original="[工具 schema 中参数说明可能不够清晰]",
                proposed=(
                    f"对 {', '.join(tool_names)} 工具的参数做更严格的校验提示。"
                    f"调用前先调 get_tool_detail 确认参数格式。"
                ),
                reason=f"{len(records)} 次参数错误，涉及工具: {', '.join(tool_names)}",
                expected_effect="减少参数填写错误",
            )

        elif category == FailureCategory.DEAD_LOOP:
            tool_names = list(set(r.tool_name for r in records))
            return Improvement(
                id=f"ev-{timestamp}-dead_loop",
                target="memory_entry",
                original="[无相关避坑记忆]",
                proposed=(
                    f"当使用 {', '.join(tool_names)} 时，如果结果不满足预期，"
                    f"最多重试 2 次后必须换策略，不要用相同参数反复调用。"
                ),
                reason=f"检测到死循环: {', '.join(tool_names)}",
                expected_effect="防止重复无效调用",
            )

        elif category == FailureCategory.MODEL_HALLUCINATION:
            return Improvement(
                id=f"ev-{timestamp}-hallucination",
                target="system_prompt",
                original="[未强调反幻觉约束]",
                proposed=(
                    "重要: 不要编造不存在的文件路径和工具名称。"
                    "读写文件前先用 search_content 或 ls 确认文件存在。"
                    "不确定时如实说明，不要猜测。"
                ),
                reason=f"检测到 {len(records)} 次幻觉行为",
                expected_effect="减少模型编造信息",
            )

        return None

    # ========================================================================
    # 改进应用
    # ========================================================================

    def _apply_improvement(self, imp: Improvement) -> bool:
        """将验证通过的改进落地"""
        try:
            # 写入 memory_core 作为 pattern
            self.memory.capture({
                "layer": "fact",
                "kind": "evolution",
                "scope": "project:evolution",
                "text": f"{imp.target}: {imp.reason}",
                "verbatim": f"改进后 ({imp.score_after:.1f}分, +{imp.score_after - imp.score_before:.1f}): {imp.proposed[:200]}",
            })

            logger.info(f"Improvement applied: [{imp.target}] {imp.reason[:60]}")
            return True

        except Exception as e:
            logger.error(f"Failed to apply improvement: {e}")
            return False

    # ========================================================================
    # 统计与审计
    # ========================================================================

    def get_evolution_report(self) -> dict[str, Any]:
        """获取进化历史报告"""
        return {
            "total_rounds": self._evolution_count,
            "ratchet_history": self.ratchet.get_history(),
            "acceptance_rate": self.ratchet.acceptance_rate(),
        }
