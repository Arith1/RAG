"""业务服务层：CLI 与前端（FastAPI 等）共用，返回 JSON 友好结构。

- ingest_files：批量入库/增量同步文件
- search_documents：双路召回 + rerank 精排检索
- list_documents：列出已入库文档
"""
from typing import List, Optional

from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import cache_clear_prefix, cache_get, cache_index_sources, cache_key, cache_set
from rag个人知识库.crud.vector import count_file_names, select_file_names, select_visible_file_ids
from rag个人知识库.service.ingest import ingest_files_batched
from rag个人知识库.vector_store.milvus_store import SEARCH_CACHE_TTL, asearch_with_rerank

# 数据发生变更（新增/更新/重放）的状态集合：命中任一即需清检索/回答缓存，
# 避免旧数据在 TTL 内继续被返回；skipped/error 不涉及数据变更，无需清缓存。
_CHANGED_STATUSES = {"inserted", "updated", "retried"}


async def ingest_files(
    file_paths: List[str],
    owner_id: int,
    is_public: Optional[bool] = None,
) -> List[dict]:
    """入库一组文件：先预检过滤 skip，再把复杂文档批量解析，按原始顺序返回结果。

    返回每个文件的结构化结果（status/version/added/unchanged/removed/message）。
    owner_id / is_public: 上传归属与共享标记（API 上传场景传入）。
    """
    async with async_session() as db:
        results = await ingest_files_batched(db, file_paths, owner_id=owner_id, is_public=is_public)

    # 缓存失效统一在这里处理：只要本批有任何文件实际更改了知识库数据，
    # 就清一次检索/回答缓存（全批只清一次）。与改造前"每个文件清两次"相比，
    # 批量入库（如队列连续消费 10 个文件）只需失效一次，避免缓存踩踏。
    if any(r.get("status") in _CHANGED_STATUSES for r in results):
        await cache_clear_prefix("search:")
        await cache_clear_prefix("ans:")
        # 文档列表缓存：chunk_count / sync_status 变化影响所有可见者的文档列表
        await cache_clear_prefix("docs:")
    return results


async def search_documents(
    query: str,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
    user_id: Optional[int] = None,
    retrieve_own_private: bool = True,
    retrieve_own_public: bool = True,
    retrieve_kb_public: bool = True,
    retrieve_owner_ids: Optional[List[int]] = None,
    file_ids: Optional[List[int]] = None,
) -> List[dict]:
    """检索：双路召回 Top recall_k → bge-reranker 精排 → 阈值过滤取 Top k。

    结果按 (query, k, source, expr, user_id, 检索范围) 缓存到 Redis
    （SEARCH_CACHE_TTL，默认 10 分钟），相同问题秒回，省 embedding + rerank 调用。
    返回 JSON 友好的命中列表。

    user_id 非 None 时的可见性控制（普通用户/管理员同规则）：
      - 先从 MySQL vector_files 按「会话检索范围」查该用户可见的文件 id；
      - 用 file_id in (...) 作为 Milvus 过滤条件，只召回可见文档的 chunk。
      检索范围四项：自己的私有文档 / 自己的公开文档 / 知识库公开文档（所有他人）/
      指定用户的公开文档（retrieve_owner_ids，多选，服务端强制 is_public=1）。
    user_id 为 None（CLI/评测）不做可见性过滤，行为与旧版一致。
    """
    owner_ids_digest = ",".join(str(x) for x in sorted({t for t in (retrieve_owner_ids or []) if t is not None}))
    cache_key_ = cache_key(
        "search", query, k, source or "", expr or "",
        user_id if user_id is not None else "",
        int(bool(retrieve_own_private)), int(bool(retrieve_own_public)),
        int(bool(retrieve_kb_public)), owner_ids_digest,
    )
    cached = await cache_get(cache_key_)
    if cached is not None:
        return cached

    # file_ids 可由调用方（chat 编排层）预先计算并复用；未传入时再查 MySQL
    if file_ids is None and user_id is not None:
        async with async_session() as db:
            file_ids = await select_visible_file_ids(
                db,
                user_id,
                retrieve_own_private=retrieve_own_private,
                retrieve_own_public=retrieve_own_public,
                retrieve_kb_public=retrieve_kb_public,
                retrieve_owner_ids=retrieve_owner_ids,
            )
    if user_id is not None and not file_ids:
        # 当前用户在当前检索范围内无任何可见文档，直接返回空，不发起 Milvus 检索
        return []

    hits = await asearch_with_rerank(query, k=k, expr=expr, source=source, file_ids=file_ids)
    result = [
        {
            "content": hit.page_content,
            "score": hit.metadata.get("rerank_score"),
            "source": hit.metadata.get("source"),
            "metadata": hit.metadata,
        }
        for hit in hits
    ]
    await cache_set(cache_key_, result, SEARCH_CACHE_TTL)
    await cache_index_sources(cache_key_, [h.get("source") for h in result])
    return result


async def list_documents(
    limit: Optional[int] = None,
    offset: int = 0,
    user_id: Optional[int] = None,
    with_total: bool = False,
):
    """列出已入库文档（读 vector_files），供用户选择检索范围。

    user_id 非 None 时只返回该用户可见的文档（本人或共享）；None 返回全部（CLI/管理）。
    按 updated_at 倒序返回；limit 为空时返回全部，offset 用于分页。
    with_total=True 时返回 (docs, total)：docs 为 [{id, file_name, version, source,
    chunk_count, sync_status, owner_id, is_public, download_count, updated_at}, ...]，
    total 为同一可见性规则下的文档总数（供前端分页统计）。
    """
    async with async_session() as db:
        files = await select_file_names(db, limit=limit, offset=offset, user_id=user_id)
        total = await count_file_names(db, user_id=user_id) if with_total else None
    docs = [
        {
            "id": f.id,
            "file_name": f.file_name,
            "version": str(f.version),
            "source": f.source,
            "chunk_count": f.chunk_count,
            "sync_status": f.sync_status,
            "owner_id": f.owner_id,
            "is_public": f.is_public,
            "download_count": f.download_count,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None,
        }
        for f in files
    ]
    if with_total:
        return docs, total
    return docs
