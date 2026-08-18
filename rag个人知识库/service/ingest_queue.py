"""基于 Redis Streams 的入库任务队列。

为什么用 Streams：
  - Consumer Group + PEL（Pending Entries List）：任务被 worker 领取后、ACK 前若进程崩溃，
    消息留在 PEL，重启后 XAUTOCLAIM 回收重新投递——任务不丢
  - XACK 显式确认：处理失败不 ACK，配合重试计数与死信队列
  - mkstream 自动建流，多 worker 可横向扩展（GROUP 消费）

关键 Key：
  ingest_queue            任务流（XADD 入队 / XREADGROUP 消费）
  ingest_queue:dead       死信流（重试超限的任务）
  ingest:inflight         Set：正在入库的文件路径（删除接口据此返回 409，防孤儿向量竞态）
  ingest:retry            Hash：path -> 已重试次数

用法：
  # 入队（Redis 不可用时返回 None，调用方回退进程内执行）
  msg_id = await enqueue_ingest(path)

  # 启动 worker（FastAPI lifespan 内嵌，或独立进程 python -m rag个人知识库.service.ingest_queue）
  await run_worker()
"""
import asyncio
import os

from rag个人知识库.config.redis import get_redis, redis_available
from rag个人知识库.service.service import ingest_files

STREAM = "ingest_queue"
DEAD_LETTER = "ingest_queue:dead"
GROUP = "ingest_workers"
INFLIGHT_KEY = "ingest:inflight"
RETRY_HASH = "ingest:retry"
MAX_RETRIES = 3
CONSUMER = f"worker-{os.getpid()}"


async def enqueue_ingest(file_path: str) -> str | None:
    """把文件路径加入入库队列，返回消息 ID；Redis 不可用时返回 None。"""
    if not await redis_available():
        return None
    r = get_redis()
    await r.sadd(INFLIGHT_KEY, file_path)
    return await r.xadd(STREAM, {"path": file_path})


async def is_inflight(file_path: str) -> bool:
    """文件是否正在入库（删除接口据此返回 409）。Redis 不可用时返回 False（放行）。"""
    if not await redis_available():
        return False
    return bool(await get_redis().sismember(INFLIGHT_KEY, file_path))


async def _ensure_group() -> None:
    r = get_redis()
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:  # BUSYGROUP 已存在属正常
        if "BUSYGROUP" not in str(e):
            raise


async def _recover_pending() -> None:
    """启动时回收上次崩溃未 ACK 的任务（PEL 中的残留，空闲 > 10s）。"""
    try:
        result = await get_redis().xautoclaim(STREAM, GROUP, "recovery", 10000, "0", count=100)
        claimed = result[1] if result else []
        for msg_id, fields in claimed:
            print(f"[ingest_queue] 回收崩溃残留任务 {msg_id}: {fields.get('path')}")
    except Exception as e:
        print(f"[ingest_queue] 崩溃任务回收跳过：{e}")


async def process_message(msg_id: str, fields: dict) -> bool:
    """执行一次入库，成功返回 True。文件已不存在视为成功（可能被删除接口清理）。"""
    path = fields.get("path", "")
    if not path:
        return True
    if not os.path.isfile(path):
        print(f"[ingest_queue] 任务 {msg_id} 文件已不存在，丢弃：{path}")
        return True
    try:
        await ingest_files([path])
        return True
    except Exception as e:
        print(f"[ingest_queue] 任务 {msg_id} 处理失败：{e}")
        return False


async def _handle_failure(msg_id: str, fields: dict) -> None:
    """失败重试：指数退避重入队，超限进死信。"""
    r = get_redis()
    path = fields.get("path", "")
    retries = await r.hincrby(RETRY_HASH, path, 1)
    await r.xack(STREAM, GROUP, msg_id)  # 先确认，再由重试机制重新入队
    await r.xdel(STREAM, msg_id)  # 原消息删除，防止 stream 增长
    if retries >= MAX_RETRIES:
        await r.xadd(DEAD_LETTER, {"path": path, "error": f"重试 {retries} 次仍失败", "origin": msg_id})
        await r.hdel(RETRY_HASH, path)
        print(f"[ingest_queue] 任务 {msg_id} 进入死信队列：{path}")
        return
    delay = 2 ** int(retries)  # 指数退避 2s / 4s
    print(f"[ingest_queue] 任务 {msg_id} 第 {retries} 次失败，{delay}s 后重试：{path}")
    await asyncio.sleep(delay)
    await enqueue_ingest(path)


async def run_worker(stop: "asyncio.Event | None" = None) -> None:
    """消费循环：阻塞读新任务 → 处理 → ACK / 失败重试。

    FastAPI lifespan 内嵌运行，或独立进程执行：
      python -m rag个人知识库.service.ingest_queue
    """
    await _ensure_group()
    await _recover_pending()
    r = get_redis()
    print(f"[ingest_queue] worker 启动（consumer={CONSUMER}）")
    while not (stop is not None and stop.is_set()):
        try:
            # BLOCK 2s 等新消息；">" 只读尚未投递的新消息
            resp = await r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=1, block=2000)
            if not resp:
                continue
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    ok = await process_message(msg_id, fields)
                    if ok:
                        await r.xack(STREAM, GROUP, msg_id)
                    else:
                        await _handle_failure(msg_id, fields)
                    # ACK 后删除消息本体，避免 stream 无限增长（XACK 不会移除条目）
                    await r.xdel(STREAM, msg_id)
                    await r.srem(INFLIGHT_KEY, fields.get("path", ""))
        except asyncio.CancelledError:
            print("[ingest_queue] worker 停止")
            raise
        except Exception as e:
            print(f"[ingest_queue] worker 异常：{e}")
            await asyncio.sleep(2)


async def queue_stats() -> dict:
    """队列统计（供 /api/ingest/stats 展示）。"""
    if not await redis_available():
        return {"enabled": False}
    r = get_redis()
    try:
        info = await r.xinfo_groups(STREAM)
        pending, delivered, last_id = 0, 0, "0-0"
        for g in info:
            pending += g.get("pending", 0)
            delivered += g.get("consumers", 0)  # 占位，实际看 entries
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
    print("独立 worker 进程启动（Ctrl+C 退出）")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        sys.exit(0)
