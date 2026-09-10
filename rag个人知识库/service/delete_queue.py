"""基于 Redis Streams 的账户删除队列（两阶段删除的执行端）。

调度：
  1. 用户请求删除（/api/auth/delete-account）：users.status='deleting'，
     该用户全部文档 is_public=0（立即锁定账号、下架公开内容），
     并写入 account_deletions（delete_after = 请求时间 + 宽限期）。
  2. 本 worker 周期性扫描 account_deletions，宽限期到期者 enqueue_delete 入队。

执行顺序（重要，保证不产生孤儿向量/不可下载原件）：
  a. 删除 Milvus 中该用户全部向量（按 owner_id 过滤）
  b. 删除阿里云 OSS 中该用户的文档原件（逐个 source）
  c. 只有 Milvus 和 OSS 都成功后，才删除 MySQL users 行
     （vector_files/chunk_records/chat_sessions/scope_users 由外键级联清理）
  d. 清理 Postgres 对话记忆（checkpoint，thread_id={user_id}:{session_id}）
  e. 清检索/回答缓存（按 source）与 Redis 会话缓存，标记 account_deletions=done
     计费(llm_usage)/链路(rag_traces)/审计(audit_logs) 记录保留（无外键，留痕）。

失败重试与 ingest_queue 保持一致：Consumer Group + PEL 崩溃恢复、指数退避、死信队列。
"""
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime

from sqlalchemy import delete, select, update

from rag个人知识库.agent.ai_assist import clear_thread
from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import cache_clear_source, get_redis, redis_available
from rag个人知识库.models.chat import ChatSession
from rag个人知识库.models.user import AccountDeletion, AuditLog, User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.oss_archive import delete_source_artifact, local_source_exists
from rag个人知识库.service.operation_lock import owner_operation_lock
from rag个人知识库.service.parent_child import invalidate_parent_cache_by_source
from rag个人知识库.service.session_cache import invalidate_user_sessions
from rag个人知识库.vector_store.milvus_store import adelete_chunks_by_owner

logger = logging.getLogger(__name__)

STREAM = "delete_queue"
DEAD_LETTER = "delete_queue:dead"
GROUP = "delete_workers"
INFLIGHT_KEY = "delete:inflight"
INFLIGHT_LOCK_PREFIX = "delete:lock:"
INFLIGHT_LOCK_TTL = int(os.getenv("DELETE_INFLIGHT_TTL", str(6 * 3600)))
RETRY_HASH = "delete:retry"
RETRY_DELAY_KEY = "delete:retry:delayed"
MAX_RETRIES = 3
CONSUMER = f"delete-worker-{os.getpid()}"
# 同 ingest_queue：恢复阈值必须大于单条删除任务最长耗时（OSS/Milvus 级联删除），
# 否则会抢占仍在处理中的任务，与原 worker 争抢用户级锁。默认 10 分钟。
RECOVER_IDLE_MS = int(os.getenv("DELETE_RECOVER_IDLE_MS", str(10 * 60 * 1000)))

# 只有持有者能释放锁，防止旧任务误删并发删除请求新建的锁。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def enqueue_delete(user_id: int) -> str | None:
    """把账户删除任务加入队列；Redis 不可用时返回 None，调用方回退后台任务。"""
    if not await redis_available():
        return None
    r = get_redis()
    member = str(user_id)
    key = f"{INFLIGHT_LOCK_PREFIX}{member}"
    try:
        token = uuid.uuid4().hex
        claimed = await r.set(key, f"{member}|{token}", nx=True, ex=INFLIGHT_LOCK_TTL)
        if not claimed:
            logger.warning("[delete_queue] 用户 %s 已有删除任务在执行，拒绝重复入队", user_id)
            return None
        await r.sadd(INFLIGHT_KEY, member)
        try:
            return await r.xadd(STREAM, {"user_id": member, "inflight_token": token})
        except Exception:
            # XADD 失败时不能留下永远的删除中标记。调用方会负责恢复 DB 状态。
            await release_delete_inflight(user_id, token)
            logger.exception("[delete_queue] 删除任务入队失败：user_id=%s", user_id)
            return None
    except Exception:
        logger.exception("[delete_queue] 声明删除任务入队权失败：user_id=%s", user_id)
        return None


async def release_delete_inflight(user_id: int | str, token: str | None = None) -> None:
    """释放账户删除的入队权；token 匹配时才删除锁，同时清理旧版本集合成员。"""
    try:
        r = get_redis()
        member = str(user_id)
        key = f"{INFLIGHT_LOCK_PREFIX}{member}"
        if token:
            await r.eval(_RELEASE_LUA, 1, key, f"{member}|{token}")
        else:
            await r.delete(key)
        if not await r.exists(key):
            await r.srem(INFLIGHT_KEY, member)
    except Exception as e:
        logger.warning("[delete_queue] 释放删除入队权失败：%s（%s）", user_id, e)


