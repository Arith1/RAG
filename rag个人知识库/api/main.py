"""FastAPI application assembly."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag个人知识库.api.auth import (
    allow_request, audit, check_allowed, clear_key, hash_password, record_failure, require_admin,
)
from rag个人知识库.api.helpers import _client_ip, _escape_like, _upload_user_active
from rag个人知识库.api.lifecycle import lifespan
from rag个人知识库.api.schemas import *
from rag个人知识库.api.settings import CORS_ORIGINS, DOCS_ENABLED, REGISTRATION_ENABLED, TRUST_PROXY_HEADERS
from rag个人知识库.api.routers import auth, billing, chat, documents, obs, queues, system
from rag个人知识库.api.routers.auth import (
    change_password, delete_account, login, logout, me, register, user_profile, user_search,
)
from rag个人知识库.api.routers.chat import _iter_with_heartbeat, chat_api, chat_stream_api, search_api
from rag个人知识库.api.routers.documents import download_document, remove_document, upload_documents
from rag个人知识库.api.routers.obs import obs_summary

app = FastAPI(
    title="RAG 个人知识库",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
    redoc_url=None,
)

# CORS：允许 Vue 开发服务器（Vite 默认 5173）跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(queues.router)
app.include_router(chat.router)
app.include_router(billing.router)
app.include_router(obs.router)
