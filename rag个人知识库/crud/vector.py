from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.models.vector import ChunkRecord, VectorFile
from rag个人知识库.utils.hash_utils import compute_identity_hash

# Milvus 同步状态机：pending(期望状态已落库，待同步) / in_sync(一致) / failed(同步失败可重试)
SYNC_PENDING = "pending"
SYNC_IN_SYNC = "in_sync"
SYNC_FAILED = "failed"


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
    owner_id: int,
    version: str = "1.0",
    is_public: bool = False,
) -> VectorFile:
    """新增文件记录，返回带 id 的 VectorFile（调用方负责 commit）"""
    file = VectorFile(
        file_name=file_name,
        source=source,
        identity_hash=compute_identity_hash(file_name, source),
        file_content_hash=content_hash,
        version=Decimal(version),
        chunk_count=0,
        sync_status=SYNC_PENDING,
        owner_id=owner_id,
        is_public=is_public,
    )
    db.add(file)
    await db.flush()
    return file


async def update_file_version(
    db: AsyncSession,
    file_id: int,
    new_version: Decimal,
    new_content_hash: str,
    is_public: Optional[bool] = None,
) -> None:
    """更新文件版本号与内容哈希，并置为 pending（期望状态已变更，待同步 Milvus）。

    is_public 非 None 时同时更新共享状态，保证同名文件重新上传时 is_public 生效。
    """
    values = {
        "version": new_version,
        "file_content_hash": new_content_hash,
        "sync_status": SYNC_PENDING,
        "last_error": None,
    }
    if is_public is not None:
        values["is_public"] = is_public
    await db.execute(
        update(VectorFile)
        .where(VectorFile.id == file_id)
        .values(**values)
    )


async def set_sync_status(
    db: AsyncSession,
    file_id: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """更新文件 Milvus 同步状态（in_sync / failed 等），error 为 None 时清空 last_error"""
    values = {"sync_status": status}
    if error is None:
        values["last_error"] = None
    else:
        values["last_error"] = error
    await db.execute(update(VectorFile).where(VectorFile.id == file_id).values(**values))


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

async def select_visible_file_ids(
    db: AsyncSession,
    user_id: int,
    retrieve_own_private: bool = True,
    retrieve_own_public: bool = True,
    retrieve_kb_public: bool = True,
    retrieve_owner_ids: Optional[List[int]] = None,
) -> List[int]:
    """按会话检索范围查当前用户可见的文件 id 集合。

    四项范围（互斥与兜底校验由 API 层负责）：
      - retrieve_own_private：自己的私有文档（owner_id=user_id 且非公开）
      - retrieve_own_public ：自己的公开文档（owner_id=user_id 且公开）
      - retrieve_kb_public  ：知识库里的公开文档（其他所有人的公开文档）
      - retrieve_owner_ids  ：指定用户的公开文档（多选，服务端强制 AND is_public=1 不越权）

    全部为 False / 空时返回空列表（不发起检索）。
    admin 与普通用户共用此规则（admin 仅额外拥有"取消他人共享"权限，不扩大检索范围）。
    """
    conds: list = []
    if retrieve_own_private:
        conds.append(
            (VectorFile.owner_id == user_id) & (VectorFile.is_public.is_(False))
        )
    if retrieve_own_public:
        conds.append(
            (VectorFile.owner_id == user_id) & (VectorFile.is_public.is_(True))
        )
    if retrieve_kb_public:
        conds.append(
            (VectorFile.owner_id != user_id) & (VectorFile.is_public.is_(True))
        )
    target_ids = [t for t in (retrieve_owner_ids or []) if t is not None]
    if target_ids:
        conds.append(
            (VectorFile.owner_id.in_(target_ids)) & (VectorFile.is_public.is_(True))
        )
    if not conds:
        return []
    result = await db.execute(
        select(VectorFile.id).where(or_(*conds))
    )
    return [r for r in result.scalars().all()]


async def count_file_names(
    db: AsyncSession,
    user_id: Optional[int] = None,
) -> int:
    """统计可见文档总数（与 select_file_names 使用同一可见性规则）。

    user_id 非 None 时统计"本人或共享"的可见文档；None 统计全部（CLI/管理场景）。
    """
    stmt = select(func.count(VectorFile.id))
    if user_id is not None:
        stmt = stmt.where(
            (VectorFile.owner_id == user_id) | (VectorFile.is_public.is_(True))
        )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def select_file_names(
    db: AsyncSession,
    limit: Optional[int] = None,
    offset: int = 0,
    user_id: Optional[int] = None,
):
    """按 updated_at 倒序列出文件记录，支持可选分页与可见性过滤。

    user_id 非 None 时仅返回该用户可见的文件（本人或共享）；
    user_id 为 None 返回全部（CLI/管理场景）。
    """
    stmt = select(VectorFile).order_by(VectorFile.updated_at.desc())
    if user_id is not None:
        stmt = stmt.where(
            (VectorFile.owner_id == user_id) | (VectorFile.is_public.is_(True))
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)
    result = await db.execute(stmt)
    return result.scalars().all()
