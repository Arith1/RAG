"""会话 / 用户 / 文档列表的 Redis 缓存（Cache-Aside + 登录预热）。

Key 设计：
  sess:list:{user_id}                会话列表（与 /api/chat/sessions 同构），TTL 1h
  sess:detail:{user_id}:{session_id} 单会话完整记录（含 messages），TTL 1h
  sess:detail_idx:{user_id}          Set：该用户已缓存详情的 session_id（删除账号时整批清理）
  docs:{user_id}:{limit}:{offset}    文档列表分页缓存，TTL 60s
  users:{viewer_id}:{limit}:{q}      用户搜索缓存（指定用户多选器），TTL 5min

一致性：
  - 会话列表/详情：每轮问答结束、重命名、删除会话时精准失效
  - 文档列表：上传/删除/共享/取消共享/入库完成时按前缀失效（数据变更影响所有可见者）
  - 用户搜索：注册/删除账号时按前缀失效
  - Redis 不可用时所有读写静默降级（get 返回 None → 回源 DB，set 忽略）
"""
import logging
from typing import List, Optional

from rag个人知识库.config.redis import cache_clear_prefix, cache_get, cache_set, get_redis
from rag个人知识库.service.chat_history import build_session_detail, get_session_info, list_sessions

logger = logging.getLogger(__name__)

SESSION_CACHE_TTL = 3600          # 会话列表/详情 1 小时
DOCS_CACHE_TTL = 60               # 文档列表 60s（入库是异步的，短 TTL 兜底 + 变更时前缀失效）
USER_SEARCH_CACHE_TTL = 300       # 用户搜索 5 分钟
WARMUP_TOP_N = 10                 # 登录预热：最多预热的最近会话数


# ── 会话列表 ──
def _list_key(user_id: int) -> str:
    return f"sess:list:{user_id}"


async def get_cached_session_list(user_id: int) -> Optional[list]:
    return await cache_get(_list_key(user_id))


async def set_session_list(user_id: int, items: list) -> None:
    await cache_set(_list_key(user_id), items, SESSION_CACHE_TTL)


async def invalidate_session_list(user_id: int) -> None:
    try:
        await get_redis().delete(_list_key(user_id))
    except Exception:
        pass


# ── 会话详情 ──
def _detail_key(user_id: int, session_id: str) -> str:
    return f"sess:detail:{user_id}:{session_id}"


def _detail_idx_key(user_id: int) -> str:
    return f"sess:detail_idx:{user_id}"


async def get_cached_session_detail(user_id: int, session_id: str) -> Optional[dict]:
    return await cache_get(_detail_key(user_id, session_id))


async def set_session_detail(user_id: int, session_id: str, payload: dict) -> None:
    try:
        r = get_redis()
        await r.sadd(_detail_idx_key(user_id), session_id)
        await r.expire(_detail_idx_key(user_id), SESSION_CACHE_TTL)
    except Exception:
        pass
    await cache_set(_detail_key(user_id, session_id), payload, SESSION_CACHE_TTL)


async def invalidate_session_detail(user_id: int, session_id: str) -> None:
    try:
        r = get_redis()
        await r.srem(_detail_idx_key(user_id), session_id)
        await r.delete(_detail_key(user_id, session_id))
    except Exception:
        pass


async def invalidate_user_sessions(user_id: int) -> None:
    """删除账号时清空该用户全部会话缓存（列表 + 所有已缓存详情）。"""
    try:
        r = get_redis()
        idx = _detail_idx_key(user_id)
        sids = list(await r.smembers(idx))
        if sids:
            await r.unlink(*(_detail_key(user_id, s) for s in sids))
        await r.delete(idx)
        await r.delete(_list_key(user_id))
    except Exception:
        pass


async def get_cached_session_info(user_id: int, session_id: str) -> Optional[dict]:
    """读取会话检索范围：优先从会话列表缓存定位（避免每轮对话都查 MySQL），
    列表缓存未命中再回源 MySQL。返回与 get_session_info 同构的 dict。"""
    cached_list = await get_cached_session_list(user_id)
    if cached_list is not None:
        for item in cached_list:
            if item.get("session_id") == session_id:
                return item
    return await get_session_info(user_id, session_id)


# ── 文档列表缓存 ──
def _docs_key(user_id: int, limit: int, offset: int) -> str:
    return f"docs:{user_id}:{limit}:{offset}"


async def get_cached_docs(user_id: int, limit: int, offset: int) -> Optional[dict]:
    return await cache_get(_docs_key(user_id, limit, offset))


async def set_cached_docs(user_id: int, limit: int, offset: int, payload: dict) -> None:
    await cache_set(_docs_key(user_id, limit, offset), payload, DOCS_CACHE_TTL)


async def invalidate_docs() -> None:
    """文档数据变更（上传/删除/共享/入库完成）后清空所有可见者的文档列表缓存。"""
    await cache_clear_prefix("docs:")


# ── 用户搜索缓存 ──
def _users_key(viewer_id: int, limit: int, q: str) -> str:
    return f"users:{viewer_id}:{limit}:{q or ''}"


async def get_cached_user_search(viewer_id: int, limit: int, q: str) -> Optional[list]:
    return await cache_get(_users_key(viewer_id, limit, q))


async def set_cached_user_search(viewer_id: int, limit: int, q: str, items: list) -> None:
    await cache_set(_users_key(viewer_id, limit, q), items, USER_SEARCH_CACHE_TTL)


async def invalidate_user_search() -> None:
    """注册/删除账号后清空用户搜索缓存（active 用户集合变化）。"""
    await cache_clear_prefix("users:")


# ── 登录预热 ──
async def warmup_user_sessions(user_id: int) -> None:
    """登录后后台预热：会话列表 + 最近 WARMUP_TOP_N 个会话的完整记录。

    后台任务执行，任何失败都只记日志，绝不影响登录响应。
    """
    try:
        items = await list_sessions(user_id)
        await set_session_list(user_id, items)
        for s in items[:WARMUP_TOP_N]:
            payload = await build_session_detail(user_id, s["session_id"])
            if payload is not None:
                await set_session_detail(user_id, s["session_id"], payload)
    except Exception as e:
        logger.warning("[session_cache] 登录预热失败（不影响登录）：%s", e)
