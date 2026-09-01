"""RAG 可观测性服务：采集每次问答请求的链路指标并异步写入 rag_traces。

设计：
  - API 层用 trace_request() 设置请求上下文（request_id / user_id / session_id），
    与 billing_request() 并存在同一请求作用域内；
  - chat 编排层在各阶段用 trace_set_intent() / trace_set_retrieval() /
    trace_set_generation() / trace_fail() 就地更新当前请求的 TraceContext；
  - 请求结束由 API 层 flush_trace() 落库；写入失败只记日志，绝不影响问答主流程。
  - 查询：list_traces（普通用户看自己 / 管理员全量）、get_trace_detail、
    get_trace_summary（1h/24h/7d 聚合）。
"""
import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.config.db_config import async_session
from rag个人知识库.models.obs import RagTrace
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.vector_store.milvus_store import COLLECTION_NAME, get_collection_row_count

logger = logging.getLogger(__name__)

# ── 进程内检索缓存计数（重启清零；仅本进程可见，用于存储概览）──
_retrieval_cache_hits = 0
_retrieval_cache_total = 0


def record_retrieval_cache(hit: bool) -> None:
    """记录一次检索缓存判定（命中/未命中），供进程内命中率统计。"""
    global _retrieval_cache_hits, _retrieval_cache_total
    _retrieval_cache_total += 1
    if hit:
        _retrieval_cache_hits += 1


def get_retrieval_cache_stats() -> dict:
    """返回进程内检索缓存命中计数与命中率。"""
    rate = round(_retrieval_cache_hits / _retrieval_cache_total, 4) if _retrieval_cache_total else 0.0
    return {"hits": _retrieval_cache_hits, "total": _retrieval_cache_total, "rate": rate}


@dataclass
class TraceContext:
    """一次请求的可观测性上下文：请求元信息 + 各阶段采集到的链路指标。"""

    request_id: str
    user_id: int
    session_id: Optional[str]
    intent: Optional[str] = None
    query: Optional[str] = None
    status: str = "success"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    intent_ms: int = 0
    retrieval_ms: int = 0
    generation_ms: int = 0
    retrieval_cache_hit: bool = False
    retrieval_has_scope: bool = True
    recall_count: int = 0
    rerank_count: int = 0
    rerank_avg_score: Optional[float] = None
    rerank_max_score: Optional[float] = None
    rerank_degraded: bool = False
    answer_len: int = 0
    sources: Optional[List[dict]] = None
    trace_type: Optional[str] = None
    query_raw: Optional[str] = None
    embedding_ms: int = 0
    milvus_ms: int = 0
    rerank_ms: int = 0
    cache_ms: int = 0
    # 内部：请求开始时刻，flush 时换算 total_ms
    _started_at: float = field(default_factory=time.monotonic, repr=False)


_trace_ctx: ContextVar[Optional[TraceContext]] = ContextVar("trace_ctx", default=None)


@contextmanager
def trace_request(ctx: TraceContext):
    """在请求作用域内设置 trace 上下文（contextvar），退出自动清理。"""
    token = _trace_ctx.set(ctx)
    try:
        yield
    finally:
        _trace_ctx.reset(token)


def get_trace_ctx() -> Optional[TraceContext]:
    """读取当前请求的 trace 上下文；未处于埋点作用域时返回 None。"""
    return _trace_ctx.get()


def trace_set_intent(
    intent: Optional[str],
    query: Optional[str],
    intent_ms: int = 0,
    query_raw: Optional[str] = None,
) -> None:
    """记录意图识别阶段：意图、提炼后查询与耗时；query_raw 为原始输入。"""
    ctx = _trace_ctx.get()
    if ctx is None:
        return
    ctx.intent = intent
    ctx.query = query
    ctx.intent_ms = int(intent_ms)
    if query_raw is not None:
        ctx.query_raw = query_raw


