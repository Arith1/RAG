"""
文件指纹入库编排：MySQL 元数据表 + Milvus 向量库配合，按指纹做增量同步。

跨库一致性设计（MySQL 先落库，Milvus 后同步，状态机记录）：
  1. 基础校验 → 加载前预检（命中、内容未变且已同步 → 跳过）
  2. 阶段一（MySQL）：把"期望状态"落库并提交（sync_status=pending）。
     该阶段失败则回滚，Milvus 完全未被改动，不会产生孤儿向量。
  3. 阶段二（Milvus）：按期望状态幂等同步（确定性 ID 先删后插），
     成功置 in_sync；失败置 failed + last_error，期望状态仍在 MySQL。
  4. 重试：文件 status 为 pending/failed 且内容未变时，precheck 返回 retry，
     重新同步 Milvus（幂等重放），成功后置 in_sync。
"""
import asyncio
import logging
import os
from decimal import Decimal

from sqlalchemy import delete, select
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.config.db_config import async_session as session_factory
from rag个人知识库.crud.vector import (
    SYNC_FAILED,
    SYNC_IN_SYNC,
    delete_chunks_by_fingerprints,
    get_chunks_by_file_id,
    get_file_by_identity,
    insert_chunks,
    insert_file,
    set_sync_status,
    update_chunk_count,
    update_chunk_version,
    update_file_version,
)
from rag个人知识库.loader.load_file import (
    load_mineru_md_from_result,
    load_single,
    needs_mineru,
    validate_file,
)
from rag个人知识库.loader.parser.mineru_parser import minerU_files_ordered
from rag个人知识库.models.user import User
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.splitter.spliter import split_documents
from rag个人知识库.service.oss_archive import rel_source_from_local
from rag个人知识库.utils.hash_utils import compute_chunk_fingerprint, compute_file_hash
from rag个人知识库.vector_store.milvus_store import (
    aadd_chunks,
    adelete_chunks_by_ids,
    adelete_chunks_by_source,
)

# 版本号步进：1.0 -> 1.1 -> ... -> 1.9 -> 2.0（Numeric(5,1) 保留一位小数）
VERSION_STEP = Decimal("0.1")
INITIAL_VERSION = Decimal("1.0")

logger = logging.getLogger(__name__)


def _next_version(current: Decimal) -> Decimal:
    return (current + VERSION_STEP).quantize(Decimal("0.1"))


async def _owner_is_active(owner_id: int) -> bool:
    """读取用户最新状态；入库只允许 active 用户继续。"""
    async with session_factory() as db:
        status = await db.scalar(select(User.status).where(User.id == owner_id))
    return status == "active"


async def _drop_staged_insert(file_id: int) -> None:
    """删除刚落库但尚未写入 Milvus 的新文件记录，避免账号取消入库后留下 pending 孤儿行。"""
    try:
        async with session_factory() as db:
            await db.execute(delete(VectorFile).where(VectorFile.id == file_id))
            await db.commit()
        logger.info("[Ingest] 已清理未同步 Milvus 的新入库记录：file_id=%s", file_id)
    except Exception:
        logger.exception("[Ingest] 清理未同步新入库记录失败：file_id=%s", file_id)


async def precheck(
    db: AsyncSession,
    file_path: str,
) -> Tuple[str, str, Optional[VectorFile]]:
    """加载前预检：只读文件字节算 hash + 查库，不做任何解析。

    返回 (action, content_hash, record)：
      - action: "skip"（内容未变且已同步）/ "retry"（内容未变但上次同步失败，重放 Milvus）
                / "update"（内容已变）/ "insert"（未命中）
      - content_hash: 本次文件内容哈希，供入库/更新复用
      - record: 命中的 VectorFile 或 None
    """
    file_name = os.path.basename(file_path)
    content_hash = await asyncio.to_thread(compute_file_hash, file_path)
    # source 统一为相对路径（uploads/{user_id}/file），保证预检与入库/检索口径一致
    record = await get_file_by_identity(db, file_name, rel_source_from_local(file_path))
    if record is None:
        return "insert", content_hash, None
    if record.file_content_hash != content_hash:
        return "update", content_hash, record
    if record.sync_status == SYNC_IN_SYNC:
        return "skip", content_hash, record
    return "retry", content_hash, record


def _unique_fingerprints(chunks: List[Document], source: str) -> List[str]:
    """按内容指纹去重，顺序保留首次出现（与 Milvus 侧批内去重保持一致）"""
    seen = set()
    fingerprints = []
    for chunk in chunks:
        fp = compute_chunk_fingerprint(chunk.page_content, source)
        if fp not in seen:
            seen.add(fp)
            fingerprints.append(fp)
    return fingerprints


