"""基于 Redis Streams 的账户删除队列。

删除顺序（重要，保证不产生孤儿向量/不可下载原件）：
  1. 用户提交删除请求时：users.status='deleting'，所有该用户文档 is_public=0
  2. 进入 delete_queue 后：
     a. 删除 Milvus 中该用户全部向量（按 owner_id 过滤）
     b. 删除阿里云 OSS 中该用户的文档原件（逐个 source）
     c. 只有 Milvus 和 OSS 都成功后，才删除 MySQL users 行
        （vector_files/chunk_records 由外键 ON DELETE CASCADE 级联清理）

失败重试与 ingest_queue 保持一致：Consumer Group + PEL 崩溃恢复、指数退避、死信队列。
"""
import asyncio
import logging
import os
import time

from sqlalchemy import delete, select

from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import get_redis, redis_available
from rag个人知识库.models.user import AuditLog, User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.oss_archive import delete_source_artifact
from rag个人知识库.vector_store.milvus_store import adelete_chunks_by_owner

logger = logging.getLogger(__name__)

STREAM = "delete_queue"
DEAD_LETTER = "delete_queue:dead"
GROUP = "delete_workers"
INFLIGHT_KEY = "delete:inflight"
RETRY_HASH = "delete:retry"
MAX_RETRIES = 3
CONSUMER = f"delete-worker-{os.getpid()}"


async def enqueue_delete(user_id: int) -> str | None:
    """把账户删除任务加入队列；Redis 不可用时返回 None，调用方回退后台任务。"""
    if not await redis_available():
        return None
    r = get_redis()
    await r.sadd(INFLIGHT_KEY, str(user_id))
    return await r.xadd(STREAM, {"user_id": str(user_id)})


async def is_delete_inflight(user_id: int) -> bool:
    """是否已有该用户的删除任务在队列/inflight（用于幂等或状态展示）。"""
    if not await redis_available():
        return False
    return bool(await get_redis().sismember(INFLIGHT_KEY, str(user_id)))


async def _ensure_group() -> None:
    r = get_redis()
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _recover_pending() -> None:
    """回收上次崩溃未 ACK 的账户删除任务并立即重新处理。"""
    r = get_redis()
    try:
        start = "0"
        while True:
            result = await r.xautoclaim(STREAM, GROUP, "recovery", 10000, start, count=100)
            claimed = result[1] if result else []
            if not claimed:
                break
            for msg_id, fields in claimed:
                user_id = fields.get("user_id", "")
                logger.info("[delete_queue] 回收崩溃残留任务 %s 并重新处理: %s", msg_id, user_id)
                ok = await process_delete_message(msg_id, fields)
                if ok:
                    await r.xack(STREAM, GROUP, msg_id)
                    await r.xdel(STREAM, msg_id)
                else:
                    await _handle_failure(msg_id, fields)
                await r.srem(INFLIGHT_KEY, user_id)
            start = result[0] if result else "0"
    except Exception as e:
        logger.warning("[delete_queue] 崩溃任务回收失败：%s", e)


async def process_delete_message(msg_id: str, fields: dict) -> bool:
    """执行一次账户删除。

    返回 True 表示该任务可 ACK：
      - user_id 缺失/非法
      - 用户已不存在（可能已被删除）
      - 用户当前状态不是 deleting（防止误删 active/disabled 用户）
      - Milvus + OSS + MySQL 全部删除成功

    返回 False 表示需要重试：Milvus 或 OSS 删除失败。
    """
    raw = fields.get("user_id")
    if raw is None:
        logger.info("[delete_queue] 任务 %s 缺少 user_id，丢弃", msg_id)
        return True
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        logger.warning("[delete_queue] 任务 %s user_id 非法：%r，丢弃", msg_id, raw)
        return True

    try:
        async with async_session() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                logger.info("[delete_queue] 任务 %s 用户 %s 已不存在，丢弃", msg_id, user_id)
                return True
            if user.status != "deleting":
                logger.warning(
                    "[delete_queue] 任务 %s 用户 %s 当前状态为 %s，不是 deleting，丢弃",
                    msg_id, user_id, user.status,
                )
                return True

            # 1. 删除 Milvus 向量（按 owner_id 幂等；失败抛异常走重试）
            await adelete_chunks_by_owner(user_id)

            # 2. 删除 OSS 原件：任一失败都不进入 MySQL 删除
            sources = (await db.execute(
                select(VectorFile.source).where(VectorFile.owner_id == user_id)
            )).scalars().all()
            for source in sources:
                ok = await delete_source_artifact(source)
                if not ok:
                    logger.warning(
                        "[delete_queue] 用户 %s 的 OSS 原件删除失败：%s，保留 MySQL 等待重试",
                        user_id, source,
                    )
                    return False

            # 3. Milvus + OSS 都成功后才删 MySQL 用户（vector_files/chunks 级联删除）
            db.add(AuditLog(
                user_id=user_id,
                username=user.username,
                action="delete_account",
                target=user.username,
                detail="delete_queue completed: milvus+oss+mysql",
            ))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
            logger.info("[delete_queue] 用户 %s 已删除（Milvus+OSS+MySQL 完成）", user_id)
            return True
    except Exception as e:
        logger.warning("[delete_queue] 任务 %s 处理失败：%s", msg_id, e)
        return False