async def is_delete_inflight(user_id: int) -> bool:
    """是否已有该用户的删除任务在队列/inflight（用于幂等或状态展示）。"""
    if not await redis_available():
        return False
    return bool(await get_redis().exists(f"{INFLIGHT_LOCK_PREFIX}{user_id}"))


async def _ensure_group() -> None:
    r = get_redis()
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise
async def _schedule_retry(fields: dict, delay: float) -> None:
    """把删除失败任务写入 Redis ZSET 延迟队列，避免进程崩溃丢任务。"""
    r = get_redis()
    member = json.dumps(fields, ensure_ascii=False)
    await r.zadd(RETRY_DELAY_KEY, {member: time.time() + delay})


async def _flush_due_retries() -> None:
    """把已到期的账户删除延迟重试任务重新入队。"""
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
                    user_id = fields.get("user_id")
                    if not user_id:
                        await r.zrem(RETRY_DELAY_KEY, member)
                        continue
                    msg_id = await enqueue_delete(int(user_id))
                    if msg_id is not None:
                        await r.zrem(RETRY_DELAY_KEY, member)
                    else:
                        logger.warning("[delete_queue] 延迟重试 Redis 不可用，保留 ZSET 等待下次：%s", user_id)
                except Exception as e:
                    logger.warning("[delete_queue] 延迟重试处理失败，保留 ZSET：%s（%s）", member, e)
        finally:
            await r.delete(lock_key)
    except Exception as e:
        logger.warning("[delete_queue] 扫描延迟重试队列失败：%s", e)