def trace_set_retrieval(
    retrieval_ms: int,
    cache_hit: bool = False,
    has_scope: bool = True,
    recall_count: int = 0,
    rerank_count: int = 0,
    rerank_avg_score: Optional[float] = None,
    rerank_max_score: Optional[float] = None,
    rerank_degraded: bool = False,
    sources: Optional[List[dict]] = None,
    embedding_ms: int = 0,
    milvus_ms: int = 0,
    rerank_ms: int = 0,
    cache_ms: int = 0,
) -> None:
    """记录检索/精排阶段：耗时、缓存命中、召回/精排数/分数、降级、来源、分跳耗时。"""
    ctx = _trace_ctx.get()
    if ctx is None:
        return
    ctx.retrieval_ms = int(retrieval_ms)
    ctx.retrieval_cache_hit = bool(cache_hit)
    ctx.retrieval_has_scope = bool(has_scope)
    ctx.recall_count = int(recall_count)
    ctx.rerank_count = int(rerank_count)
    ctx.rerank_avg_score = round(float(rerank_avg_score), 4) if rerank_avg_score is not None else None
    ctx.rerank_max_score = round(float(rerank_max_score), 4) if rerank_max_score is not None else None
    ctx.rerank_degraded = bool(rerank_degraded)
    ctx.sources = sources
    ctx.embedding_ms = int(embedding_ms or 0)
    ctx.milvus_ms = int(milvus_ms or 0)
    ctx.rerank_ms = int(rerank_ms or 0)
    ctx.cache_ms = int(cache_ms or 0)


def trace_set_generation(answer_len: int, generation_ms: int = 0) -> None:
    """记录 LLM 生成阶段：回答长度与耗时。"""
    ctx = _trace_ctx.get()
    if ctx is None:
        return
    ctx.answer_len = int(answer_len)
    ctx.generation_ms = int(generation_ms)


def trace_fail(error_type: str, error_message: Optional[str] = None) -> None:
    """把当前请求标记为失败并记录错误信息。"""
    ctx = _trace_ctx.get()
    if ctx is None:
        return
    ctx.status = "failed"
    ctx.error_type = error_type
    ctx.error_message = (error_message or "")[:512]


async def flush_trace(ctx: TraceContext) -> None:
    """把请求收集到的链路指标写入 rag_traces；失败只记日志。"""
    total_ms = int((time.monotonic() - ctx._started_at) * 1000)
    try:
        async with async_session() as db:
            db.add(
                RagTrace(
                    request_id=ctx.request_id,
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    intent=ctx.intent,
                    query=ctx.query,
                    status=ctx.status,
                    error_type=ctx.error_type,
                    error_message=ctx.error_message,
                    total_ms=total_ms,
                    intent_ms=ctx.intent_ms,
                    retrieval_ms=ctx.retrieval_ms,
                    retrieval_cache_hit=ctx.retrieval_cache_hit,
                    retrieval_has_scope=ctx.retrieval_has_scope,
                    recall_count=ctx.recall_count,
                    rerank_count=ctx.rerank_count,
                    rerank_avg_score=ctx.rerank_avg_score,
                    rerank_max_score=ctx.rerank_max_score,
                    rerank_degraded=ctx.rerank_degraded,
                    generation_ms=ctx.generation_ms,
                    answer_len=ctx.answer_len,
                    sources=ctx.sources,
                    trace_type=ctx.trace_type,
                    query_raw=ctx.query_raw,
                    embedding_ms=ctx.embedding_ms,
                    milvus_ms=ctx.milvus_ms,
                    rerank_ms=ctx.rerank_ms,
                    cache_ms=ctx.cache_ms,
                )
            )
            await db.commit()
        logger.info(
            "[obs] 已写入 rag_trace（request_id=%s status=%s total=%dms intent=%s）",
            ctx.request_id, ctx.status, total_ms, ctx.intent,
        )
    except Exception as e:
        logger.warning("[obs] 写入 rag_traces 失败（不影响问答）：%s", e)


# ── 链路查询（只读，不改表结构；前端「监控」页与管理员视角共用）──
def _range_start(range_key: str) -> Optional[datetime]:
    """把 range 参数换算成起始时间；all 返回 None（不限时间）。"""
    now = datetime.now()
    if range_key == "1h":
        return now - timedelta(hours=1)
    if range_key == "24h":
        return now - timedelta(hours=24)
    if range_key == "7d":
        return now - timedelta(days=7)
    return None