async def _delayed_requeue(user_id: int, delay: float) -> None:
    """独立后台任务：延迟后重新入队，不阻塞 worker 主消费循环。"""
    await asyncio.sleep(delay)
    await enqueue_delete(user_id)


async def _handle_failure(msg_id: str, fields: dict) -> None:
    """失败重试：指数退避重入队，超限进死信。"""
    r = get_redis()
    user_id = int(fields.get("user_id", 0))
    retries = await r.hincrby(RETRY_HASH, user_id, 1)
    await r.xack(STREAM, GROUP, msg_id)
    await r.xdel(STREAM, msg_id)
    if retries >= MAX_RETRIES:
        await r.xadd(DEAD_LETTER, {"user_id": str(user_id), "error": f"重试 {retries} 次仍失败", "origin": msg_id})
        await r.hdel(RETRY_HASH, user_id)
        logger.warning("[delete_queue] 任务 %s 进入死信队列：%s", msg_id, user_id)
        return
    delay = 2 ** int(retries)
    logger.warning("[delete_queue] 任务 %s 第 %d 次失败，%ds 后重试：user_id=%s", msg_id, retries, delay, user_id)
    asyncio.create_task(_delayed_requeue(user_id, delay))


async def run_worker(stop: "asyncio.Event | None" = None) -> None:
    """账户删除队列消费循环，可由 FastAPI lifespan 内嵌或独立进程启动。"""
    await _ensure_group()
    await _recover_pending()
    r = get_redis()
    logger.info("[delete_queue] worker 启动（consumer=%s）", CONSUMER)
    last_recover = time.monotonic()
    while not (stop is not None and stop.is_set()):
        try:
            resp = await r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=1, block=2000)
            if not resp:
                if time.monotonic() - last_recover > 60:
                    await _recover_pending()
                    last_recover = time.monotonic()
                continue
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    ok = await process_delete_message(msg_id, fields)
                    if ok:
                        await r.xack(STREAM, GROUP, msg_id)
                    else:
                        await _handle_failure(msg_id, fields)
                    await r.xdel(STREAM, msg_id)
                    await r.srem(INFLIGHT_KEY, fields.get("user_id", ""))
        except asyncio.CancelledError:
            logger.info("[delete_queue] worker 停止")
            raise
        except Exception as e:
            logger.warning("[delete_queue] worker 异常：%s", e)
            try:
                await _ensure_group()
            except Exception:
                pass
            await asyncio.sleep(2)


async def queue_stats() -> dict:
    """账户删除队列统计（供接口展示）。"""
    if not await redis_available():
        return {"enabled": False}
    r = get_redis()
    try:
        info = await r.xinfo_groups(STREAM)
        pending, delivered, last_id = 0, 0, "0-0"
        for g in info:
            pending += g.get("pending", 0)
            delivered += g.get("consumers", 0)
            last_id = g.get("last-delivered-id", last_id)
        n = await r.xlen(STREAM)
        dead = await r.xlen(DEAD_LETTER)
        inflight = await r.scard(INFLIGHT_KEY)
        return {
            "enabled": True,
            "stream_len": n,
            "pending": pending,
            "dead_letter": dead,
            "inflight": inflight,
            "last_delivered_id": last_id,
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))
    print("独立账户删除 worker 进程启动（Ctrl+C 退出）")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        sys.exit(0)