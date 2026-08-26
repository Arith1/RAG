"""会话模型（MySQL 业务库）—— 仅会话元信息。

方案约定：
- MySQL chat_sessions 只存「会话列表 + 摘要」：标题、消息数、最后一条用户消息摘要、最后活跃时间，
  供问答侧边栏快速加载与 TTL 清理扫描。
- 完整消息 / Agent 记忆由 Postgres（langgraph PostgresSaver checkpoint）持有，
  按 thread_id={user_id}:{session_id} 恢复。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String

from sqlalchemy.orm import Mapped, mapped_column

from rag个人知识库.models.vector import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 id（users.id）",
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="会话标识（agent 记忆 thread_id 共用）"
    )
    title: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="新会话",
        server_default="新会话",
        comment="会话标题（首问自动生成，可重命名）",
    )
    message_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default="0",
        comment="消息条数（user+assistant 都算，展示用）",
    )
    last_message_preview: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        default="",
        server_default="",
        comment="最后一条用户消息摘要（侧边栏展示，过长截断）",
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="最后一条消息时间（侧边栏排序 / TTL 清理依据）"
    )

    def __repr__(self):
        return f"<ChatSession(id={self.id}, user={self.user_id}, title='{self.title}')>"
