"""文档管理服务：删除文档（Milvus 向量 + MySQL 元数据 + 磁盘文件 + 审计）。"""
import os

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.models.user import AuditLog, User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.vector_store.milvus_store import adelete_chunks_by_source


async def delete_document(
    db: AsyncSession,
    file_id: int,
    actor: User,
    upload_dir: str,
) -> bool:
    """删除指定文档：先删 Milvus 向量（按 source），再删 MySQL 文件行（级联 chunk_records），
    最后删除磁盘文件（仅在 upload_dir 内，防止误删资源目录原始文件）。

    返回 False 表示文档不存在。事务由调用方提交（get_db 依赖自动 commit）。
    """
    result = await db.execute(select(VectorFile).where(VectorFile.id == file_id))
    record = result.scalar_one_or_none()
    if record is None:
        return False

    # 1. Milvus：按 source 删除全部向量（幂等；失败会抛异常触发回滚）
    await adelete_chunks_by_source(record.source)

    # 2. MySQL：删除文件行，chunk_records 由 ON DELETE CASCADE 级联清理
    await db.execute(delete(VectorFile).where(VectorFile.id == file_id))

    # 3. 审计
    db.add(AuditLog(
        user_id=actor.id,
        username=actor.username,
        action="delete",
        target=record.file_name,
        detail=record.source,
    ))

    # 4. 磁盘文件：只删上传目录内的副本，避免误删 resources 下手工放置的原始文件
    abs_source = os.path.abspath(record.source)
    abs_upload = os.path.abspath(upload_dir)
    if abs_source.startswith(abs_upload + os.sep) and os.path.isfile(abs_source):
        try:
            os.remove(abs_source)
        except OSError as e:
            print(f"[document_admin] 删除磁盘文件失败（不影响库内删除）：{e}")

    print(f"[document_admin] 已删除文档：{record.file_name} (id={file_id})")
    return True
