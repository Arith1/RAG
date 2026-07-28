"""
向量存储层：把切分后的 chunk 向量化并写入 Milvus。

设计要点：
1. Embedding 走 SiliconFlow 的 OpenAI 兼容接口（BAAI/bge-m3，1024 维），
   与切分层按 bge 系 512 token 窗口设定的 chunk 参数配套
2. chunk 的 metadata 键不固定（Header 1~4 / images / md_path 可能缺失），
   启用 Milvus 动态字段（enable_dynamic_field）统一兜底，无需预定义 schema
3. 幂等入库：用 source + 内容哈希生成确定性主键，重复入库前先删同 ID 旧数据，
   反复运行 main.py 不会产生重复向量
"""
import hashlib
import os
from typing import List

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_openai import OpenAIEmbeddings

load_dotenv()

# Milvus 连接地址：Docker Standalone 默认暴露 19530 gRPC 端口
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
# 知识库集合名，所有文档的 chunk 统一入这一个集合，靠 metadata.source 区分来源
COLLECTION_NAME = "rag_knowledge_base"
# SiliconFlow 上的 bge-m3：中英双语效果好，输出 1024 维向量
EMBEDDING_MODEL = "BAAI/bge-m3"


def get_embeddings() -> OpenAIEmbeddings:
    """构造 SiliconFlow embedding 客户端（OpenAI 兼容协议）"""
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=os.getenv("SILICONFLOW_API_KEY"),
        base_url=os.getenv("SILICONFLOW_BASE_URL"),
        # bge-m3 不是 OpenAI 模型，tiktoken 无法为它预分词，
        # 必须关闭本地长度检查，把原文直接交给服务端处理
        check_embedding_ctx_length=False,
    )


def get_vector_store() -> Milvus:
    """构造 Milvus 向量库实例（集合不存在时会在首次写入时自动建集合和索引）"""
    return Milvus(
        embedding_function=get_embeddings(),
        collection_name=COLLECTION_NAME,
        connection_args={"uri": MILVUS_URI},
        # 动态字段兜底不固定的 metadata 键（Header 1~4、images 等）
        enable_dynamic_field=True,
        # 关闭自动主键，改用确定性 ID 实现幂等入库
        auto_id=False,
        # HNSW 图索引 + 余弦相似度：中小规模知识库检索精度和速度的均衡选择
        index_params={"index_type": "HNSW", "metric_type": "COSINE"},
    )


def _chunk_id(chunk: Document) -> str:
    """由 source + 正文内容生成确定性主键：同一 chunk 反复入库 ID 不变"""
    raw = f"{chunk.metadata.get('source', '')}|{chunk.page_content}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def save_chunks(chunks: List[Document]) -> List[str]:
    """
    把 chunk 列表向量化并写入 Milvus，返回写入的主键列表。

    幂等策略：先按确定性 ID 删除旧数据再插入（Milvus 的 insert 不校验主键唯一，
    直接重复插入会产生冗余向量，必须先删后插）。
    """
    if not chunks:
        print("[VectorStore] 没有需要入库的 chunk，跳过")
        return []

    # 批内去重：完全相同的 chunk（如重复段落）只保留一份，避免同批主键冲突
    unique_chunks, ids, seen = [], [], set()
    for chunk in chunks:
        cid = _chunk_id(chunk)
        if cid in seen:
            continue
        seen.add(cid)
        unique_chunks.append(chunk)
        ids.append(cid)

    vector_store = get_vector_store()
    # 集合已存在时先删除同 ID 旧数据（首次运行集合尚未创建，col 为 None，直接插入）
    if vector_store.col is not None:
        vector_store.delete(ids=ids)

    inserted = vector_store.add_documents(unique_chunks, ids=ids)
    print(f"[VectorStore] 成功写入 {len(inserted)} 个 chunk（原始 {len(chunks)} 个，批内去重 {len(chunks) - len(unique_chunks)} 个）")
    return inserted


def search(query: str, k: int = 3) -> List[Document]:
    """相似度检索：返回与 query 最相近的 k 个 chunk，用于验证入库效果"""
    vector_store = get_vector_store()
    return vector_store.similarity_search(query, k=k)
