"""DeepEval RAG 端到端评测脚本（适配 deepeval 4.x）。

对 golden 集跑真实链路（检索 → Agent 生成），用 DeepEval 的 LLM-as-judge 指标
评测"生成侧质量"，与 evaluation/run_evaluation.py（检索侧 hit@k / 精排分）互补。

用法（需联网；先安装 deepeval）：
  uv add --dev "deepeval>=1.0.0"
  python -m evaluation.evaluate_rag_deepeval                    # 端到端：检索 + Agent 生成 + 评判
  python -m evaluation.evaluate_rag_deepeval --mode retrieval   # 仅检索：用 top-1 片段做代理答案，只跑 AnswerRelevancy
  python -m evaluation.evaluate_rag_deepeval --k 5 --recall-k 20
  python -m evaluation.evaluate_rag_deepeval --limit 3          # 只跑前 3 题（冒烟验证）
  DEEPEVAL_JUDGE_TIMEOUT=120 python -m evaluation.evaluate_rag_deepeval  # 调大 judge 超时

Judge 模型（DeepEval 的 LLM 评判器，OpenAI 兼容端点）：
  deepeval 4.x 不再有全局 judge，改为每个 metric 传 model=。本项目用 deepeval 自带的
  OpenAIModel(model=..., api_key=..., base_url=...) 指向 OpenAI 兼容端点，默认复用
  DashScope 配置，也可用 JUDGE_* 显式覆盖：
    JUDGE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    JUDGE_API_KEY=<DASHSCOPE_API_KEY>
    JUDGE_MODEL=qwen-plus
  （或设 JUDGE_BASE_URL=${SILICONFLOW_BASE_URL} / JUDGE_API_KEY=${SILICONFLOW_API_KEY}
    / JUDGE_MODEL=Qwen/Qwen2.5-72B-Instruct 等）

指标：
  - 端到端：Faithfulness（回答忠于检索片段）+ AnswerRelevancy（回答相关）
    + ContextualPrecision/ContextualRecall（golden 每题提供 expected_answer 文本时才生效）
  - 仅检索：AnswerRelevancy（top-1 片段相对问题的相关性，作为检索质量代理）

注意：deepeval 4.x 的 evaluate() 内部会自己创建事件循环，因此必须在普通同步上下文
（asyncio.run 之外）调用；本脚本把"异步收集检索/生成"与"同步 evaluate()"分开。
"""
import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

# 读取项目根目录 .env（DEEPSEEK_API_KEY / JUDGE_* 等），不依赖运行 cwd。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，deepeval/rich 输出含 Unicode（框线等）会 UnicodeEncodeError。
# 统一把 stdout/stderr 切成 UTF-8（现代终端 / VS Code / PowerShell 7 可正常显示）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# judge 请求超时控制：deepeval 4.x 的真实配置是 *_OVERRIDE 字段 + 总开关 DEEPEVAL_DISABLE_TIMEOUTS。
#   - DEEPEVAL_DISABLE_TIMEOUTS=true：关掉 deepeval 自带的 per-attempt/per-task/gather 超时；
#   - DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE：同时控制 OpenAI 客户端超时（默认 120s，
#     网络很慢可调大，如 300）；之前的 DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS 是计算属性、设了无效；
#   - DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE：外层单用例预算留大兜底。
# 必须在 deepeval 初始化前设置（本脚本在模块顶部执行）。
os.environ.setdefault("DEEPEVAL_DISABLE_TIMEOUTS", "true")


def _positive_seconds(name: str, default: float) -> str:
    """把环境变量读成合法正秒数（pydantic 要求 override 必须 > 0）。"""
    try:
        val = float(os.getenv(name, default))
    except (TypeError, ValueError):
        val = default
    return str(val if val > 0 else default)


os.environ.setdefault(
    "DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE",
    _positive_seconds("DEEPEVAL_JUDGE_TIMEOUT", 120),
)
os.environ.setdefault(
    "DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE",
    _positive_seconds("DEEPEVAL_JUDGE_TASK_TIMEOUT", 600),
)

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(EVAL_DIR, "golden_qa.json")
REPORT = os.path.join(EVAL_DIR, "deepeval_report.json")