async def _stage_insert(
    db: AsyncSession,
    file_path: str,
    content_hash: str,
    chunks: List[Document],
    owner_id: int,
    is_public: bool = False,
) -> Tuple[int, Decimal, Dict[str, int]]:
    """阶段一（仅 MySQL）：全新入库，落库期望状态（status=pending）。

    返回 (file_id, version, summary)。Milvus 写入由调用方在阶段二执行。
    """
    file_name = os.path.basename(file_path)
    # 用相对路径作为 source（uploads/{user_id}/file），与 Milvus 主键/检索口径一致
    source = rel_source_from_local(file_path)

    file = await insert_file(
        db, file_name, source, content_hash,
        version=INITIAL_VERSION, owner_id=owner_id, is_public=is_public,
    )
    fingerprints = _unique_fingerprints(chunks, source)
    await insert_chunks(db, file.id, fingerprints, INITIAL_VERSION)
    await update_chunk_count(db, file.id)

    summary = {"added": len(fingerprints), "unchanged": 0, "removed": 0}
    logger.info("[Ingest] 全新入库 v%s：%s，chunk 数 %d", INITIAL_VERSION, file_path, len(fingerprints))
    return file.id, INITIAL_VERSION, summary


async def _stage_update(
    db: AsyncSession,
    record: VectorFile,
    content_hash: str,
    chunks: List[Document],
    is_public: Optional[bool] = None,
) -> Tuple[int, Decimal, List[Document], List[str], Dict[str, int]]:
    """阶段一（仅 MySQL）：内容已变，先升文件版本，再按指纹差集更新 chunk 记录。

    is_public 非 None 时同步更新共享状态（同名重新上传时 is_public 生效）。
    返回 (file_id, new_version, added_chunks, removed_ids, summary)。
    Milvus 写入/删除由调用方在阶段二执行。
    """
    source = record.source
    file_id = record.id
    new_version = _next_version(record.version)

    # 1. 先更新文件表版本号 + 内容哈希（置 pending，表示期望状态已变更）
    await update_file_version(db, file_id, new_version, content_hash, is_public=is_public)

    # 2. 取旧 chunk 指纹集合（MySQL 是 chunk 清单的权威来源）
    old_records = await get_chunks_by_file_id(db, file_id)
    old_fps = {r.chunk_fingerprint for r in old_records}

    # 3. 新 chunk 指纹（去重），并建立 指纹 -> chunk 映射供阶段二向量化入库
    fp_to_chunk: Dict[str, Document] = {}
    for chunk in chunks:
        fp = compute_chunk_fingerprint(chunk.page_content, source)
        fp_to_chunk.setdefault(fp, chunk)
    new_fps = list(fp_to_chunk.keys())

    # 4. 交集（内容未变）/ 新增 / 消失 分类
    new_fp_set = set(new_fps)
    unchanged = [fp for fp in new_fps if fp in old_fps]
    added = [fp for fp in new_fps if fp not in old_fps]
    removed = [fp for fp in old_fps if fp not in new_fp_set]

    # 5. MySQL chunk 变更：交集只刷版本号，新增插入，消失删除（Milvus 放到阶段二）
    if unchanged:
        await update_chunk_version(db, file_id, unchanged, new_version)
    if added:
        await insert_chunks(db, file_id, added, new_version)
    if removed:
        await delete_chunks_by_fingerprints(db, removed)

    # 6. 刷新 chunk_count
    await update_chunk_count(db, file_id)

    summary = {"added": len(added), "unchanged": len(unchanged), "removed": len(removed)}
    logger.info(
        "[Ingest] 更新完成 v%s -> v%s：新增 %d，未变 %d，删除 %d",
        record.version, new_version, len(added), len(unchanged), len(removed),
    )
    return file_id, new_version, [fp_to_chunk[fp] for fp in added], removed, summary


async def _sync_milvus(
    file_id: int,
    added_chunks: List[Document],
    removed_ids: List[str],
    rebuild_source: Optional[str] = None,
) -> None:
    """阶段二：同步 Milvus（幂等可重放）。成功置 in_sync，失败置 failed + last_error。

    本阶段使用**独立会话**标记状态，与阶段一（MySQL 落期望状态）事务完全隔离：
    Milvus 同步成功/失败的状态更新不会再触碰阶段一已提交的事务，避免跨事务复用
    session 带来的隐式提交/状态串扰风险。

    rebuild_source: 非 None 时先按 source 删除该文件全部向量再全量插入
    （retry 重建用，清理上次更新失败残留的旧 chunk 孤儿向量）。
    """
    try:
        if rebuild_source:
            await adelete_chunks_by_source(rebuild_source)
        if added_chunks:
            await aadd_chunks(added_chunks)
        if removed_ids:
            await adelete_chunks_by_ids(removed_ids)
        async with session_factory() as db:
            await set_sync_status(db, file_id, SYNC_IN_SYNC)
            await db.commit()
    except Exception as e:
        try:
            async with session_factory() as db:
                await set_sync_status(db, file_id, SYNC_FAILED, str(e))
                await db.commit()
        except Exception as e2:
            logger.warning("[Ingest] 标记同步失败状态出错：%s", e2)
        raise RuntimeError(f"Milvus 同步失败（已标记 failed，可重跑恢复）：{e}") from e


