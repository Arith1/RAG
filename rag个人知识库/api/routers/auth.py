"""auth API routes."""
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
from rag个人知识库.api.helpers import _client_ip, _escape_like
from rag个人知识库.api.schemas import *
from rag个人知识库.api.settings import *

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    """注册入口：可通过环境变量关闭；成功/失败请求都占用 IP 滑动窗口额度。"""
    if not REGISTRATION_ENABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前未开放注册")
    reg_key = f"register:{_client_ip(request)}"
    if not await allow_request(reg_key, REGISTER_MAX_REQUESTS_PER_MINUTE):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "注册过于频繁，请稍后再试")
    username = body.username.strip()
    if len(username) < 2 or len(body.password) < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名至少 2 个字符，密码至少 6 位")
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    user = User(username=username, password_hash=hash_password(body.password), role="user")
    db.add(user)
    await db.flush()
    audit(db, user, "register", target=username)
    await invalidate_user_search()  # 新用户进入可检索列表，清用户搜索缓存
    return UserOut(id=user.id, username=user.username, role=user.role)

@router.get("/api/users/{user_id}/profile", response_model=ProfileOut)
async def user_profile(
    user_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查看任意用户的个人详情（仅公开安全字段）。

    - 他人账号非 active 视为不存在（404），避免已删除/禁用账号仍可被浏览。
    - 本人查看时 is_self=True，前端据此展示私有文档数与账号管理入口。
    """
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None or (target.status != "active" and target.id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return ProfileOut(
        id=target.id,
        username=target.username,
        role=target.role,
        created_at=target.created_at,
        is_self=(target.id == user.id),
    )

@router.get("/api/users/search", response_model=List[UserOut])
async def user_search(
    q: str = Query(default="", max_length=64, description="用户名关键字（模糊匹配）"),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户搜索（供问答页「指定用户的公开文档」多选器）。

    只返回 active 用户，且排除当前用户（自己的文档由「自己的私有/公开文档」覆盖）。
    按用户名模糊匹配（通配符已转义、按字面命中），按 username 字典序返回前 limit 条。
    """
    keyword = (q or "").strip()
    cached = await get_cached_user_search(user.id, limit, keyword)
    if cached is not None:
        return cached
    stmt = (
        select(User)
        .where(User.status == "active", User.id != user.id)
        .order_by(User.username.asc())
        .limit(limit)
    )
    if keyword:
        stmt = stmt.where(User.username.like(f"%{_escape_like(keyword)}%"))
    result = await db.execute(stmt)
    rows = result.scalars().all()
    items = [UserOut(id=r.id, username=r.username, role=r.role) for r in rows]
    await set_cached_user_search(user.id, limit, keyword, [i.model_dump() for i in items])
    return items

@router.post("/api/auth/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    body: ChangePasswordIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改密码：校验原密码 → 新密码至少 6 位且不与原密码相同 → 更新 bcrypt 哈希。"""
    if not verify_password(body.old_password, user.password_hash):
        await write_audit("change_password_failed", username=user.username, detail="old password mismatch")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    if len(body.new_password) < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码至少 6 位")
    if body.new_password == body.old_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与原密码相同")
    # 显式 UPDATE 落库：user 可能来自 Redis 缓存（游离对象），直接改属性 + commit 不会生效
    new_hash = hash_password(body.new_password)
    # L1：改密同时 token_version+1，吊销该用户所有已签发旧 token（必须重新登录）
    await db.execute(update(User).where(User.id == user.id).values(
        password_hash=new_hash, token_version=User.token_version + 1,
    ))
    await db.commit()
    audit(db, user, "change_password", target=user.username)
    return {"message": "密码修改成功，请重新登录"}

@router.post("/api/auth/login", response_model=TokenOut)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """OAuth2 密码流登录，返回 JWT；失败写入审计。

    M3：登录硬锁只按 IP（`login_ip|{ip}`，跨用户名共享阈值）——攻击者无法用
    自己的 IP 跨 IP 锁死指定用户名；按「用户名+IP」的失败计数仅作记录，不作硬锁。
    M2：无论用户是否存在/状态如何都执行一次 bcrypt（不存在/非 active 用假哈希），
    消除用户名枚举的时序侧信道；401 文案统一不暴露账号状态。
    """
    ip = _client_ip(request)
    ip_key = f"login_ip|{ip}"
    if not await check_allowed(ip_key, LOGIN_MAX_ATTEMPTS_PER_IP):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "尝试过于频繁，请 1 分钟后再试")
    user_key = f"login|{form.username}|{ip}"
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()
    # M2：恒执行一次 bcrypt（不存在/非 active 对假哈希校验，耗时与真实用户一致）
    password_ok = verify_password(
        form.password,
        user.password_hash if user is not None else DUMMY_PASSWORD_HASH,
    )
    # 非 active 账号与不存在/密码错误统一返回 401，避免暴露账号状态
    if user is None or user.status != "active" or not password_ok:
        await record_failure(ip_key)
        await record_failure(user_key)
        await write_audit("login_failed", username=form.username, detail=f"ip={ip}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    await clear_key(ip_key)
    await clear_key(user_key)
    # 登录后后台预热会话列表 + 最近 10 个会话记录（不阻塞登录响应）
    if background_tasks is not None:
        background_tasks.add_task(warmup_user_sessions, user.id)
    return TokenOut(access_token=create_access_token(user), role=user.role)

@router.get("/api/auth/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut(id=user.id, username=user.username, role=user.role)

@router.post("/api/auth/logout")
async def logout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """退出登录：递增 token_version，吊销该用户所有已签发 token。"""
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(token_version=User.token_version + 1)
    )
    audit(db, user, "logout", target=user.username)
    await db.commit()
    return {"status": "logged_out", "user_id": user.id}

@router.post("/api/auth/delete-account", status_code=status.HTTP_200_OK)
async def delete_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """请求删除账号（两阶段删除）：立即锁定 + 下架公开文档 → 宽限期后彻底清除。

    1) 把 status 置为 deleting：get_current_user 对非 active 状态返回 403，
       账号即刻不可登录/不可操作（现有 token 立即失效）。
    2) 把该用户全部公开文档置为私有（is_public=0）：owner 已离开后，
       内容不再被他人检索/列表/下载，堵住"已删账号公开文档永久可见"的漏洞。
    3) 写入 account_deletions 记录宽限期（DELETE_GRACE_DAYS 天，默认 7）；
       到期后由 Redis Streams 删除队列彻底清除 Milvus 向量 / OSS 原件 / 本地文件
       / MySQL 元数据（级联 vector_files/chunk_records/chat_sessions）/ Postgres 对话记忆。
    4) 计费(llm_usage)/链路(rag_traces)/审计(audit_logs) 记录保留（无外键，留痕）。
    """
    if user.status != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前账号状态不可删除")
    # 管理员不能删除自己的账号（防止误删唯一管理员导致系统失控）
    if user.role == "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员账号不能删除自己，请联系系统管理员处理")
    if await is_delete_inflight(user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "账号删除任务已在进行中，请勿重复操作")

    now = datetime.now()
    delete_after = now + timedelta(days=max(0, DELETE_GRACE_DAYS))

    # 1) 锁定账号（显式 UPDATE 落库：user 可能来自 Redis 缓存，直接改属性 + commit 不会生效）
    update_result = await db.execute(
        update(User)
        .where(User.id == user.id, User.status == "active")
        .values(status="deleting")
    )
    if update_result.rowcount != 1:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "账号删除状态已发生变化，请稍后重试")

    # 2) 立即下架该用户全部公开文档（is_public 置 0），堵住"已删账号内容仍公开"的隐私漏洞
    await db.execute(
        update(VectorFile)
        .where(VectorFile.owner_id == user.id, VectorFile.is_public.is_(True))
        .values(is_public=False)
    )

    # 3) 记录宽限期调度（幂等：已存在请求则刷新时间并回到 pending）
    existing = await db.get(AccountDeletion, user.id)
    if existing is None:
        db.add(AccountDeletion(user_id=user.id, delete_after=delete_after))
    else:
        existing.status = "pending"
        existing.delete_after = delete_after
        existing.requested_at = now
    audit(
        db, user, "delete_account", target=user.username,
        detail=f"status=deleting, grace={DELETE_GRACE_DAYS}d, delete_after={delete_after:%Y-%m-%d %H:%M:%S}",
    )
    await db.commit()

    # 4) 立即清缓存：会话列表/详情、文档列表、用户搜索，以及引用该用户文档的检索/回答缓存
    await invalidate_user_sessions(user.id)
    await invalidate_docs()
    await invalidate_user_search()
    rows = await db.execute(select(VectorFile.source).where(VectorFile.owner_id == user.id))
    for src in rows.scalars().all():
        await cache_clear_source(src)
        await invalidate_parent_cache_by_source(src)  # 父块切片缓存随账号删除显式失效
    await bump_cache_generation()

    return {
        "status": "deleting",
        "message": (
            f"删除请求已受理：账号已锁定，公开文档已下架；"
            f"将在 {max(0, DELETE_GRACE_DAYS)} 天后彻底删除（不可恢复），计费与审计记录将保留"
        ),
    }
