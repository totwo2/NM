"""
会话历史存储测试
覆盖: 追加/读取/裁剪/清空/持久化
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nm.session_store import SessionStore, MAX_MESSAGES


class TestSessionStore:
    def setup_method(self):
        self.path = tempfile.mktemp(suffix=".json")
        self.store = SessionStore(path=self.path)

    def teardown_method(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_append_and_get(self):
        self.store.append("s1", "user", "你好")
        self.store.append("s1", "assistant", "你好，有什么可以帮你")
        msgs = self.store.get_messages("s1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "你好"}

    def test_empty_content_skipped(self):
        self.store.append("s1", "user", "   ")
        assert self.store.get_messages("s1") == []

    def test_trim_to_max(self):
        for i in range(MAX_MESSAGES + 10):
            self.store.append("s1", "user", f"msg{i}")
        msgs = self.store.get_messages("s1")
        assert len(msgs) == MAX_MESSAGES
        assert msgs[-1]["content"] == f"msg{MAX_MESSAGES + 9}"

    def test_clear(self):
        self.store.append("s1", "user", "x")
        assert self.store.clear("s1") is True
        assert self.store.get_messages("s1") == []
        assert self.store.clear("s1") is False

    def test_persistence(self):
        self.store.append("s1", "user", "持久化测试")
        store2 = SessionStore(path=self.path)
        assert store2.get_messages("s1")[0]["content"] == "持久化测试"

    def test_list_sessions(self):
        self.store.append("u1:s1", "user", "a")
        self.store.append("u1:s2", "user", "b")
        self.store.append("u2:s3", "user", "c")
        assert len(self.store.list_sessions("u1")) == 2
        assert len(self.store.list_sessions("u2")) == 1
        assert len(self.store.list_sessions()) == 3