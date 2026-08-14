"""
tests/test_context_manager.py — ContextManager 压缩测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from nm.context_manager import ContextManager
from nm.types import Message, ContentBlock


def make_manager(threshold=40, keep_recent=20):
    return ContextManager(
        system_prompt_template="You are a helpful assistant.",
        workspace_dir="/tmp/test_ws",
        compaction_threshold=threshold,
        keep_recent=keep_recent,
    )


def add_user_with_tool_result(mgr, text, tool_name="read_file"):
    msg = Message(
        role="user",
        content=[
            ContentBlock(type="text", text=text),
            ContentBlock(type="tool_result", tool_name=tool_name, text="x" * 200),
        ]
    )
    mgr.messages.append(msg)


class TestCompaction:
    def test_no_compact_under_threshold(self):
        mgr = make_manager(threshold=40, keep_recent=20)
        for i in range(30):
            mgr.add_user(f"msg {i}")
        assert mgr.maybe_compact() is False

    def test_compacts_over_threshold(self):
        mgr = make_manager(threshold=10, keep_recent=5)
        for i in range(15):
            add_user_with_tool_result(mgr, f"msg {i}")
        result = mgr.maybe_compact()
        assert result is True, f"Expected compaction, got {result}, messages={len(mgr.messages)}"

    def test_keeps_recent_intact(self):
        mgr = make_manager(threshold=10, keep_recent=3)
        for i in range(12):
            mgr.add_user(f"keep_{i}")
        mgr.maybe_compact()
        for i in range(3):
            msg_text = mgr.messages[-(3 - i)].content[0].text
            assert f"keep_{12 - 3 + i}" in msg_text

    def test_threshold_configurable(self):
        mgr = make_manager(threshold=5, keep_recent=2)
        assert mgr.compaction_threshold == 5
        assert mgr.keep_recent == 2

    def test_clear_preserves_memory_context(self):
        """AgentLoop 的 run() 顺序是 clear → set_memory，
        若 clear 清掉 _memory_context 会导致回灌记忆丢失。
        本测试锁定"clear 只清 messages、保留记忆"的语义。
        """
        mgr = ContextManager(
            "You are a helpful assistant.\n已加载的记忆: {memory_context}",
            "/tmp/test_ws",
        )
        mgr.set_memory("【重要记忆】Q3 营收增长 15%")
        mgr.clear()
        sys_msg = mgr.build_system()
        content = sys_msg.content[0].text if isinstance(sys_msg.content, list) else sys_msg.content
        assert "Q3 营收增长" in content, "clear() 不应清掉回灌的记忆"

    def test_compaction_callback_receives_original(self):
        """压缩时 on_compaction 回调收到被压掉的 tool_result 原文
        （中间层 → 记忆体的事件通道）
        """
        sunk: list[dict] = []
        mgr = ContextManager(
            "模板",
            "/tmp/test_ws",
            compaction_threshold=3,
            keep_recent=2,
            on_compaction=lambda items: sunk.extend(items),
        )
        for i in range(5):
            mgr.messages.append(Message(
                role="tool",
                content=[ContentBlock(type="tool_result", text="y" * 100, tool_id=f"t{i}", tool_name="read_file")],
            ))
        assert mgr.maybe_compact() is True
        assert len(sunk) == 3
        assert all(len(item["original"]) == 100 for item in sunk)

    def test_compression_archive_persists(self):
        """CCR 存档落盘，重启（新实例）后可恢复原文"""
        import tempfile
        tmp = tempfile.mkdtemp()
        mgr = ContextManager("模板", tmp, compaction_threshold=2, keep_recent=1)
        for i in range(3):
            mgr.messages.append(Message(role="tool", content=[ContentBlock(type="tool_result", text="z" * 50, tool_id=f"tk{i}", tool_name="bash")]))
        assert mgr.maybe_compact() is True
        assert os.path.exists(os.path.join(tmp, ".nm", "compression_archive.json"))
        # 新实例从磁盘恢复
        mgr2 = ContextManager("模板", tmp, compaction_threshold=2, keep_recent=1)
        assert mgr2.retrieve_original("tk0") == "z" * 50