async def _flush_due_deletions() -> None:
    """扫描已过宽限期的账号删除请求，到期者加入 delete_queue 彻底删除。

    两阶段删除调度：delete-account 接口置 status='deleting' 并写入 account_deletions
    （delete_after = 请求时间 + 宽限期）；本函数周期性扫描，到期后 enqueue_delete。
    Redis 不可用或入队失败时保留 pending，等待下次扫描重试。
    """
    try:
        async with async_session() as db:
            due_ids = list((await db.execute(
                select(AccountDeletion.user_id).where(
                    AccountDeletion.status == "pending",
                    AccountDeletion.delete_after <= datetime.now(),
                )
            )).scalars().all())
        for user_id in due_ids:
            msg_id = await enqueue_delete(user_id)
            if msg_id is None:
                # P3：入队失败可能是 Redis 不可用，也可能是该用户已有删除任务在飞（锁占用）；
                # 统一文案避免误导排障。保留 pending，下次扫描再试。
                logger.warning(
                    "[delete_queue] 账号 %s 宽限期到期但入队失败（Redis 不可用或该用户已有删除任务在飞），"
                    "保留 pending 等待下次扫描", user_id,
                )
                continue
            try:
                async with async_session() as db:
                    await db.execute(
                        update(AccountDeletion)
                        .where(AccountDeletion.user_id == user_id, AccountDeletion.status == "pending")
                        .values(status="enqueued")
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("[delete_queue] 标记账号 %s 删除请求为 enqueued 失败（不影响入队）：%s", user_id, e)
            logger.info("[delete_queue] 账号 %s 宽限期到期，已加入删除队列（彻底删除）", user_id)
    except Exception as e:
        logger.warning("[delete_queue] 扫描到期删除请求失败：%s", e)


async def _recover_pending() -> None:
    """回收上次崩溃未 ACK 的账户删除任务并立即重新处理。"""
    r = get_redis()
    try:
        start = "0"
        while True:
            result = await r.xautoclaim(STREAM, GROUP, "recovery", RECOVER_IDLE_MS, start, count=100)
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
                await release_delete_inflight(user_id, fields.get("inflight_token"))
            start = result[0] if result else "0"
    except Exception as e:
        logger.warning("[delete_queue] 崩溃任务回收失败：%s", e)


async def process_delete_message(msg_id: str, fields: dict) -> bool:
    """在用户级锁内执行删除，和入库 worker 串行化。"""
    raw = fields.get("user_id")
    if raw is None:
        return await _process_delete_message_unlocked(msg_id, fields)
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return await _process_delete_message_unlocked(msg_id, fields)
    try:
        async with owner_operation_lock(user_id):
            return await _process_delete_message_unlocked(msg_id, fields)
    except Exception as e:
        logger.warning("[delete_queue] 用户 %s 删除锁获取/处理失败：%s", user_id, e)
        return False


async def _process_delete_message_unlocked(msg_id: str, fields: dict) -> bool:
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
        # 第一步：只读 DB 获取用户状态和待删 OSS source 列表，尽快释放连接
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
            username = user.username
            sources = list((await db.execute(
                select(VectorFile.source).where(VectorFile.owner_id == user_id)
            )).scalars().all())

        # 第二步：外部资源删除（Milvus / OSS / 本地 uploads 原件），不再占用 DB 连接
        await adelete_chunks_by_owner(user_id)
        for source in sources:
            if not await delete_source_artifact(source):
                logger.warning(
                    "[delete_queue] 用户 %s 的 OSS 原件删除失败：%s，保留 MySQL 等待重试",
                    user_id, source,
                )
                return False
            # OSS 未启用或本地仍保留副本时，必须删除本地 uploads 原件
            local_path = local_source_exists(source)
            if local_path:
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.warning(
                        "[delete_queue] 用户 %s 的本地原件删除失败：%s（%s），保留 MySQL 等待重试",
                        user_id, local_path, e,
                    )
                    return False

        # 第三步：Milvus + OSS 都成功后才删 MySQL 用户（vector_files/chunks/chat_sessions 级联删除）
        session_ids: list = []
        async with async_session() as db:
            # 再次确认用户仍处于 deleting，避免误删已被恢复/重新激活的账号
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                logger.info("[delete_queue] 任务 %s 用户 %s 在外部删除期间已消失，丢弃", msg_id, user_id)
                return True
            if user.status != "deleting":
                logger.warning(
                    "[delete_queue] 任务 %s 用户 %s 状态已变为 %s，不再删除",
                    msg_id, user_id, user.status,
                )
                return True
            # 删除前先取该用户的会话 id 列表：chat_sessions 会随用户行级联删除，
            # 但 Postgres 里的对话记忆（checkpoint，thread_id={user_id}:{session_id}）
            # 不会级联，必须在删行前记住 id、删行后逐个清理，否则孤儿记忆永久泄漏。
            session_ids = list((await db.execute(
                select(ChatSession.session_id).where(ChatSession.user_id == user_id)
            )).scalars().all())
            db.add(AuditLog(
                user_id=user_id,
                username=username,
                action="delete_account",
                target=username,
                detail="delete_queue completed: milvus+oss+mysql+memory",
            ))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()

        # 账户删除后的清理（不占用 DB 会话连接）：
        # 1) 按 source 清检索/回答缓存，避免他人仍命中已删账号的旧共享结果
        for source in sources:
            await cache_clear_source(source)
            await invalidate_parent_cache_by_source(source)  # 父块切片缓存随账号删除显式失效
        # 2) 清该用户 Redis 会话列表/详情缓存
        await invalidate_user_sessions(user_id)
        # 3) 清 Postgres 对话记忆（best-effort，失败不影响删除结果）
        for sid in session_ids:
            try:
                await asyncio.to_thread(clear_thread, f"{user_id}:{sid}")
            except Exception as e:
                logger.warning("[delete_queue] 清理用户 %s 会话 %s 的对话记忆失败：%s", user_id, sid, e)
        # 4) 标记删除调度记录完成（留痕：本行不随用户行级联，user_id 无外键）
        try:
            async with async_session() as db:
                await db.execute(
                    update(AccountDeletion)
                    .where(AccountDeletion.user_id == user_id)
                    .values(status="done")
                )
                await db.commit()
        except Exception as e:
            logger.warning("[delete_queue] 标记账号 %s 删除调度记录完成失败（不影响删除结果）：%s", user_id, e)

        logger.info("[delete_queue] 用户 %s 已彻底删除（Milvus+OSS+MySQL+记忆 完成）", user_id)
        return True
    except Exception as e:
        logger.warning("[delete_queue] 任务 %s 处理失败：%s", msg_id, e)
        return False


async def _handle_failure(msg_id: str, fields: dict) -> None:
    """失败重试：指数退避重入队，超限进死信。

    顺序与 ingest_queue 一致：先持久化重试计划（ZSET / 死信流），
    再 ACK + XDEL 原消息，避免「已 ACK 未入重试队列」窗口内崩溃导致任务丢失。
    """
    r = get_redis()
    user_id = int(fields.get("user_id", 0))
    retries = await r.hincrby(RETRY_HASH, user_id, 1)
    if retries >= MAX_RETRIES:
        await r.xadd(DEAD_LETTER, {"user_id": str(user_id), "error": f"重试 {retries} 次仍失败", "origin": msg_id})
        await r.hdel(RETRY_HASH, user_id)
        logger.warning("[delete_queue] 任务 %s 进入死信队列：%s", msg_id, user_id)
    else:
        delay = 2 ** int(retries)
        logger.warning("[delete_queue] 任务 %s 第 %d 次失败，%ds 后重试：user_id=%s", msg_id, retries, delay, user_id)
        await _schedule_retry(fields, delay)
    await r.xack(STREAM, GROUP, msg_id)
    await r.xdel(STREAM, msg_id)


async def run_worker(stop: "asyncio.Event | None" = None) -> None:
    """账户删除队列消费循环，可由 FastAPI lifespan 内嵌或独立进程启动。"""
    # L6：启动段（建消费组/回收 PEL/刷新延迟重试/宽限期扫描）依赖 Redis——
    # 瞬时故障不得让 worker 停摆，带退避重试直到就绪
    while not (stop is not None and stop.is_set()):
        try:
            await _ensure_group()
            await _recover_pending()
            await _flush_due_retries()
            await _flush_due_deletions()  # 启动时先处理已过宽限期的删除请求
            break
        except Exception as e:
            logger.warning("[delete_queue] 启动初始化失败（Redis 未就绪？），5s 后重试：%s", e)
            await asyncio.sleep(5)
    r = get_redis()
    logger.info("[delete_queue] worker 启动（consumer=%s）", CONSUMER)
    last_recover = time.monotonic()
    last_delete_scan = time.monotonic()
    while not (stop is not None and stop.is_set()):
        try:
            # 先处理 Redis ZSET 中的到期延迟重试，再消费新消息
            await _flush_due_retries()
            # 周期性扫描 MySQL 中已过宽限期的账号删除请求（避免每 2s 空转查询 DB）
            if time.monotonic() - last_delete_scan > 60:
                await _flush_due_deletions()
                last_delete_scan = time.monotonic()
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
                    await release_delete_inflight(fields.get("user_id", ""), fields.get("inflight_token"))
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
        logger.exception("[delete_queue] 队列统计失败")
        return {"enabled": True, "error": "queue_unavailable"}


# ── 删除队列死信管理（P2：删除任务失败进死信后提供管理员重放路径，避免账号永久卡在 deleting）──

async def list_delete_dead(limit: int = 100) -> list[dict]:
    """列出账户删除死信队列条目（含失败原因与原始消息 ID）。"""
    if not await redis_available():
        return []
    r = get_redis()
    try:
        entries = await r.xrevrange(DEAD_LETTER, "+", "-", count=limit)
    except Exception as e:
        logger.warning("[delete_queue] 读取删除死信队列失败：%s", e)
        return []
    result = []
    for msg_id, fields in entries:
        raw = fields.get("user_id", "")
        result.append({
            "msg_id": msg_id,
            "user_id": int(raw) if str(raw).isdigit() else None,
            "error": fields.get("error", ""),
            "origin": fields.get("origin", ""),
        })
    return result


async def retry_delete_dead(msg_id: str) -> str | None:
    """重放单条删除死信：重新入队（保留 user_id），成功后清死信条目并重置重试计数。"""
    if not await redis_available():
        return None
    r = get_redis()
    try:
        entries = await r.xrange(DEAD_LETTER, msg_id, msg_id)
    except Exception as e:
        logger.warning("[delete_queue] 读取删除死信条目失败：%s（%s）", msg_id, e)
        return None
    if not entries:
        return None
    fields = entries[0][1]
    raw = fields.get("user_id", "")
    if not str(raw).isdigit():
        logger.warning("[delete_queue] 删除死信条目缺少有效 user_id，删除：%s", msg_id)
        await r.xdel(DEAD_LETTER, msg_id)
        return None
    new_id = await enqueue_delete(int(raw))
    if new_id is None:
        logger.warning("[delete_queue] 删除死信重试入队失败（Redis 不可用或锁占用）：%s", msg_id)
        return None
    await r.xdel(DEAD_LETTER, msg_id)
    await r.hdel(RETRY_HASH, str(raw))  # 重置重试计数，重新从 0 开始
    logger.info("[delete_queue] 删除死信 %s 已重新入队（user_id=%s）", msg_id, raw)
    return new_id


async def retry_all_delete_dead() -> dict:
    """重放全部删除死信，返回成功/失败计数。"""
    if not await redis_available():
        return {"retried": 0, "failed": 0}
    r = get_redis()
    try:
        entries = await r.xrange(DEAD_LETTER, "-", "+")
    except Exception as e:
        logger.warning("[delete_queue] 读取删除死信队列失败：%s", e)
        return {"retried": 0, "failed": 0}
    retried = failed = 0
    for msg_id, fields in entries:
        raw = fields.get("user_id", "")
        if not str(raw).isdigit():
            await r.xdel(DEAD_LETTER, msg_id)
            failed += 1
            continue
        new_id = await enqueue_delete(int(raw))
        if new_id is not None:
            await r.xdel(DEAD_LETTER, msg_id)
            await r.hdel(RETRY_HASH, str(raw))
            retried += 1
        else:
            failed += 1
    return {"retried": retried, "failed": failed}


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", ".env"))
    print("独立账户删除 worker 进程启动（Ctrl+C 退出）")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        sys.exit(0)
