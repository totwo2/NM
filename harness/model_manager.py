"""
ModelManager — 企业级模型管理层
账号池 · 分组配额 · 用量追踪 · 费用报表
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import Account, Group, ProviderConfig, UsageRecord

logger = logging.getLogger(__name__)


class ModelManager:
    """
    企业级模型管理

    功能:
    - 注册/管理多个 API 账号（本地 + 云端）
    - 部门和用户组分配
    - 日/月配额控制
    - 每次调用的用量追踪
    - 费用统计与报表

    使用:
        mm = ModelManager()
        mm.add_account("acct_01", "openai", api_key, ["gpt-4o", "gpt-4o-mini"])
        mm.add_group("finance", "财务部", ["acct_01"], quota_monthly=500_000)
        mm.assign_user_to_group("zhangsan", "finance")

        # AgentLoop 调用
        client, model_name, acct_id = mm.get_model("zhangsan")
        response = client.chat.completions.create(...)
        mm.track_usage("zhangsan", acct_id, model_name, response.usage, ...)
    """

    def __init__(self, config_path: str | None = None):
        self._providers: dict[str, ProviderConfig] = {}  # provider_name → config
        self._accounts: dict[str, Account] = {}
        self._groups: dict[str, Group] = {}
        self._user_groups: dict[str, str] = {}  # user_id → group_id
        self._usage_log: list[UsageRecord] = []

        # OpenAI clients cache: account_id → {model_name: client}
        self._clients: dict[str, dict[str, Any]] = {}

        self._config_path = config_path
        self._lock = threading.RLock()

        if config_path and os.path.exists(config_path):
            self._load_config()

    # ========================================================================
    # Provider 管理
    # ========================================================================

    def register_provider(
        self,
        name: str,
        provider_type: str,  # "local" | "cloud"
        base_url: str,
        api_key: str = "",
        models: list[str] | None = None,
    ) -> None:
        """注册一个模型供应商"""
        self._providers[name] = ProviderConfig(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            models=models or [],
        )
        logger.info(f"Provider registered: {name} ({provider_type}) -> {base_url}")

    # ========================================================================
    # 账号管理
    # ========================================================================

    def add_account(
        self,
        account_id: str,
        provider_name: str,
        api_key: str,
        model_names: list[str] | None = None,
    ) -> Account:
        """添加一个 API 账号"""
        if provider_name not in self._providers:
            raise ValueError(f"Provider not found: {provider_name}")

        provider = self._providers[provider_name]
        models = model_names or provider.models

        account = Account(
            id=account_id,
            provider=provider_name,
            api_key=api_key,
        )

        self._accounts[account_id] = account
        self._init_clients(account_id, provider, api_key, models)

        logger.info(f"Account added: {account_id} ({provider_name}) models={models}")
        return account

    def remove_account(self, account_id: str) -> None:
        self._accounts.pop(account_id, None)
        self._clients.pop(account_id, None)

    def list_accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    # ========================================================================
    # 用户组管理
    # ========================================================================

    def add_group(
        self,
        group_id: str,
        name: str,
        account_ids: list[str] | None = None,
        quota_daily_tokens: int = 0,
        quota_monthly_tokens: int = 0,
        default_model: str = "",
    ) -> Group:
        """创建一个用户组"""
        group = Group(
            id=group_id,
            name=name,
            assigned_accounts=account_ids or [],
            quota_daily_tokens=quota_daily_tokens,
            quota_monthly_tokens=quota_monthly_tokens,
            default_model=default_model,
        )
        self._groups[group_id] = group
        return group

    def assign_user_to_group(self, user_id: str, group_id: str) -> None:
        """将用户分配到组"""
        if group_id not in self._groups:
            raise ValueError(f"Group not found: {group_id}")
        self._user_groups[user_id] = group_id

    def assign_account_to_group(self, account_id: str, group_id: str) -> None:
        """将账号分配到组"""
        if group_id not in self._groups:
            raise ValueError(f"Group not found: {group_id}")
        if account_id not in self._accounts:
            raise ValueError(f"Account not found: {account_id}")
        if account_id not in self._groups[group_id].assigned_accounts:
            self._groups[group_id].assigned_accounts.append(account_id)

    def get_user_group(self, user_id: str) -> Group | None:
        gid = self._user_groups.get(user_id)
        return self._groups.get(gid) if gid else None

    # ========================================================================
    # 模型获取（AgentLoop 调用的核心接口）
    # ========================================================================

    def get_model(self, user_id: str, model_name: str | None = None) -> tuple[Any, str, str]:
        """
        获取可用于调用的模型客户端

        Returns:
            (client, model_name, account_id)

        逻辑:
        1. 查用户所在组
        2. 从组的账号池中选一个活跃账号
        3. 返回 OpenAI client
        """
        group = self.get_user_group(user_id)
        if not group:
            raise ValueError(f"User '{user_id}' not assigned to any group")

        model = model_name or group.default_model
        if not model:
            raise ValueError(f"No model specified for user '{user_id}'")

        # 从组的可用账号中选
        for acct_id in group.assigned_accounts:
            account = self._accounts.get(acct_id)
            if not account or not account.is_active:
                continue

            # 检查配额
            if not self._check_quota(user_id, group):
                logger.warning(f"Quota exceeded for user '{user_id}' in group '{group.name}'")
                continue

            # 查找客户端
            if acct_id in self._clients and model in self._clients[acct_id]:
                return self._clients[acct_id][model], model, acct_id

        raise ValueError(
            f"No available model for user '{user_id}', model='{model}'. "
            f"Available accounts: {group.assigned_accounts}"
        )

    def get_model_by_account(self, account_id: str, model_name: str) -> tuple[Any, str]:
        """直接通过账号 ID 获取模型"""
        if account_id in self._clients and model_name in self._clients[account_id]:
            return self._clients[account_id][model_name], model_name
        raise ValueError(f"Model '{model_name}' not found for account '{account_id}'")

    def list_available_models(self, user_id: str) -> list[str]:
        """列出用户可用的所有模型"""
        group = self.get_user_group(user_id)
        if not group:
            return []

        models = set()
        for acct_id in group.assigned_accounts:
            if acct_id in self._clients:
                models.update(self._clients[acct_id].keys())
        return sorted(models)

    # ========================================================================
    # 用量追踪
    # ========================================================================

    def track_usage(
        self,
        user_id: str,
        account_id: str,
        model_name: str,
        usage: dict[str, int] | Any,
        task_complexity: str = "",
        task_domain: str = "",
        routing_reason: str = "",
    ) -> UsageRecord:
        """记录一次模型调用"""
        tokens_in = 0
        tokens_out = 0

        # 处理 usage 对象
        if isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0)
        elif hasattr(usage, "prompt_tokens"):
            tokens_in = usage.prompt_tokens
            tokens_out = usage.completion_tokens

        cost = self._estimate_cost(model_name, tokens_in, tokens_out)

        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_id=user_id,
            account_id=account_id,
            model_name=model_name,
            task_complexity=task_complexity,
            task_domain=task_domain,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            routing_reason=routing_reason,
        )

        with self._lock:
            self._usage_log.append(record)

            # 更新账号累计用量
            account = self._accounts.get(account_id)
            if account:
                account.total_tokens_used += tokens_in + tokens_out
                account.cost_accumulated += cost
                account.last_used_at = record.timestamp

        return record

    def get_usage_report(
        self,
        group_id: str | None = None,
        user_id: str | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        """生成用量报表"""
        cutoff = datetime.now(timezone.utc).isoformat()

        filtered = self._usage_log

        if group_id:
            # 找到该组所有用户
            group_users = [uid for uid, gid in self._user_groups.items() if gid == group_id]
            filtered = [r for r in filtered if r.user_id in group_users]

        if user_id:
            filtered = [r for r in filtered if r.user_id == user_id]

        # 统计
        total_tokens = sum(r.tokens_in + r.tokens_out for r in filtered)
        total_cost = sum(r.cost for r in filtered)
        by_model = {}
        for r in filtered:
            if r.model_name not in by_model:
                by_model[r.model_name] = {"tokens": 0, "cost": 0.0, "calls": 0}
            by_model[r.model_name]["tokens"] += r.tokens_in + r.tokens_out
            by_model[r.model_name]["cost"] += r.cost
            by_model[r.model_name]["calls"] += 1

        return {
            "period_days": days,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "total_calls": len(filtered),
            "by_model": by_model,
            "records": [r.__dict__ for r in filtered[-100:]],  # 最近 100 条
        }

    def save_usage_log(self, path: str | None = None) -> None:
        """持久化用量日志到 JSONL"""
        path = path or self._config_path or "usage_log.jsonl"
        if not path:
            return

        with open(path, "w") as f:
            for record in self._usage_log:
                f.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")

    # ========================================================================
    # 配额控制
    # ========================================================================

    def _check_quota(self, user_id: str, group: Group) -> bool:
        """检查用户/组配额是否耗尽"""
        if group.quota_daily_tokens <= 0 and group.quota_monthly_tokens <= 0:
            return True  # 不限配额

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        this_month = datetime.now(timezone.utc).strftime("%Y-%m")

        daily_usage = sum(
            r.tokens_in + r.tokens_out
            for r in self._usage_log
            if r.user_id == user_id and r.timestamp.startswith(today)
        )

        monthly_usage = sum(
            r.tokens_in + r.tokens_out
            for r in self._usage_log
            if r.user_id == user_id and r.timestamp.startswith(this_month)
        )

        if group.quota_daily_tokens > 0 and daily_usage >= group.quota_daily_tokens:
            return False
        if group.quota_monthly_tokens > 0 and monthly_usage >= group.quota_monthly_tokens:
            return False

        return True

    # ========================================================================
    # 费用估算
    # ========================================================================

    @staticmethod
    def _estimate_cost(model_name: str, tokens_in: int, tokens_out: int) -> float:
        """粗略估算费用（USD）"""
        # 价格参考（2026 年大致价格，每 1M token）
        PRICES: dict[str, tuple[float, float]] = {
            # (input_price, output_price) per 1M tokens
            "gpt-4o": (2.50, 10.00),
            "gpt-4o-mini": (0.15, 0.60),
            "gpt-4-turbo": (10.00, 30.00),
            "claude-4": (3.00, 15.00),
            "claude-3.5-sonnet": (3.00, 15.00),
            "claude-3.5-haiku": (0.80, 4.00),
            "deepseek-chat": (0.14, 0.28),
            "qwen-turbo": (0.07, 0.14),
        }

        # 模糊匹配
        for key, (in_price, out_price) in PRICES.items():
            if key in model_name.lower():
                return (tokens_in * in_price + tokens_out * out_price) / 1_000_000

        # 本地模型 → 免费
        if "local" in model_name.lower() or "qwen" in model_name.lower() or "llama" in model_name.lower():
            return 0.0

        # 默认价格
        return (tokens_in * 1.0 + tokens_out * 4.0) / 1_000_000

    # ========================================================================
    # 初始化 / 持久化
    # ========================================================================

    def _init_clients(self, account_id: str, provider: ProviderConfig, api_key: str, models: list[str]) -> None:
        """初始化 OpenAI clients"""
        from openai import OpenAI

        self._clients[account_id] = {}

        for model in models:
            self._clients[account_id][model] = OpenAI(
                base_url=provider.base_url,
                api_key=api_key or provider.api_key,
            )
            logger.debug(f"Client initialized: {account_id}/{model}")

    def _load_config(self) -> None:
        """从 JSON 配置文件加载"""
        if not self._config_path or not os.path.exists(self._config_path):
            return

        with open(self._config_path) as f:
            data = json.load(f)

        # 恢复 provider
        for p in data.get("providers", []):
            self.register_provider(**p)

        # 恢复 account
        for a in data.get("accounts", []):
            self.add_account(**a)

        # 恢复 group
        for g in data.get("groups", []):
            self.add_group(**g)

        # 恢复 user-group 映射
        for ug in data.get("user_groups", []):
            self.assign_user_to_group(ug["user_id"], ug["group_id"])

    def save_config(self, path: str | None = None) -> None:
        """保存配置到 JSON"""
        path = path or self._config_path
        if not path:
            return

        with self._lock:
            data = {
                "providers": [p.__dict__ for p in self._providers.values()],
                "accounts": [
                    {
                        "account_id": a.id,
                        "provider_name": a.provider,
                        "api_key": a.api_key,
                        "model_names": list(self._clients.get(a.id, {}).keys()),
                    }
                    for a in self._accounts.values()
                ],
                "groups": [
                    {
                        "group_id": g.id,
                        "name": g.name,
                        "account_ids": g.assigned_accounts,
                        "quota_daily_tokens": g.quota_daily_tokens,
                        "quota_monthly_tokens": g.quota_monthly_tokens,
                        "default_model": g.default_model,
                    }
                    for g in self._groups.values()
                ],
                "user_groups": [
                    {"user_id": uid, "group_id": gid}
                    for uid, gid in self._user_groups.items()
                ],
            }

            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
