"""
向量化存储与查询接口（MySQL 指纹 与 Milvus 主键共用同一口径）
"""
import asyncio
import logging
import os
import threading
import time
from functools import lru_cache
from typing import List, Optional, Tuple

import requests
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_milvus import BM25BuiltInFunction, Milvus
from langchain_openai import OpenAIEmbeddings
from pymilvus import Function, FunctionType

from rag个人知识库.config.redis import cache_get, cache_get_sync, cache_key, cache_set, cache_set_sync
from rag个人知识库.utils.hash_utils import compute_chunk_fingerprint

load_dotenv()

logger = logging.getLogger(__name__)

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

# 线程级 embedding 计时：embed_query 内写入，检索结束后读取，用于可观测性分跳
_embedding_timer = threading.local()


def _require_env(name: str) -> str:
    """读取必需的环境变量，缺失时直接报清晰错误，避免后续出现难排查的 401/空地址异常"""
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(f"缺少环境变量 {name}，请在 .env 中配置")
    return value


_env_cache: dict = {}


def _require_env_cached(name: str) -> str:
    """读取必需环境变量并缓存结果（rerank 热路径免重复 os.getenv）"""
    if name not in _env_cache:
        _env_cache[name] = _require_env(name)
    return _env_cache[name]


# 缓存 TTL（秒）：embedding 7 天 / 检索结果 10 分钟 / 回答 1 小时（可用环境变量覆盖）
EMBEDDING_CACHE_TTL = int(os.getenv("EMBEDDING_CACHE_TTL", str(7 * 24 * 3600)))
SEARCH_CACHE_TTL = int(os.getenv("SEARCH_CACHE_TTL", "600"))
ANSWER_CACHE_TTL = int(os.getenv("ANSWER_CACHE_TTL", "3600"))


# 精排前召回候选数（默认 20，可用环境变量 RAG_RECALL_K 调整）：越大 reranker 候选池越宽，
# 但 rerank 延迟/token 随候选数线性增长。低分根因是阈值过滤误杀，见 _search_with_rerank_metrics 保底填充。
DEFAULT_RECALL_K = int(os.getenv("RAG_RECALL_K", "20"))

# fill-to-k 补进来的候补分数下限：低于此分即使达标数不足也不补（避免纯噪声混入）。
# 可用环境变量 RAG_FILL_FLOOR 调整（0=不设下限）。
FILL_SCORE_FLOOR = float(os.getenv("RAG_FILL_FLOOR", "0.1"))


class CachedEmbeddings(OpenAIEmbeddings):
    """OpenAIEmbeddings 子类：embed_query 结果缓存到 Redis。

    相同查询文本免重复调用 embedding API（省额度 + 降延迟）。
    同步实现（embed_query 在线程中执行）；Redis 不可用时静默透传。
    """

    def embed_query(self, text: str):
        t0 = time.monotonic()
        try:
            key = cache_key("emb", text)
            cached = cache_get_sync(key)
            if cached is not None:
                return cached
            vector = super().embed_query(text)
            cache_set_sync(key, vector, EMBEDDING_CACHE_TTL)
            return vector
        finally:
            _embedding_timer.ms = int((time.monotonic() - t0) * 1000)

    async def aembed_query(self, text: str):
        t0 = time.monotonic()
        try:
            key = cache_key("emb", text)
            cached = await cache_get(key)
            if cached is not None:
                return cached
            vector = await super().aembed_query(text)
            await cache_set(key, vector, EMBEDDING_CACHE_TTL)
            return vector
        finally:
            _embedding_timer.ms = int((time.monotonic() - t0) * 1000)


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """构造 SiliconFlow embedding 客户端（OpenAI 兼容协议，进程内单例复用）；
    返回 CachedEmbeddings 包装，embed_query 走 Redis 缓存。"""
    return CachedEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=_require_env("SILICONFLOW_API_KEY"),
        base_url=_require_env("SILICONFLOW_BASE_URL"),
        # bge-m3 不是 OpenAI 模型，tiktoken 无法为它预分词，
        # 必须关闭本地长度检查，把原文直接交给服务端处理
        check_embedding_ctx_length=False,
    )


@lru_cache(maxsize=1)
def _build_vector_store() -> Milvus:
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


def get_vector_store() -> Milvus:
    """获取 Milvus 实例（进程内单例）。

    连接/执行异常由 _invalidate_vector_store() 触发缓存重建，实现断线自愈：
    不会因为一次连接中断就让整个进程的后续检索/写入永久失败。
    """
    return _build_vector_store()


def _invalidate_vector_store() -> None:
    """Milvus 连接/执行异常后丢弃缓存单例，下次调用自动重建。"""
    _build_vector_store.cache_clear()


