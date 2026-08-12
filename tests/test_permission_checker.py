"""
tests/test_permission_checker.py — 三级权限控制测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from harness.permission_checker import PermissionChecker, PermissionMode, DANGEROUS_PATTERNS


class TestAutoMode:
    def test_allows_read(self):
        pc = PermissionChecker(mode=PermissionMode.AUTO)
        assert pc.check("read_file", {"path": "/tmp/a.txt"}) is True

    def test_allows_search(self):
        pc = PermissionChecker(mode=PermissionMode.AUTO)
        assert pc.check("search_content", {"path": "/tmp", "query": "x"}) is True

    def test_allows_write(self):
        pc = PermissionChecker(mode=PermissionMode.AUTO)
        assert pc.check("write_file", {"path": "/tmp/a.txt", "content": "hi"}) is True

    def test_safe_bash(self):
        pc = PermissionChecker(mode=PermissionMode.AUTO)
        assert pc.check("bash", {"command": "echo hello"}) is True

    def test_dangerous_patterns_exist(self):
        assert len(DANGEROUS_PATTERNS) >= 5

    def test_dangerous_patterns_compiled(self):
        import re
        for pattern, desc in DANGEROUS_PATTERNS:
            assert isinstance(pattern, type(re.compile('')))


class TestStrictMode:
    def test_strict_read(self):
        pc = PermissionChecker(mode=PermissionMode.STRICT)
        assert pc.check("read_file", {"path": "/tmp/a.txt"}) is True

    def test_strict_search(self):
        pc = PermissionChecker(mode=PermissionMode.STRICT)
        assert pc.check("search_content", {"path": "/tmp", "query": "x"}) is True

    def test_strict_denies_write(self):
        pc = PermissionChecker(mode=PermissionMode.STRICT)
        with pytest.raises(Exception):
            pc.check("write_file", {"path": "/tmp/a", "content": "b"})

    def test_strict_denies_bash(self):
        pc = PermissionChecker(mode=PermissionMode.STRICT)
        with pytest.raises(Exception):
            pc.check("bash", {"command": "echo hi"})


class TestAskMode:
    def test_ask_approves(self):
        pc = PermissionChecker(mode=PermissionMode.ASK, ask_callback=lambda t, d: True)
        assert pc.check("write_file", {"path": "/tmp/a", "content": "b"}) is True

    def test_ask_denies(self):
        pc = PermissionChecker(mode=PermissionMode.ASK, ask_callback=lambda t, d: False)
        assert pc.check("write_file", {"path": "/tmp/a", "content": "b"}) is False

    def test_ask_no_callback(self):
        pc = PermissionChecker(mode=PermissionMode.ASK)
        with pytest.raises(Exception):
            pc.check("write_file", {"path": "/tmp/a", "content": "b"})
