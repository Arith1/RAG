"""system API routes."""
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


@router.get("/")
async def root():
    return {"app": "RAG 个人知识库", "docs": "/docs", "health": "/api/health"}

@router.get("/api/health")
async def health():
    """轻量 liveness 探针：进程存活即可返回，不依赖外部服务。"""
    return {"status": "ok"}

def _probe_milvus() -> None:
    """在工作线程中触发 Milvus 客户端初始化，避免阻塞事件循环。"""
    get_vector_store()

@router.get("/api/ready")
async def ready():
    """readiness 探针：检查 MySQL、Redis 和 Milvus 是否可用。"""
    checks = {"mysql": False, "redis": False, "milvus": False}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["mysql"] = True
    except Exception:
        logger.exception("[ready] MySQL 检查失败")
    try:
        checks["redis"] = bool(await redis_available())
        if not checks["redis"]:
            logger.warning("[ready] Redis 检查失败")
    except Exception:
        logger.exception("[ready] Redis 检查失败")
    try:
        await asyncio.to_thread(_probe_milvus)
        checks["milvus"] = True
    except Exception:
        logger.exception("[ready] Milvus 检查失败")
    if not all(checks.values()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}
