"""入库任务队列纯逻辑单元测试：不依赖 Redis 实例的部分。"""
import asyncio
from unittest.mock import AsyncMock

from rag个人知识库.service import ingest_queue
from rag个人知识库.service.ingest_queue import (
    INFLIGHT_KEY,
    claim_inflight,
    enqueue_ingest,
    process_message,
    release_inflight,
)


class TestProcessMessage:
    def test_missing_file_discarded(self):
        # 文件不存在视为可丢弃（可能已被删除接口清理），不触发入库
        ok = asyncio.run(process_message("fake-id", {"path": "F:/not/exist.md"}))
        assert ok is True

    def test_empty_fields_discarded(self):
        assert asyncio.run(process_message("id", {})) is True


class _FakeRedis:
    def __init__(self, set_result=True):
        self.set = AsyncMock(return_value=set_result)
        self.sadd = AsyncMock(return_value=1)
        self.eval = AsyncMock(return_value=1)
        self.exists = AsyncMock(return_value=False)
        self.srem = AsyncMock(return_value=1)
        self.delete = AsyncMock(return_value=1)
        self.xadd = AsyncMock(return_value="msg-1")


def _patch_redis(monkeypatch, fake):
    monkeypatch.setattr(ingest_queue, "redis_available", AsyncMock(return_value=True))
    monkeypatch.setattr(ingest_queue, "get_redis", lambda: fake)
    monkeypatch.setattr(ingest_queue, "rel_source_from_local", lambda p: "uploads/1/a.md")


class TestClaimInflight:
    def test_claim_success_returns_token(self, monkeypatch):
        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        token = asyncio.run(claim_inflight("F:/uploads/1/a.md"))
        assert isinstance(token, str) and token
        key = ingest_queue._inflight_lock_key("F:/uploads/1/a.md")
        fake.set.assert_awaited_once()
        _args, kwargs = fake.set.await_args
        assert kwargs["nx"] is True
        assert kwargs["ex"] == ingest_queue.INFLIGHT_LOCK_TTL
        assert key in fake.set.await_args.args
        fake.sadd.assert_awaited_once_with(INFLIGHT_KEY, "uploads/1/a.md")

    def test_claim_duplicate(self, monkeypatch):
        fake = _FakeRedis(set_result=False)
        _patch_redis(monkeypatch, fake)
        assert asyncio.run(claim_inflight("x")) is False
        fake.sadd.assert_not_called()

    def test_claim_redis_error(self, monkeypatch):
        fake = _FakeRedis()
        fake.set.side_effect = RuntimeError("down")
        _patch_redis(monkeypatch, fake)
        assert asyncio.run(claim_inflight("x")) is None


class TestReleaseInflight:
    def test_release_with_token(self, monkeypatch):
        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        asyncio.run(release_inflight("F:/uploads/1/a.md", "tok"))
        fake.eval.assert_awaited_once()
        _args, kwargs = fake.eval.await_args
        assert "uploads/1/a.md|tok" in _args
        fake.srem.assert_awaited_once_with(INFLIGHT_KEY, "uploads/1/a.md")

    def test_release_token_mismatch_keeps_lock(self, monkeypatch):
        fake = _FakeRedis()
        fake.eval.return_value = 0
        fake.exists.return_value = True
        _patch_redis(monkeypatch, fake)
        asyncio.run(release_inflight("F:/uploads/1/a.md", "wrong"))
        fake.srem.assert_not_called()


class TestEnqueueIngest:
    def test_includes_inflight_token(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(ingest_queue, "redis_available", AsyncMock(return_value=True))
        monkeypatch.setattr(ingest_queue, "get_redis", lambda: fake)
        msg_id = asyncio.run(enqueue_ingest(
            "F:/uploads/1/a.md", owner_id=1, already_claimed=True, inflight_token="tok",
        ))
        assert msg_id == "msg-1"
        fields = fake.xadd.await_args.args[1]
        assert fields["inflight_token"] == "tok"
        assert fields["owner_id"] == "1"


class TestHandleFailure:
    """失败重试的关键顺序：先持久化重试计划/死信，再 ACK + XDEL。"""

    def _fields(self):
        return {"path": "F:/uploads/1/a.md", "owner_id": "1", "is_public": "0"}

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

        monkeypatch.setattr(ingest_queue, "_schedule_retry", fake_schedule)
        fake.xack.side_effect = track_xack

        asyncio.run(ingest_queue._handle_failure("msg-1", self._fields()))

        assert order == ["schedule", "ack"]
        fake.xdel.assert_awaited_once()
        fake.xadd.assert_not_called()  # 未到重试上限，不进死信

    def test_dead_letter_written_before_ack(self, monkeypatch):
        fake = self._fake_redis(retries=ingest_queue.MAX_RETRIES)
        _patch_redis(monkeypatch, fake)
        order = []

        async def track_xadd(*args, **kwargs):
            order.append("dead")

        async def track_xack(*args, **kwargs):
            order.append("ack")

        fake.xadd.side_effect = track_xadd
        fake.xack.side_effect = track_xack

        asyncio.run(ingest_queue._handle_failure("msg-1", self._fields()))

        assert order == ["dead", "ack"]
        dead_fields = fake.xadd.await_args.args[1]
        assert dead_fields["path"] == self._fields()["path"]
        assert dead_fields["origin"] == "msg-1"
