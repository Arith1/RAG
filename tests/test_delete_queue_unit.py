"""账户删除队列纯逻辑单元测试：不依赖 Redis/MySQL 实例的部分。"""
import asyncio

from rag个人知识库.service.delete_queue import process_delete_message


class TestProcessDeleteMessage:
    def test_missing_user_id_discarded(self):
        assert asyncio.run(process_delete_message("id", {})) is True

    def test_invalid_user_id_discarded(self):
        assert asyncio.run(process_delete_message("id", {"user_id": "abc"})) is True