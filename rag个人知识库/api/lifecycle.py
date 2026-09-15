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
from rag个人知识库.api.settings import DELETE_GRACE_DAYS, TRUST_PROXY_HEADERS

logger = logging.getLogger(__name__)


async def _memory_cleanup_loop() -> None:
    """后台循环：定期清理过期对话记忆和超期 trace。"""
    while True:
        try:
            await cleanup_expired_memory(MEMORY_TTL_DAYS)
        except Exception as e:
            logger.warning("[api] 对话记忆清理任务异常：%s", e)
        try:
            deleted = await cleanup_expired_traces(TRACE_RETENTION_DAYS)
            if deleted:
                logger.info("[api] 已清理 %d 条超期 rag_traces", deleted)
        except Exception as e:
            logger.warning("[api] rag_traces 清理任务异常：%s", e)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 统一日志配置：未覆写时会输出到 stderr（uvicorn 启动时可看到各模块 INFO/WARNING 日志）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    await _check_business_tables()
    await seed_admin()
    cleanup_task = asyncio.create_task(_memory_cleanup_loop())
    # worker 内部会等待 Redis 恢复；不能在启动瞬间 Redis 不可用时就永久跳过。
    ingest_worker_task = asyncio.create_task(run_ingest_worker())
    delete_worker_task = asyncio.create_task(run_delete_worker())
    logger.info("[api] 入库/删除队列 worker 已启动（Redis 未就绪时内部自动重试）")
    logger.info("[api] 上传目录：%s", UPLOAD_DIR)
    logger.info("[api] 对话记忆 TTL=%s 天，清理间隔=%ss", MEMORY_TTL_DAYS, CLEANUP_INTERVAL_SECONDS)
    logger.info("[api] rag_traces 保留期=%s 天", TRACE_RETENTION_DAYS)
    yield
    for task in (cleanup_task, ingest_worker_task, delete_worker_task):
        task.cancel()
    await asyncio.gather(
        cleanup_task, ingest_worker_task, delete_worker_task,
        return_exceptions=True,
    )
    close_memory()  # 关闭对话记忆连接池（Postgres），释放连接与后台线程
    await engine.dispose()

async def _check_business_tables() -> None:
    """启动时校验 schema 版本与关键表/列，避免缺列到请求期才暴露。"""
    try:
        await ensure_schema(engine, include_parent_chunks=RAG_PARENT_CHILD)
        logger.info("[api] 数据库 schema 校验通过（版本=%s）", SCHEMA_VERSION)
    except Exception as e:
        logger.error("[api] 数据库 schema 缺失、版本不匹配或数据库未就绪：%s", e)
        raise RuntimeError(f"业务表检查失败：{e}") from e
