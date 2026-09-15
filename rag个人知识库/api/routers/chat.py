"""chat API routes."""
from fastapi import APIRouter
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from rag个人知识库.api.auth import (
    DUMMY_PASSWORD_HASH,
    LOGIN_MAX_ATTEMPTS_PER_IP,
    allow_request,
    audit, check_allowed, clear_key, create_access_token, get_current_user,
    hash_password, record_failure, require_admin, seed_admin, verify_password,
    write_audit,
)
from rag个人知识库.config.db_config import async_session, engine, get_db
from rag个人知识库.config.redis import (
    bump_cache_generation,
    cache_clear_prefix,
    cache_clear_source,
    redis_available,
)
from rag个人知识库.config.schema import SCHEMA_VERSION, ensure_schema
from rag个人知识库.models.user import AccountDeletion, User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.chat import chat, chat_stream
from rag个人知识库.agent.ai_assist import clear_thread, close_memory
from rag个人知识库.service.chat_history import (
    build_session_detail,
    delete_session as delete_chat_session,
    list_sessions as list_chat_sessions,
    rename_session as rename_chat_session,
    upsert_chat_session,
)
from rag个人知识库.service.session_cache import (
    get_cached_docs,
    get_cached_session_detail,
    get_cached_session_info,
    get_cached_session_list,
    get_cached_user_search,
    invalidate_docs,
    invalidate_session_detail,
    invalidate_session_list,
    invalidate_user_search,
    invalidate_user_sessions,
    set_cached_docs,
    set_cached_user_search,
    set_session_detail,
    set_session_list,
    warmup_user_sessions,
)
from rag个人知识库.utils.sanitize import (
    resolve_upload_file_path,
    sanitize_source,
    sanitize_source_paths,
)
from rag个人知识库.service.delete_queue import (
    is_delete_inflight,
    list_delete_dead,
    retry_all_delete_dead,
    retry_delete_dead,
    run_worker as run_delete_worker,
)
from rag个人知识库.service.billing import (
    BillingContext,
    billing_request,
    flush_usage,
    get_admin_billing_overview,
    get_user_billing_summary,
    list_admin_user_usage,
    list_user_billing_usage,
)
from rag个人知识库.service.obs import (
    TRACE_RETENTION_DAYS,
    TraceContext,
    cleanup_expired_traces,
    flush_trace,
    get_storage_overview,
    get_trace_detail,
    get_trace_summary,
    list_traces,
    trace_fail,
    trace_request,
    trace_set_intent,
    trace_set_retrieval,
)
from rag个人知识库.service.document_admin import delete_document, revoke_document_public, share_document_public
from rag个人知识库.service.ingest_queue import (
    claim_inflight, clear_dead, enqueue_ingest, is_inflight, list_dead, list_inflight,
    list_pending, queue_stats, retry_all_dead, retry_dead,
    release_inflight, run_worker as run_ingest_worker,
)
from rag个人知识库.service.memory_maintenance import (
    CLEANUP_INTERVAL_SECONDS, MEMORY_TTL_DAYS, cleanup_expired_memory,
)
from rag个人知识库.service.oss_archive import (
    UPLOAD_DIR, build_download_url, local_source_exists,
)
from rag个人知识库.service.operation_lock import session_operation_lock
from rag个人知识库.service.parent_child import invalidate_parent_cache_by_source
from rag个人知识库.service.service import ingest_files, list_documents, search_documents
from rag个人知识库.splitter.spliter import RAG_PARENT_CHILD
from rag个人知识库.vector_store.milvus_store import get_vector_store
from rag个人知识库.api.helpers import *
from rag个人知识库.api.schemas import *
from rag个人知识库.api.settings import *

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/chat/sessions", response_model=List[ChatSessionOut])
async def chat_sessions(user: User = Depends(get_current_user)):
    """历史会话列表（问答侧边栏），按最后消息时间倒序；优先读 Redis 缓存。"""
    cached = await get_cached_session_list(user.id)
    if cached is not None:
        return cached
    items = await list_chat_sessions(user.id)
    await set_session_list(user.id, items)
    return items

