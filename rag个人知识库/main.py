"""RAG 知识库命令行入口：入库 / 检索两个子命令。

用法：
  python rag个人知识库/main.py ingest               # 入库/增量同步
  python rag个人知识库/main.py search "查询词" -k 3  # 双路召回 + rerank 精排检索
"""

import argparse
import asyncio

from rag个人知识库.config.db_config import AsyncSession, engine, init_db
from rag个人知识库.ingest import process_file
from rag个人知识库.vector_store.milvus_store import asearch_with_rerank

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


async def run_ingest(_args) -> None:
    """入库入口：建表 → 逐文件预检（加载前判断）→ 按需加载 → 切分 → 入库/更新/跳过 → 汇总"""
    await init_db()

    results = []
    async with AsyncSession() as db:
        for i, file_path in enumerate(file_path_list, start=1):
            print("=" * 30)
            print(f"第{i}个文档：{file_path}")
            results.append(await process_file(db, file_path))
            print("=" * 30)

    print("\n[入库汇总]")
    for result in results:
        if result["status"] == "error":
            print(f"  ✗ {result['file_path']} 失败：{result['message']}")
            continue
        label = {
            "inserted": "全新入库",
            "updated": "内容变更，已更新",
            "retried": "上次同步失败，已重放",
            "skipped": "内容未变，已跳过",
        }[result["status"]]
        line = f"  {result['file_path']} -> {label}，版本 v{result['version']}"
        if result["status"] == "updated":
            line += (
                f"（新增 {result['added']} / 未变 {result['unchanged']} / "
                f"删除 {result['removed']}）"
            )
        print(line)


async def run_search(args) -> None:
    """查询入口：双路召回 Top recall_k → bge-reranker 精排 → 阈值过滤取 Top k"""
    await init_db()
    print(f"\n[检索] 查询：{args.query}")
    hits = await asearch_with_rerank(args.query, k=args.top_k)
    if not hits:
        print("  未检索到相关结果")
        return
    for rank, hit in enumerate(hits, start=1):
        print(f"Top{rank}（精排分 {hit.metadata.get('rerank_score')}）: {hit.page_content[:120]}...")
        print(f"  来源：{hit.metadata.get('source')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 知识库：入库 / 检索")
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser("ingest", help="入库/增量同步")
    ingest_parser.set_defaults(handler=run_ingest)

    search_parser = subparsers.add_parser("search", help="语义检索（向量召回 + rerank 精排）")
    search_parser.add_argument("query", help="查询词")
    search_parser.add_argument("-k", "--top-k", type=int, default=3, help="返回条数（默认 3）")
    search_parser.set_defaults(handler=run_search)

    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return
    try:
        await handler(args)
    finally:
        # 事件循环关闭前主动释放 aiomysql 连接池，
        # 避免连接在 loop 关闭后被 GC 触发 __del__ 异步关闭而报 "Event loop is closed"
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())