async def process_file(
    db: AsyncSession,
    file_path: str,
    owner_id: int,
    mineru_result: Optional[dict] = None,
    is_public: Optional[bool] = None,
) -> dict:
    """单文件完整流程：校验 → 预检 → 按需加载/切分 → MySQL 落期望状态 → 同步 Milvus。

    mineru_result: 批量 MinerU 解析结果；非 None 时不再单独触发 MinerU 上传解析。
    owner_id / is_public: 文档归属与共享标记（API 上传场景传入，写入 vector_files + chunk metadata）。
    """
    # 0. 用户状态预检：即使任务已进入队列，删除/禁用期间也不得继续写库。
    try:
        if not await _owner_is_active(owner_id):
            logger.info("[Ingest] 用户 %s 已非 active，取消入库：%s", owner_id, file_path)
            return {"file_path": file_path, "status": "cancelled", "message": "账号当前不可入库"}
    except Exception:
        logger.exception("[Ingest] 读取用户状态失败：owner_id=%s, file=%s", owner_id, file_path)
        raise

    # 1. 基础校验
    error = validate_file(file_path)
    if error is not None:
        return {"file_path": file_path, "status": "error", "message": error.error_msg}

    # 2. 预检（加载前）：内容未变且已同步 → 直接跳过
    action, content_hash, record = await precheck(db, file_path)
    if action == "skip":
        # 内容未变时仍允许更新 is_public（例如改为私有/共享）
        if is_public is not None and record.is_public != is_public:
            record.is_public = is_public
            await db.commit()
            logger.info("[Ingest] %s 内容未变化，但已更新共享状态 is_public=%s", file_path, is_public)
            return {
                "file_path": file_path,
                "status": "updated",
                "version": record.version,
            }
        logger.info("[Ingest] %s 内容未变化且已同步，跳过加载与入库", file_path)
        return {
            "file_path": file_path,
            "status": "skipped",
            "version": record.version,
        }

    # 3. 只有非 skip 才加载（避免对未变化文档做无谓解析）
    try:
        if mineru_result is not None and mineru_result.get("status") != "success":
            return {
                "file_path": file_path,
                "status": "error",
                "message": "文档解析失败，请稍后重试",
            }
        if mineru_result is not None:
            docs = await asyncio.to_thread(
                load_mineru_md_from_result,
                file_path,
                mineru_result,
                "批量文档",
            )
        else:
            docs = await asyncio.to_thread(load_single, file_path)
    except Exception:
        logger.exception("[Ingest] 文件加载失败：%s", file_path)
        return {
            "file_path": file_path,
            "status": "error",
            "message": "文件加载失败，请稍后重试",
        }
    if not docs:
        return {"file_path": file_path, "status": "error", "message": "加载结果为空"}

    # 保证 metadata.source 与入库 source 一致：强制覆盖 loader 可能设置的任何 source，
    # 确保 MySQL 指纹与 Milvus 主键（chunk ID）口径完全一致。
    # source 统一为相对路径（uploads/{user_id}/file）；retry/update 走 record.source（同口径）。
    upload_source = rel_source_from_local(file_path)
    for doc in docs:
        doc.metadata["source"] = upload_source

    # 4. 切分
    chunks = await asyncio.to_thread(split_documents, docs)
    if not chunks:
        return {"file_path": file_path, "status": "error", "message": "切分结果为空"}

    # 5. 阶段一：MySQL 落库期望状态（status=pending）。提交前 Milvus 完全未改动
    if action == "retry":
        # 期望状态已在 MySQL（上次同步失败）：先按 source 清掉该文件旧向量，
        # 再全量重放（幂等），同时清理上次更新失败残留的孤儿 chunk
        file_id, version = record.id, record.version
        added_chunks: List[Document] = chunks
        removed_ids: List[str] = []
        summary = {"added": len(chunks), "unchanged": 0, "removed": 0}
        rebuild_source = record.source
        owner_id = owner_id or record.owner_id  # 重放沿用记录里的 owner
    else:
        rebuild_source = None
        try:
            if action == "insert":
                file_id, version, summary = await _stage_insert(
                    db, file_path, content_hash, chunks,
                    owner_id=owner_id, is_public=bool(is_public),
                )
                added_chunks = chunks
                removed_ids = []
            else:
                file_id, version, added_chunks, removed_ids, summary = await _stage_update(
                    db, record, content_hash, chunks, is_public=is_public,
                )
                owner_id = owner_id or record.owner_id  # 更新沿用已有 owner
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("[Ingest] 元数据落库失败，已回滚（Milvus 未改动）：%s", file_path)
            return {
                "file_path": file_path,
                "status": "error",
                "message": "元数据保存失败，请稍后重试",
            }

    # 5.5 阶段二前：把 file_id / owner_id 写入即将入 Milvus 的 chunk metadata
    #   （检索可见性过滤载体：file_id in (...) / owner_id 溯源；source 已在切分前写入）
    if owner_id is not None:
        for doc in added_chunks:
            doc.metadata["file_id"] = file_id
            doc.metadata["owner_id"] = owner_id

    # 6. 阶段二：同步 Milvus（独立会话标记状态，与阶段一事务隔离）
    # 在真正写向量前再次回源，避免账号在解析/落库期间进入 deleting 后仍写回 Milvus。
    try:
        if not await _owner_is_active(owner_id):
            logger.info("[Ingest] 用户 %s 在 Milvus 同步前已非 active，取消入库：%s", owner_id, file_path)
            if action == "insert":
                await _drop_staged_insert(file_id)
            return {"file_path": file_path, "status": "cancelled", "message": "账号当前不可入库"}
        await _sync_milvus(file_id, added_chunks, removed_ids, rebuild_source)
        logger.info("[Ingest] %s 处理完成：%s", file_path, summary)
        return {
            "file_path": file_path,
            "status": {
                "insert": "inserted",
                "update": "updated",
                "retry": "retried",
            }[action],
            "version": str(version),
            **summary,
        }
    except RuntimeError:
        logger.exception("[Ingest] Milvus 同步失败：%s", file_path)
        return {
            "file_path": file_path,
            "status": "error",
            "message": "向量同步失败，请稍后重试",
            "retryable": True,
        }


