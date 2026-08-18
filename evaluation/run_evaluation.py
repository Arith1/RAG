"""RAG 检索评测脚本：对 golden 集跑检索，计算 hit@k 与精排分统计。

用法：
  python -m evaluation.run_evaluation            # 运行并打印报告
  python -m evaluation.run_evaluation --json     # 额外输出 report.json

指标：
  hit@1 / hit@3 / hit@5 ：期望源文档出现在 Top-k 的比例
  mean_top1_score       ：命中问题的 Top-1 精排分均值（越高越好）
  per_doc               ：按文档分组的命中率，定位薄弱文档
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag个人知识库.service.service import search_documents

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(EVAL_DIR, "golden_qa.json")
REPORT = os.path.join(EVAL_DIR, "report.json")


def _hit(expected: str, hits: list) -> int:
    """返回期望文档命中在第几位（1 起），未命中返回 0。"""
    for rank, h in enumerate(hits, start=1):
        if expected in str(h.get("source", "")):
            return rank
    return 0


async def main() -> int:
    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)
    questions = golden["questions"]
    k = golden["meta"]["k"]

    print("=" * 70)
    print(f"RAG 检索评测：{len(questions)} 题，k={k}")
    print("=" * 70)

    results, per_doc = [], {}
    for q in questions:
        hits = await search_documents(q["question"], k=k)
        rank = _hit(q["expected"], hits)
        top1_score = hits[0].get("score") if hits else None
        results.append({
            "id": q["id"], "question": q["question"], "expected": q["expected"],
            "hit_rank": rank, "top1_score": top1_score,
            "actual_sources": sorted({str(h.get("source", "")).split(os.sep)[-1] for h in hits}),
        })
        per_doc.setdefault(q["expected"], []).append(rank)
        mark = "✓" if rank else "✗"
        score_str = f"{top1_score:.3f}" if top1_score is not None else "  -  "
        print(f"  [{mark}] {q['id']:10s} hit@{rank if rank else '-':<2} top1={score_str} <- {q['expected']}")

    n = len(results)
    hits1 = sum(1 for r in results if r["hit_rank"] == 1)
    hits3 = sum(1 for r in results if 0 < r["hit_rank"] <= 3)
    hits5 = sum(1 for r in results if r["hit_rank"] > 0)
    scored = [r["top1_score"] for r in results if r["top1_score"] is not None]
    mean_score = sum(scored) / len(scored) if scored else 0.0

    report = {
        "total": n, "k": k,
        "hit@1": round(hits1 / n, 3), "hit@3": round(hits3 / n, 3), "hit@5": round(hits5 / n, 3),
        "mean_top1_score": round(mean_score, 4),
        "per_doc": {
            doc: {"hit": sum(1 for rk in ranks if rk > 0), "total": len(ranks),
                  "rate": round(sum(1 for rk in ranks if rk > 0) / len(ranks), 3)}
            for doc, ranks in sorted(per_doc.items())
        },
        "details": results,
    }

    print("=" * 70)
    print(f"hit@1 = {report['hit@1']:.1%}   hit@3 = {report['hit@3']:.1%}   "
          f"hit@5 = {report['hit@5']:.1%}   mean_top1_score = {mean_score:.4f}")
    print("-" * 70)
    print("分文档命中率：")
    for doc, st in report["per_doc"].items():
        print(f"  {st['hit']:>2}/{st['total']}  {st['rate']:.0%}  {doc}")

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
