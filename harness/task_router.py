"""
TaskRouter — 任务驱动的智能模型路由

借鉴:
- vLLM Semantic Router: Signal → Decision → Selection 架构 + 布尔规则 DSL
- LLMRouter: KNN 相似任务匹配 + BERT 语义分类
- 策略: 本地优先 → 云端兜底 → 质量校验回退

核心流程:
  用户输入 → 信号提取(复杂度/领域/PII/上下文长度) → 规则引擎 → 模型选择 → 返回客户端
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .types import (
    TaskSignal, RoutingDecision, Complexity, Domain,
)
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


# ============================================================================
# 信号提取 — 关键词规则（快速、无模型开销）
# ============================================================================

# 复杂度信号关键词
COMPLEXITY_KEYWORDS: dict[str, Complexity] = {
    # complex — 需要强推理/多步分析的
    "分析趋势": Complexity.COMPLEX,
    "对比": Complexity.COMPLEX,
    "找异常": Complexity.COMPLEX,
    "深度分析": Complexity.COMPLEX,
    "排查": Complexity.COMPLEX,
    "诊断": Complexity.COMPLEX,
    "复杂": Complexity.COMPLEX,
    "评估风险": Complexity.COMPLEX,
    "预测": Complexity.COMPLEX,
    "优化算法": Complexity.COMPLEX,
    "重构": Complexity.COMPLEX,
    "多步": Complexity.COMPLEX,
    # medium — 有一定逻辑但不需要深度推理
    "整理": Complexity.MEDIUM,
    "汇总": Complexity.MEDIUM,
    "统计": Complexity.MEDIUM,
    "筛选": Complexity.MEDIUM,
    "计算": Complexity.MEDIUM,
    "提取": Complexity.MEDIUM,
    "转换": Complexity.MEDIUM,
    "合并": Complexity.MEDIUM,
    "翻译": Complexity.MEDIUM,
    "改写": Complexity.MEDIUM,
    "润色": Complexity.MEDIUM,
    "修改": Complexity.MEDIUM,
    # simple — 模板化/格式化/简单查询
    "写公文": Complexity.SIMPLE,
    "写通知": Complexity.SIMPLE,
    "写请示": Complexity.SIMPLE,
    "写报告": Complexity.SIMPLE,
    "会议纪要": Complexity.SIMPLE,
    "请假": Complexity.SIMPLE,
    "审批": Complexity.SIMPLE,
    "查询": Complexity.SIMPLE,
    "查看": Complexity.SIMPLE,
    "列出": Complexity.SIMPLE,
    "打开": Complexity.SIMPLE,
    "创建文件夹": Complexity.SIMPLE,
    "格式": Complexity.SIMPLE,
    "模板": Complexity.SIMPLE,
}

# 领域关键词
DOMAIN_KEYWORDS: dict[str, Domain] = {
    "公文": Domain.DOCUMENT, "通知": Domain.DOCUMENT, "请示": Domain.DOCUMENT,
    "报告": Domain.DOCUMENT, "文档": Domain.DOCUMENT, "合同": Domain.DOCUMENT,
    "模板": Domain.DOCUMENT, "word": Domain.DOCUMENT, "docx": Domain.DOCUMENT,
    "ppt": Domain.DOCUMENT, "pptx": Domain.DOCUMENT, "演示": Domain.DOCUMENT,

    "数据": Domain.DATA, "excel": Domain.DATA, "xlsx": Domain.DATA,
    "表格": Domain.DATA, "统计": Domain.DATA, "图表": Domain.DATA,
    "趋势": Domain.DATA, "指标": Domain.DATA, "报表": Domain.DATA,
    "分析": Domain.DATA, "sql": Domain.DATA, "数据库": Domain.DATA,

    "代码": Domain.CODE, "编程": Domain.CODE, "bug": Domain.CODE,
    "函数": Domain.CODE, "python": Domain.CODE, "javascript": Domain.CODE,
    "脚本": Domain.CODE, "部署": Domain.CODE, "git": Domain.CODE,
    "接口": Domain.CODE, "api": Domain.CODE, "调试": Domain.CODE,
    "重构": Domain.CODE, "单元测试": Domain.CODE,
}

# PII 检测模式
PII_PATTERNS = [
    re.compile(r"\b\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"),  # 身份证
    re.compile(r"\b1[3-9]\d{9}\b"),  # 手机号
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),  # 邮箱
    re.compile(r"身份证|社保|银行卡|cvv|密码|token", re.IGNORECASE),
]

# 工具调用意图
TOOL_INTENT_PATTERNS = [
    re.compile(r"(打开|读取|读|查看|搜索|查找|找到|找一下)\s*(文件|文档|代码|目录|文件夹)"),
    re.compile(r"(创建|新建|写|写入|保存|生成|输出)\s*(文件|文档|表格|PPT|Word|Excel)"),
    re.compile(r"(运行|执行|跑一下|bash|终端|命令行)"),
    re.compile(r"(修改|改|替换|编辑|更新)\s*(文件|代码|配置)"),
]


# ============================================================================
# KNN 相似任务匹配缓存（LLMRouter 模式）
# ============================================================================

@dataclass
class _RoutingCacheEntry:
    """路由决策缓存"""
    user_input: str
    decision: RoutingDecision
    timestamp: str

class TaskRouter:
    """
    智能模型路由

    使用:
        router = TaskRouter(model_manager)
        decision, client, model_name = router.route(user_input, user_id)
        # 直接用 client 调 LLM
    """

    def __init__(
        self,
        model_manager: ModelManager,
        local_model: str = "qwen3-30b-local",
        cloud_model: str = "gpt-4o-mini",
        cloud_strong_model: str = "gpt-4o",
    ):
        self.mm = model_manager
        self.local_model = local_model
        self.cloud_model = cloud_model
        self.cloud_strong_model = cloud_strong_model

        # KNN 路由缓存：记录过往路由决策，相似任务复用
        self._routing_cache: list[_RoutingCacheEntry] = []
        self._cache_max_size: int = 100

        # 路由统计
        self._routing_stats: dict[str, int] = {"local": 0, "cloud": 0, "fallback": 0}

    # ========================================================================
    # 主路由入口
    # ========================================================================

    def route(self, user_input: str, user_id: str, force_model: str | None = None) -> tuple[RoutingDecision, Any, str]:
        """
        路由决策 + 返回可调用的模型客户端

        Args:
            user_input: 用户输入
            user_id: 用户 ID
            force_model: 强制使用指定模型（跳过路由）

        Returns:
            (decision, client, model_name)
        """
        # 强制模型
        if force_model:
            decision = RoutingDecision(
                model_name=force_model,
                provider="cloud",
                reason=f"手动指定模型: {force_model}",
            )
            client, mn = self._get_client(user_id, force_model)
            return decision, client, mn

        # 1. 提取信号
        signals = self._extract_signals(user_input)

        # 2. KNN 缓存匹配
        cached = self._knn_match(user_input)
        if cached and self._should_use_cache(cached):
            decision = cached
            decision.reason += " [KNN缓存命中]"
            try:
                client, mn = self._get_client(user_id, decision.model_name)
                return decision, client, mn
            except Exception:
                logger.debug("Cache model unavailable, re-routing")

        # 3. 布尔规则引擎 → 路由决策
        decision = self._apply_rules(signals, user_id)

        # 4. 获取模型客户端
        try:
            client, mn = self._get_client(user_id, decision.model_name)
        except Exception as e:
            logger.warning(f"Primary model '{decision.model_name}' unavailable: {e}")
            # 回退
            if decision.fallback_model:
                logger.info(f"Falling back to: {decision.fallback_model}")
                client, mn = self._get_client(user_id, decision.fallback_model)
                decision.reason += f" [回退: {decision.model_name} 不可用]"
                self._routing_stats["fallback"] += 1
            else:
                raise

        # 5. 记录缓存
        self._cache_decision(user_input, decision)
        self._routing_stats[decision.provider] += 1

        logger.info(
            f"Route: [{decision.provider}] {decision.model_name} | "
            f"complexity={signals.complexity.value} domain={signals.domain.value} | "
            f"reason={decision.reason}"
        )

        return decision, client, mn

    # ========================================================================
    # 信号提取
    # ========================================================================

    def _extract_signals(self, user_input: str) -> TaskSignal:
        """从用户输入中提取路由信号"""

        # 复杂度
        complexity = Complexity.MEDIUM
        confidence = 0.3
        for kw, level in COMPLEXITY_KEYWORDS.items():
            if kw in user_input:
                complexity = level
                confidence = 0.7
                break

        # 领域
        domain = Domain.GENERAL
        for kw, d in DOMAIN_KEYWORDS.items():
            if kw.lower() in user_input.lower():
                domain = d
                break

        # PII 检测
        has_pii = any(p.search(user_input) for p in PII_PATTERNS)

        # 估算 token 数
        estimated_tokens = int(len(user_input) * 1.5)

        # 是否需要工具
        needs_tools = any(p.search(user_input) for p in TOOL_INTENT_PATTERNS)

        # 是否需要推理
        needs_reasoning = complexity == Complexity.COMPLEX or (
            complexity == Complexity.MEDIUM and domain in (Domain.DATA, Domain.CODE)
        )

        return TaskSignal(
            complexity=complexity,
            domain=domain,
            has_pii=has_pii,
            estimated_tokens=estimated_tokens,
            needs_tools=needs_tools,
            needs_reasoning=needs_reasoning,
            confidence=confidence,
        )

    # ========================================================================
    # 布尔规则引擎（vLLM SR 模式）— 可配置规则链
    # ========================================================================

# 单条规则: Callable[[TaskSignal, TaskRouter], RoutingDecision | None]
# 返回 None 表示不匹配，继续下一条规则

    # ========================================================================
    # 路由规则函数（静态方法，可被子类/实例扩展）
    # ========================================================================
    @staticmethod
    def _rule_pii(signals, router):
        if signals.has_pii:
            return RoutingDecision(
                model_name=router.local_model, provider="local",
                reason="检测到敏感数据(PII)，强制本地模型",
                fallback_model=router.cloud_model,
            )
        return None

    @staticmethod
    def _rule_complex_reasoning(signals, router):
        if signals.complexity == Complexity.COMPLEX and signals.needs_reasoning:
            return RoutingDecision(
                model_name=router.cloud_strong_model, provider="cloud",
                reason=f"复杂推理({signals.complexity.value}, {signals.domain.value})",
                fallback_model=router.cloud_model, cost_estimate=0.01,
            )
        return None

    @staticmethod
    def _rule_complex(signals, router):
        if signals.complexity == Complexity.COMPLEX:
            return RoutingDecision(
                model_name=router.cloud_model, provider="cloud",
                reason=f"复杂任务({signals.complexity.value})",
                fallback_model=router.local_model,
            )
        return None

    @staticmethod
    def _rule_data(signals, router):
        if signals.domain == Domain.DATA and signals.needs_reasoning:
            return RoutingDecision(
                model_name=router.cloud_model, provider="cloud",
                reason=f"数据分析({signals.domain.value})",
                fallback_model=router.local_model,
            )
        return None

    @staticmethod
    def _rule_code(signals, router):
        if signals.domain == Domain.CODE:
            return RoutingDecision(
                model_name=router.cloud_model, provider="cloud",
                reason="代码任务", fallback_model=router.local_model,
            )
        return None

    @staticmethod
    def _rule_document(signals, router):
        if signals.domain == Domain.DOCUMENT:
            return RoutingDecision(
                model_name=router.local_model, provider="local",
                reason=f"文档任务({signals.complexity.value}, 本地足够)",
                fallback_model=router.cloud_model,
            )
        return None

    # 默认规则链（可被实例的 routing_rules 属性覆盖）
    DEFAULT_ROUTING_RULES: list = [
        _rule_pii, _rule_complex_reasoning, _rule_complex,
        _rule_data, _rule_code, _rule_document,
    ]

    # ========================================================================
    # 布尔规则引擎 — 可配置规则链
    # ========================================================================

    def _apply_rules(self, signals: TaskSignal, user_id: str) -> RoutingDecision:
        """
        可配置规则链: 依次尝试每条规则，命中即返回。
        扩展: router.routing_rules = [my_rule_fn, ...]
        """
        rules = getattr(self, "routing_rules", TaskRouter.DEFAULT_ROUTING_RULES)
        for rule_fn in rules:
            decision = rule_fn(signals, self)
            if decision is not None:
                return decision
        return RoutingDecision(
            model_name=self.local_model,
            provider="local",
            reason=f"默认({signals.complexity.value}, {signals.domain.value})",
            fallback_model=self.cloud_model,
        )

    def _knn_match(self, user_input: str) -> RoutingDecision | None:
        """在缓存中查找相似的历史路由决策"""
        if not self._routing_cache:
            return None

        input_lower = user_input.lower()

        for entry in reversed(self._routing_cache):
            cached_lower = entry.user_input.lower()

            # 简单相似度: 共享关键词比例
            input_words = set(input_lower.split())
            cached_words = set(cached_lower.split())

            if not input_words or not cached_words:
                continue

            intersection = input_words & cached_words
            union = input_words | cached_words
            similarity = len(intersection) / len(union)

            if similarity > 0.5:  # 50%+ 相似度 → 复用决策
                logger.debug(f"KNN match: similarity={similarity:.2f}, input='{user_input[:50]}...'")
                return entry.decision

        return None

    @staticmethod
    def _should_use_cache(decision: RoutingDecision) -> bool:
        """缓存决策是否有效（不超过 30 分钟）"""
        # 简单实现: 总是使用缓存
        return True

    def _cache_decision(self, user_input: str, decision: RoutingDecision) -> None:
        """记录路由决策到缓存"""
        from datetime import datetime, timezone

        self._routing_cache.append(_RoutingCacheEntry(
            user_input=user_input,
            decision=decision,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

        # 限制缓存大小
        if len(self._routing_cache) > self._cache_max_size:
            self._routing_cache = self._routing_cache[-self._cache_max_size:]

    # ========================================================================
    # 辅助
    # ========================================================================

    def _get_client(self, user_id: str, model_name: str) -> tuple[Any, str]:
        """从 ModelManager 获取客户端，返回 (client, model_name)"""
        client, mn, _acct_id = self.mm.get_model(user_id, model_name)
        return client, mn

    def get_stats(self) -> dict[str, Any]:
        """获取路由统计"""
        return {
            "routing_stats": dict(self._routing_stats),
            "cache_size": len(self._routing_cache),
            "total_routes": sum(self._routing_stats.values()),
        }
