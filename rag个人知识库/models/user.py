"""用户与审计模型（MySQL 业务库）。

- User：账号 + 角色（admin/user），RBAC 的根
- AuditLog：管理员操作审计（上传/删除等），用户名冗余存储，防用户删除后审计丢失
"""
from typing import Optional

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rag个人知识库.models.vector import Base


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
        comment="角色: admin(管理员) / user(普通用户)"
    )

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"


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
