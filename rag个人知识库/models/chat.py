"""会话模型（MySQL 业务库）—— 仅会话元信息。

方案约定：
- MySQL chat_sessions 只存「会话列表 + 摘要」：标题、消息数、最后一条用户消息摘要、最后活跃时间，
  供问答侧边栏快速加载；TTL 清理以 updated_at（会话最后活跃时间）为准。
- 完整消息 / Agent 记忆由 Postgres（langgraph PostgresSaver checkpoint）持有，
  按 thread_id={user_id}:{session_id} 恢复。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)

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
        DateTime, nullable=True, comment="最后一条消息时间（侧边栏排序依据）"
    )
    retrieve_own_private: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="是否检索自己的私有文档（首问后锁定）",
    )
    retrieve_own_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="是否检索自己的公开文档（首问后锁定）",
    )
    retrieve_kb_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
        comment="是否检索知识库里的公开文档（所有他人，首问后锁定）",
    )

    def __repr__(self):
        return f"<ChatSession(id={self.id}, user={self.user_id}, title='{self.title}')>"


class ChatSessionScopeUser(Base):
    """会话检索范围-指定用户集合（MySQL 业务库）。

    一个会话可指定多个目标用户（多选），每行存一个 target_user_id；
    仅检索这些用户的「公开文档」，不越权（服务端强制 AND is_public=1）。
    会话删除 / 目标用户删除时对应行级联删除（FK ON DELETE CASCADE）。
    """

    __tablename__ = "chat_session_scope_users"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "session_id", "target_user_id",
            name="uk_scope_session_target",
        ),
        Index("idx_scope_target", "target_user_id"),
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["chat_sessions.user_id", "chat_sessions.session_id"],
            name="fk_scope_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            name="fk_scope_target_user",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, comment="会话所属用户 id（users.id）"
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="会话标识（对应 chat_sessions.session_id）"
    )
    target_user_id: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, comment="指定检索的目标用户 id（仅检索其公开文档）"
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )

    def __repr__(self):
        return (
            f"<ChatSessionScopeUser(user={self.user_id}, "
            f"session={self.session_id!r}, target={self.target_user_id})>"
        )
