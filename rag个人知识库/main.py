"""RAG 知识库命令行入口：入库 / 检索 / 问答三个子命令（业务逻辑在 service 层，前端可复用）。

用法：
  python -m rag个人知识库.main ingest                    # 入库/增量同步
  python -m rag个人知识库.main search --list             # 列出已入库文档
  python -m rag个人知识库.main search "查询词" -k 3       # 全库检索
  python -m rag个人知识库.main search "查询词" --source "路径"  # 指定文档内检索
  python -m rag个人知识库.main chat "LangChain 是什么" -k 3  # 基于知识库问答
"""

import argparse
import asyncio

from rag个人知识库.config.db_config import engine
from rag个人知识库.service.chat import chat
from rag个人知识库.service.service import ingest_files, list_documents, search_documents

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
    """入库入口：逐文件预检 → 按需加载 → 入库/更新/跳过 → 打印汇总"""
    results = await ingest_files(file_path_list)

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


def _format_score(score) -> str:
    """精排分为 None 说明 rerank 服务降级（按召回顺序返回），避免显示 'None'"""
    return f"{score}" if score is not None else "—（精排降级，按召回顺序）"


async def run_search(args) -> None:
    """查询入口：列出已入库文档，或按指定文档检索（双路召回 + rerank 精排）"""
    if args.list:
        await _print_documents()
        return
    if not args.query:
        print('请提供查询词，例如：python -m rag个人知识库.main search "LangChain 是什么"')
        return
    print(f"\n[检索] 查询：{args.query}")
    hits = await search_documents(args.query, k=args.top_k, expr=args.expr, source=args.source)
    if not hits:
        print("  未检索到相关结果")
        return
    for rank, hit in enumerate(hits, start=1):
        print(f"Top{rank}（精排分 {_format_score(hit['score'])}）: {hit['content'][:120]}...")
        print(f"  来源：{hit['source']}")


async def run_chat(args) -> None:
    """问答入口：意图识别 → 向量检索 → Agent 生成回答"""
    if not args.query:
        print('请提供问题，例如：python -m rag个人知识库.main chat "LangChain 是什么"')
        return

    print(f"\n[问答] 问题：{args.query}")
    result = await chat(
        args.query,
        k=args.top_k,
        source=args.source,
        expr=args.expr,
    )

    print("\n[回答]")
    print(result["answer"])
    if result.get("sources"):
        print("\n[来源]")
        for source in result["sources"]:
            print(f"  [{source['index']}] {source['source']}（精排分 {_format_score(source['score'])}）")


async def _print_documents() -> None:
    """打印已入库文档清单"""
    files = await list_documents()
    if not files:
        print("  知识库中暂无已入库文档")
        return
    print(f"\n[已入库文档] 共 {len(files)} 个：")
    for i, f in enumerate(files, start=1):
        print(f"  {i}. {f['file_name']}  v{f['version']}")
        print(f"     source: {f['source']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 知识库：入库 / 检索 / 问答")
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser("ingest", help="入库/增量同步")
    ingest_parser.set_defaults(handler=run_ingest)

    search_parser = subparsers.add_parser("search", help="语义检索（向量召回 + rerank 精排）")
    search_parser.add_argument("query", nargs="?", help="查询词（--list 时可不填）")
    search_parser.add_argument("-k", "--top-k", type=int, default=3, help="返回条数（默认 3）")
    search_parser.add_argument("--list", action="store_true", help="只列出已入库文档，不检索")
    search_parser.add_argument("--source", help="只在该文档（source）内检索，需与入库 source 一致")
    search_parser.add_argument("--expr", help="原生 Milvus 过滤表达式（与 --source 二选一，同时传以 --source 为准）")
    search_parser.set_defaults(handler=run_search)

    chat_parser = subparsers.add_parser("chat", help="基于知识库的智能问答")
    chat_parser.add_argument("query", help="用户问题")
    chat_parser.add_argument("-k", "--top-k", type=int, default=3, help="检索召回条数（默认 3）")
    chat_parser.add_argument("--source", help="只在该文档（source）内检索")
    chat_parser.add_argument("--expr", help="原生 Milvus 过滤表达式")
    chat_parser.set_defaults(handler=run_chat)

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