def _trace_to_dict(t: RagTrace) -> dict:
    return {
        "id": t.id,
        "request_id": t.request_id,
        "user_id": t.user_id,
        "session_id": t.session_id,
        "intent": t.intent,
        "query": t.query,
        "status": t.status,
        "error_type": t.error_type,
        "error_message": t.error_message,
        "total_ms": t.total_ms,
        "intent_ms": t.intent_ms,
        "retrieval_ms": t.retrieval_ms,
        "retrieval_cache_hit": bool(t.retrieval_cache_hit),
        "retrieval_has_scope": bool(t.retrieval_has_scope),
        "recall_count": t.recall_count,
        "rerank_count": t.rerank_count,
        "rerank_avg_score": float(t.rerank_avg_score) if t.rerank_avg_score is not None else None,
        "rerank_max_score": float(t.rerank_max_score) if t.rerank_max_score is not None else None,
        "rerank_degraded": bool(t.rerank_degraded),
        "generation_ms": t.generation_ms,
        "answer_len": t.answer_len,
        "sources": t.sources,
        "trace_type": t.trace_type,
        "query_raw": t.query_raw,
        "embedding_ms": t.embedding_ms,
        "milvus_ms": t.milvus_ms,
        "rerank_ms": t.rerank_ms,
        "cache_ms": t.cache_ms,
        "created_at": t.created_at,
    }


