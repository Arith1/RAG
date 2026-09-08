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
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from rag个人知识库.utils.hash_utils import compute_chunk_fingerprint

logger = logging.getLogger(__name__)

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
# M18：超过此长度的原子块（公式/表格/问答对）不再整体保护——会被按正常规则切分，
# 避免「超长原子块整段保留」产生超 bge 窗口/超 Milvus 上限的巨型 chunk（强制截断兜底）
MAX_ATOMIC_CHARS = int(os.getenv("MAX_ATOMIC_CHARS", "2000"))

# ── 分层检索（Parent-Child）配置（默认关，保持现状；开启见 docs/PARENT_CHILD_RETRIEVAL_DESIGN.md）──
RAG_PARENT_CHILD = os.getenv("RAG_PARENT_CHILD", "false").strip().lower() in ("1", "true", "yes", "on")
CHILD_CHUNK_SIZE = int(os.getenv("CHILD_CHUNK_SIZE", "250"))      # 子块大小（检索单位）
CHILD_OVERLAP = int(os.getenv("CHILD_OVERLAP", "30"))             # 子块重叠
PARENT_MAX_CHARS = int(os.getenv("PARENT_MAX_CHARS", "2000"))     # Markdown 父块封顶（超长再切）
PARENT_OVERLAP = int(os.getenv("PARENT_OVERLAP", "50"))            # 超长父块再切时的重叠（保边界上下文）
PLAIN_PARENT_CHARS = int(os.getenv("PLAIN_PARENT_CHARS", "2000"))  # 纯文本父块大小
PLAIN_PARENT_OVERLAP = 100

# 块级公式：$$...$$（跨行）
_FORMULA_RE = re.compile(r'\$\$.*?\$\$', re.DOTALL)
# 表格：连续的以 | 开头的行
_TABLE_RE = re.compile(r'(?:^\|[^\n]*\|[ \t]*\n?)+', re.MULTILINE)
# 问答对（QA 文档）：**Q：...** 与紧随的 A：... 视为一个原子单元，防止 Q 和 A 被 chunk 边界拆散。
# 注意 MarkdownHeaderTextSplitter 会把段落间 \n\n 规范化为硬换行（两个空格+\n），
# 因此分隔符兼容 "  \n" / "\n" / "\n\n" 三种形态；
# ① Q 锚定行首（^），避免正文里引用的 "**Q：...**" 字样被误识别为问答对开头；
# ② 结尾 lookahead 直接匹配"分隔符 + 下一对 Q"，让匹配在段间分隔符之前停下，
#    分隔符保留在占位符之间，后续才能按段落切分（否则整节粘成一个 chunk）
_QA_PAIR_RE = re.compile(r'(?m)^\*\*Q：.*?\*\*[ \t]*\n+[ \t]*A：.*?(?=[ \t]*\n+[ \t]*\*\*Q：|\Z)', re.DOTALL)
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
    """把公式/表格/问答对替换为占位符，返回 (替换后的文本, {占位符: 原始块})，防止被字符切分拦腰切断。

    问答对最外层先保护：其 A 回答中可能内嵌公式/表格，作为整体保护后，
    内部内容不会再生成嵌套占位符，还原时无需考虑嵌套顺序。
    """
    blocks: Dict[str, str] = {}

    def _repl(match):
        # M18：超长原子块不保护（按正常规则切分），避免整段保留产生超限巨型 chunk
        if len(match.group(0)) > MAX_ATOMIC_CHARS:
            return match.group(0)
        key = f"<ATOMIC_{len(blocks)}>"
        blocks[key] = match.group(0)
        return key

    text = _QA_PAIR_RE.sub(_repl, text)
    text = _FORMULA_RE.sub(_repl, text)
    text = _TABLE_RE.sub(_repl, text)
    return text, blocks


def _restore_atomic_blocks(text: str, blocks: Dict[str, str]) -> str:
    """把占位符还原为原始公式/表格块"""
    for key, block in blocks.items():
        text = text.replace(key, block)
    return text


_ATOMIC_TOKEN_RE = re.compile(r'<ATOMIC_\d+>')


def _effective_len(text: str, blocks: Dict[str, str]) -> int:
    """按"还原后"长度计算：占位符按其原始块长度计，其余文本按字符数计。

    避免占位符让文本显得很短、导致整节问答对堆进一个超长 chunk。
    """
    total, tokens_len = 0, 0
    for token in _ATOMIC_TOKEN_RE.findall(text):
        tokens_len += len(token)
        total += len(blocks.get(token, token))
    return total + (len(text) - tokens_len)


