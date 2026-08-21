"""RAG API 性能压测（QPS + 延迟）：针对 /api/search，重点对比 Redis 缓存命中/未命中的延迟。

用法：
  python benchmark/bench_search.py --concurrency 20 --total 300
  python benchmark/bench_search.py --concurrency 10 --total 200 --warmup 5

指标：
  - 命中 Redis 缓存（search: 前缀）的 QPS 与延迟分位（p50/p95/p99/max）
  - 冷（未命中缓存）单次请求延迟（触发 embedding + Milvus 召回 + rerank 精排）
  - /api/chat 非流式单次延迟（调用 LLM，参考性）

说明：
  - 热缓存压测不触碰外部 API（命中 Redis 直接返回），反映缓存命中的吞吐能力；
  - 冷请求每问会真实调用 SiliconFlow embedding + rerank，一次即可观测完整链路延迟。
"""
import argparse
import asyncio
import json
import statistics
import time

import aiohttp

BASE = "http://127.0.0.1:8010"
LOGIN_URL = BASE + "/api/auth/login"
SEARCH_URL = BASE + "/api/search"
CHAT_URL = BASE + "/api/chat"

USERNAME = "admin"
PASSWORD = "admin123"
K = 5

# 真实检索查询池（来自 golden 集，可作为稳定的 Redis 缓存 key）
QUERY_POOL = [
    "JWT 的三段结构分别是什么？",
    "JWT 怎么防止 token 被篡改？",
    "为什么用 Redis Streams 做入库任务队列？",
    "PEL 是什么？崩溃后未完成的任务怎么恢复？",
    "意图识别为什么要分规则层和 LLM 层？",
    "会话记忆的 TTL 过期清理规则是什么？",
    "复杂文档为什么要走 MinerU 解析？",
    "表格和公式在切分时是怎么被保护的？",
    "文件指纹有几把钥匙，各自作用是什么？",
    "MySQL 和 Milvus 为什么用两阶段落库？",
]

# 冷缓存专用新 query（不预热，保证未命中缓存）
COLD_QUERY = "LangGraph 与 LangChain 的核心差异是什么？"


async def get_token(session: aiohttp.ClientSession) -> str:
    """登录拿 JWT（表单编码）。"""
    data = "username=%s&password=%s" % (USERNAME, PASSWORD)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with session.post(LOGIN_URL, data=data, headers=headers) as resp:
        assert resp.status == 200, f"登录失败: {resp.status} {await resp.text()}"
        return (await resp.json())["access_token"]


async def one_search(session: aiohttp.ClientSession, token: str, query: str) -> float:
    """发一次 /api/search，返回耗时秒数。"""
    headers = {"Authorization": "Bearer " + token}
    payload = {"query": query, "k": K, "source": None}
    t0 = time.perf_counter()
    async with session.post(SEARCH_URL, json=payload, headers=headers) as resp:
        body = await resp.json()
    dt = time.perf_counter() - t0
    assert resp.status == 200, f"search 返回 {resp.status}: {body}"
    return dt


async def warmup(session, token, queries):
    """预热：每个 query 各发一次，让 Redis 缓存 search:* 命中。"""
    results = []
    for q in queries:
        results.append(await one_search(session, token, q))
    return results


async def hot_bench(session, token, queries, concurrency, total):
    """热缓存并发压测：随机打预热过的 query，统计 QPS 与延迟分位。"""
    latencies = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)

    async def worker(_):
        nonlocal errors
        q = queries[time.time_ns() % len(queries)]
        try:
            async with sem:
                latencies.append(await one_search(session, token, q))
        except Exception as e:
            errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*[worker(i) for i in range(total)])
    elapsed = time.perf_counter() - t0

    qps = total / elapsed
    lat = sorted(latencies)
    def pct(p):
        if not lat:
            return 0.0
        idx = min(len(lat) - 1, int(len(lat) * p))
        return lat[idx] * 1000

    return {
        "requests": total,
        "elapsed": elapsed,
        "qps": qps,
        "errors": errors,
        "mean_ms": (sum(latencies) / len(latencies) * 1000) if latencies else 0,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": (lat[-1] * 1000) if lat else 0,
    }


async def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--total", type=int, default=300)
    ap.add_argument("--warmup", type=int, default=2, help="预热所需的最小成功请求数（此处为轮次，每轮=全 query）")
    args = ap.parse_args(argv)

    async with aiohttp.ClientSession() as session:
        token = await get_token(session)
        headers = {"Authorization": "Bearer " + token}

        print("=== 预热（填充 Redis search: 缓存） ===")
        warm_times = await warmup(session, token, QUERY_POOL)
        print("  预热耗时均值: %.1f ms" % (sum(warm_times) / len(warm_times) * 1000))

        print(f"\n=== 热缓存并发压测（并发={args.concurrency}, 总请求={args.total}） ===")
        hot = await hot_bench(session, token, QUERY_POOL, args.concurrency, args.total)
        print(f"  QPS = {hot['qps']:.1f} /s   错误 = {hot['errors']}")
        print(f"  延迟均值 {hot['mean_ms']:.1f} ms | p50 {hot['p50_ms']:.1f} | p95 {hot['p95_ms']:.1f} | p99 {hot['p99_ms']:.1f} | max {hot['max_ms']:.1f}")

        print("\n=== 冷缓存单次请求（未命中缓存，含 embedding+Milvus+rerank） ===")
        cold = await one_search(session, token, COLD_QUERY)
        print(f"  冷查询耗时 = {cold*1000:.1f} ms")

        print("\n=== /api/chat 非流式单次（调用 DeepSeek LLM，参考性） ===")
        try:
            t0 = time.perf_counter()
            async with session.post(CHAT_URL, json={"content": COLD_QUERY}, headers=headers) as resp:
                await resp.json()
            chat_dt = time.perf_counter() - t0
            print(f"  chat 耗时 = {chat_dt*1000:.1f} ms (HTTP {resp.status})")
        except Exception as e:
            chat_dt = None
            print(f"  chat 异常: {e}")

        # 输出机器可读结果
        report = {
            "api": "RAG /api/search hot-cache benchmark",
            "params": {"concurrency": args.concurrency, "total": args.total, "k": K},
            "hot_cache": hot,
            "cold_ms": cold * 1000,
            "chat_ms": (chat_dt * 1000) if chat_dt is not None else None,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open("benchmark/bench_result.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print("\n结果已写入 benchmark/bench_result.json")


if __name__ == "__main__":
    import sys
    asyncio.run(main(sys.argv[1:]))
