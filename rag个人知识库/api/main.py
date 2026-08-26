"""FastAPI 应用入口：认证 + RBAC + 文档管理 + 问答接口。

启动：
  uvicorn rag个人知识库.api.main:app --host 0.0.0.0 --port 8000
接口文档（Swagger UI）：http://localhost:8000/docs

权限模型（共享知识库）：
  - 普通用户：登录后仅可提问/检索/查看文档列表
  - 管理员：额外拥有文档上传/删除权限（RBAC 在服务端依赖强制，不依赖前端隐藏）
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.api.auth import (
    audit, check_allowed, clear_key, create_access_token, get_current_user,
    hash_password, record_failure, require_admin, seed_admin, verify_password,
    write_audit,
)
from rag个人知识库.config.db_config import engine, get_db
from rag个人知识库.config.redis import cache_clear_source, redis_available
from rag个人知识库.models.user import User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.chat import chat, chat_stream
from rag个人知识库.agent.ai_assist import clear_thread, load_thread_messages
from rag个人知识库.service.chat_history import (
    delete_session as delete_chat_session,
    get_session_info,
    list_sessions as list_chat_sessions,
    rename_session as rename_chat_session,
    upsert_chat_session,
)
from rag个人知识库.service.delete_queue import (
    enqueue_delete,
    run_worker as run_delete_worker,
)
from rag个人知识库.service.document_admin import delete_document, revoke_document_public, share_document_public
from rag个人知识库.service.ingest_queue import (
    clear_dead, enqueue_ingest, is_inflight, list_dead, list_inflight,
    list_pending, queue_stats, retry_all_dead, retry_dead,
    run_worker as run_ingest_worker,
)
from rag个人知识库.service.memory_maintenance import (
    CLEANUP_INTERVAL_SECONDS, MEMORY_TTL_DAYS, cleanup_expired_memory,
)
from rag个人知识库.service.oss_archive import (
    UPLOAD_DIR, build_download_url, local_source_exists,
)
from rag个人知识库.service.service import ingest_files, list_documents, search_documents

logger = logging.getLogger(__name__)

# ── 上传目录与限制（与 loader/oss_archive 的校验口径一致）──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_FILE_SIZE", 10*1024*1024))
MAX_BATCH_UPLOAD = int(os.getenv("MAX_BATCH_UPLOAD", 10))  # 单次批量上传文件数上限
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}


# ── 请求/响应模型 ──
class RegisterIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class ChatIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000, description="用户输入")
    session_id: Optional[str] = Field(default=None, max_length=64)  # 为空则服务端生成


class ChatOut(BaseModel):
    answer: str
    intent: str
    query: Optional[str]
    sources: List[dict]
    session_id: str
    error: Optional[str] = None


class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    k: int = Field(default=3, ge=1, le=50)  # 限制召回/精排条数，防超限请求
    source: Optional[str] = Field(default=None, max_length=512)

class ChatSessionOut(BaseModel):
    session_id: str
    title: str
    message_count: int
    last_message_at: Optional[str] = None
    last_message_preview: str = ''
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ChatMessageOut(BaseModel):
    role: str
    content: str
    sources: List[dict] = Field(default_factory=list)
    intent: Optional[str] = None
    created_at: Optional[str] = None


class ChatSessionDetailOut(BaseModel):
    session_id: str
    title: str
    messages: List[ChatMessageOut]


class ChatRenameIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=128)


class DocumentOut(BaseModel):
    id: int
    file_name: str
    version: str
    source: str
    chunk_count: int
    sync_status: str
    owner_id: Optional[int] = None
    is_public: bool = False
    # 下载量（非所有者下载 +1）与最近更新时间（知识库排序用；后端已提供，缺省兼容旧数据）
    download_count: int = 0
    updated_at: Optional[str] = None


class DocumentListOut(BaseModel):
    """文档列表分页响应：items 为当前页文档，total 为可见文档总数（供分页统计）。"""

    total: int
    items: List[DocumentOut]


# ── 生命周期：建表（幂等）+ 种子管理员 + 对话记忆清理后台任务 ──
async def _memory_cleanup_loop() -> None:
    """后台循环：定期清理过期的对话记忆（Postgres checkpoints）。"""
    while True:
        try:
            await cleanup_expired_memory(MEMORY_TTL_DAYS)
        except Exception as e:
            logger.warning("[api] 对话记忆清理任务异常：%s", e)
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
    ingest_worker_task = None
    delete_worker_task = None
    if await redis_available():
        ingest_worker_task = asyncio.create_task(run_ingest_worker())
        delete_worker_task = asyncio.create_task(run_delete_worker())
        logger.info("[api] 入库任务队列已启用（Redis Streams worker 启动）")
        logger.info("[api] 账户删除队列已启用（Redis Streams worker 启动）")
    else:
        logger.info("[api] Redis 不可用，入库任务回退进程内执行（配置 REDIS_URL 并启动 Redis 后启用队列）")
    logger.info("[api] 上传目录：%s", UPLOAD_DIR)
    logger.info("[api] 对话记忆 TTL=%s 天，清理间隔=%ss", MEMORY_TTL_DAYS, CLEANUP_INTERVAL_SECONDS)
    yield
    cleanup_task.cancel()
    if ingest_worker_task is not None:
        ingest_worker_task.cancel()
    if delete_worker_task is not None:
        delete_worker_task.cancel()
    await engine.dispose()


async def _check_business_tables() -> None:
    """启动时只读校验业务表存在（表结构由 models/vector.sql 维护，代码不做任何 DDL）。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM vector_files LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM audit_logs LIMIT 1"))
    except Exception as e:
        logger.error("[api] 业务表缺失或数据库未就绪，请先执行表结构初始化：")
        logger.error("      mysql -u root -p rag_demo < rag个人知识库/models/vector.sql")
        raise RuntimeError(f"业务表检查失败：{e}") from e


