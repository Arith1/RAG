"""切分器单元测试：QA 原子保护 / 还原长度分组 / 占位符机制（纯函数，无网络）。"""
from langchain_core.documents import Document

from rag个人知识库.splitter.spliter import (
    _QA_PAIR_RE, _effective_len, _protect_atomic_blocks, _restore_atomic_blocks,
    _split_by_restored_length, split_documents,
)

QA_TEXT = (
    "**Q：问题一？**\n\nA：答案一。\n\n"
    "**Q：问题二？**\n\nA：答案二。\n\n"
    "**Q：问题三？**\n\nA：答案三。"
)


class TestQAPairRegex:
    def test_match_pairs_separately(self):
        ms = list(_QA_PAIR_RE.finditer(QA_TEXT))
        assert len(ms) == 3
        assert "答案一" in ms[0].group(0) and "问题二" not in ms[0].group(0)

    def test_ignore_inline_quotes(self):
        # 正文里内联引用的 "**Q：...**" 不应被识别为问答对开头
        text = "文档说明：把 `**Q：...**` 与 `A：...` 作为示例。\n\n**Q：真问题？**\n\nA：真答案。"
        assert len(list(_QA_PAIR_RE.finditer(text))) == 1

    def test_tolerate_hardbreak_normalization(self):
        # MarkdownHeaderTextSplitter 会把 \n\n 规范化为 "  \n"（两个空格+换行）
        normalized = QA_TEXT.replace("\n\n", "  \n")
        assert len(list(_QA_PAIR_RE.finditer(normalized))) == 3


class TestAtomicBlocks:
    def test_protect_keeps_separators_between_blocks(self):
        protected, blocks = _protect_atomic_blocks(QA_TEXT)
        assert len(blocks) == 3
        # 占位符之间必须保留分隔符，否则无法按段落切分
        assert "\n" in protected
        restored = _restore_atomic_blocks(protected, blocks)
        assert "答案一" in restored and "问题三" in restored

    def test_effective_len_counts_block_length(self):
        blocks = {"<ATOMIC_0>": "x" * 300}
        assert _effective_len("<ATOMIC_0>", blocks) == 300
        assert _effective_len("普通文本", blocks) == 4

    def test_group_by_restored_length_bounds_chunks(self):
        protected, blocks = _protect_atomic_blocks(QA_TEXT)
        groups = _split_by_restored_length(protected, blocks, max_chunk_size=60)
        # 还原后每组长度的近似值应受控（不出现 3000+ 的超大 chunk）
        for g in groups:
            assert _effective_len(g, blocks) <= 60 + 10

    def test_split_documents_qa_pairs_intact(self):
        chunks = split_documents([Document(page_content=QA_TEXT, metadata={"source": "qa.md"})])
        all_text = "\n".join(c.page_content for c in chunks)
        assert "问题一" in all_text and "答案三" in all_text
        # 没有任何占位符残留
        assert not any("<ATOMIC_" in c.page_content for c in chunks)
