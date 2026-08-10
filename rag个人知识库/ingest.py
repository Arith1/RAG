"""
文件指纹入库编排：MySQL 元数据表 + Milvus 向量库配合，按指纹做增量同步。

单文件处理流程：
  1. 基础校验（存在性 / 格式 / 大小）
  2. 预检（文件加载前）：计算文件内容 hash，按 file_name+source 查 vector_files
     - 命中且 hash 相同 → 直接跳过，不触发加载/解析（省 MinerU 解析额度）
     - 命中且 hash 不同 → 加载后走更新流程
     - 未命中 → 加载后走全新入库流程
  3. 更新流程（内容已变）：
     - 先更新文件表版本号（+0.1）与内容 hash
     - 新旧 chunk 指纹做差集/交集
       - 交集（内容未变）：chunk 只刷版本号，向量库不动
       - 新增：写入 MySQL + Milvus
       - 消失：删除 MySQL + Milvus
"""
import os
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.crud.vector import (
    delete_chunks_by_fingerprints,
    get_chunks_by_file_id,
    get_file_by_identity,
    insert_chunks,
    insert_file,
    update_chunk_count,
    update_chunk_version,
    update_file_version,
)
from rag个人知识库.load_file import load_single, validate_file
from rag个人知识库.models.vector import VectorFile
from rag个人知识库.spliter.spliter import split_documents
from rag个人知识库.utils.hash_utils import compute_chunk_fingerprint, compute_file_hash
from rag个人知识库.vector_store.milvus_store import add_chunks, delete_chunks_by_ids

# 版本号步进：1.0 -> 1.1 -> ... -> 1.9 -> 2.0（Numeric(5,1) 保留一位小数）
VERSION_STEP = Decimal("0.1")
INITIAL_VERSION = Decimal("1.0")


def _next_version(current: Decimal) -> Decimal:
    return (current + VERSION_STEP).quantize(Decimal("0.1"))


async def precheck(
    db: AsyncSession,
    file_path: str,
) -> Tuple[str, str, Optional[VectorFile]]:
    """加载前预检：只读文件字节算 hash + 查库，不做任何解析。

    返回 (action, content_hash, record)：
      - action: "skip"（命中且内容未变）/ "update"（命中且内容已变）/ "insert"（未命中）
      - content_hash: 本次文件内容哈希，供入库/更新复用，避免重复计算
      - record: 命中的 VectorFile 或 None
    """
    file_name = os.path.basename(file_path)
    content_hash = compute_file_hash(file_path)
    record = await get_file_by_identity(db, file_name, file_path)
    if record is None:
        return "insert", content_hash, None
    if record.file_content_hash == content_hash:
        return "skip", content_hash, record
    return "update", content_hash, record


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


async def _ingest_new(
    db: AsyncSession,
    file_path: str,
    content_hash: str,
    chunks: List[Document],
) -> Tuple[Decimal, Dict[str, int]]:
    """全新入库：文件 v1.0 + 全部 chunk 写入 MySQL 与 Milvus"""
    file_name = os.path.basename(file_path)
    source = file_path

    file = await insert_file(db, file_name, source, content_hash, version=INITIAL_VERSION)
    fingerprints = _unique_fingerprints(chunks, source)
    # 先写 Milvus（幂等），成功后再落 MySQL，失败由调用方回滚
    add_chunks(chunks)
    await insert_chunks(db, file.id, fingerprints, INITIAL_VERSION)
    await update_chunk_count(db, file.id)

    print(f"[Ingest] 全新入库 v{INITIAL_VERSION}：{file_path}，chunk 数 {len(fingerprints)}")
    return INITIAL_VERSION, {"added": len(fingerprints), "unchanged": 0, "removed": 0}


async def _update_existing(
    db: AsyncSession,
    record: VectorFile,
    content_hash: str,
    chunks: List[Document],
) -> Tuple[Decimal, Dict[str, int]]:
    """内容已变：先升文件版本号，再做 chunk 指纹差集同步"""
    source = record.source
    file_id = record.id
    new_version = _next_version(record.version)

    # 1. 先更新文件表版本号 + 内容哈希
    await update_file_version(db, file_id, new_version, content_hash)

    # 2. 取旧 chunk 指纹集合（MySQL 是 chunk 清单的权威来源）
    old_records = await get_chunks_by_file_id(db, file_id)
    old_fps = {r.chunk_fingerprint for r in old_records}

    # 3. 新 chunk 指纹（去重），并建立 指纹 -> chunk 映射供向量化入库
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

    # 5. 先做向量库操作（新增写入、删除消失项），成功后再落 MySQL chunk 变更
    if added:
        add_chunks([fp_to_chunk[fp] for fp in added])
    if removed:
        delete_chunks_by_ids(removed)

    # 6. MySQL chunk 变更：交集只刷版本号，新增插入，消失删除
    if unchanged:
        await update_chunk_version(db, file_id, unchanged, new_version)
    if added:
        await insert_chunks(db, file_id, added, new_version)
    if removed:
        await delete_chunks_by_fingerprints(db, removed)

    # 7. 刷新 chunk_count
    await update_chunk_count(db, file_id)

    summary = {"added": len(added), "unchanged": len(unchanged), "removed": len(removed)}
    print(
        f"[Ingest] 更新完成 v{record.version} -> v{new_version}："
        f"新增 {len(added)}，未变 {len(unchanged)}，删除 {len(removed)}"
    )
    return new_version, summary


async def process_file(db: AsyncSession, file_path: str) -> dict:
    """单文件完整流程：校验 → 预检 → 按需加载/切分 → 入库/更新/跳过。"""
    # 1. 基础校验
    error = validate_file(file_path)
    if error is not None:
        return {"file_path": file_path, "status": "error", "message": error.error_msg}

    # 2. 预检（加载前）：命中且内容未变 → 直接跳过该文档，继续下一个
    action, content_hash, record = await precheck(db, file_path)
    if action == "skip":
        print(f"[Ingest] {file_path} 内容未变化，跳过加载与入库")
        return {
            "file_path": file_path,
            "status": "skipped",
            "version": record.version,
        }

    # 3. 只有预检未命中或内容已变才加载（避免对未变化文档做无谓解析）
    try:
        docs = load_single(file_path)
    except Exception as e:
        print(f"[Ingest] {file_path} 加载失败：{e}")
        return {"file_path": file_path, "status": "error", "message": f"加载失败：{e}"}
    if not docs:
        return {"file_path": file_path, "status": "error", "message": "加载结果为空"}

    # 保证 metadata.source 与入库 source 一致，避免 MySQL 指纹与 Milvus 主键口径不一致
    for doc in docs:
        doc.metadata.setdefault("source", file_path)

    # 4. 切分
    chunks = split_documents(docs)
    if not chunks:
        return {"file_path": file_path, "status": "error", "message": "切分结果为空"}

    # 5. 入库 / 更新（Milvus 失败时回滚 MySQL，避免半更新状态）
    try:
        if action == "insert":
            version, summary = await _ingest_new(db, file_path, content_hash, chunks)
        else:
            version, summary = await _update_existing(db, record, content_hash, chunks)
        await db.commit()
        print(f"[Ingest] {file_path} 处理完成：{summary}")
        return {
            "file_path": file_path,
            "status": "inserted" if action == "insert" else "updated",
            "version": version,
            **summary,
        }
    except Exception as e:
        await db.rollback()
        print(f"[Ingest] {file_path} 入库/更新失败，已回滚 MySQL：{e}")
        return {"file_path": file_path, "status": "error", "message": str(e)}