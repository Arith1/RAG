"""入库任务队列纯逻辑单元测试：不依赖 Redis 实例的部分。"""
import asyncio

from rag个人知识库.service.ingest_queue import process_message


class TestProcessMessage:
    def test_missing_file_discarded(self):
        # 文件不存在视为可丢弃（可能已被删除接口清理），不触发入库
        ok = asyncio.run(process_message("fake-id", {"path": "F:/not/exist.md"}))
        assert ok is True

    def test_empty_fields_discarded(self):
        assert asyncio.run(process_message("id", {})) is True