def _split_by_restored_length(protected: str, blocks: Dict[str, str], max_chunk_size: int) -> List[str]:
    """按段落切分受保护文本、并按"还原后长度"贪心分组：
    - 原子块（公式/表格/问答对）作为整体永不被拆散
    - 每组还原后总长度 ≤ max_chunk_size（单段本身超限时独立成组，不强行切原子块）
    仅在存在原子块时使用；纯文本 section 仍走字符切分器。
    """
    parts = re.split(r'(\n+)', protected)  # [段, 分隔符, 段, ...]
    groups: List[List[str]] = [[]]
    cur_len = 0
    for i in range(0, len(parts), 2):
        seg = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        piece = seg + sep
        if not piece.strip():
            continue
        eff = _effective_len(piece, blocks)
        if groups[-1] and cur_len + eff > max_chunk_size:
            groups.append([])
            cur_len = 0
        groups[-1].append(piece)
        cur_len += eff
    return ["".join(g).strip() for g in groups if "".join(g).strip()]


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

            # 公式/表格/问答对替换为占位符后再切分，保证原子性
            protected, blocks = _protect_atomic_blocks(section.page_content)
            if blocks:
                # 存在原子块：按"还原后长度"在段落间分组，原子块不拆散且 chunk 不超长
                sub_texts = _split_by_restored_length(protected, blocks, max_chunk_size)
            elif len(protected) > max_chunk_size:
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

    logger.info("[HybridSplitter] 最终共切分出 %d 个 chunk", len(final_chunks))
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
    logger.info("[CharSplitter] 共切分出 %d 个 chunk", len(chunks))
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
    logger.info("[split_documents] markdown 文档 %d 个，纯文本文档 %d 个，共 %d 个 chunk",
            len(md_docs), len(plain_docs), len(chunks))
    return chunks


# ═══ 分层检索（Parent-Child）：父块 + 子块（含精确偏移）═══

def _atomic_ranges(text: str) -> List[Tuple[int, int]]:
    """原子块（公式/表格/问答对）区间：子块切分不得穿越这些区间。"""
    ranges: List[Tuple[int, int]] = []
    for pat in (_QA_PAIR_RE, _FORMULA_RE, _TABLE_RE):
        for m in pat.finditer(text):
            ranges.append((m.start(), m.end()))
    return sorted(ranges)