DEFAULT_THRESHOLD = float(os.getenv("DEEPEVAL_THRESHOLD", "0.7"))


# ── Judge LLM：用 OpenAIModel 指向 OpenAI 兼容端点（deepeval 4.x 每 metric 传 model=）──
def _build_judge():
    """构造 DeepEval 评判模型（复用 DashScope / SiliconFlow / DeepSeek 等兼容端点）。"""
    from deepeval.models import OpenAIModel

    # 优先级：显式 JUDGE_* → DeepSeek（默认 judge，复用 DEEPSEEK_* 配置）→ DashScope 千问兜底
    base_url = os.getenv("JUDGE_BASE_URL")
    api_key = os.getenv("JUDGE_API_KEY")
    model = os.getenv("JUDGE_MODEL")
    if not (base_url and api_key):
        base_url = base_url or "https://api.deepseek.com"
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        model = model or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    if not (base_url and api_key):
        base_url = os.getenv("DASHSCOPE_BASE_URL")
        api_key = os.getenv("DASHSCOPE_API_KEY")
        model = os.getenv("QWEN_MODEL") or "qwen-plus"
    if not (base_url and api_key):
        raise SystemExit(
            "缺少 Judge LLM 配置：请设置 JUDGE_BASE_URL/JUDGE_API_KEY/JUDGE_MODEL，"
            "或配置 DEEPSEEK_API_KEY（默认 judge），或复用 DASHSCOPE_BASE_URL/DASHSCOPE_API_KEY（见脚本头部说明）。"
        )
    return OpenAIModel(model=model, api_key=api_key, base_url=base_url)


# ── 真实链路：检索 / 生成（仅在异步收集阶段运行）───────────────────────────
async def _retrieve(question: str, k: int) -> list:
    from rag个人知识库.service.service import search_documents
    return await search_documents(question, k=k)


async def _generate_answer(question: str, k: int) -> str:
    from rag个人知识库.service.chat import chat
    result = await chat(
        question,
        k=k,
        thread_id="deepeval-eval",
        user_id=None,  # 不过滤可见性：评测用全量知识库
        load_history=False,
    )
    if result.get("error"):
        raise RuntimeError(f"Agent 生成失败：{result['error']}")
    return result["answer"]


async def _collect_test_cases(questions, k, recall_k, mode, expected_map=None) -> tuple:
    """异步收集：检索 + （可选）生成 → LLMTestCase 列表 + 检索元信息。

    expected_map: {id: {"expected_answer": str}} —— 来自旁挂 expected_answers.json，
    用于给 LLMTestCase 提供 expected_output（启用 ContextualPrecision/Recall）。
    """
    from deepeval.test_case import LLMTestCase

    expected_map = expected_map or {}
    test_cases, meta_rows = [], []
    for q in questions:
        question = q["question"]
        hits = await _retrieve(question, k=recall_k)
        contexts = [h["content"] for h in hits]
        expected_answer = (expected_map.get(q["id"]) or {}).get("expected_answer")

        if mode == "end-to-end":
            answer = await _generate_answer(question, k=k)
        else:  # retrieval：用 top-1 片段作代理答案
            answer = contexts[0] if contexts else ""

        test_cases.append(LLMTestCase(
            input=question,
            actual_output=answer,
            expected_output=expected_answer,
            retrieval_context=contexts,
        ))
        meta_rows.append({
            "id": q["id"],
            "question": question,
            "expected": q.get("expected"),
            "hit_rank": _hit(q.get("expected", ""), hits),
            "recall_count": len(hits),
            "expected_answer": expected_answer,
        })
        # 用 ASCII 标记（✓/✗ 在 Windows GBK 控制台会 UnicodeEncodeError）
        mark = "OK" if meta_rows[-1]["hit_rank"] else "--"
        print(f"  [{mark}] {q['id']:10s} hit@{meta_rows[-1]['hit_rank'] or '-'}  recall={len(hits)}")
    return test_cases, meta_rows


