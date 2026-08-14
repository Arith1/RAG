"""
向量化存储与查询接口（MySQL 指纹 与 Milvus 主键共用同一口径）
"""
import asyncio
import os
from functools import lru_cache
from typing import List, Optional, Tuple

import requests
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_milvus import BM25BuiltInFunction, Milvus
from langchain_openai import OpenAIEmbeddings
from pymilvus import Function, FunctionType

from rag个人知识库.utils.hash_utils import compute_chunk_fingerprint

load_dotenv()

# Milvus 连接地址：Docker Standalone 默认暴露 19530 gRPC 端口
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
# 知识库集合名，所有文档的 chunk 统一入这一个集合，靠 metadata.source 区分来源
COLLECTION_NAME = "rag_knowledge_base"
# SiliconFlow 上的 bge-m3：中英双语效果好，输出 1024 维向量
EMBEDDING_MODEL = "BAAI/bge-m3"
# 重排序模型：与 bge-m3 同源，语义对齐，专门用于小批量候选的精排打分
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
# 精排分阈值：reranker 输出的 relevance_score 区分度高，低于此值视为不相关直接丢弃；
# 可通过环境变量 RERANK_SCORE_THRESHOLD 调整（默认 0.3）
RERANK_SCORE_THRESHOLD = float(os.getenv("RERANK_SCORE_THRESHOLD", "0.3"))
# 双路召回的两个向量字段：dense 存 bge-m3 语义向量，sparse 存 BM25 词频稀疏向量
DENSE_FIELD = "dense"
SPARSE_FIELD = "sparse"

# 复用 HTTP 连接池：避免每次 rerank 请求都重新建立 TCP/TLS 连接
_session = requests.Session()


def _require_env(name: str) -> str:
    """读取必需的环境变量，缺失时直接报清晰错误，避免后续出现难排查的 401/空地址异常"""
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(f"缺少环境变量 {name}，请在 .env 中配置")
    return value


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """构造 SiliconFlow embedding 客户端（OpenAI 兼容协议，进程内单例复用）"""
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=_require_env("SILICONFLOW_API_KEY"),
        base_url=_require_env("SILICONFLOW_BASE_URL"),
        # bge-m3 不是 OpenAI 模型，tiktoken 无法为它预分词，
        # 必须关闭本地长度检查，把原文直接交给服务端处理
        check_embedding_ctx_length=False,
    )


