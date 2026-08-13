"""
Harness CLI — 交互式命令行入口
"""

import json
import os
import readline  # noqa: F401 — enables line editing
import sys
from pathlib import Path

from .config import HarnessConfig
from .agent_loop import AgentLoop
from .context_manager import ContextManager
from .permission_checker import PermissionChecker
from .task_router import TaskRouter
from .tool_registry import ToolRegistry
from .memory.memory_adapter import MemoryAdapter
from .model_manager import ModelManager
from .types import PermissionMode


def _build_harness(
    workspace_dir: str | None = None,
    permission_mode: str = "ask",
    config_file: str | None = None,
) -> AgentLoop:
    """构建完整的 Harness 实例"""

    # 配置
    if config_file and os.path.exists(config_file):
        config = HarnessConfig.from_file(config_file)
    else:
        config = HarnessConfig.from_env()

    if workspace_dir:
        config.workspace_dir = workspace_dir
    if permission_mode:
        config.permission_mode = PermissionMode(permission_mode)

    # ModelManager
    model_manager = ModelManager(config_file or ".nm/model_config.json")
    # 如果没有配置，尝试从环境变量读
    if not model_manager._providers:
        _setup_from_env(model_manager, config)

    # Memory
    memory = MemoryAdapter(
        store_path=config.memory_store_path,
        transcript_path=config.transcript_path,
        ttl_days=config.memory_ttl_days,
        enable_tree=config.enable_tree_sessions,
        enable_deep_distill=config.enable_deep_distill,
    )

    # Tools
    tool_registry = ToolRegistry.create_default(config.workspace_dir)

    # Context
    context_manager = ContextManager(
        system_prompt_template=config.system_prompt_template,
        workspace_dir=config.workspace_dir,
        compaction_threshold=config.context_compaction_threshold,
        keep_recent=config.context_keep_recent,
    )

    # Permission
    def _ask_callback(tool_name: str, description: str) -> bool:
        answer = input(f"\n  [确认] 执行 {tool_name}({description})? [y/N]: ")
        return answer.strip().lower() == "y"

    permission_checker = PermissionChecker(
        mode=config.permission_mode,
        web_mode=config.web_mode,
        ask_callback=_ask_callback,
    )

    # TaskRouter
    task_router = TaskRouter(model_manager)

    # AgentLoop
    def _on_text(text: str):
        sys.stdout.write(text)
        sys.stdout.flush()

    def _on_tool_call(name: str, args: dict):
        print(f"\n  [工具] {name}({json.dumps(args, ensure_ascii=False)[:100]})")

    return AgentLoop(
        config=config,
        memory=memory,
        task_router=task_router,
        tool_registry=tool_registry,
        context_manager=context_manager,
        permission_checker=permission_checker,
        on_text=_on_text,
        on_tool_call=_on_tool_call,
    )


