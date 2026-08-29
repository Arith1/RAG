"""对话记忆维护：TTL 过期清理（MySQL 会话元信息 + Postgres checkpoints 联动）。

方案约定（用户确认）：
  - MySQL chat_sessions 是会话列表的权威来源，记录最后活跃时间（updated_at，ORM 更新行时自动刷新）。
  - TTL 清理流程：
      1) 先查 MySQL 里过期的会话（updated_at < now - TTL）
      2) 删除对应的 Postgres checkpoint（thread_id={user_id}:{session_id}；
         失败时不删 MySQL，这批会话下轮重新列入并重试）
      3) 删除 MySQL 会话记录
  - 未配置 MEMORY_DATABASE_URL（InMemory 模式）时只删 MySQL 元信息，跳过 Postgres。

执行方式：
  1) FastAPI lifespan 后台任务周期执行（间隔 MEMORY_CLEANUP_INTERVAL_MINUTES）
  2) 独立脚本 / cron：
       python -m rag个人知识库.service.memory_maintenance
"""
import asyncio
import logging
import os
import sys
import time

import psycopg

from rag个人知识库.service.chat_history import delete_by_keys, list_expired_sessions

logger = logging.getLogger(__name__)

MEMORY_TTL_DAYS = float(os.getenv("MEMORY_TTL_DAYS", "1"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("MEMORY_CLEANUP_INTERVAL_MINUTES", "60")) * 60


def _connect():
    url = os.getenv("MEMORY_DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, connect_timeout=5)


async def cleanup_expired_memory(ttl_days: float = MEMORY_TTL_DAYS) -> int:
    """清理超过 ttl_days 未活动的会话，返回清理的会话数。

    流程：先查 MySQL 过期会话 → 删 Postgres checkpoint → 删 MySQL 记录。

    删除顺序经过权衡：Postgres 在前。若先删 MySQL，Postgres 删除失败会留下
    永久孤儿 checkpoint（清理列表按 MySQL 扫描，行已删便不再重试）；改为
    Postgres 在前且失败时本轮不删 MySQL，失败会随下轮清理自动重试（幂等）。
    """
    keys = await list_expired_sessions(ttl_days)
    if not keys:
        logger.info("[memory_maintenance] 无过期会话（TTL %s 天）", ttl_days)
        return 0

    conn = _connect()
    if conn is None:
        logger.info("[memory_maintenance] 未配置 MEMORY_DATABASE_URL，仅清理 MySQL 会话元信息")
    else:
        try:
            conn.autocommit = True
            cur = conn.cursor()
            thread_ids = [f"{uid}:{sid}" for uid, sid in keys]
            for table in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"):
                cur.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (thread_ids,))
        except Exception as e:
            logger.warning("[memory_maintenance] 删除 Postgres 记忆失败（本轮跳过，下轮重试）：%s", e)
            return 0
        finally:
            conn.close()

    # 2) 删 MySQL 会话元信息（此后这些会话不再出现在清理列表）
    deleted = await delete_by_keys(keys)
    logger.info("[memory_maintenance] 已清理 %d 个过期会话（TTL %s 天）", deleted, ttl_days)
    return deleted


def cleanup_loop(
    ttl_days: float = MEMORY_TTL_DAYS,
    interval: int = CLEANUP_INTERVAL_SECONDS,
    stop: "object | None" = None,
) -> None:
    """阻塞循环：周期性执行清理（供独立进程 / cron 以线程方式运行）。"""
    while not (stop is not None and stop.is_set()):
        try:
            asyncio.run(cleanup_expired_memory(ttl_days))
        except Exception as e:
            logger.warning("[memory_maintenance] 清理任务异常：%s", e)
        time.sleep(interval)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))
    n = asyncio.run(cleanup_expired_memory())
    print(f"清理完成，处理会话数：{n}")
    sys.exit(0 if n != -1 else 1)