# ── 指标与报告 ─────────────────────────────────────────────────────────────
def _build_metrics(judge, has_expected_answer: bool, threshold: float) -> list:
    """构造 DeepEval 指标：deepeval 4.x 通过 model= 传入 judge。"""
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )
    metrics = [
        FaithfulnessMetric(threshold=threshold, model=judge),
        AnswerRelevancyMetric(threshold=threshold, model=judge),
    ]
    if has_expected_answer:
        metrics += [
            ContextualPrecisionMetric(threshold=threshold, model=judge),
            ContextualRecallMetric(threshold=threshold, model=judge),
        ]
    return metrics


def _extract_results(result) -> list:
    """从 deepeval 4.x 的 EvaluationResult 抽取每题指标分数。"""
    rows = []
    try:
        for tr in result.test_results:
            row = {"input": getattr(tr, "input", None)}
            for md in (tr.metrics_data or []):
                row[md.name] = {
                    "score": getattr(md, "score", None),
                    "success": getattr(md, "success", None),
                    "passed": getattr(md, "passed", None),  # 兼容旧版字段名
                    "threshold": getattr(md, "threshold", None),
                    "reason": getattr(md, "reason", None),
                }
            rows.append(row)
        return rows
    except Exception as e:
        print(f"[warn] 结构化结果提取失败（{e}），仅打印控制台结果。")
        return rows


def _align_results(meta_rows: list, rows: list) -> list:
    """把检索元信息按 input 问题文本合并回 deepeval 结果行（乱序安全）。

    deepeval 异步评测的 test_results 不保证与传入 test_cases 同序，按位置 zip
    会把 id/question/expected_answer 错挂到别的行。这里用 input == question 精确
    匹配；无对应结果行的 meta 原样保留、多余结果行追加在尾部，保证不错位也不丢数据。
    """
    by_input: dict = {}
    for row in rows:
        inp = row.get("input")
        if inp is not None:
            by_input.setdefault(inp, row)

    aligned: list = []
    matched: set = set()
    for meta in meta_rows:
        row = by_input.get(meta.get("question"))
        if row is not None and id(row) not in matched:
            row.update(meta)
            matched.add(id(row))
            aligned.append(row)
        else:
            # 找不到对应结果行（异常情况）：保留 meta，避免静默丢题
            aligned.append(dict(meta))
    for row in rows:
        if id(row) not in matched:
            aligned.append(row)
    return aligned


def _hit(expected: str, hits: list) -> int:
    for rank, h in enumerate(hits, start=1):
        if expected in str(h.get("source", "")):
            return rank
    return 0


