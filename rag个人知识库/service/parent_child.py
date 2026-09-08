"""分层检索（Parent-Child）回填层：命中子块 → 命中点动态开窗 → 合并 → Token 预算 → 组装。

实现 v6 设计（docs/PARENT_CHILD_RETRIEVAL_DESIGN.md）：
  ① 子块命中（search_documents 已拿到 top-k 子块，含 parent_id/parent_char_start/end）
  ② 按 parent_id 分组 → 命中点 ± BACK/FWD 扩展 → merge_ranges 区间并集
  ③ 跨 Parent 方向性合并：A 贴近自身末尾 且 B 贴近自身开头 且 A.parent_index+1==B.parent_index
  ④ Token Budget（est_tokens 估算）：完整 Core 硬约束 + min_ctx + full→core→skip（不误 break）
  ⑤ 组装：跨 source 按该 source 最高分降序、文档内按 (parent_index, start)、保留各父块标题边界

父块文本从 Redis `parent:{parent_id}`（内容派生 key，版本自失效）优先、MySQL parent_chunks 兜底，
写入时登记 `parent_src:{source}` 集合供删除/下架显式失效。
"""
import json
import math
import os
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import get_redis
from rag个人知识库.models.vector import ParentChunk

# 参数（默认按 evaluation/experiment_report_window.md 实验结论，见设计文档 §7）
BACK_EXPAND_CHARS = int(os.getenv("BACK_EXPAND_CHARS", "200"))
FWD_EXPAND_CHARS = int(os.getenv("FWD_EXPAND_CHARS", "500"))
PARENT_BOUNDARY_MARGIN = int(os.getenv("PARENT_BOUNDARY_MARGIN", "100"))
CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "1200"))
CONTEXT_MIN_TOKENS = int(os.getenv("CONTEXT_MIN_TOKENS", "300"))
EST_CHARS_PER_TOKEN = float(os.getenv("EST_CHARS_PER_TOKEN", "1.6"))
PARENT_CACHE_TTL = int(os.getenv("PARENT_CACHE_TTL", "600"))


def est_tokens(text: str) -> int:
    """字符→token 快速估算（仅检索预算用，非计费——计费由 billing.py 按真实 token 记录）。"""
    return math.ceil(len(text) / EST_CHARS_PER_TOKEN)


