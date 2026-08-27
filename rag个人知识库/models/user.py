"""用户与审计模型（MySQL 业务库）。

- User：账号 + 角色（admin/user/guest）+ 状态（active/deleting/disabled），RBAC 的根
- AuditLog：操作审计（注册/上传/删除等），用户名冗余存储，防用户删除后审计丢失
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, DateTime, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rag个人知识库.models.vector import Base, VectorFile


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True, comment="用户名"
    )
    password_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="bcrypt 密码哈希"
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user",
        comment="角色: admin(管理员) / user(普通用户) / guest(访客,暂未开放注册)"
    )
    # 账号状态：active(正常) / deleting(删除处理中) / disabled(禁用)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
        comment="账号状态: active(正常)/deleting(删除处理中)/disabled(禁用)"
    )

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(), nullable=False, server_default=text('CURRENT_TIMESTAMP'),
        comment="创建时间"
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(), nullable=False, server_default=text('CURRENT_TIMESTAMP'),
        onupdate=func.now(), comment="更新时间"
    )

    # 关系：该用户拥有的文档（SQL 外键 ON DELETE CASCADE 已负责级联删除）
    documents: Mapped[List["VectorFile"]] = relationship(
        back_populates="owner",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}', status='{self.status}')>"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=True, comment="自增主键"
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True, comment="操作用户 id"
    )
    username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="操作用户名（冗余，防用户删除后审计丢失）"
    )
    action: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="操作类型: register/upload/delete 等"
    )
    target: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, comment="操作对象（文件名/路径等）"
    )
    detail: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="补充信息"
    )

    def __repr__(self):
        return f"<AuditLog(id={self.id}, user='{self.username}', action='{self.action}', target='{self.target}')>"