@router.get("/api/chat/sessions/{session_id}", response_model=ChatSessionDetailOut)
async def chat_session_detail(session_id: str, user: User = Depends(get_current_user)):
    """读取单个历史会话及完整消息（消息从 Postgres checkpoint 加载，元信息从 MySQL）。

    优先读 Redis 缓存；未命中回源 MySQL + Postgres 后写回缓存（TTL 1h）。
    """
    cached = await get_cached_session_detail(user.id, session_id)
    if cached is not None:
        return cached
    payload = await build_session_detail(user.id, session_id)
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    await set_session_detail(user.id, session_id, payload)
    return payload

@router.patch("/api/chat/sessions/{session_id}", response_model=ChatSessionOut)
async def chat_session_rename(session_id: str, body: ChatRenameIn, user: User = Depends(get_current_user)):
    """重命名会话（侧边栏编辑标题）。"""
    ok = await rename_chat_session(user.id, session_id, body.title)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    await invalidate_session_list(user.id)
    await invalidate_session_detail(user.id, session_id)
    info = await get_cached_session_info(user.id, session_id)
    return ChatSessionOut(**info)

@router.delete("/api/chat/sessions/{session_id}")
async def chat_session_delete(session_id: str, user: User = Depends(get_current_user)):
    """删除会话：先清 Postgres 记忆，再删 MySQL 元信息，允许清理孤儿会话。"""
    try:
        async with session_operation_lock(user.id, session_id, required=True):
            try:
                memory_existed = await asyncio.to_thread(clear_thread, f"{user.id}:{session_id}")
            except Exception as e:
                logger.warning("[chat] 清除会话记忆失败：%s", e)
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "会话记忆暂时不可用，请稍后重试",
                )
            # 记忆已经不可恢复：无论 MySQL 行是否还存在，都必须先失效详情缓存。
            await invalidate_session_detail(user.id, session_id)
            ok = await delete_chat_session(user.id, session_id)
            if not ok and not memory_existed:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
            await invalidate_session_list(user.id)
            return {"status": "deleted", "session_id": session_id}
    except RuntimeError as e:
        code = status.HTTP_503_SERVICE_UNAVAILABLE if "Redis 不可用" in str(e) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(e))

async def _resolve_chat_scope(body: ChatIn, user: User) -> dict:
    """解析本次问答的检索范围。

    锁定规则：会话已存在（库中已有范围）→ 以库中为准（首问后不可更改）；
    新会话 / 会话不存在 → 用请求参数并做互斥与最少一项校验。
    """
    if body.session_id:
        info = await get_cached_session_info(user.id, body.session_id)
        if info is not None:
            return {
                "retrieve_own_private": info["retrieve_own_private"],
                "retrieve_own_public": info["retrieve_own_public"],
                "retrieve_kb_public": info["retrieve_kb_public"],
                "retrieve_owner_ids": info["retrieve_owner_ids"],
            }
    scope = {
        "retrieve_own_private": bool(body.retrieve_own_private),
        "retrieve_own_public": bool(body.retrieve_own_public),
        "retrieve_kb_public": bool(body.retrieve_kb_public),
        "retrieve_owner_ids": sorted({t for t in (body.retrieve_owner_ids or []) if t is not None}),
    }
    if scope["retrieve_kb_public"] and scope["retrieve_owner_ids"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "“知识库里的公开文档”与“指定用户的公开文档”互斥，请二选一",
        )
    if not (
        scope["retrieve_own_private"]
        or scope["retrieve_own_public"]
        or scope["retrieve_kb_public"]
        or scope["retrieve_owner_ids"]
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "至少需要选择一种检索范围",
        )
    return scope

