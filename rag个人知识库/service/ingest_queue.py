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
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime

from typing import Optional

from rag个人知识库.config.redis import get_redis, redis_available
from rag个人知识库.utils.sanitize import sanitize_source
from rag个人知识库.service.oss_archive import archive_local_file, rel_source_from_local
from rag个人知识库.service.operation_lock import owner_operation_lock
from rag个人知识库.service.service import ingest_files

logger = logging.getLogger(__name__)

STREAM = "ingest_queue"
DEAD_LETTER = "ingest_queue:dead"
GROUP = "ingest_workers"
INFLIGHT_KEY = "ingest:inflight"
INFLIGHT_LOCK_PREFIX = "ingest:lock:"
INFLIGHT_LOCK_TTL = int(os.getenv("INGEST_INFLIGHT_TTL", str(6 * 3600)))
RETRY_HASH = "ingest:retry"
RETRY_DELAY_KEY = "ingest:retry:delayed"
MAX_RETRIES = 3
CONSUMER = f"worker-{os.getpid()}"
# 崩溃恢复阈值：XAUTOCLAIM 的 min-idle 必须大于单条任务的最长处理耗时
# （MinerU 解析可达数分钟），否则会把仍在处理中的任务误判为崩溃残留并抢占，
# 与原 worker 争抢用户锁、误增重试计数。默认 10 分钟，可用环境变量调整。
RECOVER_IDLE_MS = int(os.getenv("INGEST_RECOVER_IDLE_MS", str(10 * 60 * 1000)))

# 只有持有者能释放锁：值格式为 source|token，释放时 Lua 比较后删除，
# 避免旧任务或失败任务误删并发上传/重试新建的锁。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def enqueue_ingest(
    file_path: str,
    owner_id: int,
    is_public: bool = False,
    *,
    already_claimed: bool = False,
    inflight_token: str | None = None,
) -> str | None:
    """把文件路径加入入库队列，返回消息 ID；Redis 不可用时返回 None。

    owner_id 为必填参数，从队列接口层面杜绝无主入库。
    inflight 集合统一存**相对 source**（uploads/{user_id}/file），与记录 source 口径一致，
    否则删除接口按 source 判断 in-flight 会对不上。
    """
    if not await redis_available():
        return None
    claimed_here = False
    if not already_claimed:
        claimed = await claim_inflight(file_path)
        if not claimed:
            return None
        inflight_token = claimed
        claimed_here = True
    r = get_redis()
    msg: dict = {
        "path": file_path,
        "owner_id": str(owner_id),
        "is_public": "1" if is_public else "0",
        "inflight_token": inflight_token or "",
    }
    try:
        return await r.xadd(STREAM, msg)
    except Exception as e:
        if claimed_here:
            await release_inflight(file_path, inflight_token)
        logger.warning("[ingest_queue] 入队失败：%s（%s）", file_path, e)
        return None


def _inflight_lock_key(file_path: str) -> str:
    source = rel_source_from_local(file_path)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"{INFLIGHT_LOCK_PREFIX}{digest}"


async def claim_inflight(file_path: str) -> str | bool | None:
    """原子声明文件入库权，返回本次声明的 token；已占用返回 False；不可用返回 None。"""
    if not await redis_available():
        return None
    source = rel_source_from_local(file_path)
    try:
        r = get_redis()
        token = uuid.uuid4().hex
        claimed = await r.set(
            _inflight_lock_key(file_path),
            f"{source}|{token}",
            nx=True,
            ex=INFLIGHT_LOCK_TTL,
        )
        if not claimed:
            return False
        await r.sadd(INFLIGHT_KEY, source)
        return token
    except Exception as e:
        logger.warning("[ingest_queue] 声明文件入库权失败：%s（%s）", source, e)
        return None


async def release_inflight(file_path: str, token: str | None = None) -> None:
    """释放文件入库权；token 匹配时才删除锁，同时清理旧版本集合成员。"""
    try:
        r = get_redis()
        source = rel_source_from_local(file_path)
        key = _inflight_lock_key(file_path)
        if token:
            await r.eval(_RELEASE_LUA, 1, key, f"{source}|{token}")
        else:
            # 旧版本消息没有 token：无条件清理（升级窗口内的兼容路径）
            await r.delete(key)
        if not await r.exists(key):
            await r.srem(INFLIGHT_KEY, source)
    except Exception as e:
        logger.warning("[ingest_queue] 释放文件入库权失败：%s（%s）", file_path, e)