def _milvus_op(fn, *args, **kwargs):
    """执行 Milvus 操作；异常时丢弃单例以便下次重建连接，然后向上抛出。

    只做"下次自愈"、不自动重试当前操作：写入类操作（insert/delete）重试可能
    产生重复数据，保守起见让调用方决定是否重试。
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        _invalidate_vector_store()
        raise


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
        logger.info("[VectorStore] 没有需要入库的 chunk，跳过")
        return []

    unique_chunks, ids = _dedup_chunks(chunks)
    vector_store = get_vector_store()
    # 集合已存在时先删除同 ID 旧数据（首次运行集合尚未创建，col 为 None，直接插入）。
    # 与 delete_chunks_by_ids 一致按 batch_size 分片删除，避免超大文件拼出超长
    # pk in [...] 表达式被 Milvus 拒绝（同 _FILE_ID_EXPR_CHUNK 的考量）。
    if vector_store.col is not None:
        for start in range(0, len(ids), batch_size):
            _milvus_op(vector_store.delete, ids=ids[start:start + batch_size])

    inserted: List[str] = []
    for start in range(0, len(unique_chunks), batch_size):
        batch = unique_chunks[start:start + batch_size]
        batch_ids = ids[start:start + batch_size]
        inserted.extend(_milvus_op(vector_store.add_documents, batch, ids=batch_ids))
    logger.info("[VectorStore] 成功写入 %d 个 chunk（原始 %d 个，批内去重 %d 个）",
                len(inserted), len(chunks), len(chunks) - len(unique_chunks))
    return inserted


def delete_chunks_by_ids(ids: List[str], batch_size: int = 64) -> None:
    """按确定性主键删除 Milvus 中的旧 chunk（更新流程中已消失的 chunk）"""
    if not ids:
        logger.info("[VectorStore] 没有需要删除的 chunk ID，跳过")
        return
    vector_store = get_vector_store()
    if vector_store.col is None:
        logger.info("[VectorStore] 集合尚未创建，无需删除")
        return
    for start in range(0, len(ids), batch_size):
        _milvus_op(vector_store.delete, ids=ids[start:start + batch_size])
    logger.info("[VectorStore] 已删除 %d 个旧 chunk", len(ids))


def _source_expr(source: str) -> str:
    """构造按 source 过滤的 Milvus 表达式（转义反斜杠与双引号）"""
    escaped = source.replace("\\", "\\\\").replace('"', '\\"')
    return f'source == "{escaped}"'


def delete_chunks_by_source(source: str) -> None:
    """按 source 删除该文件在 Milvus 的全部向量（retry 重建用，先清后插避免残留孤儿向量）"""
    vector_store = get_vector_store()
    if vector_store.col is None:
        logger.info("[VectorStore] 集合尚未创建，无需删除")
        return
    ok = _milvus_op(vector_store.delete, expr=_source_expr(source))
    if not ok:
        raise RuntimeError(f"按 source 删除 Milvus 向量失败：{source}")
    logger.info("[VectorStore] 已按 source 删除该文件全部向量：%s", source)


def _owner_expr(owner_id: int) -> str:
    """构造按用户 id 过滤的 Milvus 表达式：owner_id == 15（账户删除清向量用）"""
    return f"owner_id == {int(owner_id)}"


def delete_chunks_by_owner(owner_id: int) -> None:
    """按 owner_id 删除该用户在 Milvus 的全部向量（账户删除队列表征，幂等可重试）"""
    vector_store = get_vector_store()
    if vector_store.col is None:
        logger.info("[VectorStore] 集合尚未创建，无需删除")
        return
    ok = _milvus_op(vector_store.delete, expr=_owner_expr(owner_id))
    if not ok:
        raise RuntimeError(f"按 owner_id 删除 Milvus 向量失败：{owner_id}")
    logger.info("[VectorStore] 已按 owner_id 删除该用户全部向量：%s", owner_id)

# 单个 in 子句的 id 数上限：文档极多的用户可见集很大时，
# 超长过滤表达式可能被 Milvus 拒绝，按此分段（500 id ≈ 4KB 表达式）
_FILE_ID_EXPR_CHUNK = 500

# RRF 融合器：按各路排名倒数 1/(k+rank) 加总打分，与两路分数量纲无关，无需调权重；
# 无状态对象，模块级定义一次处处复用
_RRF_RANKER = Function(
    name="rrf",
    input_field_names=[],
    function_type=FunctionType.RERANK,
    params={"reranker": "rrf", "k": 60},
)


def _file_ids_expr(file_ids: List[int]) -> str:
    """构造按文件 id 过滤的 Milvus 表达式：file_id in [1,2,3]（可见性过滤的核心载体）。

    超过单个 in 子句的 id 数上限时分段为 (in [...] or in [...])，语义等价，
    避免超大可见集拼出被 Milvus 拒绝的超长表达式；
    去重排序保证同一可见集生成确定性表达式（有利于检索缓存命中）。
    """
    unique_ids = sorted(set(file_ids))
    clauses = [
        "file_id in [" + ",".join(str(i) for i in unique_ids[start:start + _FILE_ID_EXPR_CHUNK]) + "]"
        for start in range(0, len(unique_ids), _FILE_ID_EXPR_CHUNK)
    ]
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " or ".join(clauses) + ")"


def search(
    query: str,
    k: int = 3,
    expr: Optional[str] = None,
    source: Optional[str] = None,
    file_ids: Optional[List[int]] = None,
    fetch_k: Optional[int] = None,
) -> List[Document]:
    """双路召回：dense 语义 + BM25 关键词两路各取候选，RRF 融合后返回 Top k。

    expr: 原生 Milvus 过滤表达式（如 'file_name == "a.docx"'）；
    source: 便捷的按来源过滤；file_ids: 按文件 id 集合过滤（可见性控制）。
    expr 与 source 互斥：二者都用于限定 Milvus 检索范围，同时传入会引发歧义，
    直接抛 ValueError（请二选一）；file_ids 可与二者任一叠加，作为最外层用 and 组合。
    """
    if source is not None and expr:
        raise ValueError("source 与 expr 互斥，不能同时指定（请二选一）")
    vector_store = get_vector_store()
    parts = []
    if file_ids:
        parts.append(_file_ids_expr(file_ids))
    if source is not None:
        parts.append(_source_expr(source))
    if expr:
        parts.append(expr)
    filter_expr = " and ".join(parts) if parts else None
    kwargs: dict = {
        # 每路各自预取 fetch_k 条再融合（库内默认只预取 4 条，必须显式放大）。
        # 默认与 k 相同；调用方可单独放大召回宽度（如 rerank 前先召回更多候选）。
        "fetch_k": fetch_k if fetch_k is not None else k,
        "reranker": _RRF_RANKER,
    }
    if filter_expr:
        kwargs["expr"] = filter_expr
    _embedding_timer.ms = 0  # 重置，避免读到上一次检索的计时
    return _milvus_op(vector_store.similarity_search, query, k=k, **kwargs)


def _rerank(query: str, documents: List[str], top_n: int) -> List[dict]:
    """调用 SiliconFlow rerank 接口，返回 [{index, relevance_score}, ...]（按分数降序）"""
    if not documents:
        return []

    resp = _session.post(
        f"{_require_env_cached('SILICONFLOW_BASE_URL').rstrip('/')}/rerank",
        headers={"Authorization": f"Bearer {_require_env_cached('SILICONFLOW_API_KEY')}"},
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


def _empty_retrieval_metrics() -> dict:
    """检索指标默认值（无召回 / 无法统计时）。"""
    return {
        "recall_count": 0,
        "rerank_count": 0,
        "rerank_avg_score": None,
        "rerank_max_score": None,
        "rerank_degraded": False,
        "embedding_ms": 0,
        "milvus_ms": 0,
        "rerank_ms": 0,
    }


def _search_with_rerank_metrics(
    query: str,
    k: int = 5,
    recall_k: int = DEFAULT_RECALL_K,
    score_threshold: float = RERANK_SCORE_THRESHOLD,
    expr: Optional[str] = None,
    source: Optional[str] = None,
    file_ids: Optional[List[int]] = None,
) -> Tuple[List[Document], dict]:
    """双路召回 + 精排（含检索指标）：返回 (文档列表, 指标)。

    recall_k 需明显大于 k（默认 20 vs 5），给精排留足挑选空间；
    精排分写入 metadata 的 rerank_score，便于下游观察区分度；
    rerank 接口故障时降级返回召回 Top k，不让精排环节成为单点故障。
    指标供可观测性使用：recall_count / rerank_count / 平均与最高分 / 降级标记。
    expr 与 source 互斥（同 search）：同时传参会抛 ValueError。
    file_ids: 按文件 id 集合过滤（可见性控制），透传给 search。
    """
    t_retr0 = time.monotonic()
    candidates = search(query, k=recall_k, expr=expr, source=source, file_ids=file_ids)
    t_retr1 = time.monotonic()
    embedding_ms = int(getattr(_embedding_timer, "ms", 0) or 0)
    milvus_ms = max(0, int((t_retr1 - t_retr0) * 1000) - embedding_ms)
    if not candidates:
        return [], _empty_retrieval_metrics()

    try:
        # 对全部召回候选做精排，避免阈值过滤后无法用后续候选补位
        t_rr0 = time.monotonic()
        results = _rerank(
            query,
            [c.page_content for c in candidates],
            top_n=len(candidates),
        )
        rerank_ms = int((time.monotonic() - t_rr0) * 1000)
    except Exception as e:
        # 精排只是质量增强，接口/响应异常时退回双路召回结果，保检索可用；
        # 降级结果没有精排分，显式写 None 并打 rerank_degraded 标记，供展示层区分
        logger.warning("[Rerank] 接口调用失败，降级返回召回 Top%d：%s", k, e)
        degraded = candidates[:k]
        for doc in degraded:
            doc.metadata["rerank_score"] = None
            doc.metadata["rerank_degraded"] = True
        return degraded, {
            "recall_count": len(candidates),
            "rerank_count": len(degraded),
            "rerank_avg_score": None,
            "rerank_max_score": None,
            "rerank_degraded": True,
            "embedding_ms": embedding_ms,
            "milvus_ms": milvus_ms,
            "rerank_ms": 0,
        }

    # results 已按 relevance_score 降序返回（rerank 接口保证）
    scored: List[Tuple[float, Document]] = []
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
        doc = candidates[index]
        doc.metadata["rerank_score"] = round(score, 4)
        scored.append((score, doc))

    # 第一优先：达标项（>= threshold）按分数降序直接入选
    reranked: List[Document] = []
    scores: List[float] = []
    for score, doc in scored:
        if len(reranked) >= k:
            break
        if score >= score_threshold:
            reranked.append(doc)
            scores.append(score)

    # 保底填充：达标项不足 k 时，用"未达标但分数最高"的候补补齐到 k。
    # 背景：小知识库 + 0.3 阈值一刀切会把真正的答案 chunk 误杀，导致最终返回数
    # 远小于 k（实测常只剩 2~5 条）。补齐后保证答案能进 Top-k，由下游生成侧兜底。
    if len(reranked) < k:
        used = {id(d) for d in reranked}
        for score, doc in scored:
            if score < FILL_SCORE_FLOOR:
                # 候选按分数降序：一旦低于下限，后续只会更低，直接结束
                break
            if len(reranked) >= k:
                break
            if id(doc) in used:
                continue
            used.add(id(doc))
            doc.metadata["rerank_low_score"] = True  # 低于阈值的候补标记
            reranked.append(doc)
            scores.append(score)

    logger.info("[Rerank] 召回 %d 条，精排后保留 %d 条（阈值 %s）",
                len(candidates), len(reranked), score_threshold)
    return reranked, {
        "recall_count": len(candidates),
        "rerank_count": len(reranked),
        "rerank_avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        "rerank_max_score": round(max(scores), 4) if scores else None,
        "rerank_degraded": False,
        "embedding_ms": embedding_ms,
        "milvus_ms": milvus_ms,
        "rerank_ms": rerank_ms,
    }


def search_with_rerank(
    query: str,
    k: int = 5,
    recall_k: int = DEFAULT_RECALL_K,
    score_threshold: float = RERANK_SCORE_THRESHOLD,
    expr: Optional[str] = None,
    source: Optional[str] = None,
    file_ids: Optional[List[int]] = None,
) -> List[Document]:
    """双路召回 + 精排：dense/BM25 双路召回 Top recall_k → reranker 精排 → 阈值过滤后取 Top k。
    expr 与 source 互斥（同 search）：同时传参会抛 ValueError。
    """
    docs, _ = _search_with_rerank_metrics(
        query, k, recall_k, score_threshold, expr, source, file_ids
    )
    return docs


def get_collection_row_count() -> Optional[int]:
    """查询 Milvus 集合当前行数（存储概览用）；连接/集合异常返回 None。"""
    try:
        store = get_vector_store()
        if _milvus_op(store.client.has_collection, store.collection_name):
            stats = _milvus_op(store.client.get_collection_stats, store.collection_name)
            row_count = stats.get("row_count")
            if row_count is not None:
                return int(row_count)
    except Exception as e:
        logger.warning("[milvus] 读取集合行数失败：%s", e)
    return None


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


async def adelete_chunks_by_owner(owner_id: int) -> None:
    """delete_chunks_by_owner 的异步版本：线程池中执行按 owner_id 删除"""
    await asyncio.to_thread(delete_chunks_by_owner, owner_id)


async def asearch_with_rerank(
    query: str,
    k: int = 5,
    recall_k: int = DEFAULT_RECALL_K,
    score_threshold: float = RERANK_SCORE_THRESHOLD,
    expr: Optional[str] = None,
    source: Optional[str] = None,
    file_ids: Optional[List[int]] = None,
) -> Tuple[List[Document], dict]:
    """search_with_rerank 的异步版本（含检索指标）：线程池中执行召回 + 精排。
    expr 与 source 互斥（同 search）：同时传参会抛 ValueError。
    """
    return await asyncio.to_thread(
        _search_with_rerank_metrics, query, k, recall_k, score_threshold, expr, source, file_ids
    )
