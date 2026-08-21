"""对话记忆维护：TTL 过期清理（Postgres checkpoints）。

设计：
  - 时间依据：checkpoints 表的 created_at 列（默认 now()，langgraph 每次写入 checkpoint
    都会新建一行并自动带上时间）。**清理以线程最后一次活跃时间为基准倒计时**——
    即 max(created_at) 超过 MEMORY_TTL_DAYS（默认 1 天）未活动的线程整线程删除
    （checkpoints / checkpoint_writes / checkpoint_blobs 三表联动）。
  - created_at 列属于表结构变更，由 models/postgres_memory.sql 手动执行（幂等），
    代码中不做任何 DDL；清理前只做只读检查，列缺失时给出明确指引。
  - 执行方式：
      1) FastAPI lifespan 后台任务周期执行（间隔 MEMORY_CLEANUP_INTERVAL_MINUTES）
      2) 独立脚本 / cron：
           python -m rag个人知识库.service.memory_maintenance
  - 未配置 MEMORY_DATABASE_URL（InMemory 模式）时自动跳过——进程内记忆随进程消亡，无需清理。
"""
import logging
import os
import sys
import time

import psycopg

logger = logging.getLogger(__name__)

MEMORY_TTL_DAYS = float(os.getenv("MEMORY_TTL_DAYS", "1"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("MEMORY_CLEANUP_INTERVAL_MINUTES", "60")) * 60

# 表结构变更统一在 models/postgres_memory.sql 中维护，此处只读检查，不执行 DDL
_ALTER_HINT = ("[memory_maintenance] 缺少 created_at 列，请先执行表结构变更："
               "psql -U <user> -d <db> -f rag个人知识库/models/postgres_memory.sql")


def _connect():
    url = os.getenv("MEMORY_DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, connect_timeout=5)


def _has_created_at(cur) -> bool:
    """只读检查 created_at 列是否存在（不修改表结构）。"""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='checkpoints' AND column_name='created_at'"
    )
    return cur.fetchone() is not None


def cleanup_expired_memory(ttl_days: float = MEMORY_TTL_DAYS) -> int:
    """清理超过 ttl_days 未活动的会话线程（按最后一次活跃时间倒计时），返回清理的线程数。

    返回 -1 表示未配置 Postgres（跳过）；列缺失时打印指引并返回 0。
    """
    conn = _connect()
    if conn is None:
        logger.info("[memory_maintenance] 未配置 MEMORY_DATABASE_URL，跳过清理（InMemory 模式无需持久化清理）")
        return -1
    try:
        conn.autocommit = True
        cur = conn.cursor()
        if not _has_created_at(cur):
            logger.warning("%s", _ALTER_HINT)
            return 0
        cur.execute(
            """
            WITH expired AS (
                SELECT thread_id FROM checkpoints
                GROUP BY thread_id
                HAVING max(created_at) < now() - (%s * interval '1 day')
            )
            SELECT thread_id FROM expired
            """,
            (ttl_days,),
        )
        threads = [r[0] for r in cur.fetchall()]
        if threads:
            for table in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"):
                cur.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (threads,))
            logger.info("[memory_maintenance] 已清理 %d 个超过 %s 天未活动的会话线程", len(threads), ttl_days)
        else:
            logger.info("[memory_maintenance] 无过期会话（TTL %s 天）", ttl_days)
        return len(threads)
    finally:
        conn.close()


def cleanup_loop(
    ttl_days: float = MEMORY_TTL_DAYS,
    interval: int = CLEANUP_INTERVAL_SECONDS,
    stop: "object | None" = None,
) -> None:
    """阻塞循环：周期性执行清理（供 FastAPI 后台任务以线程方式运行）。"""
    while not (stop is not None and stop.is_set()):
        try:
            cleanup_expired_memory(ttl_days)
        except Exception as e:
            logger.warning("[memory_maintenance] 清理任务异常：%s", e)
        time.sleep(interval)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))
    n = cleanup_expired_memory()
    print(f"清理完成，处理线程数：{n}")
    sys.exit(0 if n != -1 else 1)