async def is_inflight(file_path: str) -> bool:
    """文件是否正在入库（删除接口据此返回 409）。Redis 不可用时返回 False（放行）。"""
    if not await redis_available():
        return False
    r = get_redis()
    source = rel_source_from_local(file_path)
    if await r.exists(_inflight_lock_key(file_path)):
        return True
    # 兼容旧版本遗留集合；发现无对应租约时主动清掉陈旧成员。
    if await r.sismember(INFLIGHT_KEY, source):
        await r.srem(INFLIGHT_KEY, source)
    return False


async def _ensure_group() -> None:
    r = get_redis()
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:  # BUSYGROUP 已存在属正常
        if "BUSYGROUP" not in str(e):
            raise

async def _schedule_retry(fields: dict, delay: float) -> None:
    """把失败任务写入 Redis ZSET 延迟队列。

    之前用 asyncio.create_task 在进程内 sleep，进程崩溃会丢掉重试任务；
    改为持久化到 Redis，worker 重启后仍然能继续重试。
    """
    r = get_redis()
    member = json.dumps(fields, ensure_ascii=False)
    await r.zadd(RETRY_DELAY_KEY, {member: time.time() + delay})


async def _flush_due_retries() -> None:
    """把已到期的延迟重试任务重新入队。

    先入队，再删除 ZSET 成员：如果进程在入队前崩溃，任务仍在 ZSET 中不会丢；
    如果入队后崩溃，最多导致重复入队，但由于入库语义幂等，不会造成数据错误。
    使用 Redis 锁减少多 worker 并发扫描导致的重复入队。
    """
    try:
        r = get_redis()
        lock_key = f"{RETRY_DELAY_KEY}:lock"
        locked = await r.set(lock_key, "1", nx=True, ex=5)
        if not locked:
            return
        try:
            now = time.time()
            members = await r.zrangebyscore(RETRY_DELAY_KEY, "-inf", now)
            for member in members:
                try:
                    fields = json.loads(member)
                    path = fields.get("path")
                    if not path:
                        await r.zrem(RETRY_DELAY_KEY, member)
                        continue
                    if not fields.get("owner_id"):
                        logger.warning("[ingest_queue] 延迟重试任务缺少 owner_id，保留 ZSET：%s", path)
                        continue
                    owner_id = int(fields["owner_id"])
                    is_public = fields.get("is_public") == "1"
                    msg_id = await enqueue_ingest(path, owner_id=owner_id, is_public=is_public)
                    if msg_id is not None:
                        await r.zrem(RETRY_DELAY_KEY, member)
                    else:
                        logger.warning("[ingest_queue] 延迟重试 Redis 不可用，保留 ZSET 等待下次：%s", path)
                except Exception as e:
                    logger.warning("[ingest_queue] 延迟重试处理失败，保留 ZSET：%s（%s）", member, e)
        finally:
            await r.delete(lock_key)
    except Exception as e:
        logger.warning("[ingest_queue] 扫描延迟重试队列失败：%s", e)

async def _recover_pending() -> None:
    """回收上次崩溃未 ACK 的任务并**立即重新处理**（PEL 中的残留，空闲超过阈值）。

    注意：XAUTOCLAIM 只是把 pending 消息的所有权转移给本 consumer，
    消息不会自动重新投递——必须认领后手动执行 process_message + XACK，
    否则任务永久滞留 PEL、文件永不入库（本函数此前只打印不处理，属缺陷，已修复）。
    """
    r = get_redis()
    try:
        start = "0"
        while True:
            # xautoclaim 返回 (next_start_id, claimed_messages, deleted_ids)
            result = await r.xautoclaim(STREAM, GROUP, "recovery", RECOVER_IDLE_MS, start, count=100)
            claimed = result[1] if result else []
            if not claimed:
                break
            for msg_id, fields in claimed:
                path = fields.get("path", "")
                logger.info("[ingest_queue] 回收崩溃残留任务 %s 并重新处理: %s", msg_id, path)
                ok = await process_message(msg_id, fields)
                if ok:
                    await r.xack(STREAM, GROUP, msg_id)
                    await r.xdel(STREAM, msg_id)
                else:
                    await _handle_failure(msg_id, fields)
                await release_inflight(path, fields.get("inflight_token"))
            start = result[0] if result else "0"
    except Exception as e:
        logger.warning("[ingest_queue] 崩溃任务回收失败：%s", e)


