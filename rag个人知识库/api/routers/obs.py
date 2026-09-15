"""obs API routes."""
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


@router.get("/api/obs/summary")
async def obs_summary(
    range: str = Query(default="1h", pattern="^(1h|24h|7d|all)$"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """链路聚合：请求量、成功率、各阶段平均耗时、零命中/降级/缓存命中率、意图分布。"""
    return await get_trace_summary(db, range)

@router.get("/api/obs/traces")
async def obs_traces(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None, pattern="^(success|failed)$"),
    user_id: Optional[int] = Query(default=None, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """链路列表（分页，可按 status / user_id 过滤）；普通用户只能看自己。"""
    is_admin = user.role == "admin"
    if user_id is not None and not is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可按用户过滤")
    target_user_id = user_id if is_admin else user.id
    return await list_traces(db, page, page_size, status, user_id=target_user_id, is_admin=is_admin)

@router.get("/api/obs/traces/{request_id}")
async def obs_trace_detail(
    request_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """单条链路详情；普通用户只能看自己的。"""
    is_admin = user.role == "admin"
    detail = await get_trace_detail(db, request_id, user_id=user.id, is_admin=is_admin)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "未找到该链路")
    return detail

@router.get("/api/obs/storage")
async def obs_storage(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """存储概览（管理员）：文档同步分布 + Milvus 行数 + 检索缓存命中计数。"""
    return await get_storage_overview(db)