app = FastAPI(title="RAG 个人知识库", version="0.2.0", lifespan=lifespan)

# CORS：允许 Vue 开发服务器（Vite 默认 5173）跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _sse_events(gen):
    """把 chat_stream 的 JSON 事件封装为 SSE 帧：data: {json}\n\n"""
    async for event in gen:
        # 流式事件同样需要脱敏 source，避免暴露服务器本地绝对路径
        if event.get("sources"):
            _sanitize_source_paths(event["sources"])
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/")
async def root():
    return {"app": "RAG 个人知识库", "chat": "/chat", "docs": "/docs", "health": "/api/health"}


@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    """浏览器问答入口（手动测试用）。"""
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "chat.html")
    with open(page, encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ══ 认证 ══
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.post("/api/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    """开放注册，默认角色 user（管理员由环境变量播种）。按 IP 限流防批量注册。"""
    reg_key = f"reg|{_client_ip(request)}"
    if not await check_allowed(reg_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "注册过于频繁，请稍后再试")
    username = body.username.strip()
    if len(username) < 2 or len(body.password) < 6:
        await record_failure(reg_key)  # 注册只按失败次数计数
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名至少 2 个字符，密码至少 6 位")
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none() is not None:
        await record_failure(reg_key)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    user = User(username=username, password_hash=hash_password(body.password), role="user")
    db.add(user)
    await db.flush()
    audit(db, user, "register", target=username)
    await clear_key(reg_key)  # 注册成功后清空失败计数
    return UserOut(id=user.id, username=user.username, role=user.role)


@app.post("/api/auth/login", response_model=TokenOut)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """OAuth2 密码流登录，返回 JWT。滑动窗口限流（5 次/分钟），失败写入审计。"""
    key = f"login|{form.username}|{_client_ip(request)}"
    if not await check_allowed(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "尝试过于频繁，请 1 分钟后再试")
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()
    # 非 active 账号与不存在/密码错误统一返回 401，避免暴露账号状态
    if user is None or user.status != "active" or not verify_password(form.password, user.password_hash):
        await record_failure(key)
        await write_audit("login_failed", username=form.username, detail=f"ip={_client_ip(request)}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    await clear_key(key)
    return TokenOut(access_token=create_access_token(user), role=user.role)


@app.get("/api/auth/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut(id=user.id, username=user.username, role=user.role)


@app.post("/api/auth/delete-account", status_code=status.HTTP_202_ACCEPTED)
async def delete_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交账户删除请求。

    处理顺序：
      1. 当前用户 status 置为 deleting
      2. 该用户所有文档 is_public 置为 0（防止删除队列执行期间共享文档仍被他人检索）
      3. 进入 delete_queue（Redis Streams），队列内先删 Milvus，再删 OSS，最后删 MySQL 用户

    删除请求必须先成功进入 Redis Streams 再提交 DB，避免 status=deleting 但没有可靠任务导致账号卡死。
    """
    if user.status != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前账号状态不可删除")

    source_result = await db.execute(
        select(VectorFile.source).where(VectorFile.owner_id == user.id)
    )
    user_sources = list(source_result.scalars().all())

    user.status = "deleting"
    await db.execute(
        update(VectorFile)
        .where(VectorFile.owner_id == user.id)
        .values(is_public=False)
    )
    audit(db, user, "delete_account_request", target=user.username, detail="status=deleting, docs is_public=0")

    msg_id = await enqueue_delete(user.id)
    if msg_id is None:
        await db.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis 不可用，暂时无法提交账户删除请求")

    await db.commit()

    # 文档私有化后立即清理相关缓存，避免其他用户在删除队列执行前仍命中旧共享结果
    for source in user_sources:
        await cache_clear_source(source)

    return {
        "status": "deleting",
        "queued": True,
        "message": "已提交账户删除队列，删除完成后账号将无法登录",
    }


# ══ 文档管理（上传：登录用户可上传自己的文档；删除：本人或管理员）══
@app.post("/api/documents/upload")
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
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请至少选择一个文件")
    if len(files) > MAX_BATCH_UPLOAD:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"一次最多上传 {MAX_BATCH_UPLOAD} 个文件，当前选择了 {len(files)} 个",
        )
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
        content = await file.read()
        if not content:
            results.append({"file_name": file_name, "status": "error", "message": "文件为空"})
            continue
        if len(content) > MAX_UPLOAD_SIZE:
            results.append({"file_name": file_name, "status": "error", "message": "文件超过 10MB 上限"})
            continue
        path = os.path.join(user_dir, file_name)
        # 写盘/入队前先检查是否已有同一文件正在入库，避免并发重复任务
        if await is_inflight(path):
            results.append({"file_name": file_name, "status": "error", "message": "文档正在入库中，请勿重复上传"})
            continue
        with open(path, "wb") as f:
            f.write(content)
        audit(db, user, "upload", target=file_name, detail=_sanitize_source(path))
        # 入库任务必须进入 Redis Streams 持久队列，避免进程内存任务在崩溃时丢失
        msg_id = await enqueue_ingest(path, owner_id=user.id, is_public=is_public)
        if msg_id is None:
            # 任务未入队时不能保留半成品文件，清理后按失败返回
            try:
                os.remove(path)
            except OSError:
                pass
            results.append({"file_name": file_name, "status": "error", "message": "Redis 不可用，暂时无法提交入库任务"})
            continue
        accepted += 1
        results.append({
            "file_name": file_name,
            "status": "processing",
            "source": _sanitize_source(path),
            "is_public": is_public,
            "message": "已提交入库队列，稍后刷新文档列表查看结果",
        })

    failed = len(results) - accepted
    summary = f"{accepted} 个文件已提交入库，{failed} 个文件失败" if failed else f"{accepted} 个文件已提交入库"
    return {
        "status": "processing",
        "results": results,
        "accepted": accepted,
        "failed": failed,
        "message": summary,
    }


def _sanitize_source(path: str) -> str:
    """对外隐藏本地绝对路径；相对路径（uploads/{user_id}/文件）直接展示，不暴露服务器盘符。

    相对 source 是服务器环境下与 OSS 归档/下载一致的标准口径，脱敏后原样返回；
    绝对路径（存量/CLI 手动入库）则归一为 uploads/ 前缀的相对形式。
    """
    if not path:
        return path
    if path.startswith("uploads/"):
        return path  # 已是相对路径，直接展示
    abs_path = os.path.abspath(path)
    abs_upload = os.path.abspath(UPLOAD_DIR)
    if abs_path.startswith(abs_upload + os.sep):
        return "uploads/" + os.path.relpath(abs_path, abs_upload).replace(os.sep, "/")
    base = os.path.abspath(BASE_DIR)
    if abs_path.startswith(base + os.sep):
        return os.path.relpath(abs_path, base).replace(os.sep, "/")
    return os.path.basename(abs_path)


def _sanitize_source_paths(items: List[dict]) -> None:
    """就地脱敏 items 中的本地路径字段（source / metadata.source / metadata.md_path），
    兼容 chat 来源列表与检索命中列表，避免向客户端泄露服务器文件路径。"""
    for item in items:
        if item.get("source"):
            item["source"] = _sanitize_source(item["source"])
        meta = item.get("metadata")
        if isinstance(meta, dict):
            if meta.get("source"):
                meta["source"] = _sanitize_source(meta["source"])
            if meta.get("md_path"):
                meta["md_path"] = _sanitize_source(meta["md_path"])


@app.get("/api/documents", response_model=DocumentListOut)
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
    docs, total = await list_documents(limit=limit, offset=offset, user_id=user.id, with_total=True)
    for d in docs:
        d["source"] = _sanitize_source(d["source"])
    return {"total": total, "items": docs}


@app.delete("/api/documents/{file_id}")
async def remove_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除自己的文档：Milvus 向量 + MySQL 元数据（级联 chunk）+ 磁盘文件 + 审计。

    普通用户和管理员都只能删除自己的文档；他人文档不可删除。
    若文档正在入库队列中处理，返回 409，避免"删除先执行、入库后写完向量"的孤儿向量竞态。
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
    return {"status": "deleted", "file_id": file_id}
@app.post("/api/documents/{file_id}/revoke")
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
    return {
        "status": "revoked",
        "file_id": file_id,
        "file_name": record.file_name,
        "is_public": record.is_public,
    }




@app.post("/api/documents/{file_id}/share")
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
    return {
        "status": "shared",
        "file_id": file_id,
        "file_name": record.file_name,
        "is_public": record.is_public,
    }


@app.get("/api/documents/{file_id}/download")
async def download_document(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载文档原件：仅 owner 或 is_public=1 的共享文档可下载。

    优先返回 OSS 签名/公有 URL；OSS 未启用且本地原件还在时直接回文件。
    服务器环境下原始文件已归档到 OSS，此接口提供可下载的链接（或直接流式返回本地副本）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")

    # 下载权限：仅 owner 或共享文档（is_public=1）可下载；无权限与不存在统一 404，避免探测 file_id
    if record.owner_id != user.id and not record.is_public:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")

    # 下载量统计：仅「非所有者（陌生人）」下载成功时 +1（owner 自己下载不计）
    if record.owner_id != user.id:
        record.download_count = (record.download_count or 0) + 1

    # 1) OSS 启用的场景：返回会过期的签名 URL / 公有 URL，前端直接打开
    url = await build_download_url(record.source)
    if url:
        return {"file_name": record.file_name, "source": _sanitize_source(record.source), "url": url, "expires_in": 3600}

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


@app.get("/api/ingest/stats")
async def ingest_stats(user: User = Depends(get_current_user)):
    """入库任务队列状态（Redis Streams：待处理 / 死信 / 正在入库）。"""
    return await queue_stats()


@app.get("/api/ingest/queue")
async def ingest_queue_list(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
):
    """入库队列：列出待处理任务（最新 limit 条，含重试次数与入队时间）。"""
    return await list_pending(limit)


@app.get("/api/ingest/inflight")
async def ingest_inflight_list(user: User = Depends(get_current_user)):
    """入库队列：列出正在入库的文件。"""
    return await list_inflight()


@app.get("/api/ingest/dead")
async def ingest_dead_list(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
):
    """入库队列：列出失败（死信）任务（含失败原因）。"""
    return await list_dead(limit)


@app.post("/api/ingest/dead/retry-all")
async def ingest_dead_retry_all(admin: User = Depends(require_admin)):
    """入库队列：全部死信任务重新入队（管理员）。"""
    return await retry_all_dead()


@app.post("/api/ingest/dead/{msg_id}/retry")
async def ingest_dead_retry(msg_id: str, admin: User = Depends(require_admin)):
    """入库队列：单条死信任务重新入队（管理员）。"""
    new_id = await retry_dead(msg_id)
    if new_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "死信任务不存在或缺少必要字段，无法重试")
    return {"status": "retried", "msg_id": msg_id, "new_msg_id": new_id}


@app.delete("/api/ingest/dead")
async def ingest_dead_clear(admin: User = Depends(require_admin)):
    """入库队列：清空死信队列（管理员）。"""
    return await clear_dead()


# ══ 问答 / 检索（所有登录用户）══
# ══ 问答 / 检索（所有登录用户）══
@app.get("/api/chat/sessions", response_model=List[ChatSessionOut])
async def chat_sessions(user: User = Depends(get_current_user)):
    """历史会话列表（问答侧边栏），按最后消息时间倒序。"""
    return await list_chat_sessions(user.id)


@app.get("/api/chat/sessions/{session_id}", response_model=ChatSessionDetailOut)
async def chat_session_detail(session_id: str, user: User = Depends(get_current_user)):
    """读取单个历史会话及完整消息（消息从 Postgres checkpoint 加载，元信息从 MySQL）。"""
    info = await get_session_info(user.id, session_id)
    messages = await asyncio.to_thread(load_thread_messages, f"{user.id}:{session_id}")
    # 来源引用中的本地路径与实时问答一致做脱敏（绝对路径归一为 uploads/ 相对形式）
    for m in messages:
        if m.get("sources"):
            _sanitize_source_paths(m["sources"])
    if info is None and not messages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return {
        "session_id": session_id,
        "title": (info or {}).get("title") or "新会话",
        "messages": messages,
    }


@app.patch("/api/chat/sessions/{session_id}", response_model=ChatSessionOut)
async def chat_session_rename(session_id: str, body: ChatRenameIn, user: User = Depends(get_current_user)):
    """重命名会话（侧边栏编辑标题）。"""
    ok = await rename_chat_session(user.id, session_id, body.title)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    info = await get_session_info(user.id, session_id)
    return ChatSessionOut(**info)


@app.delete("/api/chat/sessions/{session_id}")
async def chat_session_delete(session_id: str, user: User = Depends(get_current_user)):
    """删除会话：先删 MySQL 元信息，再同步清除 Postgres 完整记忆。"""
    ok = await delete_chat_session(user.id, session_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    try:
        await asyncio.to_thread(clear_thread, f"{user.id}:{session_id}")
    except Exception as e:
        logger.warning("[chat] 清除会话记忆失败（不影响删除）：%s", e)
    return {"status": "deleted", "session_id": session_id}


@app.post("/api/chat", response_model=ChatOut)
async def chat_api(body: ChatIn, user: User = Depends(get_current_user)):
    """知识库问答。thread_id 按用户隔离：{user_id}:{session_id}，同一 session 保持多轮记忆。"""
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    try:
        result = await chat(body.content, thread_id=thread_id, user_id=user.id)
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"问答服务异常：{e}")
    sources = result.get("sources", [])
    _sanitize_source_paths(sources)
    # 完成一轮对话后写入会话历史（失败不影响回答本身）
    try:
        await upsert_chat_session(
            user.id, session_id, body.content,
            result["answer"], result.get("intent"),
        )
    except Exception as e:
        logger.warning("[chat] 会话历史落库失败（不影响回答）：%s", e)
    return ChatOut(
        answer=result["answer"],
        intent=result.get("intent", ""),
        query=result.get("query"),
        sources=sources,
        session_id=session_id,
        error=result.get("error"),
    )


@app.post("/api/chat/stream")
async def chat_stream_api(body: ChatIn, user: User = Depends(get_current_user)):
    """知识库问答（SSE 流式）：meta 事件 → 逐 token → done 事件。

    前端用 fetch ReadableStream 消费（SSE POST 不支持 EventSource），
    禁用缓冲保证逐段即时到达。流结束后把整轮对话落库（会话历史）。
    """
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    gen = chat_stream(body.content, thread_id=thread_id, session_id=session_id, user_id=user.id)

    async def _stream_with_persist():
        final_answer = None
        final_sources = []
        final_intent = None
        async for event in gen:
            if event.get("sources"):
                _sanitize_source_paths(event["sources"])
            etype = event.get("type")
            if etype == "meta":
                final_sources = event.get("sources") or []
                final_intent = event.get("intent")
            elif etype in ("answer", "done") and event.get("answer"):
                final_answer = event["answer"]
                final_sources = event.get("sources") or final_sources
                final_intent = event.get("intent") or final_intent
            elif etype == "error":
                final_answer = event.get("message") or "回答失败，请重试。"
                final_sources = []
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        # 流结束：有产出即落库（失败不影响已推送的回答）
        if final_answer is not None:
            try:
                await upsert_chat_session(
                    user.id, session_id, body.content,
                    final_answer, final_intent,
                )
            except Exception as e:
                logger.warning("[chat] 会话历史落库失败（不影响回答）：%s", e)

    return StreamingResponse(
        _stream_with_persist(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关 Nginx 缓冲（若前置代理）
        },
    )


@app.post("/api/search")
async def search_api(body: SearchIn, user: User = Depends(get_current_user)):
    """语义检索（双路召回 + rerank 精排），仅返回当前用户可见的文档（自己的 + 共享的）。"""
    try:
        hits = await search_documents(body.query, k=body.k, source=body.source, user_id=user.id)
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"检索服务异常：{e}")
    _sanitize_source_paths(hits)
    return {"query": body.query, "hits": hits}