async def _persist_chat_round(
    user_id: int,
    session_id: str,
    content: str,
    answer: str,
    intent: Optional[str],
    scope: dict,
) -> None:
    """一轮成功对话后写会话历史并失效相关缓存（失败不影响已返回的回答）。"""
    try:
        await upsert_chat_session(
            user_id, session_id, content, answer, intent, **scope,
        )
    except Exception as e:
        logger.warning("[chat] 会话历史落库失败（不影响回答）：%s", e)
    # 会话内容/摘要变化：失效该会话的列表与详情缓存
    await invalidate_session_list(user_id)
    await invalidate_session_detail(user_id, session_id)

@router.post("/api/chat", response_model=ChatOut)
async def chat_api(body: ChatIn, user: User = Depends(get_current_user)):
    """知识库问答。thread_id 按用户隔离：{user_id}:{session_id}，同一 session 保持多轮记忆。

    检索范围：首问（新会话）用请求参数并落库锁定；后续轮次以库中为准。
    """
    if not await allow_request(f"chat:{user.id}", CHAT_MAX_REQUESTS_PER_MINUTE):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "提问太频繁，请稍后再试",
        )
    scope = await _resolve_chat_scope(body, user)
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    request_id = uuid.uuid4().hex
    billing_ctx = BillingContext(
        request_id=request_id,
        user_id=user.id,
        session_id=session_id,
    )
    trace_ctx = TraceContext(
        request_id=request_id,
        user_id=user.id,
        session_id=session_id,
    )
    try:
        async with session_operation_lock(user.id, session_id):
            with billing_request(billing_ctx), trace_request(trace_ctx):
                try:
                    result = await chat(
                        body.content,
                        thread_id=thread_id,
                        user_id=user.id,
                        load_history=body.session_id is not None,
                        **scope,
                    )
                except Exception:
                    trace_fail("server_error", "问答服务内部异常")
                    logger.exception("[chat] 问答服务异常")
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "问答服务暂时不可用，请稍后重试",
                    )
                finally:
                    # 无论成功/失败都把已收集的 LLM 用量落库（失败只记日志，不影响返回）
                    await flush_usage(billing_ctx)
                    await flush_trace(trace_ctx)
            sources = result.get("sources", [])
            sanitize_source_paths(sources)
            # 模型不可用等失败轮次不落库：错误文案不进会话历史，
            # 也与 Agent 记忆一致（该轮 checkpoint 未写入）
            if not result.get("error"):
                await _persist_chat_round(
                    user.id, session_id, body.content, result["answer"],
                    result.get("intent"), scope,
                )
    except RuntimeError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return ChatOut(
        answer=result["answer"],
        intent=result.get("intent", ""),
        query=result.get("query"),
        sources=sources,
        session_id=session_id,
        error=result.get("error"),
    )

