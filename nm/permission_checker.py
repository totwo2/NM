"""
权限检查器
auto / ask / strict 三级权限控制 + 危险命令检测
"""

import logging
import re
from typing import Callable

from .types import PermissionMode, PermissionDeniedError

logger = logging.getLogger(__name__)


# ============================================================================
# 危险命令模式（与 base_tools.py 保持一致）
# ============================================================================

DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"rm\s+-rf\s+[/~]"), "rm -rf 删除根目录/家目录"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb 攻击"),
    (re.compile(r"dd\s+if="), "磁盘覆写操作"),
    (re.compile(r"mkfs\."), "格式化文件系统"),
    (re.compile(r">\s*/dev/[sh]d"), "写入磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "全局权限修改"),
    (re.compile(r"curl.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget.*-O-.*\|"), "下载并执行远程脚本"),
    (re.compile(r"git\s+push\s+--force"), "强制推送（可能覆盖远程仓库）"),
    (re.compile(r"DROP\s+(TABLE|DATABASE)", re.IGNORECASE), "删除数据库表/库"),
]

# 文件操作危险路径
DANGEROUS_PATHS = [
    "/etc/", "/System/", "/Library/", "~/.ssh/", "~/.aws/", "~/.config/",
    "C:\\Windows\\", "C:\\Program Files\\",
]


class PermissionChecker:
    """
    三级权限控制:

    - auto:   自动执行所有操作（Web 模式强制此模式）
    - ask:    执行前询问用户（CLI 默认）
    - strict: 拒绝所有危险操作，只允许读文件/搜索

    危险操作检测:
    - 危险 shell 命令（rm -rf /, fork bomb, 磁盘覆写...）
    - 危险文件路径（/etc/, ~/.ssh/, C:\\Windows\\...）
    """

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.ASK,
        web_mode: bool = False,
        ask_callback: Callable[[str, str], bool] | None = None,
    ):
        """
        Args:
            mode: 权限模式
            web_mode: 是否 Web 模式（强制 auto）
            ask_callback: ask 模式下的用户回调，返回 True=允许，False=拒绝
        """
        self.mode = mode if not web_mode else PermissionMode.AUTO
        self.ask_callback = ask_callback

    # ========================================================================
    # 主检查入口
    # ========================================================================

    def check(self, tool_name: str, arguments: dict | None = None) -> bool:
        """
        检查是否允许执行工具

        返回 True=允许, False=需要询问, 抛出 PermissionDeniedError=拒绝
        """
        args = arguments or {}

        # strict 模式：只允许只读操作
        if self.mode == PermissionMode.STRICT:
            if not self._is_read_only(tool_name, args):
                raise PermissionDeniedError(
                    tool_name,
                    f"当前为 strict 模式，禁止执行 '{tool_name}'。仅允许: read_file, search_content, read_docx, read_xlsx",
                )

        # 检查危险操作（所有模式都检查）
        danger = self._check_dangerous(tool_name, args)
        if danger:
            raise PermissionDeniedError(tool_name, danger)

        # auto 模式：直接允许
        if self.mode == PermissionMode.AUTO:
            return True

        # strict 模式：通过检查则允许
        if self.mode == PermissionMode.STRICT:
            return True

        # ask 模式：需要确认
        return self._ask_user(tool_name, args)

    def check_bash(self, command: str) -> bool:
        """专门检查 bash 命令"""
        # 检测危险命令
        for pattern, desc in DANGEROUS_PATTERNS:
            if pattern.search(command):
                raise PermissionDeniedError("bash", f"检测到危险命令 ({desc}): {command[:100]}")

        # ask 模式下需要确认
        if self.mode == PermissionMode.ASK:
            return self._ask_user("bash", {"command": command})

        return True

    # ========================================================================
    # 内部检测方法
    # ========================================================================

    def _check_dangerous(self, tool_name: str, args: dict) -> str | None:
        """检查是否为危险操作，返回危险描述或 None"""
        # bash 命令检测
        if tool_name == "bash" and "command" in args:
            cmd = args["command"]
            for pattern, desc in DANGEROUS_PATTERNS:
                if pattern.search(cmd):
                    return f"危险命令 ({desc}): {cmd[:100]}"

        # write_file / edit_file 路径检测
        if tool_name in ("write_file", "edit_file") and "path" in args:
            path = args["path"]
            for dp in DANGEROUS_PATHS:
                if path.startswith(dp) or dp in path:
                    return f"危险路径 ({dp}): {path}"

        return None

    def _is_read_only(self, tool_name: str, args: dict) -> bool:
        """判断是否为只读操作"""
        readonly_tools = {
            "read_file", "search_content", "read_docx", "read_xlsx",
            "list_checkpoints", "open_in_wps",
        }
        return tool_name in readonly_tools

    def _ask_user(self, tool_name: str, args: dict) -> bool:
        """向用户确认操作"""
        if self.ask_callback:
            description = f"{tool_name}({_format_args(args)})"
            return self.ask_callback(tool_name, description)

        # 没有回调，默认拒绝（避免静默执行）
        logger.warning(f"Ask mode but no callback, denying: {tool_name}")
        raise PermissionDeniedError(tool_name, "需要用户确认（无回调函数）")


def _format_args(args: dict | None) -> str:
    """格式化参数用于显示"""
    if not args:
        return ""
    items = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:60] + "..."
        items.append(f"{k}={s}")
    return ", ".join(items)