async def process_message(msg_id: str, fields: dict) -> bool:
    """执行一次入库，成功返回 True。文件已不存在视为成功（可能被删除接口清理）。

    时序（原件保管关键）：
      入库成功 → 归档原件到 OSS → 成功才删本地 upload
      任何一步失败都返回 False 走重试；期间本地原件保留，重试可继续使用。
    """
    path = fields.get("path", "")
    if not path:
        return True
    if not os.path.isfile(path):
        logger.info("[ingest_queue] 任务 %s 文件已不存在，丢弃：%s", msg_id, path)
        return True
    try:
        if not fields.get("owner_id"):
            logger.warning("[ingest_queue] 任务 %s 缺少 owner_id，拒绝无主入库：%s", msg_id, path)
            return False
        owner_id = int(fields["owner_id"])
        is_public = fields.get("is_public") == "1"
        # 账户删除也持有同一把用户级锁，确保删除与入库不会交叉写 Milvus。
        async with owner_operation_lock(owner_id):
            result = await ingest_files([path], owner_id=owner_id, is_public=is_public)
            # 缓存失效（search/ans）已下沉到 service.ingest_files 统一处理
            if any(r.get("status") == "error" for r in result):
                # 入库失败：不归档、不删原件，走重试（本地文件仍在）
                return False
            if any(r.get("status") == "cancelled" for r in result):
                # 账号已删除/禁用时安全丢弃队列任务，不再归档或无限重试。
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.exception("[ingest_queue] 取消任务清理本地原件失败：%s", path)
                return True
            # 入库成功（inserted/updated/retried/skipped）：归档原件到 OSS，成功才删本地
            archived = await archive_local_file(path)
            if not archived:
                logger.warning("[ingest_queue] 任务 %s 归档 OSS 失败，保留本地原件重试：%s", msg_id, path)
            return archived
    except Exception as e:
        logger.warning("[ingest_queue] 任务 %s 处理失败：%s", msg_id, e)
        return False


async def _handle_failure(msg_id: str, fields: dict) -> None:
    """失败重试：指数退避重入队，超限进死信。

    关键顺序：先持久化重试计划（ZSET / 死信流），再 ACK + XDEL 原消息。
    若先 ACK 再写计划，进程在两步之间崩溃会同时失去原消息和重试计划（任务丢失）；
    先写计划再 ACK，最坏情况是崩溃后恢复重放导致重复计数/重复入队，
    而入库语义幂等（内容未变则 skip），可安全收敛。

    注意：必须从 fields 中保留 owner_id / is_public 并透传给重试消息，
    否则重试写入 vector_files 时因 owner_id NOT NULL 而必然失败。
    """
    r = get_redis()
    path = fields.get("path", "")
    retries = await r.hincrby(RETRY_HASH, path, 1)
    if retries >= MAX_RETRIES:
        await r.xadd(DEAD_LETTER, {
            "path": path,
            "owner_id": fields.get("owner_id", ""),
            "is_public": fields.get("is_public", ""),
            "error": f"重试 {retries} 次仍失败",
            "origin": msg_id,
        })
        await r.hdel(RETRY_HASH, path)
        logger.warning("[ingest_queue] 任务 %s 进入死信队列：%s", msg_id, path)
    else:
        delay = 2 ** int(retries)  # 指数退避 2s / 4s
        logger.warning("[ingest_queue] 任务 %s 第 %d 次失败，%ds 后重试：%s", msg_id, retries, delay, path)
        await _schedule_retry(fields, delay)
    await r.xack(STREAM, GROUP, msg_id)
    await r.xdel(STREAM, msg_id)  # 原消息删除，防止 stream 增长