async def ingest_files_batched(
    db: AsyncSession,
    file_paths: List[str],
    owner_id: int,
    is_public: Optional[bool] = None,
) -> List[dict]:
    """批量入库编排：先过滤 skip，把复杂文档批量交给 MinerU，再按原始顺序回填结果。

    返回顺序与输入 file_paths 完全一致，复杂文档不会因为批量解析而打乱顺序。
    owner_id / is_public 透传给每个文件的入库（写入归属与共享标记）。
    """
    ordered_results: List[Optional[dict]] = [None] * len(file_paths)
    complex_batch: List[Tuple[int, str]] = []

    for idx, file_path in enumerate(file_paths):
        try:
            error = await asyncio.to_thread(validate_file, file_path)
            if error is not None:
                ordered_results[idx] = {
                    "file_path": file_path,
                    "status": "error",
                    "message": error.error_msg,
                }
                continue

            action, _content_hash, record = await precheck(db, file_path)
            if action == "skip":
                ordered_results[idx] = {
                    "file_path": file_path,
                    "status": "skipped",
                    "version": str(record.version),
                }
                continue

            if await asyncio.to_thread(needs_mineru, file_path):
                complex_batch.append((idx, file_path))
            else:
                ordered_results[idx] = await process_file(
                    db, file_path, owner_id=owner_id, is_public=is_public,
                )
        except Exception:
            await db.rollback()
            logger.exception("[Ingest] 批量处理文件失败：%s", file_path)
            ordered_results[idx] = {
                "file_path": file_path,
                "status": "error",
                "message": "文件处理失败，请稍后重试",
            }

    # 预检阶段的 SELECT 会隐式开启事务，长时间批量解析前先释放数据库连接/事务
    await db.rollback()

    if complex_batch:
        complex_paths = [path for _, path in complex_batch]
        try:
            mineru_results = await asyncio.to_thread(minerU_files_ordered, complex_paths)
        except Exception:
            logger.exception("[Ingest] MinerU 批量解析失败")
            mineru_results = None

        for position, (idx, file_path) in enumerate(complex_batch):
            try:
                if mineru_results is None:
                    ordered_results[idx] = {
                        "file_path": file_path,
                        "status": "error",
                        "message": "文档解析失败，请稍后重试",
                    }
                else:
                    ordered_results[idx] = await process_file(
                        db,
                        file_path,
                        mineru_result=mineru_results[position],
                        owner_id=owner_id,
                        is_public=is_public,
                    )
            except Exception:
                await db.rollback()
                logger.exception("[Ingest] 批量处理 MinerU 文件失败：%s", file_path)
                ordered_results[idx] = {
                    "file_path": file_path,
                    "status": "error",
                    "message": "文件处理失败，请稍后重试",
                }

    return [result for result in ordered_results if result is not None]
