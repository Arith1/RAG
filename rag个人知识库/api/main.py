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
import os
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.api.auth import (
    audit, check_allowed, clear_key, create_access_token, get_current_user,
    hash_password, record_failure, require_admin, seed_admin, verify_password,
    write_audit,
)
from rag个人知识库.config.db_config import engine, get_db
from rag个人知识库.config.redis import cache_clear_prefix, redis_available
from rag个人知识库.models.user import User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.chat import chat, chat_stream
from rag个人知识库.service.document_admin import delete_document
from rag个人知识库.service.ingest_queue import (
    enqueue_ingest, is_inflight, queue_stats, run_worker,
)
from rag个人知识库.service.memory_maintenance import (
    CLEANUP_INTERVAL_SECONDS, MEMORY_TTL_DAYS, cleanup_expired_memory,
)
from rag个人知识库.service.service import ingest_files, list_documents, search_documents

# ── 上传目录与限制（与 loader 的校验口径一致）──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "resources", "uploads"))
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".docx", ".doc", ".txt", ".md"}


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


class DocumentOut(BaseModel):
    id: int
    file_name: str
    version: str
    source: str
    chunk_count: int
    sync_status: str


# ── 生命周期：建表（幂等）+ 种子管理员 + 对话记忆清理后台任务 ──
async def _memory_cleanup_loop() -> None:
    """后台循环：定期清理过期的对话记忆（Postgres checkpoints）。"""
    while True:
        try:
            await asyncio.to_thread(cleanup_expired_memory, MEMORY_TTL_DAYS)
        except Exception as e:
            print(f"[api] 对话记忆清理任务异常：{e}")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    await _check_business_tables()
    await seed_admin()
    cleanup_task = asyncio.create_task(_memory_cleanup_loop())
    ingest_worker_task = None
    if await redis_available():
        ingest_worker_task = asyncio.create_task(run_worker())
        print("[api] 入库任务队列已启用（Redis Streams worker 启动）")
    else:
        print("[api] Redis 不可用，入库任务回退进程内执行（配置 REDIS_URL 并启动 Redis 后启用队列）")
    print(f"[api] 上传目录：{UPLOAD_DIR}")
    print(f"[api] 对话记忆 TTL={MEMORY_TTL_DAYS} 天，清理间隔={CLEANUP_INTERVAL_SECONDS}s")
    yield
    cleanup_task.cancel()
    if ingest_worker_task is not None:
        ingest_worker_task.cancel()
    await engine.dispose()


