"""N.M 一次性兼容迁移层。旧标识只允许在此包中出现。"""

from .legacy_data import migrate_legacy_data
from .legacy_env import get_env_with_legacy_fallback

__all__ = ["migrate_legacy_data", "get_env_with_legacy_fallback"]
