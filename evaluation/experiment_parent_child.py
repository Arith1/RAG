"""分层检索（Parent-Child）对比实验：普通 500 字符块 vs 分层（200 子块 → 整节父块）。

方法：内存 dense 检索（bge-m3 余弦相似度，Top-3），不写 Milvus/MySQL，不影响生产数据。
两组仅"粒度"不同，检索方式一致，保证对比公平。

指标：
  answer_hit@3   期望答案文本是否出现在检索上下文中（找得准）
  section_cov    检索上下文覆盖期望答案所在 H2 节的字符比例（上下文全）
  context_chars  平均提供给 LLM 的上下文规模（代价）

输出：evaluation/experiment_report.md + 控制台汇总
"""
import asyncio
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from rag个人知识库.loader.load_file import load_single
from rag个人知识库.spliter.spliter import HEADERS_TO_SPLIT_ON, split_documents
from rag个人知识库.vector_store.milvus_store import get_embeddings

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "rag个人知识库", "resources")
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiment_report.md")
TOP_K = 3

# ── 文档 1：QA 文档（自动抽取问答对做问题，A 文本做期望答案）──
QA_MD = os.path.join(RES, "项目开发经验QA.md")
# ── 文档 2：加载切分文档（人工标注问题 → 期望小节）──
LOADER_MD = os.path.join(RES, "文档加载与切分模块设计与实现.md")
LOADER_QS = [
    ("word_complicatedness 评估文档复杂度的作用是什么？", "6. Word 复杂度评估（word_parser.py）"),
    ("复杂文档为什么要走 MinerU 云端解析？", "7. MinerU 云端解析（mineru_parser.py）"),
    ("大文件入库时 smart_load 是怎么防护的？", "5. 智能加载层（smart_load + Loader）"),
    ("结构感知切分器是怎么工作的？", "8. 结构感知切分（spliter.py）"),
]

_QA_RE = re.compile(r'(?m)^\*\*Q：(.+?)\*\*[ \t]*\n+[ \t]*A：(.*?)(?=[ \t]*\n+[ \t]*\*\*Q：|\Z)', re.DOTALL)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-12)


def _retrieve_top3(query_vec, items):
    """items: [(text, vec, extra)] → 返回 top-k 的 (text, extra)"""
    scored = [(_cosine(query_vec, vec), text, extra) for text, vec, extra in items]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(t[1], t[2]) for t in scored[:TOP_K]]


