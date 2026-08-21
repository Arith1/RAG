"""Redis 连接管理：进程内单例连接池 + 可用性探测 + 通用缓存助手。

Redis 服务于：入库任务队列（Streams）、限流计数、检索/embedding/回答缓存等。
Redis 不可用时各功能自动回退（任务队列回退进程内执行、限流回退进程内 dict、
缓存直接未命中），系统不因 Redis 故障而中断。
"""
import hashlib
import json
import os

import redis
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis: aioredis.Redis | None = None
_sync_redis: redis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """获取进程内单例 Redis 客户端（懒初始化，连接池由 redis-py 管理）。"""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def get_sync_redis() -> redis.Redis:
    """同步 Redis 客户端：供同步代码路径使用（如 embed_query 缓存），线程安全。"""
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _sync_redis


async def redis_available() -> bool:
    """探测 Redis 是否可用（超时快速失败，供各功能判断是否回退）。"""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


# ── 通用缓存（JSON 序列化；Redis 不可用时 get 返回 None / set 返回 False）──

def cache_key(prefix: str, *parts) -> str:
    """生成确定性缓存 key：prefix + SHA256(各段拼接)。"""
    joined = "|".join(str(p) for p in parts)
    return f"{prefix}:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


async def cache_get(key: str):
    """异步读缓存（供 async 代码路径）。未命中或 Redis 不可用返回 None。"""
    try:
        raw = await get_redis().get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None


async def cache_set(key: str, value, ttl: int) -> bool:
    """异步写缓存（TTL 秒）。Redis 不可用返回 False（静默降级）。"""
    try:
        await get_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        return False


def cache_get_sync(key: str):
    """同步读缓存（供同步代码路径，如 embed_query 内）。"""
    try:
        raw = get_sync_redis().get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None


def cache_set_sync(key: str, value, ttl: int) -> bool:
    """同步写缓存。"""
    try:
        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        return False


async def cache_clear_prefix(prefix: str) -> int:
    """按前缀清除缓存（如 "search:" / "ans:"）。

    用于文档入库/删除后让检索与回答缓存失效，避免旧数据在 TTL 内继续被返回。
    返回删除的 key 数；Redis 不可用返回 0。

    性能说明：
      - scan_iter 显式 count 控制每轮迭代量，避免默认小批次下多次往返
      - 用 UNLINK 替代 DELETE：内存异步释放，大 value 下不阻塞 Redis 主线程
      - 收集到 key 后经 pipeline 批量下发，减少网络往返次数
    """
    try:
        r = get_redis()
        deleted = 0
        batch: list = []
        async for key in r.scan_iter(f"{prefix}*", count=200):
            batch.append(key)
            if len(batch) >= 200:
                deleted += len(batch)
                await r.unlink(*batch)
                batch.clear()
        if batch:
            deleted += len(batch)
            await r.unlink(*batch)
        return deleted
    except Exception:
        return 0
