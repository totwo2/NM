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
