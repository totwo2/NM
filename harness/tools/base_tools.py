"""
基础工具: read_file, write_file, edit_file, bash, search
参考 Pi 的极简设计（4 工具），加 search 用于企业知识库
"""

import os
import re
import subprocess
from typing import Any


def create_base_tools(workspace_dir: str) -> list[dict[str, Any]]:
    """创建基础工具定义列表，返回 {name, description, parameters, fn, ...}"""

    # ========================================================================
    # read_file
    # ========================================================================
    def _read_file(path: str, offset: int = 0, limit: int = 500) -> str:
        """读取文件内容，支持分页"""
        full_path = _resolve_path(workspace_dir, path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total = len(lines)
            if offset >= total:
                return f"文件共 {total} 行，offset={offset} 已超出范围"

            end = min(offset + limit, total)
            result_lines = lines[offset:end]
            result = "".join(result_lines)

            header = f"[文件: {path}] [行 {offset+1}-{end} / 共 {total} 行]\n"
            return header + result
        except FileNotFoundError:
            return f"错误: 文件不存在: {path}"
        except UnicodeDecodeError:
            return f"错误: 无法以 UTF-8 编码读取: {path}"
        except PermissionError:
            return f"错误: 无权限读取: {path}"

    # ========================================================================
    # write_file
    # ========================================================================
    def _write_file(path: str, content: str) -> str:
        """创建或覆盖写入文件"""
        full_path = _resolve_path(workspace_dir, path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        line_count = content.count("\n") + 1
        return f"已写入: {path} ({line_count} 行, {len(content)} 字符)"

    # ========================================================================
    # edit_file
    # ========================================================================
    def _edit_file(path: str, old_string: str, new_string: str) -> str:
        """精确替换文件中的文本（仅替换第一次出现）"""
        full_path = _resolve_path(workspace_dir, path)

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return f"错误: 文件不存在: {path}"

        if old_string not in content:
            return f"错误: 未找到要替换的文本。文件: {path}"

        count = content.count(old_string)
        new_content = content.replace(old_string, new_string, 1)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"已替换: {path} (共找到 {count} 处匹配，替换了第 1 处)"

    # ========================================================================
    # bash
    # ========================================================================
    # 危险命令模式（参考 JiangGongAgent）
    DANGEROUS_PATTERNS = [
        (re.compile(r"rm\s+-rf\s+[/~]"), "rm -rf 根目录/家目录"),
        (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb"),
        (re.compile(r"dd\s+if="), "磁盘覆写"),
        (re.compile(r"mkfs\."), "格式化文件系统"),
        (re.compile(r">\s*/dev/[sh]d"), "写入磁盘设备"),
        (re.compile(r"chmod\s+-R\s+777\s+/"), "全局权限修改"),
        (re.compile(r"curl.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    ]

    def _bash(command: str, timeout: int = 120) -> str:
        """执行 shell 命令"""
        # 安全检查
        for pattern, desc in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return f"[安全拦截] 检测到危险命令 ({desc}): {command}"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workspace_dir,
            )

            output = []
            if result.stdout:
                output.append(result.stdout.strip())
            if result.stderr:
                output.append(f"[stderr]\n{result.stderr.strip()}")

            response = "\n".join(output) if output else "(无输出)"
            if result.returncode != 0:
                response += f"\n[退出码: {result.returncode}]"

            # 截断过长输出
            if len(response) > 8000:
                response = response[:8000] + f"\n...[输出已截断，共 {len(response)} 字符]"

            return response

        except subprocess.TimeoutExpired:
            return f"命令超时 ({timeout}s): {command}"
        except Exception as e:
            return f"命令执行失败: {e}"

    # ========================================================================
    # search
    # ========================================================================
    def _search(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
        """在文件中搜索内容（基于 ripgrep）"""
        full_path = _resolve_path(workspace_dir, path)
        if not os.path.isdir(full_path) and not os.path.isfile(full_path):
            return f"路径不存在: {path}"

        cmd = ["rg", "--line-number", "--max-count=50", pattern]
        if os.path.isdir(full_path):
            cmd.extend(["--glob", file_pattern, full_path])
        else:
            cmd.append(full_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 1:
                return f"未找到匹配 '{pattern}' 的内容"
            if result.returncode > 1:
                return f"搜索错误: {result.stderr.strip()}"

            output = result.stdout.strip()
            if len(output) > 6000:
                output = output[:6000] + "\n...[结果已截断]"
            return output

        except FileNotFoundError:
            # rg 不可用，回退到 grep
            return _search_fallback(pattern, full_path, file_pattern)
        except subprocess.TimeoutExpired:
            return "搜索超时 (30s)"
        except Exception as e:
            return f"搜索失败: {e}"

    def _search_fallback(pattern: str, path: str, file_pattern: str) -> str:
        """grep 回退搜索"""
        import fnmatch

        results = []
        try:
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for fname in files:
                        if fnmatch.fnmatch(fname, file_pattern):
                            fpath = os.path.join(root, fname)
                            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                                for i, line in enumerate(f, 1):
                                    if pattern in line:
                                        results.append(f"{fpath}:{i}:{line.rstrip()}")
                                        if len(results) >= 50:
                                            break
                            if len(results) >= 50:
                                break
                    if len(results) >= 50:
                        break
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if pattern in line:
                            results.append(f"{path}:{i}:{line.rstrip()}")
                            if len(results) >= 50:
                                break

            return "\n".join(results) if results else f"未找到匹配 '{pattern}' 的内容"
        except Exception as e:
            return f"搜索失败: {e}"

    return [
        {
            "name": "read_file",
            "description": "读取文件内容。支持指定起始行和读取行数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于工作目录或绝对路径）"},
                    "offset": {"type": "integer", "description": "起始行号（从 0 开始）", "default": 0},
                    "limit": {"type": "integer", "description": "读取行数", "default": 500},
                },
                "required": ["path"],
            },
            "fn": _read_file,
            "requires_approval": False,
            "timeout_seconds": 10,
        },
        {
            "name": "write_file",
            "description": "创建或覆盖写入文件。会自动创建不存在的目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
            "fn": _write_file,
            "requires_approval": True,
            "timeout_seconds": 30,
        },
        {
            "name": "edit_file",
            "description": "精确替换文件中的文本（仅替换第一次出现）。old_string 必须精确匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old_string": {"type": "string", "description": "要替换的原文本（需精确匹配）"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            "fn": _edit_file,
            "requires_approval": True,
            "timeout_seconds": 10,
        },
        {
            "name": "bash",
            "description": (
                "执行 shell 命令。内置危险命令拦截（rm -rf /, fork bomb, 磁盘覆写等）。"
                "用于运行脚本、安装依赖、git 操作等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数", "default": 120},
                },
                "required": ["command"],
            },
            "fn": _bash,
            "requires_approval": True,
            "timeout_seconds": 120,
        },
        {
            "name": "search_content",
            "description": "在文件中搜索指定内容。支持正则表达式和文件类型过滤。优先使用 rg (ripgrep)，不可用时回退到 grep。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索关键词或正则表达式"},
                    "path": {"type": "string", "description": "搜索路径（文件或目录）", "default": "."},
                    "file_pattern": {"type": "string", "description": "文件类型过滤，如 *.py, *.md", "default": "*"},
                },
                "required": ["pattern"],
            },
            "fn": _search,
            "requires_approval": False,
            "timeout_seconds": 30,
        },
    ]


def _resolve_path(workspace_dir: str, path: str) -> str:
    """解析路径，确保在工作区内（含 symlink 解析）"""
    # 先解析 symlink，防止工作区内软链指向工作区外
    real_workspace = os.path.realpath(workspace_dir)
    if os.path.isabs(path):
        abs_path = os.path.realpath(os.path.abspath(path))
        if not abs_path.startswith(real_workspace + os.sep) and abs_path != real_workspace:
            return os.path.join(workspace_dir, os.path.basename(path))
        return abs_path
    joined = os.path.join(workspace_dir, path)
    return os.path.realpath(joined) if os.path.islink(joined) else joined
