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
SESSION_OPERATION_LOCK_TTL = max(1, int(os.getenv("SESSION_OPERATION_LOCK_TTL", str(30 * 60))))
SESSION_OPERATION_LOCK_WAIT = max(0.0, float(os.getenv("SESSION_OPERATION_LOCK_WAIT", "0")))


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


@asynccontextmanager
async def session_operation_lock(user_id: int, session_id: str, *, required: bool = False):
    """会话级锁：防止“生成中删除”和同会话并发写入互相覆盖。

    Redis 不可用时默认降级放行；删除会话会传 required=True，由调用方拒绝删除，
    从而避免无锁情况下删除与流式回答相互竞态。
    """
    if not await redis_available():
        if required:
            raise RuntimeError("Redis 不可用，暂时无法安全删除会话")
        yield
        return

    lock = get_redis().lock(
        f"session:operation:{user_id}:{session_id}",
        timeout=SESSION_OPERATION_LOCK_TTL,
        blocking_timeout=SESSION_OPERATION_LOCK_WAIT,
    )
    acquired = await lock.acquire()
    if not acquired:
        raise RuntimeError("会话正在生成回答，请稍后重试")
    try:
        yield
    finally:
        try:
            await lock.release()
        except Exception:
            pass