async def list_traces(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    is_admin: bool = False,
) -> dict:
    """链路列表（分页，可按 status 过滤）；管理员看全量，普通用户只看自己。"""
    cond = []
    if not is_admin:
        cond.append(RagTrace.user_id == user_id)
    if status:
        cond.append(RagTrace.status == status)
    total = int((await db.scalar(select(func.count(RagTrace.id)).where(*cond))) or 0)
    rows = (
        await db.execute(
            select(RagTrace)
            .where(*cond)
            .order_by(RagTrace.created_at.desc(), RagTrace.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"total": total, "items": [_trace_to_dict(t) for t in rows]}


async def get_trace_detail(
    db: AsyncSession,
    request_id: str,
    user_id: Optional[int] = None,
    is_admin: bool = False,
) -> Optional[dict]:
    """单条链路详情；非管理员只能取自己的记录，查不到返回 None。"""
    cond = [RagTrace.request_id == request_id]
    if not is_admin:
        cond.append(RagTrace.user_id == user_id)
    row = (await db.execute(select(RagTrace).where(*cond))).scalar_one_or_none()
    return _trace_to_dict(row) if row is not None else None


async def get_trace_summary(db: AsyncSession, range_key: str = "1h") -> dict:
    """链路聚合：请求量、成功率、各阶段平均耗时、零命中率、降级率、缓存命中率、意图分布。

    检索类指标（零命中/降级/缓存/检索/生成耗时）只统计 rag_ask 请求，
    避免 chat/other 等未检索意图把比率稀释。
    """
    since = _range_start(range_key)
    cond = []
    if since is not None:
        cond.append(RagTrace.created_at >= since)

    totals = (
        await db.execute(
            select(
                func.count(RagTrace.id),
                func.count(func.distinct(RagTrace.user_id)),
                func.coalesce(func.avg(RagTrace.total_ms), 0),
                func.coalesce(func.avg(RagTrace.intent_ms), 0),
            ).where(*cond)
        )
    ).one()
    total = int(totals[0])

    # 检索类指标聚合 rag_ask（chat 问答）与 search（检索接口）两类请求
    rag_ask_cond = [*cond, RagTrace.intent.in_(("rag_ask", "search"))]
    rag_ask_total = int(
        (await db.scalar(select(func.count(RagTrace.id)).where(*rag_ask_cond))) or 0
    )
    success = int(
        (
            await db.scalar(
                select(func.count(RagTrace.id)).where(*cond, RagTrace.status == "success")
            )
        )
        or 0
    )
    zero_hit = int(
        (
            await db.scalar(
                select(func.count(RagTrace.id)).where(
                    *rag_ask_cond, RagTrace.rerank_count == 0
                )
            )
        )
        or 0
    )
    degraded = int(
        (
            await db.scalar(
                select(func.count(RagTrace.id)).where(
                    *rag_ask_cond, RagTrace.rerank_degraded.is_(True)
                )
            )
        )
        or 0
    )
    cache_hit = int(
        (
            await db.scalar(
                select(func.count(RagTrace.id)).where(
                    *rag_ask_cond, RagTrace.retrieval_cache_hit.is_(True)
                )
            )
        )
        or 0
    )
    rag_ask_agg = (
        await db.execute(
            select(
                func.coalesce(func.avg(RagTrace.retrieval_ms), 0),
                func.coalesce(func.avg(RagTrace.generation_ms), 0),
            ).where(*rag_ask_cond)
        )
    ).one()

    intent_rows = (
        await db.execute(
            select(RagTrace.intent, func.count(RagTrace.id))
            .where(*cond)
            .group_by(RagTrace.intent)
            .order_by(func.count(RagTrace.id).desc())
        )
    ).all()
    intent_distribution = [{"intent": r[0], "count": int(r[1])} for r in intent_rows]

    # Top 慢请求：当前范围内 total_ms 最大的 5 条
    slow_rows = (
        await db.execute(
            select(
                RagTrace.request_id, RagTrace.query, RagTrace.intent,
                RagTrace.total_ms, RagTrace.status,
            )
            .where(*cond)
            .order_by(RagTrace.total_ms.desc())
            .limit(5)
        )
    ).all()
    slowest = [
        {
            "request_id": r[0], "query": r[1], "intent": r[2],
            "total_ms": int(r[3] or 0), "status": r[4],
        }
        for r in slow_rows
    ]

    # 失败分布：按 error_type 聚合 failed 请求
    fail_rows = (
        await db.execute(
            select(RagTrace.error_type, func.count(RagTrace.id))
            .where(*cond, RagTrace.status == "failed")
            .group_by(RagTrace.error_type)
            .order_by(func.count(RagTrace.id).desc())
        )
    ).all()
    failure_distribution = [{"error_type": r[0], "count": int(r[1])} for r in fail_rows]

    def _rate(n: int, base: int) -> float:
        return round(n / base, 4) if base else 0.0

    return {
        "range": range_key,
        "requests": total,
        "success_rate": _rate(success, total),
        "avg_total_ms": round(float(totals[2]), 1),
        "avg_intent_ms": round(float(totals[3]), 1),
        "avg_retrieval_ms": round(float(rag_ask_agg[0]), 1),
        "avg_generation_ms": round(float(rag_ask_agg[1]), 1),
        "zero_hit_rate": _rate(zero_hit, rag_ask_total),
        "rerank_degraded_rate": _rate(degraded, rag_ask_total),
        "retrieval_cache_hit_rate": _rate(cache_hit, rag_ask_total),
        "active_users": int(totals[1]),
        "intent_distribution": intent_distribution,
        "slowest": slowest,
        "failure_distribution": failure_distribution,
    }


async def get_storage_overview(db: AsyncSession) -> dict:
    """存储概览（管理员）：文档同步分布 + Milvus 行数 + 检索缓存命中计数。"""
    sync_rows = (
        await db.execute(
            select(VectorFile.sync_status, func.count(VectorFile.id))
            .group_by(VectorFile.sync_status)
            .order_by(func.count(VectorFile.id).desc())
        )
    ).all()
    by_sync_status = [{"status": r[0], "count": int(r[1])} for r in sync_rows]
    doc_total = sum(b["count"] for b in by_sync_status)
    milvus_rows = await asyncio.to_thread(get_collection_row_count)
    return {
        "documents": {"total": doc_total, "by_sync_status": by_sync_status},
        "milvus": {"row_count": milvus_rows, "collection": COLLECTION_NAME},
        "cache": get_retrieval_cache_stats(),
    }