async def _check_business_tables() -> None:
    """启动时只读校验业务表存在（表结构由 models/vector.sql 维护，代码不做任何 DDL）。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM vector_files LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM audit_logs LIMIT 1"))
    except Exception as e:
        print("[api] 业务表缺失或数据库未就绪，请先执行表结构初始化：")
        print("      mysql -u root -p rag_demo < rag个人知识库/models/vector.sql")
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
    await record_failure(reg_key)  # 注册按尝试次数计数（无失败概念），窗口内最多 LOGIN_MAX_ATTEMPTS 次
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
    if user is None or not verify_password(form.password, user.password_hash):
        await record_failure(key)
        await write_audit("login_failed", username=form.username, detail=f"ip={_client_ip(request)}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    await clear_key(key)
    return TokenOut(access_token=create_access_token(user), role=user.role)


@app.get("/api/auth/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut(id=user.id, username=user.username, role=user.role)


# ══ 文档管理（RBAC：上传/删除仅管理员）══
@app.post("/api/documents/upload")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """上传文档并异步入库（加载→切分→MySQL→Milvus）。MinerU 解析在后台执行，不阻塞响应。"""
    # 跨平台安全取文件名：统一先转正斜杠再取最后一段，兼容 Windows/Linux 部署
    file_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not file_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件名不能为空")
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"不支持的格式 {ext or '(无扩展名)'}，支持: {sorted(ALLOWED_EXT)}")
    if ext == ".doc":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "暂不支持旧版 .doc，请用 WPS/Word 另存为 .docx 后上传")
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件为空")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 10MB 上限")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # 保留原始文件名：同名重复上传 = 同一文档（同 source/identity）→ 指纹增量更新语义；
    # basename 已剥离路径，无路径穿越风险
    path = os.path.join(UPLOAD_DIR, file_name)
    with open(path, "wb") as f:
        f.write(content)
    audit(db, admin, "upload", target=file_name, detail=path)
    # 优先走 Redis Streams 任务队列（可靠、可重试、崩溃恢复）；
    # Redis 不可用时回退进程内后台任务，保证系统不中断
    msg_id = await enqueue_ingest(path)
    if msg_id is not None:
        return {"status": "processing", "file_name": file_name, "path": path,
                "message": "已提交入库队列，稍后刷新文档列表查看结果（sync_status 变为 in_sync 即完成）"}
    background.add_task(ingest_files, [path])
    return {"status": "processing", "file_name": file_name, "path": path,
            "message": "已提交入库（进程内任务，Redis 未启用），稍后刷新文档列表查看结果"}


def _sanitize_source(path: str) -> str:
    """对外隐藏本地绝对路径：上传目录内 → uploads/文件名；项目内 → 相对路径；其他 → 文件名。"""
    if not path:
        return path
    abs_path = os.path.abspath(path)
    abs_upload = os.path.abspath(UPLOAD_DIR)
    if abs_path.startswith(abs_upload + os.sep):
        return "uploads/" + os.path.basename(abs_path)
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


@app.get("/api/documents", response_model=List[DocumentOut])
async def list_docs(user: User = Depends(get_current_user)):
    """文档列表（所有登录用户可读，仅元数据；source 已脱敏不暴露本地路径）。"""
    docs = await list_documents()
    for d in docs:
        d["source"] = _sanitize_source(d["source"])
    return docs


@app.delete("/api/documents/{file_id}")
async def remove_document(
    file_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除文档：Milvus 向量 + MySQL 元数据（级联 chunk）+ 磁盘文件 + 审计。

    若文档正在入库队列中处理，返回 409，避免"删除先执行、入库后写完向量"的孤儿向量竞态。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    if await is_inflight(record.source):
        raise HTTPException(status.HTTP_409_CONFLICT, "文档正在入库中，请稍后重试删除")
    ok = await delete_document(db, file_id, admin, upload_dir=UPLOAD_DIR)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    # 删除后清空检索/回答缓存，避免 TTL 内继续返回已删文档的旧结果
    await cache_clear_prefix("search:")
    await cache_clear_prefix("ans:")
    return {"status": "deleted", "file_id": file_id}


@app.get("/api/ingest/stats")
async def ingest_stats(user: User = Depends(get_current_user)):
    """入库任务队列状态（Redis Streams：待处理 / 死信 / 正在入库）。"""
    return await queue_stats()


# ══ 问答 / 检索（所有登录用户）══
@app.post("/api/chat", response_model=ChatOut)
async def chat_api(body: ChatIn, user: User = Depends(get_current_user)):
    """知识库问答。thread_id 按用户隔离：{user_id}:{session_id}，同一 session 保持多轮记忆。"""
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    try:
        result = await chat(body.content, thread_id=thread_id)
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"问答服务异常：{e}")
    sources = result.get("sources", [])
    _sanitize_source_paths(sources)
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
    禁用缓冲保证逐段即时到达。
    """
    session_id = body.session_id or uuid.uuid4().hex
    thread_id = f"{user.id}:{session_id}"
    gen = chat_stream(body.content, thread_id=thread_id, session_id=session_id)
    return StreamingResponse(
        _sse_events(gen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关 Nginx 缓冲（若前置代理）
        },
    )


@app.post("/api/search")
async def search_api(body: SearchIn, user: User = Depends(get_current_user)):
    """语义检索（双路召回 + rerank 精排）。"""
    try:
        hits = await search_documents(body.query, k=body.k, source=body.source)
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"检索服务异常：{e}")
    _sanitize_source_paths(hits)
    return {"query": body.query, "hits": hits}
