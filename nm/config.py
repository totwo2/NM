"""
Harness 全局配置
从环境变量和配置文件加载，优先级: 环境变量 > config.json > 默认值
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import PermissionMode


# ============================================================================
# 默认配置
# ============================================================================

DEFAULT_SYSTEM_PROMPT = """你是一个智能办公助手，运行在企业内部 Agent Harness 上。
你可以通过工具调用来操作文件、执行命令、搜索内容、处理办公文档。

工作原则：
- 读写文件前先确认路径正确
- 执行 bash 命令时注意安全性
- 处理公文/文档时遵循模板格式
- 遇到不确定的情况主动说明而非猜测
- 每次完成任务后简要总结做了什么

{output_style_rules}
当前工作目录: {workspace_dir}
已加载的记忆: {memory_context}
"""

# Caveman 风格输出约束（参考 Caveman 8.8万星开源项目）
OUTPUT_STYLE_RULES = {
    "full": """
输出要求：简洁直接。
- 先给结论再解释，不要客套话（"好的""明白了""当然可以"等直接省略）
- 不重复用户已经说过的问题
- 能用列表的不用段落，能用短句的不用长句
- 代码、命令、文件内容原样保留不压缩
""",
    "ultra": """
输出要求：像发电报一样精简。
- 只输出核心结论和必要步骤
- 禁止任何客套话、过渡句、解释性铺垫
- 代码和命令原样保留，其余内容压缩到最短
""",
    "normal": "",
}



@dataclass
class HarnessConfig:
    """Harness 全局配置"""

    # --- 路径 ---
    workspace_dir: str = field(default_factory=os.getcwd)
    config_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), ".nm"))
    memory_store_path: str = ""
    transcript_path: str = ""
    usage_log_path: str = ""

    # --- Agent 循环 ---
    max_turns: int = 50                   # 单次对话最大轮数（0 = 不限）
    stream_output: bool = True            # 是否流式输出
    enable_anti_loop: bool = True         # 防循环检测
    anti_loop_threshold: int = 3          # 连续相同调用触发阈值

    # --- 上下文 ---
    context_compaction_threshold: int = 40  # 超过此消息数触发压缩
    context_keep_recent: int = 20           # 压缩后保留的最近消息数
    system_prompt_template: str = DEFAULT_SYSTEM_PROMPT

    # --- 权限 ---
    permission_mode: PermissionMode = PermissionMode.ASK
    web_mode: bool = False                  # Web 模式强制 auto 权限

    # --- 模型 ---
    default_model: str = "gpt-4o-mini"

    # --- 记忆 ---
    memory_ttl_days: int = 30               # 记忆条目过期天数（0 = 永不过期）
    enable_tree_sessions: bool = True       # 树状会话
    enable_deep_distill: bool = True        # 深度蒸馏
    deep_distill_hour: int = 2              # 深度蒸馏执行时间（凌晨 2 点）

    # --- 工具 ---
    tool_timeout_default: int = 60          # 工具默认超时秒数
    bash_timeout: int = 120                 # bash 工具超时
    allowed_workspace_paths: list[str] = field(default_factory=list)  # 允许操作的路径前缀

    # --- 输出压缩（Caveman 风格，参考 Caveman 8.8万星） ---
    output_style: str = "full"              # "normal" | "full" | "ultra"
    output_style_rules: str = ""            # 自动从 OUTPUT_STYLE_RULES 推导

    # --- 探测 ---
    model_discovery_configs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        # 自动派生路径
        if not self.memory_store_path:
            self.memory_store_path = os.path.join(self.config_dir, "memory_store.json")
        if not self.transcript_path:
            self.transcript_path = os.path.join(self.config_dir, "transcript.jsonl")
        if not self.usage_log_path:
            self.usage_log_path = os.path.join(self.config_dir, "usage_log.jsonl")
        # 确保配置目录存在
        Path(self.config_dir).mkdir(parents=True, exist_ok=True)
        # 自动推导输出风格
        self.output_style_rules = OUTPUT_STYLE_RULES.get(self.output_style, "")

    # ========================================================================
    # 加载与保存
    # ========================================================================

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        """从环境变量加载配置"""
        config = cls()

        # 路径
        if v := os.getenv("NM_WORKSPACE"):
            config.workspace_dir = v
        if v := os.getenv("NM_CONFIG_DIR"):
            config.config_dir = v

        # Agent 循环
        if v := os.getenv("NM_MAX_TURNS"):
            config.max_turns = int(v)
        if v := os.getenv("NM_STREAM"):
            config.stream_output = v.lower() == "true"

        # 权限
        if v := os.getenv("NM_PERMISSION"):
            try:
                config.permission_mode = PermissionMode(v)
            except ValueError:
                pass

        # 模型
        if v := os.getenv("NM_DEFAULT_MODEL"):
            config.default_model = v

        # 记忆
        if v := os.getenv("NM_MEMORY_TTL_DAYS"):
            config.memory_ttl_days = int(v)

        # 工具
        if v := os.getenv("NM_TOOL_TIMEOUT"):
            config.tool_timeout_default = int(v)

        return config

    @classmethod
    def from_file(cls, path: str) -> "HarnessConfig":
        """从 JSON 配置文件加载"""
        with open(path) as f:
            data = json.load(f)

        config = cls()
        for key, value in data.items():
            if hasattr(config, key):
                if key == "permission_mode":
                    value = PermissionMode(value)
                setattr(config, key, value)
        return config

    def to_file(self, path: str) -> None:
        """保存到 JSON 文件"""
        data = {}
        for key, value in self.__dict__.items():
            if isinstance(value, PermissionMode):
                value = value.value
            if key == "system_prompt_template":
                continue  # 太长，不存
            data[key] = value

        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_workspace_path(self, relative_path: str) -> str:
        """获取工作区内的绝对路径"""
        return os.path.join(self.workspace_dir, relative_path)
