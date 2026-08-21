"""认证与鉴权：bcrypt 密码哈希 + JWT + RBAC 依赖（FastAPI 侧）。

- 密码：bcrypt（不要用 md5/sha1 明文哈希）
- 登录：OAuth2 密码流（Swagger 的 Authorize 按钮可直接用）
- 鉴权：get_current_user（任何登录用户）/ require_admin（仅管理员）
- 审计：audit() 写入 audit_logs（用户名冗余存储）
- 防爆破：登录/注册滑动窗口限流（Redis ZSET + Lua 原子，重启不清零、多 worker 共享；
  Redis 不可用时回退进程内 dict）
"""
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.config.redis import get_redis

from rag个人知识库.config.db_config import async_session, get_db
from rag个人知识库.models.user import AuditLog, User

logger = logging.getLogger(__name__)

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
    async with async_session() as db:
        db.add(AuditLog(user_id=user_id, username=username, action=action,
                        target=target, detail=detail))
        await db.commit()


# ── 暴力破解防护：滑动窗口限流 ──
# 主后端 Redis（ZSET + Lua 原子），重启不清零、多 worker 共享计数；
# Redis 不可用时自动回退进程内 dict，系统不中断。
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = 60

# 进程内兜底（Redis 不可用时）
_attempts: dict = {}  # key -> deque[时间戳]


def _local_prune(key: str) -> None:
    now = time.time()
    dq = _attempts.get(key)
    if dq is None:
        return
    while dq and now - dq[0] > LOGIN_WINDOW_SECONDS:
        dq.popleft()
    if not dq:
        _attempts.pop(key, None)


def _local_check_allowed(key: str) -> bool:
    _local_prune(key)
    return len(_attempts.get(key, ())) < LOGIN_MAX_ATTEMPTS


def _local_record_failure(key: str) -> None:
    _attempts.setdefault(key, deque()).append(time.time())


def _local_clear_key(key: str) -> None:
    _attempts.pop(key, None)


# Redis 滑动窗口：ZSET 成员为失败时间戳（score=时间戳），窗口内计数 = ZCARD。
# 两条 Lua 保证原子性（Redis 单线程执行脚本），避免并发下计数不准。
_CHECK_LUA = """
-- KEYS[1]=限流key  ARGV[1]=窗口秒  ARGV[2]=上限  ARGV[3]=当前毫秒时间戳
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3] - ARGV[1] * 1000)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then return 0 end
return 1
"""

_RECORD_LUA = """
-- KEYS[1]=限流key  ARGV[1]=窗口秒  ARGV[3]=当前毫秒时间戳  ARGV[4]=唯一成员
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3] - ARGV[1] * 1000)
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


async def _redis_check(key: str) -> "bool | None":
    """Redis 原子检查（含窗口清理）：True 放行 / False 拦截 / None 表示 Redis 不可用。"""
    try:
        r = get_redis()
        ok = await r.eval(_CHECK_LUA, 1, key, LOGIN_WINDOW_SECONDS,
                          LOGIN_MAX_ATTEMPTS, int(time.time() * 1000))
        return bool(ok)
    except Exception:
        return None


async def _redis_record(key: str) -> bool:
    """Redis 原子记录一次失败，成功返回 True；Redis 不可用返回 False。"""
    try:
        r = get_redis()
        member = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        await r.eval(_RECORD_LUA, 1, key, LOGIN_WINDOW_SECONDS, 0,
                     int(time.time() * 1000), member)
        return True
    except Exception:
        return False


async def check_allowed(key: str) -> bool:
    """窗口内失败次数未超限返回 True（Redis 优先，不可用回退进程内）。"""
    result = await _redis_check(key)
    if result is not None:
        return result
    return _local_check_allowed(key)


async def record_failure(key: str) -> None:
    """记录一次失败（Redis 优先，不可用回退进程内）。"""
    if not await _redis_record(key):
        _local_record_failure(key)


async def clear_key(key: str) -> None:
    """清除限流记录（Redis 与进程内都清）。"""
    try:
        await get_redis().delete(key)
    except Exception:
        pass
    _local_clear_key(key)


# ── 种子管理员（首次启动时从环境变量播种）──
async def seed_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "admin").strip()
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        logger.info("[auth] 未配置 ADMIN_PASSWORD，跳过管理员播种（配置后重启生效）")
        return
    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none() is not None:
            logger.info("[auth] 管理员账号已存在：%s", username)
            return
        db.add(User(username=username, password_hash=hash_password(password), role="admin"))
        await db.commit()
        logger.info("[auth] 已创建管理员账号：%s", username)
