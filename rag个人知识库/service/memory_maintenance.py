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
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

from rag个人知识库.service.chat_history import (
    delete_by_keys,
    list_all_session_keys,
    list_expired_sessions,
)

logger = logging.getLogger(__name__)

MEMORY_TTL_DAYS = float(os.getenv("MEMORY_TTL_DAYS", "1"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("MEMORY_CLEANUP_INTERVAL_MINUTES", "60")) * 60


def _connect():
    url = os.getenv("MEMORY_DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, connect_timeout=5)


def _delete_threads(cur, thread_ids: list) -> None:
    """删除指定 thread_id 在三张 checkpoint 表中的全部数据。"""
    if not thread_ids:
        return
    for table in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"):
        cur.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (thread_ids,))


def _purge_postgres(
    thread_ids: list,
    known_threads: set[str] | None = None,
    orphan_before: datetime | None = None,
) -> int:
    """删除过期 checkpoint，并清理 MySQL 中已无元信息且超过 TTL 的孤儿线程。"""
    conn = _connect()
    if conn is None:
        return 0
    orphan_count = 0
    try:
        conn.autocommit = True
        cur = conn.cursor()
        _delete_threads(cur, thread_ids)
        if known_threads is not None and orphan_before is not None:
            # checkpoint JSONB 内的 ts 是 LangGraph 写入时间；仅清理超过 TTL 的线程，
            # 避免误删正在进行首轮写库、MySQL 元信息尚未落下的新会话。
            cur.execute(
                """
                SELECT thread_id
                FROM checkpoints
                GROUP BY thread_id
                HAVING MAX((checkpoint->>'ts')::timestamptz) < %s
                """,
                (orphan_before,),
            )
            # 只扫描本项目的 {user_id}:{session_id} 线程，避免误删其他 LangGraph 调用方。
            orphan_ids = [
                row[0]
                for row in cur.fetchall()
                if re.match(r"^\d+:", row[0]) and row[0] not in known_threads
            ]
            _delete_threads(cur, orphan_ids)
            orphan_count = len(orphan_ids)
    finally:
        conn.close()
    return orphan_count


async def cleanup_expired_memory(ttl_days: float = MEMORY_TTL_DAYS) -> int:
    """清理超过 ttl_days 未活动的会话，返回清理的会话数。

    流程：先查 MySQL 过期会话/全部会话 key → 删 Postgres checkpoint/孤儿 →
    删 MySQL 记录。

    删除顺序经过权衡：Postgres 在前。若先删 MySQL，Postgres 删除失败会留下
    永久孤儿 checkpoint（清理列表按 MySQL 扫描，行已删便不再重试）；改为
    Postgres 在前且失败时本轮不删 MySQL，失败会随下轮清理自动重试（幂等）。
    """
    keys = await list_expired_sessions(ttl_days)
    orphan_count = 0

    # M10：同步 psycopg 阻塞调用放到线程池，避免卡住事件循环（过期会话多时 DELETE 会阻塞全站）
    if os.getenv("MEMORY_DATABASE_URL"):
        try:
            known_threads = await list_all_session_keys()
            orphan_before = datetime.now(timezone.utc) - timedelta(days=ttl_days)
            orphan_count = await asyncio.to_thread(
                _purge_postgres,
                [f"{uid}:{sid}" for uid, sid in keys],
                known_threads,
                orphan_before,
            )
        except Exception as e:
            logger.warning("[memory_maintenance] 删除 Postgres 记忆失败（本轮跳过，下轮重试）：%s", e)
            return 0
    else:
        logger.info("[memory_maintenance] 未配置 MEMORY_DATABASE_URL，仅清理 MySQL 会话元信息")

    # 2) 删 MySQL 会话元信息（此后这些会话不再出现在清理列表）
    deleted = await delete_by_keys(keys) if keys else 0
    logger.info(
        "[memory_maintenance] 已清理 %d 个过期会话、%d 个孤儿 checkpoint（TTL %s 天）",
        deleted,
        orphan_count,
        ttl_days,
    )
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
