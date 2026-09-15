"""Shared HTTP helpers."""
from fastapi import Request
from sqlalchemy import select

from rag个人知识库.api.settings import TRUST_PROXY_HEADERS
from rag个人知识库.config.db_config import async_session
from rag个人知识库.models.user import User


def _check_password_utf8_limit(v: str) -> str:
    """bcrypt 硬上限 72 字节：超长输入必须在此拦下，否则 hashpw 抛 ValueError 变 500。"""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("密码过长（UTF-8 编码后最多 72 字节）")
    return v

def _client_ip(request: Request) -> str:
    """客户端 IP：默认取 socket 对端地址。

    反向代理（Nginx/Caddy）后请设置 TRUST_PROXY_HEADERS=true，取
    X-Forwarded-For 的最右一段（由最后一个可信代理追加的真实客户端地址；
    最左段可被客户端伪造，不能直接信任）。不开启时全站共享代理 IP，
    登录/注册/问答限流会互相误伤。
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"

def _escape_like(keyword: str) -> str:
    """转义 MySQL LIKE 通配符（反斜杠为 MySQL 默认转义符），让用户输入按字面匹配。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

async def _upload_user_active(user_id: int) -> bool:
    """上传前用独立会话回源用户状态，避免同一请求事务里读到陈旧快照。"""
    async with async_session() as db:
        status = await db.scalar(select(User.status).where(User.id == user_id))
    return status == "active"