def _run(args) -> int:
    try:
        import deepeval  # noqa: F401
    except ImportError:
        print(
            "未安装 deepeval。请先联网执行：uv add --dev \"deepeval>=1.0.0\"\n"
            "（当前环境无外网，无法自动安装。）"
        )
        return 2

    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)
    questions = golden["questions"]
    if args.limit > 0:
        questions = questions[:args.limit]
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        before = len(questions)
        questions = [q for q in questions if q["id"] in only]
        print(f"[info] 只跑 {len(questions)} 题：{sorted(only)}（from {before}）")
    elif args.exclude:
        excluded = {x.strip() for x in args.exclude.split(",") if x.strip()}
        before = len(questions)
        questions = [q for q in questions if q["id"] not in excluded]
        print(f"[info] 排除 {len(excluded)} 题：{sorted(excluded)}（{before} -> {len(questions)}）")
    k = args.k
    recall_k = args.recall_k
    mode = args.mode

    # 旁挂 draft 答案：存在则作为 expected_output（启用 ContextualPrecision/Recall）
    expected_map = {}
    if not args.no_expected:
        expected_path = os.path.join(EVAL_DIR, "expected_answers.json")
        if os.path.exists(expected_path):
            with open(expected_path, encoding="utf-8") as f:
                expected_map = json.load(f)
            print(f"[info] 已加载 {len(expected_map)} 条 expected_answer（{expected_path}）")
        else:
            print("[info] 未找到 expected_answers.json，仅跑 Faithfulness + AnswerRelevancy")

    judge = _build_judge()
    print(f"Judge 模型：{judge.get_model_name()}")
    print(f"评测模式：{mode}（k={k}, recall_k={recall_k}）")
    print("=" * 72)

    # 1) 异步收集：检索 + 生成（evaluate() 内部自带事件循环，必须在 asyncio.run 之外调用）
    test_cases, meta_rows = asyncio.run(
        _collect_test_cases(questions, k, recall_k, mode, expected_map)
    )

    # 2) 同步运行 DeepEval 指标（有 expected_answer 才启用 ContextualPrecision/Recall）
    metrics = _build_metrics(judge, bool(expected_map), args.threshold)
    print("-" * 72)
    print("运行 DeepEval 指标：" + ", ".join(m.__class__.__name__ for m in metrics))
    print("-" * 72)

    from deepeval import evaluate
    from deepeval.evaluate.configs import AsyncConfig, CacheConfig

    # 保守并发：默认 max_concurrent 高并发打分容易触发 judge 限流（RateLimitError）。
    # 这里降到 2 并加 1s 节流；配额充足时可调高（--concurrency）。
    # 禁用磁盘缓存：Windows 下 deepeval 的 test-run 缓存共享锁缺 pywin32 会崩
    # （AttributeError: 'NoneType' ... test_cases_lookup_map），我们自写 JSON 报告即可。
    result = evaluate(
        test_cases=test_cases,
        metrics=metrics,
        async_config=AsyncConfig(
            run_async=True,
            throttle_value=args.throttle,
            max_concurrent=args.concurrency,
        ),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
    )

    rows = _extract_results(result)
    # 把检索元信息合并进每行
    if rows:
        rows = _align_results(meta_rows, rows)

    report = {
        "tool": "deepeval",
        "deepeval_version": deepeval.__version__ if hasattr(deepeval, "__version__") else None,
        "judge_model": judge.get_model_name(),
        "mode": mode,
        "k": k,
        "recall_k": recall_k,
        "threshold": args.threshold,
        "total": len(questions),
        "hit@1": round(sum(1 for m in meta_rows if m["hit_rank"] == 1) / len(meta_rows), 3),
        "hit@k": round(sum(1 for m in meta_rows if 0 < m["hit_rank"] <= k) / len(meta_rows), 3),
        "metric_summary": {},
        "details": rows or meta_rows,
    }

    # 指标汇总（各指标均值）
    if rows:
        keys = ["Faithfulness", "Answer Relevancy", "Contextual Precision", "Contextual Recall"]
        for key in keys:
            scores = [
                r[key]["score"] for r in rows
                if isinstance(r.get(key), dict) and r[key]["score"] is not None
            ]
            if scores:
                report["metric_summary"][key] = round(sum(scores) / len(scores), 4)

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("=" * 72)
    print(f"报告已写入 {REPORT}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepEval RAG 评测")
    parser.add_argument("--mode", choices=["end-to-end", "retrieval"], default="end-to-end",
                        help="end-to-end=检索+Agent生成+评判；retrieval=仅检索（top-1 代理）")
    parser.add_argument("--k", type=int, default=5, help="最终取 Top k（端到端生成用）")
    parser.add_argument("--recall-k", type=int, default=20, help="召回候选数（进检索上下文；与 DEFAULT_RECALL_K 对齐）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="指标通过阈值")
    parser.add_argument("--concurrency", type=int, default=2, help="judge 并发数（默认 2，避免触发限流）")
    parser.add_argument("--throttle", type=float, default=1.0, help="judge 请求间隔秒（默认 1.0）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部；冒烟验证用）")
    parser.add_argument("--exclude", default="",
                        help="按题 id 排除（逗号分隔），如 --exclude langchain-1")
    parser.add_argument("--only", default="",
                        help="只跑指定题 id（逗号分隔），如 --only jwt-3")
    parser.add_argument("--no-expected", action="store_true",
                        help="不加载 expected_answers.json（仅跑 Faithfulness + AnswerRelevancy）")
    args = parser.parse_args()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
