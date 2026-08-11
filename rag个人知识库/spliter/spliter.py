"""
结构感知的文档切分器。

切分策略：
1. 标题不参与切分：按 Markdown 标题层级分节，完整标题路径存入 metadata，
   同时拼回 page_content 开头参与 embedding（双写，保留检索信号）
2. 正文超长时按字符二次切分
3. 公式（$$...$$）与表格（连续 | 行）为原子块，切分前用占位符保护，切分后还原，
   保证不被拦腰切断；超长表格按行组拆分且每块重复表头
4. 图片链接不独立成块，跟随所在段落，路径额外记入 metadata

入口 split_documents 按 metadata 中的 doc_type 自动分发策略。
"""
import re
from typing import Dict, List, Tuple

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

# 按 H1~H4 分节，key 用于 metadata、也用于拼接标题路径
HEADERS_TO_SPLIT_ON = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
    ("####", "Header 4"),
]

# 中文场景默认 chunk 参数：bge 系 embedding 有效窗口约 512 token
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50

# 块级公式：$$...$$（跨行）
_FORMULA_RE = re.compile(r'\$\$.*?\$\$', re.DOTALL)
# 表格：连续的以 | 开头的行
_TABLE_RE = re.compile(r'(?:^\|[^\n]*\|[ \t]*\n?)+', re.MULTILINE)
# Markdown 图片链接：![alt](path)
_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')


def _split_long_table(table_text: str, max_chunk_size: int) -> List[str]:
    """超长表格按行组拆分，每个子表都重复表头（前两行：表头+分隔行），保证子表可独立理解"""
    lines = [l for l in table_text.splitlines() if l.strip()]
    if len(lines) <= 2:
        return [table_text]
    header_lines, body = lines[:2], lines[2:]
    header_len = sum(len(l) + 1 for l in header_lines)
    parts, cur, cur_len = [], [], header_len
    for row in body:
        # 当前行组放不下了，先落一个子表
        if cur and cur_len + len(row) > max_chunk_size:
            parts.append("\n".join(header_lines + cur))
            cur, cur_len = [], header_len
        cur.append(row)
        cur_len += len(row) + 1
    if cur:
        parts.append("\n".join(header_lines + cur))
    return parts


def _normalize_long_tables(text: str, max_chunk_size: int) -> str:
    """把超过 max_chunk_size 的表格预先拆成多个带表头的子表（子表间以空行分隔，允许被切分开）"""
    def _repl(match):
        table = match.group(0)
        if len(table) <= max_chunk_size:
            return table
        return "\n\n".join(_split_long_table(table, max_chunk_size)) + "\n"
    return _TABLE_RE.sub(_repl, text)


def _protect_atomic_blocks(text: str) -> Tuple[str, Dict[str, str]]:
    """把公式/表格替换为占位符，返回 (替换后的文本, {占位符: 原始块})，防止被字符切分拦腰切断"""
    blocks: Dict[str, str] = {}

    def _repl(match):
        key = f"<ATOMIC_{len(blocks)}>"
        blocks[key] = match.group(0)
        return key

    text = _FORMULA_RE.sub(_repl, text)
    text = _TABLE_RE.sub(_repl, text)
    return text, blocks


def _restore_atomic_blocks(text: str, blocks: Dict[str, str]) -> str:
    """把占位符还原为原始公式/表格块"""
    for key, block in blocks.items():
        text = text.replace(key, block)
    return text


def _header_path(metadata: dict) -> str:
    """按层级拼接完整标题路径，如 '第三章 计算公式 > 3.2 丝网重量'"""
    titles = [metadata[name] for _, name in HEADERS_TO_SPLIT_ON if name in metadata]
    return " > ".join(titles)


def split_markdown_hybrid(
    documents: List[Document],
    max_chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Document]:
    """
    结构感知混合切分（Markdown 主策略）：
    1. 按标题分节，标题路径存 metadata（标题本身不参与切分）
    2. 超长小节保护公式/表格后按字符二次切分，切完还原
    3. 每个 chunk 的 page_content 开头拼上标题路径，参与 embedding
    """
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "，", " ", ""],
    )

    final_chunks = []
    for doc in documents:
        # 超长表格先拆成带表头的子表，避免单个原子块超出 embedding 窗口
        text = _normalize_long_tables(doc.page_content, max_chunk_size)
        # 按标题分节：标题进 metadata，正文为 page_content
        sections = header_splitter.split_text(text)
        for section in sections:
            # 合并来源文档 metadata（source 等），标题 metadata 一并保留——修复原实现丢 source 的问题
            merged_meta = {**doc.metadata, **section.metadata}
            # 图片链接跟随段落不独立成块，路径记入 metadata 供溯源/多模态使用
            images = _IMAGE_RE.findall(section.page_content)
            if images:
                merged_meta["images"] = ",".join(images)

            # 公式/表格替换为占位符后再切分，保证原子性
            protected, blocks = _protect_atomic_blocks(section.page_content)
            if len(protected) > max_chunk_size:
                sub_texts = char_splitter.split_text(protected)
            else:
                sub_texts = [protected]

            header_path = _header_path(section.metadata)
            for sub in sub_texts:
                restored = _restore_atomic_blocks(sub, blocks).strip()
                if not restored:
                    continue
                # 标题路径双写：既在 metadata，也拼进正文参与 embedding
                content = f"{header_path}\n\n{restored}" if header_path else restored
                final_chunks.append(Document(page_content=content, metadata=dict(merged_meta)))

    print(f"[HybridSplitter] 最终共切分出 {len(final_chunks)} 个 chunk")
    return final_chunks


def split_by_chars(
    documents: List[Document],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Document]:
    """按字符数切分，适合没有标题结构的纯文本（TXT、简单 Word）"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "，", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    print(f"[CharSplitter] 共切分出 {len(chunks)} 个 chunk")
    return chunks


def split_documents(
    documents: List[Document],
    max_chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Document]:
    """
    切分入口：按 metadata 分发策略。
      - doc_type == "markdown"（或 source 以 .md 结尾）→ 结构感知混合切分
      - 其他 → 纯字符切分
    加载阶段建议在 metadata 中写入 doc_type，未写入时按 source 后缀兜底判断。
    """
    md_docs, plain_docs = [], []
    for doc in documents:
        doc_type = doc.metadata.get("doc_type")
        source = str(doc.metadata.get("source", ""))
        if doc_type == "markdown" or (doc_type is None and source.lower().endswith(".md")):
            md_docs.append(doc)
        else:
            plain_docs.append(doc)

    chunks = []
    if md_docs:
        chunks.extend(split_markdown_hybrid(md_docs, max_chunk_size, chunk_overlap))
    if plain_docs:
        chunks.extend(split_by_chars(plain_docs, max_chunk_size, chunk_overlap))
    print(f"[split_documents] markdown 文档 {len(md_docs)} 个，纯文本文档 {len(plain_docs)} 个，共 {len(chunks)} 个 chunk")
    return chunks
