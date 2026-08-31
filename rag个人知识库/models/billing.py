"""LLM 计费模型（MySQL 业务库）—— 每次 LLM 调用一行。

- 与 models/vector.sql 的 llm_usage 表对应。
- user_id 不设外键：账号软删除（status=deleted）后计费记录仍需保留。
- credits 为独立积分体系，暂未启用，默认 0；estimated_cost 为按 .env 单价估算的费用（元）。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _BillingBase(DeclarativeBase):
    """计费表专用基类：llm_usage 为只追加的日志，只有 created_at，无 updated_at。"""


class LlmUsage(_BillingBase):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, comment="用户id"
    )
    session_id: Mapped[str] = mapped_column(
        String(64), nullable=True, comment="会话id"
    )
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="本次请求id"
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="模型厂商"
    )
    model: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="模型名称"
    )
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="调用阶段: intent/answer/chat/summarize"
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="输入令牌数"
    )
    cached_tokens: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="命中缓存输入令牌数"
    )
    uncached_tokens: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="未命中缓存输入令牌数"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="输出令牌数"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="总令牌数"
    )
    credits: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default="0", comment="积分"
    )
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default="0", comment="预估费用(元)"
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="LLM 调用耗时(毫秒)"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="success", server_default="success", comment="状态: success/failed"
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
            f"<LlmUsage(id={self.id}, user={self.user_id}, "
            f"type='{self.type}', cost={self.estimated_cost})>"
        )