def _split_text_with_offsets(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    """按段落/句末边界切分并返回精确字符区间 [(start, end)]（子块用）。

    优先在 \\n 段落边界断，其次在句末标点（。；！？）断，找不到才按 size 硬切。
    - 原子块（公式/表格/问答对）：切点落在块内 → 延到块尾，**块永不被拦腰切断**
      （超长原子块独立成一块，可略超 size）。
    - 自然边界（段落/句末/块尾）处：下一块从边界后开始，不跨界重叠（防碎片级联）。
    - 硬切（满 size）：下一块用 overlap 回退，保留跨块上下文。
    - 剩余不足 size 时整段收尾。
    start/end 直接对齐入参 text（即最终父块文本，切片偏移一致）。
    """
    n = len(text)
    para = [m.start() for m in re.finditer(r"\n", text)]
    blocks = _atomic_ranges(text)

    def _block_end(pos: int) -> Optional[int]:
        """pos 严格位于某原子块内部时返回块尾，否则 None（blocks 按起点有序）。"""
        for b_s, b_e in blocks:
            if b_s < pos < b_e:
                return b_e
            if pos <= b_s:
                return None
        return None

    ranges: List[Tuple[int, int]] = []
    start = 0
    while start < n:
        # start 落到原子块内部（overlap 回退导致）→ 跳到块尾，不从中切开
        be = _block_end(start)
        if be is not None:
            start = be
            if start >= n:
                break
        if n - start <= size:
            ranges.append((start, n))
            break
        limit = start + size
        cut = None
        from_sentence = False
        # 段落边界：取 (start, limit] 内最后一个 \n
        for b in para:
            if start < b <= limit:
                cut = b
        if cut is None:
            # 句末标点：取 (start, limit] 内最后一个
            m = None
            for mm in re.finditer(r"[。；！？]\s*", text[start:limit]):
                m = mm
            if m:
                cut = start + m.end()
                from_sentence = True
        if cut is None or cut <= start:
            cut = limit
            from_sentence = False
        # 原子块：切点落在块内 → 延到块尾（块保持完整）
        be = _block_end(cut)
        snapped = be is not None
        if snapped:
            cut = be
            from_sentence = False
        end = min(cut, n)
        ranges.append((start, end))
        if end >= n:
            break
        if from_sentence or snapped:
            # 句末/原子块尾：不跨界重叠
            start = end
        elif end < n and text[end] == "\n":
            # 段落边界：跳过换行
            start = end + 1
        else:
            # 硬切（满 size）：允许 overlap
            start = max(start + 1, end - overlap)
    return ranges


def _split_parent_pieces(text: str, max_chars: int, overlap: int = 0) -> List[Tuple[int, str]]:
    """超长父块按 max_chars 再切（可选 overlap），返回 [(base, piece)]，base 为 piece 在 text 中的偏移。"""
    if len(text) <= max_chars:
        return [(0, text)]
    return [
        (i, text[i:i + max_chars])
        for i in range(0, len(text), max_chars - overlap)
    ]


def _emit_parent(
    base_meta: dict,
    text: str,
    title: str,
    source: str,
    parent_index: int,
    parents: List[dict],
    children: List[Document],
    sec_ranges: Optional[List[Tuple[int, int, dict]]] = None,
    piece_base: int = 0,
) -> None:
    """把一个父块产出为 parents 记录 + 其子块（含偏移 + 所属节的 Header/images 元数据）。"""
    parent_id = compute_chunk_fingerprint(text, source)  # 内容派生（不含 version）
    parents.append({
        "parent_id": parent_id,
        "source": source,
        "parent_index": parent_index,
        "parent_title": title,
        "parent_text": text,
        "char_len": len(text),
    })
    for start, end in _split_text_with_offsets(text, CHILD_CHUNK_SIZE, CHILD_OVERLAP):
        child_text = text[start:end]
        content = f"{title}\n\n{child_text}" if title else child_text
        meta = dict(base_meta)
        # 补齐子块的节级溯源元数据（Header N / images）：按子块中点定位所属节
        if sec_ranges:
            abs_mid = piece_base + (start + end) // 2
            sec_meta = next((sm for s, e, sm in sec_ranges if s <= abs_mid < e), None)
            if sec_meta:
                for k in ("Header 1", "Header 2", "Header 3", "Header 4"):
                    if k in sec_meta:
                        meta[k] = sec_meta[k]
                if sec_meta.get("images"):
                    meta["images"] = sec_meta["images"]
        meta.update({
            "parent_id": parent_id,
            "parent_index": parent_index,
            "parent_char_start": start,
            "parent_char_end": end,
        })
        children.append(Document(page_content=content, metadata=meta))


def split_documents_parent_child(
    documents: List[Document],
) -> Tuple[List[Document], List[dict]]:
    """分层切分（Parent-Child）：返回 (children, parents)。

    - children：CHILD_CHUNK_SIZE(250) 子块（参与 ANN 检索），metadata 含
      parent_id / parent_index / parent_char_start / parent_char_end（偏移精确对齐 parent_text）。
    - parents：[{parent_id, source, parent_index, parent_title, parent_text, char_len}]，
      parent_text 为切片回填源（存一次）；parent_id = sha256(source|text) 内容派生。
    - Markdown 按 H2 锚定成父块（无 H2 回退 H1/(intro)），超长父块按 PARENT_MAX_CHARS 再切（PARENT_OVERLAP 重叠）；
      纯文本按 PLAIN_PARENT_CHARS 固定父块。
    - 子块切分不穿越原子块（公式/表格/问答对）；子块 metadata 含所属节的 Header N / images 溯源。
    """
    md_docs, plain_docs = [], []
    for doc in documents:
        doc_type = doc.metadata.get("doc_type")
        source = str(doc.metadata.get("source", ""))
        if doc_type == "markdown" or (doc_type is None and source.lower().endswith(".md")):
            md_docs.append(doc)
        else:
            plain_docs.append(doc)

    parents: List[dict] = []
    children: List[Document] = []
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)

    for doc in md_docs:
        source = str(doc.metadata.get("source", ""))
        text = _normalize_long_tables(doc.page_content, CHILD_CHUNK_SIZE)
        sections = header_splitter.split_text(text)
        # H2 锚定分组：同 (H2 or H1 or intro) 标题的连续节并入同一父块，保留各节元数据
        groups: List[Tuple[str, List[Tuple[str, dict]]]] = []
        for sec in sections:
            title = sec.metadata.get("Header 2") or sec.metadata.get("Header 1") or "(intro)"
            if groups and groups[-1][0] == title:
                groups[-1][1].append((sec.page_content, dict(sec.metadata)))
            else:
                groups.append((title, [(sec.page_content, dict(sec.metadata))]))
        parent_index = 0
        for title, parts in groups:
            contents = [c for c, _ in parts]
            parent_text = "\n".join(contents)
            # 各节在 parent_text 中的区间（含 \n 分隔符），供子块定位所属节元数据；
            # 图片链接跟随段落，按节提取入 sec_meta（与单层切分口径一致）
            sec_ranges: List[Tuple[int, int, dict]] = []
            off = 0
            for content, meta in parts:
                imgs = _IMAGE_RE.findall(content)
                if imgs:
                    meta["images"] = ",".join(imgs)
                sec_ranges.append((off, off + len(content), meta))
                off += len(content) + 1
            for piece_base, piece in _split_parent_pieces(parent_text, PARENT_MAX_CHARS, PARENT_OVERLAP):
                _emit_parent(doc.metadata, piece, title, source, parent_index, parents, children,
                             sec_ranges=sec_ranges, piece_base=piece_base)
                parent_index += 1

    for doc in plain_docs:
        source = str(doc.metadata.get("source", ""))
        parent_index = 0
        for piece_base, piece in _split_parent_pieces(doc.page_content, PLAIN_PARENT_CHARS, PLAIN_PARENT_OVERLAP):
            _emit_parent(doc.metadata, piece, "", source, parent_index, parents, children)
            parent_index += 1

    logger.info("[ParentChildSplitter] 分层产出 %d 个父块、%d 个子块", len(parents), len(children))
    return children, parents
