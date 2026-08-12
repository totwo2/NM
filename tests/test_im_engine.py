"""
tests/test_im_engine.py — IM 引擎集成测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from harness.im.im_engine import IMEngine


def make_engine(tmp_path):
    from harness.im.im_store import IMStore
    return IMEngine(store=IMStore(str(tmp_path / "im.json")))


class TestGroupCRUD:
    def test_create_group(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("研发部", "zhangsan", "研发群", "department")
        assert g.name == "研发部"
        assert g.created_by == "zhangsan"
        assert g.group_type == "department"

    def test_get_group(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        fetched = engine.get_group(g.id)
        assert fetched is not None
        assert fetched.name == "测试群"

    def test_get_nonexistent(self, tmp_path):
        engine = make_engine(tmp_path)
        assert engine.get_group("nonexistent") is None

    def test_delete_group_owner_only(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        with pytest.raises(PermissionError):
            engine.delete_group(g.id, "u2")
        ok = engine.delete_group(g.id, "u1")
        assert ok is True


class TestMembers:
    def test_add_member(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        m = engine.add_member(g.id, "u2", role="member", adder_id="u1")
        assert m.role == "member"
        assert len(engine.get_members(g.id)) == 2  # u1 (owner) + u2

    def test_remove_member(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        engine.add_member(g.id, "u2", role="member", adder_id="u1")
        ok = engine.remove_member(g.id, "u2", "u1")
        assert ok is True
        assert len(engine.get_members(g.id)) == 1

    def test_remove_self(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        ok = engine.remove_member(g.id, "u1", "u1")
        assert ok is True


class TestMessages:
    def test_send_and_get(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        m = engine.send_message(g.id, "u1", "hello")
        msgs = engine.get_messages(g.id, "u1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_non_member_cannot_send(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        with pytest.raises(PermissionError):
            engine.send_message(g.id, "u2", "spam")

    def test_search_messages(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        engine.send_message(g.id, "u1", "hello world")
        engine.send_message(g.id, "u1", "goodbye world")
        results = engine.search_messages(g.id, "world")
        assert len(results) == 2


class TestStats:
    def test_user_stats(self, tmp_path):
        engine = make_engine(tmp_path)
        g = engine.create_group("测试群", "u1")
        engine.send_message(g.id, "u1", "msg1")
        stats = engine.stats("u1")
        assert stats["my_groups"] == 1
        assert stats["total_unread"] == 0
