from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


def split_markdown_by_header(documents: List[Document]) -> List[Document]:
    """
    按 Markdown 标题层级（H1~H4）切分文档，保留标题元数据。
    适合结构化较强的 Markdown 文件（有明确的 # / ## / ### 层级）。
    """
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
        ("####", "Header 4"),
    ]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)

    chunks = []
    for doc in documents:
        split_docs = splitter.split_text(doc.page_content)
        chunks.extend(split_docs)

    print(f"[HeaderSplitter] 共切分出 {len(chunks)} 个 chunk")
    return chunks


def split_by_chars(
    documents: List[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 100,
) -> List[Document]:
    """
    按字符数切分文档，适合没有明显标题结构的纯文本。
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "，", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    print(f"[CharSplitter] 共切分出 {len(chunks)} 个 chunk")
    return chunks


def split_markdown_hybrid(
    documents: List[Document],
    max_chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> List[Document]:
    """
    混合切分策略：
    1. 先按 Markdown 标题切分（保留语义完整性）
    2. 对过长的片段再按字符数二次切分（防止超出 embedding 上下文限制）
    """
    # 第一步：按标题切分
    header_chunks = split_markdown_by_header(documents)

    # 第二步：对超长片段二次切分
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "，", " ", ""],
    )

    final_chunks = []
    for chunk in header_chunks:
        if len(chunk.page_content) > max_chunk_size:
            sub_chunks = char_splitter.split_documents([chunk])
            final_chunks.extend(sub_chunks)
        else:
            final_chunks.append(chunk)

    print(f"[HybridSplitter] 最终共切分出 {len(final_chunks)} 个 chunk")
    return final_chunks
