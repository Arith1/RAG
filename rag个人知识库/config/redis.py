"""Redis 连接管理：进程内单例连接池 + 可用性探测。

Redis 服务于：入库任务队列（Streams）、限流计数、检索/embedding 缓存等。
Redis 不可用时各功能自动回退（任务队列回退进程内执行、限流回退进程内 dict），
系统不因 Redis 故障而中断。
"""
import os

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """获取进程内单例 Redis 客户端（懒初始化，连接池由 redis-py 管理）。"""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def redis_available() -> bool:
    """探测 Redis 是否可用（超时快速失败，供各功能判断是否回退）。"""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
