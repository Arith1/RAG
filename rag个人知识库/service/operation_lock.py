"""跨进程的用户级操作锁。

入库会同时修改 MySQL 和 Milvus，账户删除也会清理这两个系统。
两类任务必须按用户串行，否则删除可能先清理 Milvus、入库随后又写回向量。
Redis Streams worker 都使用这里的锁来协调这类跨库操作。
"""
import os
from contextlib import asynccontextmanager

from rag个人知识库.config.redis import get_redis, redis_available


OWNER_OPERATION_LOCK_TTL = int(os.getenv("OWNER_OPERATION_LOCK_TTL", str(6 * 3600)))
OWNER_OPERATION_LOCK_WAIT = float(os.getenv("OWNER_OPERATION_LOCK_WAIT", "10"))


@asynccontextmanager
async def owner_operation_lock(owner_id: int):
    """获取用户级分布式锁；Redis 不可用或竞争超时都不能静默放行。"""
    if not await redis_available():
        raise RuntimeError("Redis 不可用，无法安全协调用户操作")

    lock = get_redis().lock(
        f"owner:operation:{owner_id}",
        timeout=OWNER_OPERATION_LOCK_TTL,
        blocking_timeout=OWNER_OPERATION_LOCK_WAIT,
    )
    acquired = await lock.acquire()
    if not acquired:
        raise RuntimeError(f"用户 {owner_id} 当前有其他操作正在执行")
    try:
        yield
    finally:
        try:
            await lock.release()
        except Exception:
            # 锁可能已因 TTL 到期由 Redis 自动释放，不能覆盖原始业务异常。
            pass
