"""
认证模块测试
覆盖: PBKDF2 密码哈希、SessionStore 签发/校验/撤销/过期
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nm.auth import hash_password, verify_password, SessionStore


class TestPasswordHash:
    def test_hash_and_verify(self):
        h = hash_password("demo123")
        assert h.startswith("pbkdf2$")
        assert verify_password("demo123", h) is True

    def test_wrong_password(self):
        h = hash_password("demo123")
        assert verify_password("wrong", h) is False

    def test_empty_hash_rejected(self):
        assert verify_password("anything", "") is False

    def test_salt_randomness(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # 不同 salt
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True


class TestSessionStore:
    def setup_method(self):
        self.path = tempfile.mktemp(suffix=".json")
        self.store = SessionStore(path=self.path, ttl_seconds=3600)

    def teardown_method(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_create_and_verify(self):
        token = self.store.create("zhangsan")
        assert len(token) > 20
        assert self.store.verify(token) == "zhangsan"

    def test_invalid_token(self):
        assert self.store.verify("bad-token") is None

    def test_revoke(self):
        token = self.store.create("lisi")
        assert self.store.verify(token) == "lisi"
        assert self.store.revoke(token) is True
        assert self.store.verify(token) is None

    def test_expiry(self):
        store = SessionStore(path=self.path, ttl_seconds=1)
        token = store.create("wangwu")
        assert store.verify(token) == "wangwu"
        time.sleep(1.2)
        assert store.verify(token) is None

    def test_persistence(self):
        token = self.store.create("zhangsan")
        # 重新加载（模拟重启）
        store2 = SessionStore(path=self.path, ttl_seconds=3600)
        assert store2.verify(token) == "zhangsan"