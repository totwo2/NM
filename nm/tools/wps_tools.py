"""
WPS Skill 交互桥接
用于日常交互场景：用户打开文件 → WPS 窗口 → 所见即所得编辑
与模板填充不同，这里是"帮用户操作 WPS GUI"
"""

from typing import Any


def create_wps_tools(workspace_dir: str) -> list[dict[str, Any]]:
    """
    创建 WPS 交互工具。
    这些工具对接 tencent-local-office-edit skill 的能力。
    适用场景：用户说"帮我打开这个文档看看"或"帮我把第三段改一下字体"。
    """

    def _open_in_wps(path: str) -> str:
        """在 WPS 中打开文件（通过 editor_sdk）"""
        # tencent-local-office-edit skill 通过 editor_sdk 操作本地 WPS
        # 这里作为桥接层，实际打开由 skill 执行
        return (
            f"[WPS 桥接] 请求打开文件: {path}\n"
            "请通过 tencent-local-office-edit skill 在 WPS 中打开此文件进行交互式编辑。"
        )

    def _save_in_wps(path: str) -> str:
        """在 WPS 中保存当前文件"""
        return f"[WPS 桥接] 请求保存文件: {path}"

    return [
        {
            "name": "open_in_wps",
            "description": (
                "在本地 WPS/Office 中打开文件进行交互式编辑。"
                "适用于需要人工查看、手动调整格式的场景。"
                "注意：此工具仅发出打开请求，实际编辑由用户在 WPS 窗口中完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要打开的文件路径"},
                },
                "required": ["path"],
            },
            "fn": _open_in_wps,
            "requires_approval": True,
            "timeout_seconds": 5,
        },
        {
            "name": "save_in_wps",
            "description": "保存当前在 WPS 中打开的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
            "fn": _save_in_wps,
            "requires_approval": False,
            "timeout_seconds": 5,
        },
    ]
