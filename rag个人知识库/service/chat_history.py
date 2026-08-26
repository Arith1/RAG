"""会话历史持久化（MySQL 业务库）—— 仅会话元信息。

方案约定：
- MySQL chat_sessions 只存「会话列表 + 摘要」：标题、消息数、最后一条用户消息摘要、最后活跃时间，
  供问答侧边栏快速加载与 TTL 清理扫描。
- 完整消息 / Agent 记忆由 Postgres（langgraph PostgresSaver checkpoint）持有，
  按 thread_id={user_id}:{session_id} 恢复；本模块不落任何完整消息。
- 每完成一轮问答（user 提问 + assistant 回答），只更新 chat_sessions 一行元信息：
  标题（首问自动生成，可重命名）、message_count+2、last_message_preview=最后一条 user 消息截断。
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import delete, select, update

from rag个人知识库.config.db_config import async_session
from rag个人知识库.models.chat import ChatSession

logger = logging.getLogger(__name__)

# 会话标题：取首问前 N 个字符，超出截断
TITLE_MAX_CHARS = 30
# 侧边栏摘要：最后一条用户消息前 N 个字符，超出截断
PREVIEW_MAX_CHARS = 60
# 返回给前端的日期格式
_ISO = "%Y-%m-%d %H:%M:%S"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_ISO) if dt else None


def _truncate(text: str, max_chars: int) -> str:
    """按字符截断（空白折叠），超出补省略号。"""
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _session_to_dict(s: ChatSession) -> dict:
    return {
        "session_id": s.session_id,
        "title": s.title,
        "message_count": s.message_count,
        "last_message_preview": s.last_message_preview or "",
        "last_message_at": _iso(s.last_message_at),
        "created_at": _iso(s.created_at),
        "updated_at": _iso(s.updated_at),
    }


async def upsert_chat_session(
    user_id: int,
    session_id: str,
    user_content: str,
    assistant_content: str = "",
    intent: Optional[str] = None,
) -> None:
    """一轮问答完成后更新会话元信息（会话不存在则自动创建）。

    - 标题：仅首次自动生成（首问前 30 字），已有标题（含用户重命名）保持不变
    - message_count +2（user + assistant）
    - last_message_preview = 最后一条 user 消息截断（60 字）
    - last_message_at = now（侧边栏排序 / TTL 清理依据）
    """
    title = _truncate(user_content or "", TITLE_MAX_CHARS) or "新会话"
    preview = _truncate(user_content or "", PREVIEW_MAX_CHARS)
    now = datetime.now()
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.session_id == session_id,
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            session = ChatSession(
                user_id=user_id,
                session_id=session_id,
                title=title,
                message_count=2,  # 首轮已含 user + assistant 两条
                last_message_preview=preview,
                last_message_at=now,
            )
            db.add(session)
        else:
            if not session.title or session.title == "新会话":
                session.title = title
            session.message_count = (session.message_count or 0) + 2
            session.last_message_preview = preview
            session.last_message_at = now
        await db.commit()


async def list_sessions(user_id: int) -> List[dict]:
    """当前用户的会话列表，按最后消息时间倒序（无消息的按创建时间倒序）。"""
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id)
            .order_by(
                # MySQL 不支持 NULLS LAST；DESC 排序下 NULL 天然排在最后
                ChatSession.last_message_at.desc(),
                ChatSession.created_at.desc(),
            )
        )
        rows = result.scalars().all()
    return [_session_to_dict(s) for s in rows]


async def get_session_info(user_id: int, session_id: str) -> Optional[dict]:
    """单个会话的元信息（与列表项同构）；不存在返回 None。"""
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.session_id == session_id,
            )
        )
        s = result.scalar_one_or_none()
    return _session_to_dict(s) if s is not None else None


async def rename_session(user_id: int, session_id: str, title: str) -> bool:
    """重命名会话；会话不存在返回 False。"""
    title = (title or "").strip()
    if not title:
        return False
    async with async_session() as db:
        result = await db.execute(
            update(ChatSession)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.session_id == session_id,
            )
            .values(title=title[:128])
        )
        await db.commit()
        return result.rowcount > 0


async def delete_session(user_id: int, session_id: str) -> bool:
    """删除会话元信息（完整记忆由调用方同步清除 Postgres checkpoint）。"""
    async with async_session() as db:
        result = await db.execute(
            delete(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.session_id == session_id,
            )
        )
        await db.commit()
        return result.rowcount > 0


async def list_expired_sessions(ttl_days: float) -> List[Tuple[int, str]]:
    """返回已过 TTL 的会话 (user_id, session_id) 列表（供 TTL 清理先删 MySQL、再删 Postgres）。

    时间依据：last_message_at；该列为 NULL 时退化为 created_at。
    """
    deadline = datetime.now() - timedelta(days=ttl_days)
    rows: List[ChatSession] = []
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.last_message_at.is_not(None),
                ChatSession.last_message_at < deadline,
            )
        )
        rows.extend(result.scalars().all())
        result2 = await db.execute(
            select(ChatSession).where(
                ChatSession.last_message_at.is_(None),
                ChatSession.created_at < deadline,
            )
        )
        rows.extend(result2.scalars().all())
    return [(s.user_id, s.session_id) for s in rows]


async def delete_by_keys(keys: List[Tuple[int, str]]) -> int:
    """按 (user_id, session_id) 批量删除会话元信息，返回删除条数。"""
    if not keys:
        return 0
    async with async_session() as db:
        total = 0
        for user_id, session_id in keys:
            result = await db.execute(
                delete(ChatSession).where(
                    ChatSession.user_id == user_id,
                    ChatSession.session_id == session_id,
                )
            )
            total += result.rowcount or 0
        await db.commit()
        return total
