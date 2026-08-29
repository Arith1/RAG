"""账户删除队列纯逻辑单元测试：不依赖 Redis/MySQL 实例的部分。"""
import asyncio
from unittest.mock import AsyncMock

from rag个人知识库.service import delete_queue
from rag个人知识库.service.delete_queue import process_delete_message


class _FakeRedis:
    def __init__(self, set_result=True):
        self.set = AsyncMock(return_value=set_result)
        self.sadd = AsyncMock(return_value=1)
        self.xadd = AsyncMock(return_value="del-1")
        self.eval = AsyncMock(return_value=1)
        self.exists = AsyncMock(return_value=False)
        self.srem = AsyncMock(return_value=1)
        self.delete = AsyncMock(return_value=1)


def _patch_redis(monkeypatch, fake):
    monkeypatch.setattr(delete_queue, "redis_available", AsyncMock(return_value=True))
    monkeypatch.setattr(delete_queue, "get_redis", lambda: fake)


class TestProcessDeleteMessage:
    def test_missing_user_id_discarded(self):
        assert asyncio.run(process_delete_message("id", {})) is True

    def test_invalid_user_id_discarded(self):
        assert asyncio.run(process_delete_message("id", {"user_id": "abc"})) is True


class TestEnqueueDelete:
    def test_claims_lock_and_includes_token(self, monkeypatch):
        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        msg_id = asyncio.run(delete_queue.enqueue_delete(7))
        assert msg_id == "del-1"
        fields = fake.xadd.await_args.args[1]
        assert fields["user_id"] == "7"
        assert fields["inflight_token"]
        fake.set.assert_awaited_once()

    def test_duplicate_claim_returns_none(self, monkeypatch):
        fake = _FakeRedis(set_result=False)
        _patch_redis(monkeypatch, fake)
        assert asyncio.run(delete_queue.enqueue_delete(7)) is None
        fake.xadd.assert_not_called()


class TestReleaseDeleteInflight:
    def test_release_with_token(self, monkeypatch):
        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        asyncio.run(delete_queue.release_delete_inflight(7, "tok"))
        fake.eval.assert_awaited_once()
        fake.srem.assert_awaited_once_with(delete_queue.INFLIGHT_KEY, "7")

    def test_token_mismatch_keeps_lock(self, monkeypatch):
        fake = _FakeRedis()
        fake.eval.return_value = 0
        fake.exists.return_value = True
        _patch_redis(monkeypatch, fake)
        asyncio.run(delete_queue.release_delete_inflight(7, "wrong"))
        fake.srem.assert_not_called()


class TestHandleFailure:
    """失败重试的关键顺序：先持久化重试计划/死信，再 ACK + XDEL。"""

    def _fake_redis(self, retries: int):
        fake = _FakeRedis()
        fake.hincrby = AsyncMock(return_value=retries)
        fake.hdel = AsyncMock(return_value=1)
        fake.xack = AsyncMock()
        fake.xdel = AsyncMock()
        return fake

    def test_retry_scheduled_before_ack(self, monkeypatch):
        fake = self._fake_redis(retries=1)
        _patch_redis(monkeypatch, fake)
        order = []

        async def fake_schedule(fields, delay):
            order.append("schedule")

        async def track_xack(*args, **kwargs):
            order.append("ack")

        monkeypatch.setattr(delete_queue, "_schedule_retry", fake_schedule)
        fake.xack.side_effect = track_xack

        asyncio.run(delete_queue._handle_failure("msg-1", {"user_id": "7"}))

        assert order == ["schedule", "ack"]
        fake.xdel.assert_awaited_once()
        fake.xadd.assert_not_called()  # 未到重试上限，不进死信

    def test_dead_letter_written_before_ack(self, monkeypatch):
        fake = self._fake_redis(retries=delete_queue.MAX_RETRIES)
        _patch_redis(monkeypatch, fake)
        order = []

        async def track_xadd(*args, **kwargs):
            order.append("dead")

        async def track_xack(*args, **kwargs):
            order.append("ack")

        fake.xadd.side_effect = track_xadd
        fake.xack.side_effect = track_xack

        asyncio.run(delete_queue._handle_failure("msg-1", {"user_id": "7"}))

        assert order == ["dead", "ack"]
        dead_fields = fake.xadd.await_args.args[1]
        assert dead_fields["user_id"] == "7"
        assert dead_fields["origin"] == "msg-1"