def merge_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """区间并集（ranges 已按起点排序）：重叠/相邻合并。"""
    merged: List[Tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _can_merge(A: dict, B: dict, margin: int) -> bool:
    """跨 Parent 方向性判断：A 贴近自身末尾 ∧ B 贴近自身开头 ∧ 相邻。"""
    return (
        A["source"] == B["source"]
        and A["parent_index"] + 1 == B["parent_index"]
        and A["parent_len"] - A["end"] <= margin
        and B["start"] <= margin
    )


def build_segments(child_hits: List[dict], parents: Dict[str, dict]) -> List[dict]:
    """子块命中 → 窗口（扩展+并集）→ 跨父块方向性合并 → 段。"""
    windows: List[dict] = []
    by_pid: Dict[str, List[dict]] = {}
    for h in child_hits:
        if h.get("parent_id"):
            by_pid.setdefault(h["parent_id"], []).append(h)
    for pid, hs in by_pid.items():
        p = parents.get(pid)
        if p is None:
            continue
        plen = len(p["text"])
        ranges = sorted((max(0, h["start"] - BACK_EXPAND_CHARS),
                         min(plen, h["end"] + FWD_EXPAND_CHARS)) for h in hs)
        for a, b in merge_ranges(ranges):
            core_hits = [h for h in hs if h["start"] < b and h["end"] > a]
            windows.append({
                "parent_id": pid, "parent_index": p["index"], "source": hs[0].get("source"),
                "parent_len": plen,
                "start": a, "end": b,
                "core_start": min(h["start"] for h in core_hits),
                "core_end": max(h["end"] for h in core_hits),
                "score": max(float(h.get("score") or 0) for h in core_hits),
                "hit_count": len(core_hits),
            })

    by_src: Dict[str, List[dict]] = {}
    for w in windows:
        by_src.setdefault(w["source"], []).append(w)

    segs: List[dict] = []
    for src, ws in by_src.items():
        ws.sort(key=lambda w: (w["parent_index"], w["start"]))
        chain: List[dict] = []
        for w in ws:
            if chain and _can_merge(chain[-1], w, PARENT_BOUNDARY_MARGIN):
                chain.append(w)
            else:
                if chain:
                    segs.append(chain)
                chain = [w]
        if chain:
            segs.append(chain)

    # chain → segment（同 parent 多窗口并成一个成员块，保留各自核心/扩展区间）
    result: List[dict] = []
    for chain in segs:
        by_pid = {}
        for w in chain:
            by_pid.setdefault(w["parent_id"], []).append(w)
        members = []
        for pid, ws in by_pid.items():
            p = parents.get(pid)
            if p is None:
                continue
            members.append({
                "parent_id": pid, "parent_index": p["index"], "source": ws[0].get("source"),
                "title": p.get("title") or "",
                "start": min(w["start"] for w in ws), "end": max(w["end"] for w in ws),
                "core_start": min(w["core_start"] for w in ws),
                "core_end": max(w["core_end"] for w in ws),
            })
        if not members:
            continue
        members.sort(key=lambda m: m["parent_index"])
        result.append({
            "members": members,
            "score": max(w["score"] for w in chain),
            "hit_count": sum(w["hit_count"] for w in chain),
        })
    return result


def _seg_blocks(seg: dict, parents: Dict[str, dict], core_only: bool) -> List[str]:
    """段 → 文本块列表（每成员 = 标题 + 切片；core_only 用命中核心区间，否则用扩展窗口）。"""
    blocks: List[str] = []
    for m in seg["members"]:
        p = parents.get(m["parent_id"])
        if p is None:
            continue
        s = m["core_start"] if core_only else m["start"]
        e = m["core_end"] if core_only else m["end"]
        if e <= s:
            continue
        text = p["text"][s:e]
        blocks.append(f"{m['title']}\n\n{text}" if m["title"] else text)
    return blocks


def assemble_context(child_hits: List[dict], parents: Dict[str, dict]) -> Tuple[List[dict], int]:
    """v6 组装：返回 (result, used_tokens)。
    result: [{content, score, source, metadata:{parent_id, parent_title, parent_index}}]
    """
    segs = build_segments(child_hits, parents)
    segs.sort(key=lambda s: -s["score"])
    chosen: List[Tuple[dict, str]] = []
    used = 0

    if segs:
        top = segs[0]
        core_blocks = _seg_blocks(top, parents, core_only=True)
        core_text = "\n\n".join(core_blocks)
        chosen.append((top, core_text))
        used = est_tokens(core_text)
        # 最小上下文保护：预算内把 top 段补到 min_ctx（满窗优先）
        if used < CONTEXT_MIN_TOKENS and used < CONTEXT_MAX_TOKENS:
            full_blocks = _seg_blocks(top, parents, core_only=False)
            full_text = "\n\n".join(full_blocks)
            if est_tokens(full_text) <= CONTEXT_MAX_TOKENS:
                chosen[-1] = (top, full_text)
                used = est_tokens(full_text)

    for seg in segs[1:]:
        if CONTEXT_MAX_TOKENS - used <= 0:
            break
        remaining = CONTEXT_MAX_TOKENS - used
        full_text = "\n\n".join(_seg_blocks(seg, parents, core_only=False))
        size_t = est_tokens(full_text)
        if size_t <= remaining:
            chosen.append((seg, full_text))
            used += size_t
        else:
            core_text = "\n\n".join(_seg_blocks(seg, parents, core_only=True))
            if est_tokens(core_text) <= remaining:
                chosen.append((seg, core_text))
                used += est_tokens(core_text)
            else:
                continue  # 连核心都放不下：跳过，不 break

    # 组装：跨 source 按该 source 最高分降序；文档内按 (parent_index, start)；保留标题边界
    by_src: Dict[str, List[Tuple[dict, str]]] = {}
    for seg, text in chosen:
        src = seg["members"][0]["source"]
        by_src.setdefault(src, []).append((seg, text))
    src_order = sorted(by_src.keys(),
                       key=lambda s: -max(seg["score"] for seg, _t in by_src[s]))

    result: List[dict] = []
    for src in src_order:
        for seg, text in sorted(
            by_src[src],
            key=lambda x: (x[0]["members"][0]["parent_index"], x[0]["members"][0]["start"]),
        ):
            first = seg["members"][0]
            result.append({
                "content": text,
                "score": seg["score"],
                "source": first["source"],
                "metadata": {
                    "parent_id": first["parent_id"],
                    "parent_title": first["title"],
                    "parent_index": first["parent_index"],
                    "rerank_score": seg["score"],
                    "hit_count": seg["hit_count"],
                },
            })
    return result, used


# ── 父块加载（Redis 优先 + MySQL 兜底）与失效 ──
async def load_parents(parent_ids: List[str]) -> Dict[str, dict]:
    """批量取父块记录：Redis parent:{id} 优先，未命中回源 MySQL 并写回缓存。"""
    ids = sorted({x for x in parent_ids if x})
    if not ids:
        return {}
    result: Dict[str, dict] = {}
    missing: List[str] = []
    try:
        r = get_redis()
        vals = await r.mget(*(f"parent:{pid}" for pid in ids))
        for pid, v in zip(ids, vals):
            if v is not None:
                rec = json.loads(v)
                result[pid] = {"text": rec["text"], "title": rec.get("title") or "",
                               "index": int(rec.get("index", 0))}
            else:
                missing.append(pid)
    except Exception:
        missing = list(ids)
    if missing:
        try:
            async with async_session() as db:
                rows = (await db.execute(
                    select(ParentChunk).where(ParentChunk.parent_id.in_(missing))
                )).scalars().all()
            for row in rows:
                result[row.parent_id] = {
                    "text": row.parent_text,
                    "title": row.parent_title or "",
                    "index": row.parent_index,
                }
            try:
                r = get_redis()
                pipe = r.pipeline()
                for pid in missing:
                    if pid in result:
                        pipe.set(f"parent:{pid}",
                                 json.dumps(result[pid], ensure_ascii=False), ex=PARENT_CACHE_TTL)
                await pipe.execute()
            except Exception:
                pass
        except Exception:
            pass
    return result


async def invalidate_parent_cache_by_source(source: str) -> None:
    """删除/账户删除时按 source 清父块缓存（parent_src:{source} 登记 → parent:{id}）。"""
    if not source:
        return
    try:
        r = get_redis()
        key = f"parent_src:{source}"
        pids = list(await r.smembers(key))
        if pids:
            await r.unlink(*(f"parent:{pid}" for pid in pids))
        await r.delete(key)
    except Exception:
        pass
