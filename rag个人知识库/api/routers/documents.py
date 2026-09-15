"""documents API routes."""
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
from rag个人知识库.api.helpers import _upload_user_active
from rag个人知识库.api.schemas import *
from rag个人知识库.api.settings import *

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/documents/upload")
async def upload_documents(
    files: List[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    is_public: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    """批量上传文档并异步入库（加载→切分→MySQL→Milvus，MinerU 解析后台执行）。

    普通用户/管理员均可上传；文档归属当前用户（owner_id=user.id），
    is_public 控制是否共享（默认私有）。一次最多 MAX_BATCH_UPLOAD 个文件，
    逐个校验/写盘/入队，返回每个文件的独立结果，文件之间互不影响；
    所有文件成功提交后整体返回 200，单个文件失败不影响其他文件入库。
    """
    if not await allow_request(
        f"upload:{user.id}", UPLOAD_MAX_REQUESTS_PER_HOUR, 3600,
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "上传过于频繁，请稍后再试",
        )
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请至少选择一个文件")
    if len(files) > MAX_BATCH_UPLOAD:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"一次最多上传 {MAX_BATCH_UPLOAD} 个文件，当前选择了 {len(files)} 个",
        )
    # user 可能来自缓存；上传前回源校验一次，避免删除/禁用期间继续接受任务。
    user_state = await db.scalar(select(User.status).where(User.id == user.id))
    if user_state != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号当前不可上传文件")
    # 按用户分目录隔离：uploads/{user_id}/{file_name}
    user_dir = os.path.join(UPLOAD_DIR, str(user.id))
    os.makedirs(user_dir, exist_ok=True)

    results: List[dict] = []
    accepted = 0
    for file in files:
        # 跨平台安全取文件名：统一先转正斜杠再取最后一段，兼容 Windows/Linux 部署
        file_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not file_name:
            results.append({"file_name": file_name, "status": "error", "message": "文件名不能为空"})
            continue
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in ALLOWED_EXT:
            results.append({
                "file_name": file_name, "status": "error",
                "message": f"不支持的格式 {ext or '(无扩展名)'}，支持: {sorted(ALLOWED_EXT)}",
            })
            continue
        if ext == ".doc":
            results.append({
                "file_name": file_name, "status": "error",
                "message": "暂不支持旧版 .doc，请用 WPS/Word 另存为 .docx 后上传",
            })
            continue
        # 解析为用户目录内的绝对路径；盘符相对路径（如 "C:evil.pdf"）等逃逸
        # 文件名一律拒绝，避免写盘越过 uploads/{user_id} 边界。
        path = resolve_upload_file_path(user_dir, file_name)
        if path is None:
            results.append({
                "file_name": file_name, "status": "error",
                "message": "文件名不合法（不能包含盘符/冒号），无法保存",
            })
            continue
        # SET NX 原子声明同一路径的入库权，避免并发请求互相覆盖文件并重复入队。
        claimed = await claim_inflight(path)
        if claimed is None:
            results.append({"file_name": file_name, "status": "error", "message": "Redis 不可用，暂时无法提交入库任务"})
            continue
        if not claimed:
            results.append({"file_name": file_name, "status": "error", "message": "文档正在入库中，请勿重复上传"})
            continue
        # 写盘前再次回源：删除请求可能在批量上传过程中提交，避免为已删除账号继续写文件/入队。
        if not await _upload_user_active(user.id):
            await release_inflight(path, claimed)
            results.append({"file_name": file_name, "status": "error", "message": "账号已删除/禁用，无法上传文件"})
            continue
        temp_path = f"{path}.uploading-{uuid.uuid4().hex}"
        try:
            size = 0
            buf = []
            buf_len = 0

            def _flush_bytes(data: bytes) -> None:
                # L7：磁盘写放到线程池——大文件上传不再同步阻塞事件循环。
                # 注意：必须是非 async 函数——asyncio.to_thread 不会 await 协程，
                # 传 async def 只会创建一个被丢弃的协程对象，文件永远不会被写入
                # （曾导致 os.replace 报 WinError 2：临时文件从未创建）。
                with open(temp_path, "ab") as f:
                    f.write(data)

            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise ValueError(f"文件超过 {MAX_UPLOAD_SIZE // (1024 * 1024)}MB 上限")
                buf.append(chunk)
                buf_len += len(chunk)
                # 内存有界：每攒 ~4MB 经线程池落一次盘
                if buf_len >= 4 * 1024 * 1024:
                    await asyncio.to_thread(_flush_bytes, b"".join(buf))
                    buf = []
                    buf_len = 0
            if buf:
                await asyncio.to_thread(_flush_bytes, b"".join(buf))
            if size == 0:
                raise ValueError("文件为空")
            # 同目录 rename 是原子的，worker 不会看到半写入文件。
            os.replace(temp_path, path)
        except ValueError as e:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            await release_inflight(path, claimed)
            results.append({"file_name": file_name, "status": "error", "message": str(e)})
            continue
        except OSError as e:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            await release_inflight(path, claimed)
            logger.warning("[upload] 写入文件失败：%s（%s）", path, e)
            results.append({"file_name": file_name, "status": "error", "message": "文件写入失败，请稍后重试"})
            continue
        audit(db, user, "upload", target=file_name, detail=sanitize_source(path))
        # 入库任务必须进入 Redis Streams 持久队列，避免进程内存任务在崩溃时丢失
        msg_id = await enqueue_ingest(
            path, owner_id=user.id, is_public=is_public,
            already_claimed=True, inflight_token=claimed,
        )
        if msg_id is None:
            # 任务未入队时不能保留半成品文件，清理后按失败返回
            try:
                os.remove(path)
            except OSError:
                pass
            await release_inflight(path, claimed)
            results.append({"file_name": file_name, "status": "error", "message": "Redis 不可用，暂时无法提交入库任务"})
            continue
        accepted += 1
        results.append({
            "file_name": file_name,
            "status": "processing",
            "source": sanitize_source(path),
            "is_public": is_public,
            "message": "已提交入库队列，稍后刷新文档列表查看结果",
        })

    failed = len(results) - accepted
    summary = f"{accepted} 个文件已提交入库，{failed} 个文件失败" if failed else f"{accepted} 个文件已提交入库"
    if accepted:
        await db.commit()  # 提交本次上传的审计记录
        await invalidate_docs()  # 新文件进入队列，文档列表立即刷新（状态 processing）
    return {
        "status": "processing",
        "results": results,
        "accepted": accepted,
        "failed": failed,
        "message": summary,
    }

@router.get("/api/documents", response_model=DocumentListOut)
async def list_docs(
    user: User = Depends(get_current_user),
    limit: int = 100,
    offset: int = 0,
):
    """文档列表（登录用户可见：自己的 + 共享的；source 已脱敏不暴露本地路径）。

    返回 {total, items} 分页结构：total 为同一可见性规则下的文档总数，
    items 为当前页文档。默认 limit=100，单次最多 500 条。
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    cached = await get_cached_docs(user.id, limit, offset)
    if cached is not None:
        return cached
    docs, total = await list_documents(limit=limit, offset=offset, user_id=user.id, with_total=True)
    for d in docs:
        d["source"] = sanitize_source(d["source"])
    payload = {"total": total, "items": docs}
    await set_cached_docs(user.id, limit, offset, payload)
    return payload

@router.delete("/api/documents/{file_id}")
async def remove_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除自己的文档：Milvus 向量 + MySQL 元数据（级联 chunk）+ 磁盘文件 + 审计。

    普通用户和管理员都只能删除自己的文档；他人文档不可删除。
    若文档正在入库队列中处理（source 处于 inflight），返回 409 快速失败；
    删除本身在 delete_document 内部持有 owner 级分布式锁（与入库 worker 串行化），
    消除"删除按 source 清向量"与"并发重传写向量"的交错（H6）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    if record.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能删除自己的文档")
    # Redis 不可用时无法可靠判断入库状态，删除操作保守拒绝
    if not await redis_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis 不可用，暂时无法执行删除")
    if await is_inflight(record.source):
        raise HTTPException(status.HTTP_409_CONFLICT, "文档正在入库中，请稍后重试删除")
    try:
        ok = await delete_document(db, file_id, user, upload_dir=UPLOAD_DIR)
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    # 删除后只清理包含该文档 source 的检索/回答缓存，避免 TTL 内继续返回已删文档的旧结果
    await cache_clear_source(record.source)
    await invalidate_parent_cache_by_source(record.source)  # 父块切片缓存按 source 显式失效
    await bump_cache_generation()
    await invalidate_docs()  # 文档列表：该文档从所有可见者的列表中移除
    return {"status": "deleted", "file_id": file_id}

@router.post("/api/documents/{file_id}/revoke")
async def revoke_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把共享文档取消为私有（is_public=1 → 0）。

    文档所有者可把自己的共享文档改回私有；管理员可把任意公开文档设为私有（审核）。
    文档不存在返回 404，越权返回 403。
    """
    try:
        record = await revoke_document_public(db, file_id, user)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    # 取消共享后只清理包含该文档 source 的检索/回答缓存，不影响其他文档缓存
    await cache_clear_source(record.source)
    await bump_cache_generation()
    await invalidate_docs()  # 可见性变化影响所有可见者的文档列表
    return {
        "status": "revoked",
        "file_id": file_id,
        "file_name": record.file_name,
        "is_public": record.is_public,
    }

@router.post("/api/documents/{file_id}/share")
async def share_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把文档设为公开共享（is_public=0 -> 1）。

    仅文档所有者或管理员可操作；文档不存在返回 404，越权返回 403。
    """
    try:
        record = await share_document_public(db, file_id, user)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    # 设为公开后清理包含该文档的检索/回答缓存，让其他用户尽快检索到
    await cache_clear_source(record.source)
    await bump_cache_generation()
    await invalidate_docs()  # 可见性变化影响所有可见者的文档列表
    return {
        "status": "shared",
        "file_id": file_id,
        "file_name": record.file_name,
        "is_public": record.is_public,
    }

@router.get("/api/documents/{file_id}/download")
async def download_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载文档原件：仅 owner 或 is_public=1 的共享文档可下载。

    优先返回 OSS 签名/公有 URL；OSS 未启用且本地原件还在时直接回文件。
    服务器环境下原始文件已归档到 OSS，此接口提供可下载的链接（或直接流式返回本地副本）。
    P2：owner 非 active 的文档（含历史软删除账号的公开文档）一律 404，与列表/检索的幽灵数据口径一致。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    owner_status = await db.scalar(select(User.status).where(User.id == record.owner_id))
    if owner_status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")

    # 下载权限：仅 owner 或共享文档（is_public=1）可下载；无权限与不存在统一 404，避免探测 file_id
    if record.owner_id != user.id and not record.is_public:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")

    # 下载量统计：仅「非所有者（陌生人）」下载成功时 +1（owner 自己下载不计）
    if record.owner_id != user.id:
        record.download_count = (record.download_count or 0) + 1
        await db.commit()  # 提交计数，否则只改内存不落库
        # 让下载者自己的文档列表缓存立即刷新，避免 60s TTL 内刷新页面仍看到旧计数
        await cache_clear_prefix(f"docs:{user.id}:")

    # 1) OSS 启用的场景：返回会过期的签名 URL / 公有 URL，前端直接打开
    url = await build_download_url(record.source)
    if url:
        return {"file_name": record.file_name, "source": sanitize_source(record.source), "url": url, "expires_in": 3600}

    # 2) OSS 未启用且本地原件保留：直接流式返回本地文件
    media_type = {
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
    }.get(os.path.splitext(record.file_name)[1].lower(), "application/octet-stream")
    local = local_source_exists(record.source)
    if local:
        return FileResponse(local, media_type=media_type, filename=record.file_name)

    # 3) 原件既不在 OSS 也未保留本地（如旧数据的绝对路径且已归档）
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail="文档原件不存在（可能未归档或已清理）"
    )
