"""业务服务层：CLI 与前端（FastAPI 等）共用，返回 JSON 友好结构。

- ingest_files：批量入库/增量同步文件
- search_documents：双路召回 + rerank 精排检索
- list_documents：列出已入库文档
"""
from typing import List, Optional

from rag个人知识库.config.db_config import AsyncSession
from rag个人知识库.config.redis import cache_get, cache_key, cache_set
from rag个人知识库.crud.vector import select_file_names
from rag个人知识库.service.ingest import ingest_files_batched
from rag个人知识库.vector_store.milvus_store import SEARCH_CACHE_TTL, asearch_with_rerank


async def ingest_files(file_paths: List[str]) -> List[dict]:
    """入库一组文件：先预检过滤 skip，再把复杂文档批量解析，按原始顺序返回结果。

    返回每个文件的结构化结果（status/version/added/unchanged/removed/message）。
    """
    async with AsyncSession() as db:
        return await ingest_files_batched(db, file_paths)


async def search_documents(
    query: str,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
) -> List[dict]:
    """检索：双路召回 Top recall_k → bge-reranker 精排 → 阈值过滤取 Top k。

    结果按 (query, k, source, expr) 缓存到 Redis（SEARCH_CACHE_TTL，默认 10 分钟），
    相同问题秒回，省 embedding + rerank 调用。返回 JSON 友好的命中列表。
    """
    cache_key_ = cache_key("search", query, k, source or "", expr or "")
    cached = await cache_get(cache_key_)
    if cached is not None:
        return cached

    hits = await asearch_with_rerank(query, k=k, expr=expr, source=source)
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
    return result


async def list_documents(limit: Optional[int] = None, offset: int = 0) -> List[dict]:
    """列出已入库文档（读 vector_files），供用户选择检索范围。

    按 updated_at 倒序返回；limit 为空时返回全部，offset 用于分页。
    返回 [{id, file_name, version, source, chunk_count, sync_status}, ...]
    """
    async with AsyncSession() as db:
        files = await select_file_names(db, limit=limit, offset=offset)
    return [
        {
            "id": f.id,
            "file_name": f.file_name,
            "version": str(f.version),
            "source": f.source,
            "chunk_count": f.chunk_count,
            "sync_status": f.sync_status,
        }
        for f in files
    ]
