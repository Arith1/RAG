"""限流器单元测试：滑动窗口 / 触顶拦截 / 清除 / 时间窗口过期（进程内实现）。"""
import time

import pytest

from rag个人知识库.api.auth import check_allowed, clear_key, record_failure

KEY = "login|testuser|127.0.0.1"


class TestRateLimiter:
    def setup_method(self):
        clear_key(KEY)

    def test_allowed_until_limit(self):
        assert check_allowed(KEY)
        for _ in range(5):
            record_failure(KEY)
        # 第 5 次失败后窗口内已达上限
        assert not check_allowed(KEY)

    def test_clear_resets(self):
        for _ in range(6):
            record_failure(KEY)
        assert not check_allowed(KEY)
        clear_key(KEY)
        assert check_allowed(KEY)

    def test_window_expiry(self, monkeypatch):
        fake_now = [1_000_000.0]

        def fake_time():
            return fake_now[0]

        monkeypatch.setattr("rag个人知识库.api.auth.time.time", fake_time)
        for _ in range(5):
            record_failure(KEY)
        assert not check_allowed(KEY)
        # 超过 60s 窗口后旧记录过期，应放行
        fake_now[0] += 61
        assert check_allowed(KEY)

    def test_different_keys_independent(self):
        other = "login|other|1.2.3.4"
        clear_key(other)
        for _ in range(6):
            record_failure(KEY)
        assert not check_allowed(KEY)
        assert check_allowed(other)  # 其他 key 不受影响
        clear_key(other)