def _setup_from_env(model_manager: ModelManager, config: HarnessConfig) -> None:
    """从环境变量自动配置 ModelManager"""
    # OpenAI
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if openai_key:
        model_manager.register_provider("openai", "cloud", openai_base, openai_key,
                                         ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"])
        model_manager.add_account("openai_main", "openai", openai_key)
        model_manager.add_group("default", "默认组", ["openai_main"], default_model="gpt-4o-mini")
        model_manager.assign_user_to_group("default", "default")

    # 本地模型（通过 OPENAI_BASE_URL 指向本地 vLLM/Ollama 等）
    local_base = os.getenv("LOCAL_LLM_BASE_URL", "")
    local_models_str = os.getenv("LOCAL_LLM_MODELS", "qwen3-30b-local,qwen2.5-7b-local")
    if local_base:
        model_manager.register_provider(
            "local", "local", local_base, "not-needed",
            local_models_str.split(","),
        )
        model_manager.add_account("local_main", "local", "not-needed", local_models_str.split(","))
        group = model_manager.get_user_group("default")
        if group:
            group.assigned_accounts.append("local_main")


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="N.M - 企业智能办公助手")
    parser.add_argument("--workspace", "-w", help="工作目录", default=os.getcwd())
    parser.add_argument("--permission", "-p", choices=["auto", "ask", "strict"],
                        default="ask", help="权限模式（默认: ask）")
    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument("--oneshot", "-o", help="单次执行（非交互模式）")
    parser.add_argument("--model", "-m", help="强制使用指定模型")
    args = parser.parse_args()

    print("=" * 60)
    print("  N.M")
    print("  企业智能办公助手")
    print("=" * 60)
    print(f"  工作目录: {args.workspace}")
    print(f"  权限模式: {args.permission}")
    print(f"  工具数量: {len(ToolRegistry.create_default(args.workspace).list_all())}")
    print()

    # 启动时自动迁移旧数据（legacy 目录 → .nm）
    try:
        from nm.migrations import migrate_legacy_data
        mres = migrate_legacy_data(args.workspace)
        if mres.get("status") == "migrated":
            print(f"  旧数据已迁移: {mres.get('files')} 个文件 → .nm")
    except Exception as e:
        print(f"  (迁移跳过: {e})")

    harness = _build_harness(
        workspace_dir=args.workspace,
        permission_mode=args.permission,
        config_file=args.config,
    )

    # 单次执行模式
    if args.oneshot:
        print(f"> {args.oneshot}\n")
        response = nm.run(args.oneshot, force_model=args.model)
        print(f"\n{response}")
        return

    # 交互模式
    user_id = os.getenv("USER", "default")
    print("输入 /help 查看命令，输入 /exit 退出\n")

    while True:
        try:
            user_input = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not user_input:
            continue

        if user_input == "/exit":
            print("再见!")
            break

        if user_input == "/help":
            print("""
  命令:
    /exit         退出
    /help         显示此帮助
    /stats        显示记忆统计
    /usage        显示用量统计
    /model <name> 临时切换模型
    /clear        清空当前会话上下文
    /checkpoints  列出检查点
    /checkpoint   创建检查点
            """)
            continue

        if user_input == "/stats":
            stats = nm.memory.stats()
            print(json.dumps(stats, indent=2, ensure_ascii=False))
            continue

        if user_input == "/clear":
            nm.context.clear()
            print("上下文已清空")
            continue

        if user_input == "/checkpoints":
            cps = nm.memory.list_checkpoints()
            if cps:
                for cp in cps:
                    print(f"  {cp['id']} ({cp['entries']} entries, {cp['timestamp'][:19]})")
            else:
                print("  无检查点")
            continue

        if user_input.startswith("/checkpoint"):
            parts = user_input.split(maxsplit=1)
            cpid = parts[1] if len(parts) > 1 else f"cp_{int(__import__('time').time())}"
            nm.memory.checkpoint(cpid)
            print(f"检查点已创建: {cpid}")
            continue

        if user_input.startswith("/model "):
            force_model = user_input.split(maxsplit=1)[1]
            print(f"临时切换模型: {force_model}")
            # 下一轮使用 force_model
            response = nm.run(
                input(f">>> [模型已切换为 {force_model}] 请输入: ").strip(),
                user_id=user_id,
                force_model=force_model,
            )
            continue

        if user_input == "/usage":
            report = nm.task_router.mm.get_usage_report(user_id=user_id)
            print(f"  总调用: {report['total_calls']} 次")
            print(f"  总 Token: {report['total_tokens']}")
            print(f"  总费用: ¥{report['total_cost']:.2f}")
            for model, data in report.get("by_model", {}).items():
                print(f"    {model}: {data['calls']} 次, {data['tokens']} tokens, ¥{data['cost']:.4f}")
            continue

        # 正常对话
        print()
        response = nm.run(user_input, user_id=user_id, force_model=args.model)
        print(f"\n")


if __name__ == "__main__":
    main()