async def run_worker(stop: "asyncio.Event | None" = None) -> None:
    """消费循环：阻塞读新任务 → 处理 → ACK / 失败重试。

    除启动时回收外，运行中每 60s 兜底回收一次 PEL（防"运行中崩溃"产生的残留，
    而不是只有启动时才恢复）。FastAPI lifespan 内嵌运行，或独立进程执行：
      python -m rag个人知识库.service.ingest_queue
    """
    await _ensure_group()
    await _recover_pending()
    await _flush_due_retries()
    r = get_redis()
    logger.info("[ingest_queue] worker 启动（consumer=%s）", CONSUMER)
    last_recover = time.monotonic()
    while not (stop is not None and stop.is_set()):
        try:
            # 先处理 Redis ZSET 中的到期延迟重试，再消费新消息
            await _flush_due_retries()
            # BLOCK 2s 等新消息；">" 只读尚未投递的新消息
            resp = await r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=1, block=2000)
            if not resp:
                # 空闲窗口内周期性兜底回收崩溃残留（认领后重新处理）
                if time.monotonic() - last_recover > 60:
                    await _recover_pending()
                    last_recover = time.monotonic()
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
                    await release_inflight(fields.get("path", ""), fields.get("inflight_token"))
        except asyncio.CancelledError:
            logger.info("[ingest_queue] worker 停止")
            raise
        except Exception as e:
            logger.warning("[ingest_queue] worker 异常：%s", e)
            try:
                await _ensure_group()  # 流/消费组被误删（如 FLUSHDB）时自愈
            except Exception:
                pass
            await asyncio.sleep(2)


async def queue_stats() -> dict:
    """队列统计（供 /api/ingest/stats 展示）。"""
    if not await redis_available():
        return {"enabled": False}
    r = get_redis()
    try:
        info = await r.xinfo_groups(STREAM)
        pending, last_id = 0, "0-0"
        for g in info:
            pending += g.get("pending", 0)
            last_id = g.get("last-delivered-id", last_id)
        n = await r.xlen(STREAM)
        dead = await r.xlen(DEAD_LETTER)
        inflight = 0
        async for _key in r.scan_iter(f"{INFLIGHT_LOCK_PREFIX}*", count=200):
            inflight += 1
        return {
            "enabled": True,
            "stream_len": n,
            "pending": pending,
            "dead_letter": dead,
            "inflight": inflight,
            "last_delivered_id": last_id,
        }
    except Exception:
        logger.exception("[ingest_queue] 队列统计失败")
        return {"enabled": True, "error": "queue_unavailable"}



# ── 队列管理（供 /api/ingest/* 展示与管理：待处理/入库中/死信/重试/清理）──

def _msg_time(msg_id: str) -> str:
    """把 Redis Stream 消息 ID 的毫秒时间戳转成本地 ISO 时间。"""
    try:
        ms = int(msg_id.split("-", 1)[0])
        return datetime.fromtimestamp(ms / 1000).isoformat(sep=" ", timespec="seconds")
    except Exception:
        return ""


async def list_pending(limit: int = 100) -> list[dict]:
    """列出待处理队列条目（最新 limit 条），附带重试次数与入队时间。"""
    if not await redis_available():
        return []
    r = get_redis()
    try:
        entries = await r.xrevrange(STREAM, "+", "-", count=limit)
    except Exception as e:
        logger.warning("[ingest_queue] 读取待处理队列失败：%s", e)
        return []
    result = []
    for msg_id, fields in entries:
        path = fields.get("path", "")
        owner_id = fields.get("owner_id")
        result.append({
            "msg_id": msg_id,
            # 路径脱敏：只返回相对 source / 文件名，避免向客户端泄露服务器绝对路径
            "path": sanitize_source(path),
            "file_name": os.path.basename(path.replace("\\", "/")),
            "owner_id": int(owner_id) if str(owner_id or "").isdigit() else None,
            "is_public": fields.get("is_public") == "1",
            "retries": int(await r.hget(RETRY_HASH, path) or 0),
            "enqueued_at": _msg_time(msg_id),
        })
    return result


async def list_inflight() -> list[dict]:
    """列出正在入库的文件（基于带 TTL 的锁，避免陈旧集合成员）。"""
    if not await redis_available():
        return []
    r = get_redis()
    try:
        paths = []
        async for key in r.scan_iter(f"{INFLIGHT_LOCK_PREFIX}*", count=200):
            raw = await r.get(key)
            if raw:
                source = raw.split("|", 1)[0]
                paths.append(source)
    except Exception as e:
        logger.warning("[ingest_queue] 读取入库中集合失败：%s", e)
        return []
    return [
        {
            "path": p,
            "file_name": os.path.basename(p.replace("\\", "/")),
        }
        for p in sorted(paths)
    ]