@lru_cache(maxsize=1)
def get_vector_store() -> Milvus:
    """构造 Milvus 向量库实例（双路：dense 语义 + BM25 稀疏，集合首次写入时自动创建）

    进程内单例复用：Milvus 实例初始化时会建立 gRPC 连接并拉取集合 schema，
    入库/检索频繁重建代价高且无必要。
    """
    return Milvus(
        embedding_function=get_embeddings(),
        # BM25 内置函数：Milvus 服务端直接从 text 字段生成稀疏向量，入库/检索都无需本地分词；
        # chinese 分析器（jieba 分词）让中文按词而非整句参与 BM25 打分
        builtin_function=BM25BuiltInFunction(
            input_field_names="text",
            output_field_names=SPARSE_FIELD,
            analyzer_params={"type": "chinese"},
        ),
        vector_field=[DENSE_FIELD, SPARSE_FIELD],
        collection_name=COLLECTION_NAME,
        connection_args={"uri": MILVUS_URI},
        # 动态字段兜底不固定的 metadata 键（Header 1~4、images 等）
        enable_dynamic_field=True,
        # 关闭自动主键，改用确定性 ID 实现幂等入库
        auto_id=False,
        # 每个向量字段一套索引，顺序与 vector_field 对应：
        # dense 用 HNSW+余弦，sparse 用稀疏倒排+BM25 打分
        index_params=[
            {"index_type": "HNSW", "metric_type": "COSINE"},
            {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"},
        ],
    )


def _normalize_metadata(metadata: dict) -> dict:
    """规范化 metadata 键名：空格替换为下划线。

    Milvus 服务端解析 output_fields 时不接受带空格的字段名（报 code=1100），
    而 MarkdownHeaderTextSplitter 默认生成的键是 "Header 1"/"Header 2" 这种带空格形式，
    必须在入库前统一规范化。
    """
    return {k.replace(" ", "_"): v for k, v in metadata.items()}


def _chunk_id(chunk: Document) -> str:
    """由 source + 正文内容生成确定性主键：与 MySQL chunk_fingerprint 同口径（SHA256）"""
    return compute_chunk_fingerprint(chunk.page_content, chunk.metadata.get("source", ""))


def _dedup_chunks(chunks: List[Document]) -> Tuple[List[Document], List[str]]:
    """批内去重 + metadata 键名规范化，返回 (去重后的 chunk, 对应的确定性主键)"""
    unique_chunks, ids, seen = [], [], set()
    for chunk in chunks:
        chunk.metadata = _normalize_metadata(chunk.metadata)
        cid = _chunk_id(chunk)
        if cid in seen:
            continue
        seen.add(cid)
        unique_chunks.append(chunk)
        ids.append(cid)
    return unique_chunks, ids


def add_chunks(chunks: List[Document], batch_size: int = 64) -> List[str]:
    """把 chunk 向量化写入 Milvus，返回写入的主键列表。

    幂等策略：先按确定性 ID 删除旧数据再插入（Milvus 的 insert 不校验主键唯一，
    直接重复插入会产生冗余向量，必须先删后插）。

    分批写入：embedding 接口单次请求体积有上限，大文档一次性全量提交容易超长报错，
    按 batch_size 分批后单批失败也不影响已写入的批次。
    """
    if not chunks:
        print("[VectorStore] 没有需要入库的 chunk，跳过")
        return []

    unique_chunks, ids = _dedup_chunks(chunks)
    vector_store = get_vector_store()
    # 集合已存在时先删除同 ID 旧数据（首次运行集合尚未创建，col 为 None，直接插入）
    if vector_store.col is not None:
        vector_store.delete(ids=ids)

    inserted: List[str] = []
    for start in range(0, len(unique_chunks), batch_size):
        batch = unique_chunks[start:start + batch_size]
        batch_ids = ids[start:start + batch_size]
        inserted.extend(vector_store.add_documents(batch, ids=batch_ids))
    print(f"[VectorStore] 成功写入 {len(inserted)} 个 chunk（原始 {len(chunks)} 个，批内去重 {len(chunks) - len(unique_chunks)} 个）")
    return inserted


def save_chunks(chunks: List[Document], batch_size: int = 64) -> List[str]:
    """兼容旧入口：等价于 add_chunks"""
    return add_chunks(chunks, batch_size=batch_size)


def delete_chunks_by_ids(ids: List[str], batch_size: int = 64) -> None:
    """按确定性主键删除 Milvus 中的旧 chunk（更新流程中已消失的 chunk）"""
    if not ids:
        print("[VectorStore] 没有需要删除的 chunk ID，跳过")
        return
    vector_store = get_vector_store()
    if vector_store.col is None:
        print("[VectorStore] 集合尚未创建，无需删除")
        return
    for start in range(0, len(ids), batch_size):
        vector_store.delete(ids=ids[start:start + batch_size])
    print(f"[VectorStore] 已删除 {len(ids)} 个旧 chunk")


def _source_expr(source: str) -> str:
    """构造按 source 过滤的 Milvus 表达式（转义反斜杠与双引号）"""
    escaped = source.replace("\\", "\\\\").replace('"', '\\"')
    return f'source == "{escaped}"'


def delete_chunks_by_source(source: str) -> None:
    """按 source 删除该文件在 Milvus 的全部向量（retry 重建用，先清后插避免残留孤儿向量）"""
    vector_store = get_vector_store()
    if vector_store.col is None:
        print("[VectorStore] 集合尚未创建，无需删除")
        return
    ok = vector_store.delete(expr=_source_expr(source))
    if not ok:
        raise RuntimeError(f"按 source 删除 Milvus 向量失败：{source}")
    print(f"[VectorStore] 已按 source 删除该文件全部向量：{source}")

# RRF 融合器：按各路排名倒数 1/(k+rank) 加总打分，与两路分数量纲无关，无需调权重；
# 无状态对象，模块级定义一次处处复用
_RRF_RANKER = Function(
    name="rrf",
    input_field_names=[],
    function_type=FunctionType.RERANK,
    params={"reranker": "rrf", "k": 60},
)


def search(
    query: str,
    k: int = 3,
    expr: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Document]:
    """双路召回：dense 语义 + BM25 关键词两路各取候选，RRF 融合后返回 Top k。

    expr: 原生 Milvus 过滤表达式（如 'file_name == "a.docx"'）；
    source: 便捷的按来源过滤，与 expr 二选一，同时提供时以 source 为准。
    """
    vector_store = get_vector_store()
    filter_expr = _source_expr(source) if source is not None else expr
    kwargs: dict = {
        # 每路各自预取 k 条再融合（库内默认只预取 4 条，必须显式放大）
        "fetch_k": k,
        "reranker": _RRF_RANKER,
    }
    if filter_expr:
        kwargs["expr"] = filter_expr
    return vector_store.similarity_search(query, k=k, **kwargs)


def _rerank(query: str, documents: List[str], top_n: int) -> List[dict]:
    """调用 SiliconFlow rerank 接口，返回 [{index, relevance_score}, ...]（按分数降序）"""
    if not documents:
        return []

    resp = _session.post(
        f"{_require_env('SILICONFLOW_BASE_URL').rstrip('/')}/rerank",
        headers={"Authorization": f"Bearer {_require_env('SILICONFLOW_API_KEY')}"},
        json={
            "model": RERANK_MODEL,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results")
    if not isinstance(results, list):
        raise ValueError(f"rerank 响应缺少 results 列表：{resp.text[:200]}")
    return results


def search_with_rerank(
    query: str,
    k: int = 5,
    recall_k: int = 20,
    score_threshold: float = RERANK_SCORE_THRESHOLD,
    expr: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Document]:
    """
    双路召回 + 精排：dense/BM25 双路召回 Top recall_k → reranker 精排 → 阈值过滤后取 Top k。

    recall_k 需明显大于 k（默认 20 vs 5），给精排留足挑选空间；
    精排分写入 metadata 的 rerank_score，便于下游观察区分度；
    rerank 接口故障时降级返回召回 Top k，不让精排环节成为单点故障。
    """
    candidates = search(query, k=recall_k, expr=expr, source=source)
    if not candidates:
        return []

    try:
        # 对全部召回候选做精排，避免阈值过滤后无法用后续候选补位
        results = _rerank(
            query,
            [c.page_content for c in candidates],
            top_n=len(candidates),
        )
    except Exception as e:
        # 精排只是质量增强，接口/响应异常时退回双路召回结果，保检索可用
        print(f"[Rerank] 接口调用失败，降级返回召回 Top{k}：{e}")
        return candidates[:k]

    reranked = []
    for item in results:
        index = item.get("index")
        score = item.get("relevance_score")
        if index is None or score is None:
            continue
        try:
            index = int(index)
            score = float(score)
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(candidates)):
            continue
        # 低于阈值的候选直接丢弃，避免不相关内容污染下游 prompt
        if score < score_threshold:
            continue
        doc = candidates[index]
        doc.metadata["rerank_score"] = round(score, 4)
        reranked.append(doc)
    reranked = reranked[:k]
    print(f"[Rerank] 召回 {len(candidates)} 条，精排后保留 {len(reranked)} 条（阈值 {score_threshold}）")
    return reranked


# ── 异步包装：把阻塞型同步调用放进线程池执行，避免卡住事件循环（便于将来接入 FastAPI 等服务）──
async def aadd_chunks(chunks: List[Document], batch_size: int = 64) -> List[str]:
    """add_chunks 的异步版本：线程池中执行 embedding + Milvus 写入"""
    return await asyncio.to_thread(add_chunks, chunks, batch_size)


async def adelete_chunks_by_ids(ids: List[str], batch_size: int = 64) -> None:
    """delete_chunks_by_ids 的异步版本：线程池中执行 Milvus 删除"""
    await asyncio.to_thread(delete_chunks_by_ids, ids, batch_size)


async def adelete_chunks_by_source(source: str) -> None:
    """delete_chunks_by_source 的异步版本：线程池中执行按 source 删除"""
    await asyncio.to_thread(delete_chunks_by_source, source)


async def asearch_with_rerank(
    query: str,
    k: int = 5,
    recall_k: int = 20,
    score_threshold: float = RERANK_SCORE_THRESHOLD,
    expr: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Document]:
    """search_with_rerank 的异步版本：线程池中执行召回 + 精排"""
    return await asyncio.to_thread(
        search_with_rerank, query, k, recall_k, score_threshold, expr, source
    )
