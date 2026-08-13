"""旧 HARNESS_* 环境变量的一次性兼容回退。"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("nm.migrations.legacy_env")


def get_env_with_legacy_fallback(name: str, *, default: str | None = None) -> str | None:
    """优先读取 NM_*；仅在缺失时回退到对应 HARNESS_* 并记录弃用警告。"""
    value = os.getenv(name)
    if value is not None:
        return value
    if name.startswith("NM_"):
        legacy_name = "HARNESS_" + name[3:]
        legacy_value = os.getenv(legacy_name)
        if legacy_value is not None:
            logger.warning("Environment variable %s is deprecated; use %s", legacy_name, name)
            return legacy_value
    return default
