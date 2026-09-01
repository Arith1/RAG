# RAG 系统 DeepEval 评测报告（端到端）

> 评测时间：2026-09（实时对已入库知识库 检索 → Agent 生成 → LLM 评判）
> 评测工具：DeepEval 4.2.0 / Judge = `deepseek-v4-flash`（OpenAI 兼容端点）
> 数据源：`golden_qa.json`（24 题，排除未入库的 `langchain-1`）
> 结果文件：`deepeval_report.json`（机器可读）、本文件（人类可读）

## 一、评测设置

- **题量**：24 题（JWT 4 / 队列 5 / 意图 4 / 会话 4 / 加载切分 5 / 项目 QA 2；`langchain-1` 源文档未入库故排除）
- **模式**：`end-to-end`——双路召回 → RRF 融合 → reranker 精排 → **Agent 基于检索片段生成回答** → LLM 评判
- **检索参数**：`k = 5`、`recall_k = 20`（检索上下文 = Top-20 片段）
- **指标**：`Faithfulness` + `AnswerRelevancy` + `ContextualPrecision` + `ContextualRecall`（阈值 0.7）
- **expected_answer**：旁挂 `evaluation/expected_answers.json`（draft，可复核修正）

## 二、核心指标

| 指标 | 结果 |
| --- | --- |
| **hit@1** | **79.2%**（19/24） |
| **hit@k (k=5)** | **100%**（24/24） |
| **Faithfulness** | **0.9902** |
| **AnswerRelevancy** | **0.9296** |
| **ContextualPrecision** | **0.9724** |
| **ContextualRecall** | **0.9306** |

> **结论**：端到端生成质量良好（AR 0.93 / Faith 0.99）。此前 retrieval 模式 AR=0.65 是「Top-1 片段必须直接回答」的严格代理，不代表真实生成质量。

## 三、未通过题（任一指标 < 0.7）与原因

| ID | 指标 | 分数 | 原因 |
| --- | --- | --- | --- |
| session-2 | AnswerRelevancy | 0.556 | 生成跑题：回答大篇幅讲 checkpointer / Summarization / TTL，未聚焦「thread_id 组成」 |
| queue-4 | ContextualRecall | 0.500 | 答案跨「入队 SADD / 删除 409 / worker SREM」三处，Top-5 未全覆盖 |
| session-3 | ContextualRecall | 0.500 | TTL 规则细节（1 天 / updated_at / 三表删除 / 每小时清理）未全在 Top-5 |
| loader-2 | ContextualRecall | 0.667 | MinerU 云流程 + 保真度细节分散多节，Top-5 覆盖不全 |
| ~~jwt-3~~ | ~~ContextualRecall~~ | ~~0.667~~ | **已修复**：原为 draft expected_answer 含源文档没有的句子（假低分），修正后 CR = **1.0**（见第五节） |

## 四、关键发现

1. **检索能命中、覆盖不一定全**：hit@k=100%，但 queue-4 / session-3 / loader-2 的 CR<0.7——答案信息都在文档里，只是**分散在不同小节、Top-5 没抓全**，属真实覆盖缺口（而非答案缺失）。
2. **生成偶发跑题**：session-2 是唯一 AR 短板，检索没问题（CP 0.83 / CR 1.0），问题在生成环节答偏。
3. **draft expected_answer 会引入假低分**：jwt-3 的 CR 低分是 draft 里有一句源文档没有的推断导致的，**不是检索问题**。

## 五、jwt-3 修复验证（2026-09）

- 问题：draft expected_answer 含「攻击者无法把 alg 换成 none 或其它算法来绕过验签」，源文档无此表述 → CR 逐句比对判 0.667。
- 修复：将 draft 收紧为「确认 alg 在白名单 → 否则 raise InvalidAlgorithmError」，严格对应源文档「④ 验算法」节。
- 复测（judge=qwen3.7-plus，retrieval 模式单跑 jwt-3）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| ContextualRecall | 0.667 | **1.0** |
| ContextualPrecision | — | 1.0 |

- 结论：**CR 低分 ≠ 检索差**，先复核 `expected_answers.json` 的 draft，再判断是检索缺口还是口径问题。修复后 jwt-3 已剔除出「未通过」列表。

## 六、提高通过率的方法

1. **先修 `expected_answer` 口径**（成本最低）：CR 逐句比对，draft 含源文档没有的句子会假低分（如 jwt-3）。收紧到可验证表述后重跑，即可区分「真缺口 vs 假低分」。
2. **提升检索覆盖（治 CR 低分）**：queue-4 / session-3 / loader-2 答案跨小节、Top-5 抓不全 → 各节开头补「结论一句话」、或小 chunk + Parent-Child 分层检索、或按需放大 `recall_k`。
3. **生成聚焦（治 AR 低分）**：session-2 跑题 → 收紧生成 prompt，让回答直接命中问题、避免堆砌无关模块细节。
4. **评测侧**：judge 多轮取均值（消 LLM 方差）、阈值用 0.65~0.7、报告保存 `actual_output` 便于定位生成问题。

## 七、复现方式

```bash
# 需先启动 MySQL / Milvus / Redis，且知识库已入库 golden 文档（联网可访问 embedding/Agent/judge）
# 完整端到端（24 题，排除未入库文档）
python -m evaluation.evaluate_rag_deepeval --mode end-to-end --exclude langchain-1

# 单题复测（如 jwt-3）
python -m evaluation.evaluate_rag_deepeval --mode retrieval --only jwt-3
```

## 八、局限

- `retrieval` 模式下 Faithfulness 无判别力（代理恒 1.0），仅 AnswerRelevancy 有效；本报告采用端到端模式，Faithfulness 为真实防幻觉信号。
- LLM judge 单次打分有方差，数字宜看趋势/多轮均值。
- `expected_answers.json` 为自动抽取 draft，CR/CP 依赖其准确性，需复核。
- 未统计 token 成本（DeepEval 未回传 cost）。
