"""RAG 可观测性模型（MySQL 业务库）—— 每次问答请求一行。

- 与 models/vector.sql 第 6 节 / models/rag_traces.sql 的 rag_traces 表对应。
- 表结构以 SQL 为准（建表语句已在库上执行），本文件仅做 ORM 映射，不负责 DDL。
- 与 llm_usage 共用 request_id，便于把费用与链路关联起来。
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _ObsBase(DeclarativeBase):
    """可观测性表专用基类：rag_traces 为只追加的链路日志，只有 created_at。"""


class RagTrace(_ObsBase):
    __tablename__ = "rag_traces"

    __table_args__ = (
        Index("idx_trace_request", "request_id"),
        Index("idx_trace_user_time", "user_id", "created_at"),
        Index("idx_trace_status_time", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="本次请求id（与 llm_usage.request_id 一致）"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, comment="用户id"
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="会话id"
    )
    intent: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="意图识别结果: rag_ask/chat/other 等"
    )
    query: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True, comment="用户提问/检索文本（便于排查）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="success",
        server_default="success",
        comment="状态: success/failed",
    )
    error_type: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="错误类型"
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, comment="错误信息"
    )
    total_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="端到端耗时(毫秒)"
    )
    intent_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="意图识别耗时"
    )
    retrieval_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="检索耗时"
    )
    retrieval_cache_hit: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default="0", comment="检索缓存是否命中"
    )
    retrieval_has_scope: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default="1", comment="是否有可见文档"
    )
    recall_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="双路召回候选数"
    )
    rerank_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="精排后命中数"
    )
    rerank_avg_score: Mapped[Optional[float]] = mapped_column(
        Numeric(6, 4), nullable=True, comment="精排平均分"
    )
    rerank_max_score: Mapped[Optional[float]] = mapped_column(
        Numeric(6, 4), nullable=True, comment="精排最高分"
    )
    rerank_degraded: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default="0", comment="精排是否降级(接口失败)"
    )
    generation_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="LLM 生成耗时"
    )
    answer_len: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="回答字符数"
    )
    sources: Mapped[Optional[List[dict]]] = mapped_column(
        JSON, nullable=True, comment="来源列表 [{source, score}]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        server_default=func.now(),
        comment="创建时间",
    )

    def __repr__(self):
        return (
            f"<RagTrace(id={self.id}, request='{self.request_id}', "
            f"user={self.user_id}, intent={self.intent!r}, status='{self.status}')>"
        )
