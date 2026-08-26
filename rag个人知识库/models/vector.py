from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Index, Text, ForeignKey, BigInteger, \
    Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from rag个人知识库.models.user import User


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=datetime.now,          # ← Python层：ORM插入时有值，无需flush
        server_default=func.now(),     # ← 数据库层：纯SQL插入也有兜底
        comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=datetime.now,          # ← Python层
        onupdate=datetime.now,         # ← Python层：ORM update时自动刷新
        server_default=func.now(),     # ← 数据库层兜底
        comment="更新时间"
    )

class VectorFile(Base):
    __tablename__ = 'vector_files'

    __table_args__ = (
        Index("idx_owner_id", "owner_id"),
        Index("idx_is_public", "is_public"),
    )

    # 字段定义
    id: Mapped[int] = mapped_column(
        BigInteger(),
        primary_key=True,
        autoincrement=True,
        comment='自增主键'
    )
    file_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment='文件名'
    )
    source: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment='来源标识（如文件路径/URL）'
    )
    identity_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="文件身份唯一标识(SHA256(file_name+source))"
    )
    # 文档归属用户（必填；用户删除时文档级联删除，对应 SQL fk_vector_files_owner CASCADE）
    owner_id: Mapped[int] = mapped_column(
        BigInteger(),
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        comment="文档归属用户 id（users.id）"
    )
    # 是否共享：False=私有（仅 owner 可见）/ True=共享（所有登录用户可检索）
    is_public: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default="0",
        comment="是否共享: 0=私有 1=共享"
    )
    file_content_hash: Mapped[str] = mapped_column(
        String(64),  # CHAR(64) 在 SQLAlchemy 中用 String 即可
        nullable=False,
        comment='整个文件内容的 SHA256,用来判断文件内容是否修改'
    )
    version: Mapped[Decimal] = mapped_column(
        Numeric(5, 1),
        nullable=False,
        default=Decimal("1.0"),
        server_default="1.0",
        comment="当前版本号（如 1.0, 1.1, 2.0）"
    )

    chunk_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default="0",
        comment='该文件的 chunk 总数'
    )

    # 下载量：非所有者（陌生人）下载成功后 +1，知识库「下载最多」排序依据
    download_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default="0",
        comment='下载量（非所有者下载 +1）'
    )
    sync_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="Milvus 同步状态: pending(待同步)/in_sync(一致)/failed(失败可重试)"
    )
    last_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="最近一次 Milvus 同步失败原因"
    )

    # 关系定义
    chunks: Mapped[List['ChunkRecord']] = relationship(
        back_populates='file',
        cascade='all, delete-orphan',  # 对应 ON DELETE CASCADE
        passive_deletes=True  # 让数据库处理级联删除
    )
    owner: Mapped['User'] = relationship(
        back_populates='documents'
    )

    def __repr__(self):
        return f"<VectorFile(id={self.id}, file_name='{self.file_name}', version={self.version})>"


class ChunkRecord(Base):
    __tablename__ = 'chunk_records'

    __table_args__ = (
        Index("idx_file_version", "file_id", "version"),
    )

    # 字段定义
    id: Mapped[int] = mapped_column(
        BigInteger(),
        primary_key=True,
        autoincrement=True,
        comment='自增主键'
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger(),
        ForeignKey('vector_files.id', ondelete='CASCADE'),
        nullable=False,
        comment='关联 vector_files 表的 ID'
    )
    chunk_fingerprint: Mapped[str] = mapped_column(
        String(64),  # CHAR(64)
        unique=True,
        nullable=False,
        comment='SHA256(chunk_content + source)，同时也是 Milvus 的 ID'
    )
    version: Mapped[Decimal] = mapped_column(
        Numeric(5, 1),
        nullable=False,
        comment="该 chunk 所属的版本号")

    # 关系定义
    file: Mapped['VectorFile'] = relationship(
        back_populates='chunks'
    )

    def __repr__(self):
        return f"<ChunkRecord(id={self.id}, file_id={self.file_id}, fingerprint='{self.chunk_fingerprint[:8]}...')>"