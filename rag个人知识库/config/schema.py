"""数据库 schema 版本与关键列合同校验。

项目仍保留可审计的 SQL 建表/迁移文件；本模块负责在应用启动时确认当前数据库
至少满足当前代码要求的 schema，并记录架构版本，避免缺表/缺列到请求期才报错。
"""
from collections import defaultdict
from typing import Iterable

from sqlalchemy import text

from rag个人知识库.models.billing import LlmUsage
from rag个人知识库.models.chat import ChatSession, ChatSessionScopeUser
from rag个人知识库.models.obs import RagTrace
from rag个人知识库.models.user import AccountDeletion, AuditLog, User
from rag个人知识库.models.vector import ChunkRecord, ParentChunk, VectorFile

SCHEMA_VERSION = 1
SCHEMA_VERSION_NAME = "baseline_schema_contract"

_CORE_MODELS = (
    User,
    AuditLog,
    AccountDeletion,
    VectorFile,
    ChunkRecord,
    ChatSession,
    ChatSessionScopeUser,
    LlmUsage,
    RagTrace,
)


def required_schema(include_parent_chunks: bool) -> dict[str, set[str]]:
    """返回当前代码要求的 table -> columns 合同。"""
    models: Iterable = (*_CORE_MODELS, ParentChunk) if include_parent_chunks else _CORE_MODELS
    return {
        model.__tablename__: {column.name for column in model.__table__.columns}
        for model in models
    }


def find_missing_columns(
    actual: dict[str, set[str]],
    required: dict[str, set[str]],
) -> dict[str, list[str]]:
    """返回缺失的表/列，便于启动失败时一次性给出完整修复线索。"""
    missing: dict[str, list[str]] = {}
    for table, expected_columns in required.items():
        actual_columns = actual.get(table)
        if actual_columns is None:
            missing[table] = ["<table>"]
            continue
        absent = sorted(expected_columns - actual_columns)
        if absent:
            missing[table] = absent
    return missing


def _format_missing(missing: dict[str, list[str]]) -> str:
    parts = []
    for table, columns in missing.items():
        if columns == ["<table>"]:
            parts.append(f"表 {table}")
        else:
            parts.append(f"{table}({', '.join(columns)})")
    return "；".join(parts)


async def ensure_schema(engine, include_parent_chunks: bool) -> None:
    """创建版本表、校验关键列，并把当前版本登记到 schema_migrations。"""
    required = required_schema(include_parent_chunks)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INT NOT NULL PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
        rows = (
            await conn.execute(text("""
                SELECT TABLE_NAME, COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
            """))
        ).all()
        actual: dict[str, set[str]] = defaultdict(set)
        for table, column in rows:
            actual[table].add(column)

        missing = find_missing_columns(actual, required)
        if missing:
            raise RuntimeError(
                "数据库 schema 与当前代码不兼容，缺少："
                f"{_format_missing(missing)}。请先执行 models/vector.sql 和对应迁移文件。"
            )

        current = (
            await conn.execute(text(
                "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ))
        ).first()
        if current is None:
            await conn.execute(
                text(
                    "INSERT INTO schema_migrations(version, name) VALUES(:version, :name)"
                ),
                {"version": SCHEMA_VERSION, "name": SCHEMA_VERSION_NAME},
            )
        elif int(current.version) < SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本过旧：当前 {current.version}，代码要求 {SCHEMA_VERSION}。"
                "请先执行未应用的迁移文件。"
            )
        elif int(current.version) > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本高于当前代码：数据库 {current.version}，代码 {SCHEMA_VERSION}。"
                "请升级应用代码或回滚数据库。"
            )
