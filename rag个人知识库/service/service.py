"""业务服务层：CLI 与前端（FastAPI 等）共用，返回 JSON 友好结构。

- ingest_files：批量入库/增量同步文件
- search_documents：双路召回 + rerank 精排检索
- list_documents：列出已入库文档
"""
from typing import List, Optional

from rag个人知识库.config.db_config import AsyncSession
from rag个人知识库.crud.vector import select_file_names
from rag个人知识库.service.ingest import process_file
from rag个人知识库.vector_store.milvus_store import asearch_with_rerank


async def ingest_files(file_paths: List[str]) -> List[dict]:
    """入库一组文件：逐文件预检（加载前判断）→ 按需加载 → 切分 → 入库/更新/跳过。

    返回每个文件的结构化结果（status/version/added/unchanged/removed/message）。
    """
    results = []
    async with AsyncSession() as db:
        for file_path in file_paths:
            results.append(await process_file(db, file_path))
    return results


async def search_documents(
    query: str,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
) -> List[dict]:
    """检索：双路召回 Top recall_k → bge-reranker 精排 → 阈值过滤取 Top k。

    返回 JSON 友好的命中列表：[{content, score, source, metadata}, ...]
    """
    hits = await asearch_with_rerank(query, k=k, expr=expr, source=source)
    return [
        {
            "content": hit.page_content,
            "score": hit.metadata.get("rerank_score"),
            "source": hit.metadata.get("source"),
            "metadata": hit.metadata,
        }
        for hit in hits
    ]


async def list_documents() -> List[dict]:
    """列出已入库文档（读 vector_files），供用户选择检索范围。

    返回 [{id, file_name, version, source, chunk_count, sync_status}, ...]
    """
    async with AsyncSession() as db:
        files = await select_file_names(db)
    return [
        {
            "id": f.id,
            "file_name": f.file_name,
            "version": str(f.version),
            "source": f.source,
            "chunk_count": f.chunk_count,
            "sync_status": f.sync_status,
        }
        for f in files
    ]