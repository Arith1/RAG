"""文档管理服务：删除文档（Milvus 向量 + MySQL 元数据 + OSS/磁盘原件 + 审计）。"""
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.models.user import AuditLog, User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.service.oss_archive import delete_source_artifact, local_source_exists, rel_source_from_local
from rag个人知识库.vector_store.milvus_store import adelete_chunks_by_source

logger = logging.getLogger(__name__)


async def delete_document(
    db: AsyncSession,
    file_id: int,
    actor: User,
    upload_dir: str,
) -> bool:
    """删除指定文档：先删 Milvus 向量（按 source），再删 MySQL 文件行（级联 chunk_records），
    最后删除原件（OSS 对象 + 本地 upload 内副本）。

    返回 False 表示文档不存在。事务由调用方提交（get_db 依赖自动 commit）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        return False

    # 1. 原件：优先删 OSS 对象（source 为相对路径 key），再删本地 upload 副本
    #    OSS 删除失败时中止删除，避免 MySQL/Milvus 已删但 OSS 对象残留
    if not await delete_source_artifact(record.source):
        raise RuntimeError(f"OSS 删除失败，已中止文档删除：{record.source}")
    local_path = local_source_exists(record.source)
    if local_path:
        try:
            import os
            os.remove(local_path)
        except OSError as e:
            logger.warning("[document_admin] 删除磁盘文件失败（不影响库内删除）：%s", e)

    # 2. Milvus：按 source 删除全部向量（幂等；失败会抛异常触发回滚）
    await adelete_chunks_by_source(record.source)

    # 3. MySQL：删除文件行，chunk_records 由 ON DELETE CASCADE 级联清理
    await db.execute(delete(VectorFile).where(VectorFile.id == file_id))

    # 4. 审计
    db.add(AuditLog(
        user_id=actor.id,
        username=actor.username,
        action="delete",
        target=record.file_name,
        detail=rel_source_from_local(record.source),
    ))

    logger.info("[document_admin] 已删除文档：%s (id=%d)", record.file_name, file_id)
    return True
async def revoke_document_public(
    db: AsyncSession,
    file_id: int,
    actor: User,
) -> VectorFile | None:
    """管理员把共享文档取消共享（is_public=1 → 0）。

    返回 None 表示文档不存在；返回 VectorFile 表示已改为私有（事务由调用方提交）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        return None
    if record.owner_id != actor.id and actor.role != "admin":
        raise PermissionError("只有文档所有者或管理员可以取消共享")

    record.is_public = False
    db.add(AuditLog(
        user_id=actor.id,
        username=actor.username,
        action="revoke_public",
        target=record.file_name,
        detail=f"{record.source} (is_public=1 -> 0 by {actor.username})",
    ))
    logger.info("[document_admin] %s 已将共享文档取消共享：%s (id=%d)", actor.username, record.file_name, file_id)
    return record

async def share_document_public(
    db: AsyncSession,
    file_id: int,
    actor: User,
) -> VectorFile | None:
    """文档所有者（或管理员）把私有文档设为公开共享（is_public=0 -> 1）。

    返回 None 表示文档不存在；否则返回已更新的 VectorFile（事务由调用方提交）。
    非所有者/管理员访问时抛 PermissionError（由 API 层转为 403）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        return None
    if record.owner_id != actor.id:
        raise PermissionError("只有文档所有者可以设置共享")
    record.is_public = True
    db.add(AuditLog(
        user_id=actor.id,
        username=actor.username,
        action="share_public",
        target=record.file_name,
        detail=f"{record.source} (is_public=0 -> 1 by {actor.username})",
    ))
    logger.info("[document_admin] %s 已把文档设为共享：%s (id=%d)", actor.username, record.file_name, file_id)
    return record

