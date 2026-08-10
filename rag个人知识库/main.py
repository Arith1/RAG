import asyncio

from rag个人知识库.config.db_config import AsyncSession, engine, init_db
from rag个人知识库.ingest import process_file
from rag个人知识库.vector_store.milvus_store import search_with_rerank

file_path_list = [
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\01.simple_word.docx",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\02.complicated_word.docx",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\03.simple_pdf.pdf",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\04.complicated_pdf.pdf",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\05.sample_text.txt",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\06.langchain-utf-8.txt",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\07.sample.md",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\AI智能体开发框架LangChain.docx",
]


async def main():
    try:
        # 1. 建表（幂等）
        await init_db()

        # 2. 逐文件：预检（加载前判断）→ 按需加载 → 切分 → 入库/更新/跳过
        results = []
        async with AsyncSession() as db:
            for i, file_path in enumerate(file_path_list, start=1):
                print("=" * 30)
                print(f"第{i}个文档：{file_path}")
                results.append(await process_file(db, file_path))
                print("=" * 30)

        # 3. 汇总
        print("\n[入库汇总]")
        for result in results:
            if result["status"] == "error":
                print(f"  ✗ {result['file_path']} 失败：{result['message']}")
                continue
            label = {
                "inserted": "全新入库",
                "updated": "内容变更，已更新",
                "skipped": "内容未变，已跳过",
            }[result["status"]]
            line = f"  {result['file_path']} -> {label}，版本 v{result['version']}"
            if result["status"] == "updated":
                line += (
                    f"（新增 {result['added']} / 未变 {result['unchanged']} / "
                    f"删除 {result['removed']}）"
                )
            print(line)

        # 4. 入库完成后做一次两段式检索：向量召回 Top20 → bge-reranker 精排取 Top3
        _query = "LangChain 是什么"
        print(f"\n[检索验证] 查询：{_query}")
        for rank, hit in enumerate(search_with_rerank(_query, k=3), start=1):
            print(f"Top{rank}（精排分 {hit.metadata.get('rerank_score')}）: {hit.page_content[:120]}...")
            print(f"  来源：{hit.metadata.get('source')}")
    finally:
        # 事件循环关闭前主动释放 aiomysql 连接池，
        # 避免连接在 loop 关闭后被 GC 触发 __del__ 异步关闭而报 "Event loop is closed"
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())