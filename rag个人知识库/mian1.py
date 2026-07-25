import os
from dotenv import load_dotenv
from openai import OpenAI
from pymilvus import MilvusClient

from 尚硅谷.RAG.rag个人知识库.load_file import load_documents
from 尚硅谷.RAG.rag个人知识库.spliter.spliter import split_markdown_hybrid

load_dotenv(override=True)

# =========================
# 1. 基本配置
# =========================
MILVUS_URI = "http://localhost:19530"
DB_NAME = "rag_tutorial"
COLLECTION_NAME = "pdf_knowledge"
EMBED_DIM = 1024  # BAAI/bge-m3 向量维度

PDF_FILE = "resources/04.sample-multilingual-text.pdf"


# =========================
# 2. 封装 SimpleEmbeddings（绕过 LangChain tokenization）
# =========================
class SimpleEmbeddings:
    """直连 OpenAI 兼容 API，绕过 LangChain 的 tiktoken 分词"""

    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in resp.data]

    def embed_query(self, text: str) -> list[float]:
        resp = self.client.embeddings.create(model=self.model, input=[text])
        return resp.data[0].embedding


# =========================
# 3. 初始化 Milvus
# =========================
def init_milvus():
    client = MilvusClient(uri=MILVUS_URI)
    # 创建数据库（如不存在）
    if DB_NAME not in client.list_databases():
        client.create_database(db_name=DB_NAME)
    client.use_database(db_name=DB_NAME)
    # 重建 collection
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.drop_collection(collection_name=COLLECTION_NAME)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        dimension=EMBED_DIM,
        metric_type="COSINE",
    )
    print(f"Milvus collection '{COLLECTION_NAME}' 已就绪")
    return client


# =========================
# 4. 主流程
# =========================
def main():
    # 4.1 加载文档
    print("=" * 50)
    print("步骤 1/4：加载文档")
    documents = load_documents(PDF_FILE)
    if not documents:
        print("文档加载失败，退出")
        return

    # 4.2 切分文档
    print("=" * 50)
    print("步骤 2/4：切分文档")
    chunks = split_markdown_hybrid(documents, max_chunk_size=800, chunk_overlap=100)
    print(f"共切分出 {len(chunks)} 个 chunk")

    # 4.3 初始化 Embedding 模型
    print("=" * 50)
    print("步骤 3/4：初始化 Embedding 模型")
    embed_client = OpenAI(
        api_key=os.getenv("SILICONFLOW_API_KEY"),
        base_url=os.getenv("SILICONFLOW_BASE_URL"),
    )
    embed_model = SimpleEmbeddings(embed_client, model="BAAI/bge-m3")

    # 4.4 生成向量并写入 Milvus
    print("=" * 50)
    print("步骤 4/4：生成向量并写入 Milvus")
    milvus_client = init_milvus()

    texts = [chunk.page_content for chunk in chunks]
    vectors = embed_model.embed_documents(texts)

    data = [
        {
            "id": i,
            "vector": vectors[i],
            "text": texts[i],
            "source": PDF_FILE,
            "chunk_id": i,
            # 保留标题元数据（MarkdownHeaderTextSplitter 会写入 metadata）
            "header": str(chunks[i].metadata) if chunks[i].metadata else "",
        }
        for i in range(len(chunks))
    ]

    milvus_client.upsert(collection_name=COLLECTION_NAME, data=data)
    milvus_client.flush(collection_name=COLLECTION_NAME)

    stats = milvus_client.get_collection_stats(collection_name=COLLECTION_NAME)
    print(f"写入完成，collection 统计：{stats}")
    print("全部完成！")


if __name__ == "__main__":
    main()