async def list_dead(limit: int = 100) -> list[dict]:
    """列出死信队列条目（最新 limit 条）：含失败原因与原始消息 ID。"""
    if not await redis_available():
        return []
    r = get_redis()
    try:
        entries = await r.xrevrange(DEAD_LETTER, "+", "-", count=limit)
    except Exception as e:
        logger.warning("[ingest_queue] 读取死信队列失败：%s", e)
        return []
    result = []
    for msg_id, fields in entries:
        path = fields.get("path", "")
        owner_id = fields.get("owner_id")
        result.append({
            "msg_id": msg_id,
            # 路径脱敏：只返回相对 source / 文件名，避免向客户端泄露服务器绝对路径
            "path": sanitize_source(path),
            "file_name": os.path.basename(path.replace("\\", "/")),
            "owner_id": int(owner_id) if str(owner_id or "").isdigit() else None,
            "is_public": fields.get("is_public") == "1",
            "error": fields.get("error", ""),
            "origin": fields.get("origin", ""),
            "dead_at": _msg_time(msg_id),
        })
    return result


async def retry_dead(msg_id: str) -> str | None:
    """把死信队列中的单条任务重新入队（保留 owner_id / is_public），返回新消息 ID。

    重试前从死信流删除原条目并重置该文件的重试计数；Redis 不可用或任务非法返回 None。
    """
    if not await redis_available():
        return None
    r = get_redis()
    try:
        entries = await r.xrange(DEAD_LETTER, msg_id, msg_id)
    except Exception as e:
        logger.warning("[ingest_queue] 读取死信条目失败：%s（%s）", msg_id, e)
        return None
    if not entries:
        return None
    fields = entries[0][1]
    path = fields.get("path", "")
    owner_id = fields.get("owner_id")
    if not path or not str(owner_id or "").isdigit():
        logger.warning("[ingest_queue] 死信条目缺少必要字段，删除：%s", msg_id)
        await r.xdel(DEAD_LETTER, msg_id)
        return None
    is_public = fields.get("is_public") == "1"
    new_id = await enqueue_ingest(path, owner_id=int(owner_id), is_public=is_public)
    if new_id is None:
        logger.warning("[ingest_queue] 死信重试入队失败（Redis 不可用）：%s", path)
        return None
    await r.xdel(DEAD_LETTER, msg_id)
    await r.hdel(RETRY_HASH, path)  # 重置重试计数，重新从 0 开始
    return new_id


async def retry_all_dead() -> dict:
    """把死信队列全部重新入队，返回成功/失败计数。"""
    if not await redis_available():
        return {"retried": 0, "failed": 0}
    r = get_redis()
    try:
        entries = await r.xrange(DEAD_LETTER, "-", "+")
    except Exception as e:
        logger.warning("[ingest_queue] 读取死信队列失败：%s", e)
        return {"retried": 0, "failed": 0}
    retried = failed = 0
    for msg_id, fields in entries:
        path = fields.get("path", "")
        owner_id = fields.get("owner_id")
        if not path or not str(owner_id or "").isdigit():
            await r.xdel(DEAD_LETTER, msg_id)
            failed += 1
            continue
        new_id = await enqueue_ingest(
            path, owner_id=int(owner_id), is_public=fields.get("is_public") == "1",
        )
        if new_id is not None:
            await r.xdel(DEAD_LETTER, msg_id)
            await r.hdel(RETRY_HASH, path)
            retried += 1
        else:
            failed += 1
    return {"retried": retried, "failed": failed}


async def clear_dead() -> dict:
    """清空死信队列，返回清理条数。"""
    if not await redis_available():
        return {"cleared": 0}
    r = get_redis()
    try:
        n = await r.xlen(DEAD_LETTER)
        await r.delete(DEAD_LETTER)
        return {"cleared": n}
    except Exception as e:
        logger.warning("[ingest_queue] 清空死信队列失败：%s", e)
        return {"cleared": 0}


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))
    print("独立 worker 进程启动（Ctrl+C 退出）")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        sys.exit(0)
