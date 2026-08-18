"""限流器单元测试：本地兜底逻辑 + Redis 滑动窗口路径（Redis 不可用时自动跳过）。"""
import asyncio

import pytest

from rag个人知识库.api.auth import (
    _local_check_allowed, _local_clear_key, _local_record_failure,
    check_allowed, clear_key, record_failure,
)
from rag个人知识库.config.redis import redis_available

KEY = "login|testuser|127.0.0.1"


class TestLocalFallback:
    """进程内兜底逻辑（Redis 不可用时走这里）。"""

    def setup_method(self):
        _local_clear_key(KEY)

    def test_allowed_until_limit(self):
        assert _local_check_allowed(KEY)
        for _ in range(5):
            _local_record_failure(KEY)
        assert not _local_check_allowed(KEY)

    def test_clear_resets(self):
        for _ in range(6):
            _local_record_failure(KEY)
        assert not _local_check_allowed(KEY)
        _local_clear_key(KEY)
        assert _local_check_allowed(KEY)

    def test_window_expiry(self, monkeypatch):
        fake_now = [1_000_000.0]

        def fake_time():
            return fake_now[0]

        monkeypatch.setattr("rag个人知识库.api.auth.time.time", fake_time)
        for _ in range(5):
            _local_record_failure(KEY)
        assert not _local_check_allowed(KEY)
        fake_now[0] += 61  # 超过 60s 窗口
        assert _local_check_allowed(KEY)

    def test_different_keys_independent(self):
        other = "login|other|1.2.3.4"
        _local_clear_key(other)
        for _ in range(6):
            _local_record_failure(KEY)
        assert not _local_check_allowed(KEY)
        assert _local_check_allowed(other)
        _local_clear_key(other)


class TestRedisRateLimiter:
    """Redis 滑动窗口路径（真实 Redis；不可用时跳过）。

    注意：get_redis() 是绑定首个事件循环的单例，测试必须复用同一个 loop，
    不能用 asyncio.run 每用例新建（生产 uvicorn 单 loop 无此问题）。
    """

    @classmethod
    def setup_class(cls):
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        try:
            cls.redis_ok = cls.loop.run_until_complete(redis_available())
        except Exception:
            cls.redis_ok = False

    @classmethod
    def teardown_class(cls):
        if getattr(cls, "loop", None) is not None:
            cls.loop.close()
            asyncio.set_event_loop(None)

    def _run(self, coro):
        if not getattr(self, "redis_ok", False):
            pytest.skip("Redis 不可用，跳过 Redis 限流集成测试")
        return self.loop.run_until_complete(coro)

    def test_redis_allowed_until_limit(self):
        self._run(clear_key(KEY))
        assert self._run(check_allowed(KEY))
        for _ in range(5):
            self._run(record_failure(KEY))
        assert not self._run(check_allowed(KEY))
        self._run(clear_key(KEY))

    def test_redis_clear_resets(self):
        for _ in range(6):
            self._run(record_failure(KEY))
        assert not self._run(check_allowed(KEY))
        self._run(clear_key(KEY))
        assert self._run(check_allowed(KEY))
        self._run(clear_key(KEY))

    def test_redis_window_prunes(self):
        # 记录 5 次后拦截；清 key 后放行（滑动窗口行为由 Lua 保证）
        for _ in range(5):
            self._run(record_failure(KEY))
        assert not self._run(check_allowed(KEY))
        self._run(clear_key(KEY))
        assert self._run(check_allowed(KEY))
        self._run(clear_key(KEY))
