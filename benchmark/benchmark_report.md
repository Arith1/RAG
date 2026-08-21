# RAG 知识库 · API 性能压测报告（QPS / 延迟 / Redis 缓存命中）

> 测试时间：2026-08-21
> 目标接口：`POST /api/search`（重点：命中 Redis 缓存）与 `POST /api/chat`（参考）
> 测试脚本：`benchmark/bench_search.py`；原始数据：`benchmark/bench_result.json`

---

## 一、测试环境

| 项 | 值 |
| --- | --- |
| 被测服务 | 本项目 FastAPI（uvicorn，`rag个人知识库.api.main:app`） |
| 地址 | `http://127.0.0.1:8010` |
| 并发工具 | asyncio + aiohttp（`benchmark/bench_search.py`） |
| 鉴权 | JWT（admin 登录） |
| 后端 | MySQL / Milvus / Redis / Postgres / SiliconFlow 均已连通 |
| 缓存配置 | `search:` 前缀，`SEARCH_CACHE_TTL = 600s`（10 分钟） |

## 二、测试方法

1. **登录** → 获取 JWT（`POST /api/auth/login`，form 编码）。
2. **预热**：对 10 个真实查询（取自 golden 集）各发一次 `/api/search`，写入 Redis `search:*` 缓存。
3. **热缓存压测**：用预热过的查询 **并发**打 `/api/search` —— 命中 Redis 缓存即返回，反映**缓存命中**的吞吐与延迟。跑了两档：
   - 档 A：并发 20，总请求 300
   - 档 B：并发 10，总请求 200
4. **冷缓存单测**：用一个**全新查询**（从未查过）发一次请求 —— 未命中缓存，完整走 `embedding → Milvus 双路召回 → RRF → rerank 精排`，得到真实冷延迟。
5. **chat 参考**：`/api/chat` 非流式发一次（会调用 DeepSeek LLM），仅作参考。

> **冷测注意**：第二档运行时，冷查询因与第一档使用同一 query 而**已写入缓存**，测出的 5ms 是"假冷"。**真实冷延迟 = 第一档的 817.9ms**。

## 三、压测结果

### 3.1 /api/search —— 命中 Redis 缓存（热）

| 并发 | 总请求 | **QPS (/s)** | 均值 (ms) | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | 错误 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 300 | **356.2** | 54.7 | 47.6 | 128.0 | 198.0 | 201.0 | 0 |
| 10 | 200 | **432.4** | 22.6 | 22.1 | 25.5 | 44.4 | 46.0 | 0 |

**解读**：更低并发（10）下命中缓存的单请求延迟更小（p95 仅 25.5ms）、QPS 更高（432/s）；并发升到 20 后出现排队，延迟尾部（p95/p99）抬升到 128/198ms，但 QPS 仍达 356/s，且全程 0 错误。

### 3.2 /api/search —— 冷（未命中缓存，完整检索链路）

| 场景 | 耗时 (ms) | 说明 |
| --- | --- | --- |
| **冷一次**（真实） | **817.9** | embedding + Milvus 双路召回 + RRF + rerank 精排 |
| 热命中（10 并发均值） | 22.6 | 直接命中 Redis 返回 |

**缓存加速比 ≈ 36 倍**（817.9ms → 22.6ms）。

### 3.3 /api/chat —— 非流式（调用 DeepSeek LLM，参考）

| 场景 | 耗时 (ms) | 说明 |
| --- | --- | --- |
| 第 1 次 | 4191.9 | LLM 生成（波动大） |
| 第 2 次 | 1114.5 | 同上 |

> chat 为 LLM 生成型接口，延迟主要由模型推理决定（1~4s 波动），不适合作为高并发压测对象，此处仅作参考。

## 四、结论

1. **命中 Redis 缓存的检索接口吞吐优秀**：10 并发下 QPS ≈ 432/s、p95 ≈ 25.5ms；20 并发下 QPS ≈ 356/s、p95 ≈ 128ms，全程无错误。缓存命中的读路径（Redis 读 + JSON 组装返回）是系统最快的一条链路。
2. **缓存收益显著**：冷检索 817.9ms → 热命中 22.6ms，**约 36 倍加速**，且热路径不消耗 SiliconFlow embedding/rerank 额度。
3. **chat（LLM）是延迟瓶颈**：1~4s 量级，与检索（几十 ms）不在一个量级；若追求体验需依赖 `ans:` 回答缓存（命中时直接回放，可接近检索级延迟）。
4. **并发放大作用**：并发从 10→20，QPS 略降、延迟 p95 从 25.5→128ms，说明当前部署在适当并发下已有排队（受 worker 数 / GIL / 阻塞型 Milvus 调用的线程池限制）。如需更高吞吐可增加 `uvicorn --workers` 或优化检索的线程池。

## 五、复现方式

```bash
# 1. 启动服务（已在 8010 运行时可跳过）
uvicorn rag个人知识库.api.main:app --host 0.0.0.0 --port 8010

# 2. 跑压测（20 并发 / 300 请求）
python -m benchmark.bench_search --concurrency 20 --total 300

# 3. 低并发档
python -m benchmark.bench_search --concurrency 10 --total 200
```

脚本会自动登录、预热缓存、输出 QPS/延迟分位，并把结果写入 `benchmark/bench_result.json`。

## 六、说明与局限

- 本测试为**简单压测**，数据来自本机回环（`127.0.0.1`），不含真实网络抖动；WAN 部署下绝对数值会变化，但**缓存 vs 冷检索的相对差异**与结论不变。
- 热缓存压测只覆盖"缓存命中"这一最快路径；真实生产混合负载（含未命中缓存 + 冷 embedding/rerank）的端到端 QPS 会低于此值。
- 冷延迟受外部 API（SiliconFlow embedding/rerank）影响，网络/额度波动会导致数值浮动（本报告两次冷测 5 ~ 818ms 即为例证）。
- 压测期间 /api/search 的查询池与 golden 集一致，缓存 TTL 内可稳定命中。