async def _iter_with_heartbeat(agen, interval: float = SSE_HEARTBEAT_SECONDS):
    """透传异步事件；长时间无事件时发送 SSE comment 维持连接。"""
    iterator = agen.__aiter__()
    next_task = asyncio.create_task(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({next_task}, timeout=interval)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                event = next_task.result()
            except StopAsyncIteration:
                break
            yield event
            next_task = asyncio.create_task(anext(iterator))
    finally:
        if not next_task.done():
            next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

@router.post("/api/chat/stream")
async def chat_stream_api(body: ChatIn, user: User = Depends(get_current_user)):
    """知识库问答（SSE 流式）：meta 事件 → 逐 token → done 事件。

    前端用 fetch ReadableStream 消费（SSE POST 不支持 EventSource），
    禁用缓冲保证逐段即时到达。流结束后把整轮对话落库（会话历史）。
    """
    if not await allow_request(f"chat:{user.id}", CHAT_MAX_REQUESTS_PER_MINUTE):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "提问太频繁，请稍后再试",
        )
    scope = await _resolve_chat_scope(body, user)
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    gen = chat_stream(
        body.content, thread_id=thread_id, session_id=session_id, user_id=user.id,
        load_history=body.session_id is not None,
        **scope,
    )

    async def _stream_with_persist():
        async with session_operation_lock(user.id, session_id):
            request_id = uuid.uuid4().hex
            billing_ctx = BillingContext(
                request_id=request_id,
                user_id=user.id,
                session_id=session_id,
            )
            trace_ctx = TraceContext(
                request_id=request_id,
                user_id=user.id,
                session_id=session_id,
            )
            final_answer = None
            final_intent = None
            stream_failed = False
            # 立即发送首帧，避免前端/代理把“检索尚未返回”误判为连接超时。
            yield ": connected\n\n"
            with billing_request(billing_ctx), trace_request(trace_ctx):
                try:
                    async for event in _iter_with_heartbeat(gen):
                        if event.get("sources"):
                            sanitize_source_paths(event["sources"])
                        etype = event.get("type")
                        if etype == "meta":
                            final_intent = event.get("intent")
                        elif etype in ("answer", "done") and event.get("answer"):
                            final_answer = event["answer"]
                        elif etype == "error":
                            # 失败轮次不落库：错误文案不进会话历史，也与未写入的 Agent 记忆一致
                            stream_failed = True
                            final_answer = None
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                finally:
                    # 流结束/中断都要把已收集的 LLM 用量落库（失败只记日志，不影响返回）
                    await flush_usage(billing_ctx)
                    await flush_trace(trace_ctx)
            # 流结束：仅成功轮次落库（error 事件已在上方标记）
            if final_answer is not None and not stream_failed:
                await _persist_chat_round(
                    user.id, session_id, body.content, final_answer, final_intent, scope,
                )

    return StreamingResponse(
        _stream_with_persist(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关 Nginx 缓冲（若前置代理）
        },
    )

@router.post("/api/search")
async def search_api(body: SearchIn, user: User = Depends(get_current_user)):
    """语义检索（双路召回 + rerank 精排），仅返回当前用户可见的文档（自己的 + 共享的）。

    与 chat 一样写 rag_traces（trace_type=search），让搜索流量进入全链路监控。
    """
    if not await allow_request(f"search:{user.id}", SEARCH_MAX_REQUESTS_PER_MINUTE):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "检索太频繁，请稍后再试",
        )
    request_id = uuid.uuid4().hex
    trace_ctx = TraceContext(
        request_id=request_id,
        user_id=user.id,
        session_id=None,
        trace_type="search",
    )
    with trace_request(trace_ctx):
        try:
            t0 = time.monotonic()
            hits, metrics = await search_documents(
                body.query, k=body.k, source=body.source, user_id=user.id, return_metrics=True,
            )
            retrieval_ms = int((time.monotonic() - t0) * 1000)
            trace_set_intent("search", 0, query_raw=body.query)
            trace_set_retrieval(
                retrieval_ms=retrieval_ms,
                cache_hit=bool(metrics.get("cache_hit")),
                has_scope=bool(metrics.get("has_scope", True)),
                recall_count=metrics.get("recall_count", 0) or 0,
                rerank_count=metrics.get("rerank_count", 0) or 0,
                rerank_avg_score=metrics.get("rerank_avg_score"),
                rerank_max_score=metrics.get("rerank_max_score"),
                rerank_degraded=bool(metrics.get("rerank_degraded")),
                sources=[
                    {"source": h.get("source"), "score": h.get("score")}
                    for h in hits if h.get("source")
                ],
                embedding_ms=metrics.get("embedding_ms", 0) or 0,
                milvus_ms=metrics.get("milvus_ms", 0) or 0,
                rerank_ms=metrics.get("rerank_ms", 0) or 0,
                cache_ms=metrics.get("cache_ms", 0) or 0,
            )
        except Exception:
            trace_fail("server_error", "检索服务异常")
            logger.exception("[search] 检索服务异常")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "检索服务暂时不可用，请稍后重试",
            )
        finally:
            await flush_trace(trace_ctx)
    sanitize_source_paths(hits)
    return {"query": body.query, "hits": hits}
