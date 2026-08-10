from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.models.vector import ChunkRecord, VectorFile
from rag个人知识库.utils.hash_utils import compute_identity_hash


async def get_file_by_identity(
    db: AsyncSession,
    file_name: str,
    source: str,
) -> Optional[VectorFile]:
    """按 file_name + source 查询文件记录（同名同源判定），未命中返回 None"""
    identity_hash = compute_identity_hash(file_name, source)
    result = await db.execute(
        select(VectorFile).where(VectorFile.identity_hash == identity_hash)
    )
    return result.scalar_one_or_none()


async def insert_file(
    db: AsyncSession,
    file_name: str,
    source: str,
    content_hash: str,
    version: str = "1.0",
) -> VectorFile:
    """新增文件记录，返回带 id 的 VectorFile（调用方负责 commit）"""
    file = VectorFile(
        file_name=file_name,
        source=source,
        identity_hash=compute_identity_hash(file_name, source),
        file_content_hash=content_hash,
        version=Decimal(version),
        chunk_count=0,
    )
    db.add(file)
    await db.flush()
    return file


async def update_file_version(
    db: AsyncSession,
    file_id: int,
    new_version: Decimal,
    new_content_hash: str,
) -> None:
    """更新文件版本号与内容哈希（更新流程的第一步）"""
    await db.execute(
        update(VectorFile)
        .where(VectorFile.id == file_id)
        .values(version=new_version, file_content_hash=new_content_hash)
    )


async def get_chunks_by_file_id(db: AsyncSession, file_id: int) -> Sequence[ChunkRecord]:
    """取文件当前全部 chunk 记录（旧指纹集合的权威来源）"""
    result = await db.execute(
        select(ChunkRecord).where(ChunkRecord.file_id == file_id)
    )
    return result.scalars().all()


async def insert_chunks(
    db: AsyncSession,
    file_id: int,
    fingerprints: List[str],
    version: Decimal,
) -> None:
    """批量新增 chunk 记录"""
    db.add_all(
        ChunkRecord(file_id=file_id, chunk_fingerprint=fp, version=version)
        for fp in fingerprints
    )
    await db.flush()


async def update_chunk_version(
    db: AsyncSession,
    file_id: int,
    fingerprints: List[str],
    new_version: Decimal,
) -> None:
    """内容未变的 chunk 只更新版本号，向量库不做任何操作"""
    await db.execute(
        update(ChunkRecord)
        .where(
            ChunkRecord.file_id == file_id,
            ChunkRecord.chunk_fingerprint.in_(fingerprints),
        )
        .values(version=new_version)
    )


async def delete_chunks_by_fingerprints(
    db: AsyncSession,
    fingerprints: List[str],
) -> None:
    """删除已消失的 chunk 记录"""
    if not fingerprints:
        return
    await db.execute(
        delete(ChunkRecord).where(ChunkRecord.chunk_fingerprint.in_(fingerprints))
    )


async def update_chunk_count(db: AsyncSession, file_id: int) -> None:
    """按当前 chunk 记录数刷新文件 chunk_count"""
    result = await db.execute(
        select(func.count()).select_from(ChunkRecord).where(ChunkRecord.file_id == file_id)
    )
    await db.execute(
        update(VectorFile)
        .where(VectorFile.id == file_id)
        .values(chunk_count=result.scalar_one())
    )

async def select_file_names(db: AsyncSession):
    result = await db.execute(select(VectorFile))
    return result.scalars().all()