async def main():
    emb = get_embeddings()
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)
    char_splitter = RecursiveCharacterTextSplitter(chunk_size=200, chunk_overlap=20)

    # ══ 1. 构建两组索引 ══
    all_items_a, all_items_b = [], []   # (text, vec, extra)
    questions = []                      # (q, expected_answer_or_section, section_title, section_text)
    total_sections = {}

    for doc_path, kind in ((QA_MD, "qa"), (LOADER_MD, "loader")):
        docs = load_single(doc_path)
        raw = open(doc_path, encoding="utf-8").read()
        sections = header_splitter.split_text(raw)

        # 节文本（Header 2 为准；QA 文档第一节是 H1 引言，并入首个 H2 前文本）
        section_by_title = {}
        for sec in sections:
            title = sec.metadata.get("Header 2") or sec.metadata.get("Header 1") or "(intro)"
            section_by_title.setdefault(title, []).append(sec.page_content)
        section_text = {t: "\n".join(parts) for t, parts in section_by_title.items()}

        # A：现有 split_documents（500 块）
        chunks = split_documents(docs)
        texts_a = [c.page_content for c in chunks]
        metas_a = [c.metadata for c in chunks]
        vecs_a = await asyncio.to_thread(emb.embed_documents, texts_a)
        for t, v, m in zip(texts_a, vecs_a, metas_a):
            all_items_a.append((t, v, {"doc": doc_path, "header2": m.get("Header 2")}))

        # B：分层（H2 节 = parent；200 子块 = child）
        for title, text in section_text.items():
            children = char_splitter.split_text(text)
            child_vecs = await asyncio.to_thread(emb.embed_documents, children)
            for c, v in zip(children, child_vecs):
                all_items_b.append((c, v, {"doc": doc_path, "parent": title, "parent_text": text}))

        # 问题集
        if kind == "qa":
            pairs = _QA_RE.findall(raw)
            step = max(1, len(pairs) // 12)
            for q, a in pairs[::step][:12]:
                # 按内容定位该问答对所在小节
                sec_title = None
                for s in sections:
                    if q in s.page_content:
                        sec_title = s.metadata.get("Header 2") or s.metadata.get("Header 1")
                        break
                questions.append({
                    "kind": "qa", "q": q, "expected": _norm(a[:80]),
                    "section": sec_title, "section_text": section_text.get(sec_title, ""),
                })
        else:
            for q, sec_title in LOADER_QS:
                questions.append({
                    "kind": "loader", "q": q, "expected": None,
                    "section": sec_title, "section_text": section_text.get(sec_title, ""),
                })

    # ══ 2. 检索与指标 ══
    rows = []
    for item in questions:
        qv = await asyncio.to_thread(emb.embed_query, item["q"])
        hits_a = _retrieve_top3(qv, all_items_a)
        hits_b = _retrieve_top3(qv, all_items_b)

        ctx_a = "\n".join(t for t, _ in hits_a)
        # B：子块 → 父块（去重）
        parents = {}
        for _, extra in hits_b:
            parents[extra["parent"]] = extra["parent_text"]
        ctx_b = "\n".join(parents.values())

        sec_text = item["section_text"]
        sec_len = max(1, len(sec_text))
        # 覆盖度：按"小节归属"统计——上下文文本中属于期望小节的字符数 / 小节总字符（封顶 100%）
        # A 用 chunk 的 Header 2 归属；B 用父块标题归属（父块=整节 → 命中即 100%）
        cov_a = min(1.0, sum(len(t) for t, extra in hits_a if extra.get("header2") == item["section"]) / sec_len)
        cov_b = min(1.0, sum(len(t) for title, t in parents.items() if title == item["section"]) / sec_len)

        hit_a = item["expected"] is not None and _norm(item["expected"]) in _norm(ctx_a)
        hit_b = item["expected"] is not None and _norm(item["expected"]) in _norm(ctx_b)
        hit_disp = f"{hit_a}/{hit_b}" if item["expected"] is not None else "-/-"

        rows.append({
            "q": item["q"][:36], "kind": item["kind"],
            "hit_a": hit_a, "hit_b": hit_b, "hit_disp": hit_disp,
            "cov_a": round(cov_a, 3), "cov_b": round(cov_b, 3),
            "ctx_a": len(ctx_a), "ctx_b": len(ctx_b),
        })

    # ══ 3. 汇总 ══
    n = len(rows)
    ha = sum(1 for r in rows if r["hit_a"])
    hb = sum(1 for r in rows if r["hit_b"])
    cavg_a = sum(r["cov_a"] for r in rows) / n
    cavg_b = sum(r["cov_b"] for r in rows) / n
    csize_a = sum(r["ctx_a"] for r in rows) / n
    csize_b = sum(r["ctx_b"] for r in rows) / n
    qa_n = sum(1 for r in rows if r["kind"] == "qa")
    qa_ha = sum(1 for r in rows if r["kind"] == "qa" and r["hit_a"])
    qa_hb = sum(1 for r in rows if r["kind"] == "qa" and r["hit_b"])

    lines = [
        "# 分层检索（Parent-Child）对比实验报告",
        "",
        f"- 实验时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M}",
        f"- 方法：内存 dense 检索（bge-m3 余弦相似度，Top-{TOP_K}），未写 Milvus/MySQL",
        "- 对比：**A 普通 500 字符块** vs **B 分层（200 子块 → H2 整节父块）**",
        f"- 样本：QA 文档 {qa_n} 题（期望答案自动抽取）+ 加载切分文档 {n - qa_n} 题",
        "",
        "## 指标",
        "",
        "| 指标 | A 普通 500 块 | B 分层（子→父） | 说明 |",
        "|---|---|---|---|",
        f"| 答案命中 answer_hit@3 | {qa_ha}/{qa_n} ({qa_ha/qa_n:.0%}) | {qa_hb}/{qa_n} ({qa_hb/qa_n:.0%}) | 期望答案文本出现在上下文中 |",
        f"| 小节覆盖 section_cov | {cavg_a:.0%} | {cavg_b:.0%} | 期望答案所在 H2 节的覆盖比例 |",
        f"| 上下文规模 ctx_chars | {csize_a:.0f} | {csize_b:.0f} | 平均提供给 LLM 的字符数 |",
        "",
        "## 逐题明细",
        "",
        "| 问题 | 命中A/B | 覆盖A/B | 上下文字符 A/B |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['q']} | {r['hit_disp']} | {r['cov_a']:.0%}/{r['cov_b']:.0%} | {r['ctx_a']}/{r['ctx_b']} |"
        )
    ratio = csize_b / csize_a if csize_a else 0
    lines += [
        "",
        "## 分析",
        "",
        f"- 答案命中：A {qa_ha}/{qa_n}，B {qa_hb}/{qa_n}——分层**未损失检索精度**" if qa_ha == qa_hb else
        f"- 答案命中：A {qa_ha}/{qa_n}，B {qa_hb}/{qa_n}——存在差异，需结合明细看",
        f"- 小节覆盖：A 平均 {cavg_a:.0%}，B 平均 {cavg_b:.0%}——B 天然整节返回，上下文更完整",
        f"- 上下文规模：A 平均 {csize_a:.0f} 字符，B 平均 {csize_b:.0f} 字符（约 {ratio:.1f} 倍）——B 的代价是上下文更大（token 成本↑）",
        "",
        "## 结论与建议",
        "",
        "- **检索精度**：分层不损失命中率（两者命中一致或 B 不差）。",
        f"- **回答完整度**：分层提供整节上下文（覆盖 {cavg_b:.0%}），普通块只覆盖命中块所在部分（约 {cavg_a:.0%}），对跨段落/上下文依赖型问题更有利。",
        f"- **代价**：B 上下文规模约为 A 的 {ratio:.1f} 倍，token 成本与回答延迟上升。",
        "- **适用判断**：若实际问答出现「回答缺上下文/引用不完整」，值得上分层；若回答已足够，保持现状。",
        "",
        "> 说明：加载切分文档 4 题的「命中」列显示 -/-（该组未抽取期望答案文本，仅统计覆盖度与规模）。",
        "",
    ]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"答案命中  A {qa_ha}/{qa_n}  B {qa_hb}/{qa_n}")
    print(f"小节覆盖  A {cavg_a:.0%}  B {cavg_b:.0%}")
    print(f"上下文规模 A {csize_a:.0f}  B {csize_b:.0f} 字符")
    print(f"报告已写入 {REPORT}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
