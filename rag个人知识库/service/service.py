"""业务服务层：CLI 与前端（FastAPI 等）共用，返回 JSON 友好结构。

- ingest_files：批量入库/增量同步文件
- search_documents：双路召回 + rerank 精排检索
- list_documents：列出已入库文档
"""
import asyncio
import os
import time
from typing import List, Optional

from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import (
    cache_clear_prefix, cache_clear_source, cache_get, cache_index_sources, cache_key,
    cache_set, cache_singleflight, jitter_ttl,
)
from rag个人知识库.crud.vector import count_file_names, select_file_names, select_visible_file_ids
from rag个人知识库.service.ingest import ingest_files_batched
from rag个人知识库.service.oss_archive import rel_source_from_local
from rag个人知识库.service.obs import record_retrieval_cache
from rag个人知识库.service.parent_child import assemble_context, load_parents
from rag个人知识库.vector_store.milvus_store import DEFAULT_RECALL_K, SEARCH_CACHE_TTL, asearch_with_rerank

# 分层检索（Parent-Child）开关（与 spliter/ingest 保持一致；默认关，保持现状）
RAG_PARENT_CHILD = os.getenv("RAG_PARENT_CHILD", "false").strip().lower() in ("1", "true", "yes", "on")
# M8：空结果检索缓存的短 TTL（秒）——新文档入库后最多该时长内仍可能命中「无结果」缓存
EMPTY_SEARCH_CACHE_TTL = int(os.getenv("EMPTY_SEARCH_CACHE_TTL", "30"))

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
        # 检索/回答缓存按 source 精细化失效：利用 src_idx:{source} 索引精确定位，
        # 只清受影响文档相关的缓存，避免全库清空（比 cache_clear_prefix("search:"/"ans:") 高效）。
        changed_sources = {
            rel_source_from_local(r["file_path"])
            for r in results
            if r.get("status") in _CHANGED_STATUSES and r.get("file_path")
        }
        for source in changed_sources:
            await cache_clear_source(source)
        # 文档列表缓存按用户维度缓存、未建立 source 索引，仍需整体失效
        await cache_clear_prefix("docs:")
    return results


