"""queues API routes."""
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


@router.get("/api/ingest/stats")
async def ingest_stats(admin: User = Depends(require_admin)):
    """入库任务队列状态（Redis Streams：待处理 / 死信 / 正在入库）。"""
    return await queue_stats()

@router.get("/api/ingest/queue")
async def ingest_queue_list(
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
):
    """入库队列：列出待处理任务（最新 limit 条，含重试次数与入队时间）。"""
    return await list_pending(limit)

@router.get("/api/ingest/inflight")
async def ingest_inflight_list(admin: User = Depends(require_admin)):
    """入库队列：列出正在入库的文件。"""
    return await list_inflight()

@router.get("/api/ingest/dead")
async def ingest_dead_list(
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
):
    """入库队列：列出失败（死信）任务（含失败原因）。"""
    return await list_dead(limit)

@router.post("/api/ingest/dead/retry-all")
async def ingest_dead_retry_all(admin: User = Depends(require_admin)):
    """入库队列：全部死信任务重新入队（管理员）。"""
    return await retry_all_dead()

@router.post("/api/ingest/dead/{msg_id}/retry")
async def ingest_dead_retry(msg_id: str, admin: User = Depends(require_admin)):
    """入库队列：单条死信任务重新入队（管理员）。"""
    new_id = await retry_dead(msg_id)
    if new_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "死信任务不存在或缺少必要字段，无法重试")
    return {"status": "retried", "msg_id": msg_id, "new_msg_id": new_id}

@router.delete("/api/ingest/dead")
async def ingest_dead_clear(admin: User = Depends(require_admin)):
    """入库队列：清空死信队列（管理员）。"""
    return await clear_dead()

@router.get("/api/delete-queue/dead")
async def delete_dead_list(
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
):
    """账户删除队列：列出死信任务（含失败原因与原始消息 ID）。"""
    return await list_delete_dead(limit)

@router.post("/api/delete-queue/dead/retry-all")
async def delete_dead_retry_all(admin: User = Depends(require_admin)):
    """账户删除队列：全部死信任务重新入队（管理员）。"""
    return await retry_all_delete_dead()

@router.post("/api/delete-queue/dead/{msg_id}/retry")
async def delete_dead_retry(msg_id: str, admin: User = Depends(require_admin)):
    """账户删除队列：单条死信任务重新入队（管理员）。"""
    new_id = await retry_delete_dead(msg_id)
    if new_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "死信任务不存在或缺少必要字段，无法重试")
    return {"status": "retried", "msg_id": msg_id, "new_msg_id": new_id}
