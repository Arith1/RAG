"""认证与鉴权：bcrypt 密码哈希 + JWT + RBAC 依赖（FastAPI 侧）。

- 密码：bcrypt（不要用 md5/sha1 明文哈希）
- 登录：OAuth2 密码流（Swagger 的 Authorize 按钮可直接用）
- 鉴权：get_current_user（任何登录用户）/ require_admin（仅管理员）
- 审计：audit() 写入 audit_logs（用户名冗余存储）
- 防爆破：登录/注册滑动窗口限流（进程内实现，重启清零；生产可换 Redis）
"""
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.config.db_config import AsyncSession, get_db
from rag个人知识库.models.user import AuditLog, User

# JWT 配置（生产环境务必在 .env 中覆盖 JWT_SECRET）
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 默认 24h

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ── 密码哈希 ──
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT ──
def create_access_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ── RBAC 依赖 ──
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 Bearer token 解析出当前用户；token 无效/过期/用户被删一律 401。"""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或过期的登录凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise credentials_error
    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_error
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_error
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """仅管理员可访问；普通用户 403。"""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可执行此操作")
    return user


# ── 审计 ──
def audit(db: AsyncSession, user: User, action: str, target: str = None, detail: str = None) -> None:
    db.add(AuditLog(
        user_id=user.id,
        username=user.username,
        action=action,
        target=target,
        detail=detail,
    ))


async def write_audit(
    action: str,
    username: str = None,
    user_id: int = None,
    target: str = None,
    detail: str = None,
) -> None:
    """独立会话写审计：用于异常路径（如登录失败，请求会抛 401 被外层回滚），
    避免审计记录随事务一起被丢弃。"""
    async with AsyncSession() as db:
        db.add(AuditLog(user_id=user_id, username=username, action=action,
                        target=target, detail=detail))
        await db.commit()


# ── 暴力破解防护：滑动窗口限流（进程内，重启清零；生产可换 Redis）──
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = 60
_attempts: dict = {}  # key -> deque[时间戳]


def _prune(key: str) -> None:
    now = time.time()
    dq = _attempts.get(key)
    if dq is None:
        return
    while dq and now - dq[0] > LOGIN_WINDOW_SECONDS:
        dq.popleft()
    if not dq:
        _attempts.pop(key, None)


def check_allowed(key: str) -> bool:
    """窗口内失败次数未超限返回 True。"""
    _prune(key)
    return len(_attempts.get(key, ())) < LOGIN_MAX_ATTEMPTS


def record_failure(key: str) -> None:
    _attempts.setdefault(key, deque()).append(time.time())


def clear_key(key: str) -> None:
    _attempts.pop(key, None)


# ── 种子管理员（首次启动时从环境变量播种）──
async def seed_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "admin").strip()
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        print("[auth] 未配置 ADMIN_PASSWORD，跳过管理员播种（配置后重启生效）")
        return
    async with AsyncSession() as db:
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none() is not None:
            print(f"[auth] 管理员账号已存在：{username}")
            return
        db.add(User(username=username, password_hash=hash_password(password), role="admin"))
        await db.commit()
        print(f"[auth] 已创建管理员账号：{username}")