async def search_documents(
    query: str,
    k: int = 3,
    recall_k: Optional[int] = None,  # None=用 DEFAULT_RECALL_K（默认 40）
    source: Optional[str] = None,
    expr: Optional[str] = None,
    user_id: Optional[int] = None,
    retrieve_own_private: bool = True,
    retrieve_own_public: bool = True,
    retrieve_kb_public: bool = True,
    retrieve_owner_ids: Optional[List[int]] = None,
    file_ids: Optional[List[int]] = None,
    return_metrics: bool = False,
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
    return_metrics=True 时返回 (result, metrics)，metrics 供可观测性埋点使用；
    默认 False 保持返回 List[dict]（/api/search 等调用方不受影响）。
    """

    # source 与 expr 互斥（与 Milvus search 同规则）：提前 fail-fast，
    # 避免非法组合继续走缓存/MySQL/检索链路，也避免写入错误的缓存 key。
    if source is not None and expr:
        raise ValueError("source 与 expr 互斥，不能同时指定（请二选一）")
    metrics = {
        "cache_hit": False,
        "has_scope": True,
        "recall_count": 0,
        "rerank_count": 0,
        "rerank_avg_score": None,
        "rerank_max_score": None,
        "rerank_degraded": False,
        "cache_ms": 0,
    }
    owner_ids_digest = ",".join(
        sorted({str(x) for x in (retrieve_owner_ids or []) if x is not None})
    )
    # None 表示由本函数按权限自动计算；显式 [] 表示调用方明确限定为空。
    # 两者必须区分，否则不同文件范围会复用错误的检索结果。
    file_ids_digest = (
        "<auto>" if file_ids is None else
        "<explicit>:" + ",".join(sorted({str(x) for x in file_ids}))
    )
    cache_key_ = cache_key(
        "search", query, k, source or "", expr or "", recall_k or DEFAULT_RECALL_K,  # M16：recall_k 入 key
        user_id if user_id is not None else "",
        int(bool(retrieve_own_private)), int(bool(retrieve_own_public)),
        int(bool(retrieve_kb_public)), owner_ids_digest, file_ids_digest,
        int(RAG_PARENT_CHILD),  # 分层模式位：新旧策略结果不串用缓存
    )
    _t_cache = time.monotonic()
    cached = await cache_get(cache_key_)
    cache_ms = int((time.monotonic() - _t_cache) * 1000)
    if cached is not None:
        record_retrieval_cache(True)
        metrics["cache_hit"] = True
        metrics["cache_ms"] = cache_ms
        # 缓存命中时无法拿到召回数，仅记录最终命中条数
        metrics["rerank_count"] = len(cached)
        return (cached, metrics) if return_metrics else cached

    # M13：单飞计算——多个并发 miss 只让一个真正检索，其余等待其写入缓存
    async def _compute():
        nonlocal file_ids
        # file_ids 可由调用方（chat 编排层）预先计算并复用；未传入时再查 MySQL
        if file_ids is None and user_id is not None:
            async with async_session() as db:
                file_ids = await select_visible_file_ids(
                    db, user_id,
                    retrieve_own_private=retrieve_own_private,
                    retrieve_own_public=retrieve_own_public,
                    retrieve_kb_public=retrieve_kb_public,
                    retrieve_owner_ids=retrieve_owner_ids,
                )
        if user_id is not None and not file_ids:
            # 当前用户在当前检索范围内无任何可见文档，直接返回空，不发起 Milvus 检索
            record_retrieval_cache(False)
            metrics["has_scope"] = False
            return []

        hits, hit_metrics = await asearch_with_rerank(
            query, k=k, recall_k=recall_k or DEFAULT_RECALL_K, expr=expr, source=source, file_ids=file_ids,
        )
        record_retrieval_cache(False)
        metrics.update(hit_metrics)

        # 分层检索：命中子块带 parent 元信息时，回填「命中点动态开窗」的父块上下文
        if RAG_PARENT_CHILD and hits and any(h.metadata.get("parent_id") for h in hits):
            child_hits = [
                {
                    "parent_id": h.metadata.get("parent_id"),
                    "parent_index": h.metadata.get("parent_index"),
                    "source": h.metadata.get("source"),
                    "start": h.metadata.get("parent_char_start"),
                    "end": h.metadata.get("parent_char_end"),
                    "score": h.metadata.get("rerank_score"),
                }
                for h in hits if h.metadata.get("parent_id")
            ]
            parents = await load_parents([h["parent_id"] for h in child_hits])
            windowed, _used_tokens = await asyncio.to_thread(assemble_context, child_hits, parents)
            # 退化路径：无 parent_id 的旧数据命中，或 parent_id 对应父块缺失（父块表被清/
            # 数据不一致）——均直接回填子块本身，避免命中被静默丢弃
            missing_parent_ids = {h["parent_id"] for h in child_hits} - set(parents.keys())
            legacy = [
                {
                    "content": h.page_content,
                    "score": h.metadata.get("rerank_score"),
                    "source": h.metadata.get("source"),
                    "metadata": h.metadata,
                }
                for h in hits
                if not h.metadata.get("parent_id")
                or h.metadata.get("parent_id") in missing_parent_ids
            ]
            result = windowed + legacy
        else:
            result = [
                {
                    "content": hit.page_content,
                    "score": hit.metadata.get("rerank_score"),
                    "source": hit.metadata.get("source"),
                    "metadata": hit.metadata,
                }
                for hit in hits
            ]
        # P3：统一按 score 降序输出（legacy 回退项按自身分数参与排序，不再垫底）；
        # 同分时 windowed 按 (parent_index, start) 保持文档内阅读序，legacy（无 parent_index）排在窗口之后
        result.sort(key=lambda h: (
            -(h.get("score") if h.get("score") is not None else -1.0),
            h.get("metadata", {}).get("parent_index", 2 ** 31),
            h.get("metadata", {}).get("parent_char_start", h.get("metadata", {}).get("start", 0)),
        ))
        # M8：空结果用短 TTL 缓存（新文档入库后最多 30s 内可见），非空用正常 TTL + 抖动（M13 雪崩防护）
        await cache_set(
            cache_key_, result,
            EMPTY_SEARCH_CACHE_TTL if not result else jitter_ttl(SEARCH_CACHE_TTL),
        )
        await cache_index_sources(cache_key_, [h.get("source") for h in result])
        return result

    result, from_cache = await cache_singleflight(cache_key_, _compute, SEARCH_CACHE_TTL, wait_ms=300)
    if from_cache:
        # 单飞等待者：他人已计算结果并写入缓存
        record_retrieval_cache(True)
        metrics["cache_hit"] = True
        metrics["cache_ms"] = cache_ms
        metrics["rerank_count"] = len(result)
        return (result, metrics) if return_metrics else result
    metrics["cache_ms"] = cache_ms
    return (result, metrics) if return_metrics else result


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
