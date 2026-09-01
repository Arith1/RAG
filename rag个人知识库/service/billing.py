"""LLM 计费服务：捕获每次 LLM 调用的 token 用量并估算费用，异步写入 llm_usage。

设计：
  - 每次 LLM 调用一行；同一 HTTP 请求的多行用 request_id 归组（intent + answer 等）。
  - API 层用 billing_request() 设置请求上下文（request_id / user_id / session_id），
    各调用点（意图识别、回答）用 billing_stage() 标注调用阶段。
  - 全局 TokenUsageCallback 通过 config.callbacks 传给意图 / Agent 调用，
    on_llm_end 读取上下文把用量收集到请求级 rows 列表；请求结束由 API 层 flush_usage() 入库。
  - 计费写入失败只记日志，绝不影响问答主流程。

价格配置（.env，单位：元 / 每百万 token）：
  DEEPSEEK_PRICE_INPUT / DEEPSEEK_PRICE_CACHED / DEEPSEEK_PRICE_OUTPUT
  QWEN_PRICE_INPUT / QWEN_PRICE_CACHED / QWEN_PRICE_OUTPUT
  未知模型回退 DEFAULT_LLM_PRICE_*。
  estimated_cost = (uncached*input + cached*cached_price + output*output_price) / 1e6
"""
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.config.db_config import async_session
from rag个人知识库.models.billing import LlmUsage
from rag个人知识库.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class BillingContext:
    """一次请求的计费上下文：请求元信息 + 该请求收集到的 LLM 用量记录。"""

    request_id: str
    user_id: int
    session_id: Optional[str]
    rows: List[dict] = field(default_factory=list)


_billing_ctx: ContextVar[Optional[BillingContext]] = ContextVar("billing_ctx", default=None)
_llm_stage: ContextVar[str] = ContextVar("llm_stage", default="answer")


@contextmanager
def billing_request(ctx: BillingContext):
    """在请求作用域内设置计费上下文（contextvar），退出自动清理。"""
    token = _billing_ctx.set(ctx)
    try:
        yield
    finally:
        _billing_ctx.reset(token)


@contextmanager
def billing_stage(stage: str):
    """标注当前 LLM 调用阶段（intent/answer/chat/summarize）。"""
    token = _llm_stage.set(stage)
    try:
        yield
    finally:
        _llm_stage.reset(token)


def record_cached_answer(model: str = "") -> None:
    """回答缓存命中：记一条 type=answer_cached 的计费（tokens=0/cost=0，status=cached）。

    让「最近用量」能看到这次回答来自缓存（省了生成费用），而不是凭空消失。
    未处于请求计费作用域（billing_request 外）时静默跳过。
    """
    ctx = _billing_ctx.get()
    if ctx is None:
        return
    ctx.rows.append({
        "user_id": ctx.user_id,
        "session_id": ctx.session_id,
        "request_id": ctx.request_id,
        "provider": "cache",
        "model": model or "answer-cache",
        "type": "answer_cached",
        "input_tokens": 0,
        "cached_tokens": 0,
        "uncached_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "credits": Decimal("0"),
        "estimated_cost": Decimal("0"),
        "latency_ms": 0,
        "status": "cached",
    })
    logger.info("[billing] 回答缓存命中记账（request_id=%s）", ctx.request_id)


def _price_for(model: str) -> Dict[str, float]:
    """按模型名返回每百万 token 单价（元）；未知模型回退默认价。"""
    if model == "deepseek-v4-flash":
        return {
            "input": float(os.getenv("DEEPSEEK_PRICE_INPUT", "2.0")),
            "cached": float(os.getenv("DEEPSEEK_PRICE_CACHED", "0.5")),
            "output": float(os.getenv("DEEPSEEK_PRICE_OUTPUT", "8.0")),
        }
    if model == "qwen3.7-flash":
        return {
            "input": float(os.getenv("QWEN_PRICE_INPUT", "1.0")),
            "cached": float(os.getenv("QWEN_PRICE_CACHED", "0.2")),
            "output": float(os.getenv("QWEN_PRICE_OUTPUT", "2.0")),
        }
    return {
        "input": float(os.getenv("DEFAULT_LLM_PRICE_INPUT", "2.0")),
        "cached": float(os.getenv("DEFAULT_LLM_PRICE_CACHED", "0.5")),
        "output": float(os.getenv("DEFAULT_LLM_PRICE_OUTPUT", "8.0")),
    }


def estimate_cost(model: str, uncached: int, cached: int, output: int) -> float:
    """按 .env 单价估算费用（元），返回保留 6 位小数的浮点数。"""
    price = _price_for(model)
    cost = (uncached * price["input"] + cached * price["cached"] + output * price["output"]) / 1_000_000.0
    return round(cost, 6)


def _extract_usage(msg) -> dict:
    """从 LLM 消息的 response_metadata / usage_metadata 提取用量字段。"""
    rm = getattr(msg, "response_metadata", None) or {}
    um = getattr(msg, "usage_metadata", None) or {}
    usage = rm.get("usage") or {}
    details = um.get("input_token_details") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or um.get("input_tokens") or 0),
        "cached_tokens": int(usage.get("prompt_cache_hit_tokens") or details.get("cache_read") or 0),
        "uncached_tokens": int(usage.get("prompt_cache_miss_tokens") or details.get("cache_creation") or 0),
        "output_tokens": int(usage.get("completion_tokens") or um.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or um.get("total_tokens") or 0),
    }


class TokenUsageCallback(BaseCallbackHandler):
    """捕获每次 LLM 调用的用量与耗时，写入当前请求的计费上下文。

    未处于请求作用域（billing_request 外，如启动连通性 ping）时静默跳过。
    """

    def __init__(self):
        self._started: Dict[str, float] = {}

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._started[run_id] = time.monotonic()

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        ctx = _billing_ctx.get()
        started = self._started.pop(run_id, None)
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        if ctx is None:
            return
        try:
            msg = None
            for gen in response.generations or []:
                if gen and gen[0].message is not None:
                    msg = gen[0].message
                    break
            if msg is None:
                return
            rm = getattr(msg, "response_metadata", None) or {}
            usage = _extract_usage(msg)
            provider = rm.get("model_provider") or ""
            model = rm.get("model_name") or ""
            cost = estimate_cost(model, usage["uncached_tokens"], usage["cached_tokens"], usage["output_tokens"])
            row = {
                "user_id": ctx.user_id,
                "session_id": ctx.session_id,
                "request_id": ctx.request_id,
                "provider": provider or "unknown",
                "model": model or "unknown",
                "type": _llm_stage.get(),
                **usage,
                "credits": Decimal("0"),
                "estimated_cost": Decimal(str(cost)),
                "latency_ms": latency_ms,
                "status": "success",
            }
            ctx.rows.append(row)
            logger.info(
                "[billing] type=%s model=%s input=%s cached=%s uncached=%s output=%s cost=%s",
                row["type"], row["model"], usage["input_tokens"], usage["cached_tokens"],
                usage["uncached_tokens"], usage["output_tokens"], row["estimated_cost"],
            )
        except Exception as e:
            logger.warning("[billing] 收集 LLM 用量失败（不影响问答）：%s", e)


token_usage_callback = TokenUsageCallback()


async def flush_usage(ctx: BillingContext) -> None:
    """把请求收集到的计费记录写入 llm_usage；失败只记日志。"""
    if not ctx.rows:
        return
    try:
        async with async_session() as db:
            for row in ctx.rows:
                db.add(LlmUsage(**row))
            await db.commit()
        logger.info("[billing] 已写入 %d 条 LLM 计费记录（request_id=%s）", len(ctx.rows), ctx.request_id)
    except Exception as e:
        logger.warning("[billing] 写入 llm_usage 失败（不影响问答）：%s", e)


# ── 用量查询（只读，不改表结构；前端「用量」页与管理员视角共用）──
def _range_start(range_key: str) -> Optional[datetime]:
    """把 range 参数换算成起始时间；all 返回 None（不限时间）。"""
    now = datetime.now()
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "7d":
        return now - timedelta(days=7)
    if range_key == "30d":
        return now - timedelta(days=30)
    return None


def _usage_buckets(
    rows,
) -> List[dict]:
    """把 GROUP BY 结果统一成 [{key, requests, tokens, cost}]。"""
    return [
        {
            "key": r[0],
            "requests": int(r[1]),
            "tokens": int(r[2]),
            "cost": round(float(r[3]), 6),
        }
        for r in rows
    ]


async def get_user_billing_summary(db: AsyncSession, user_id: int, range_key: str = "7d") -> dict:
    """当前用户用量汇总：请求/调用数、费用、tokens + 按类型/模型/按天分布。"""
    since = _range_start(range_key)
    cond = [LlmUsage.user_id == user_id]
    if since is not None:
        cond.append(LlmUsage.created_at >= since)

    totals = (
        await db.execute(
            select(
                func.count(LlmUsage.id),
                func.count(func.distinct(LlmUsage.request_id)),
                func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.cached_tokens), 0),
                func.coalesce(func.sum(LlmUsage.uncached_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            ).where(*cond)
        )
    ).one()
    total_requests = int(totals[0])
    request_count = int(totals[1])
    total_cost = round(float(totals[2]), 6)

    by_type = _usage_buckets(
        (
            await db.execute(
                select(
                    LlmUsage.type,
                    func.count(LlmUsage.id),
                    func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                )
                .where(*cond)
                .group_by(LlmUsage.type)
                .order_by(func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc())
            )
        ).all()
    )
    by_model = _usage_buckets(
        (
            await db.execute(
                select(
                    LlmUsage.model,
                    func.count(LlmUsage.id),
                    func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                )
                .where(*cond)
                .group_by(LlmUsage.model)
                .order_by(func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc())
            )
        ).all()
    )
    daily_rows = (
        await db.execute(
            select(
                func.date(LlmUsage.created_at),
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
            )
            .where(*cond)
            .group_by(func.date(LlmUsage.created_at))
            .order_by(func.date(LlmUsage.created_at).asc())
        )
    ).all()
    daily = [
        {
            "date": str(r[0]),
            "requests": int(r[1]),
            "tokens": int(r[2]),
            "cost": round(float(r[3]), 6),
        }
        for r in daily_rows
    ]

    return {
        "range": range_key,
        "request_count": request_count,
        "total_requests": total_requests,
        "total_cost": total_cost,
        "avg_cost": round(total_cost / request_count, 6) if request_count else 0.0,
        "total_tokens": int(totals[3]),
        "input_tokens": int(totals[4]),
        "cached_tokens": int(totals[5]),
        "uncached_tokens": int(totals[6]),
        "output_tokens": int(totals[7]),
        "by_type": by_type,
        "by_model": by_model,
        "daily": daily,
    }


async def list_user_billing_usage(
    db: AsyncSession,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    type_filter: Optional[str] = None,
) -> dict:
    """当前用户最近调用明细（分页，可按 type 过滤）。"""
    cond = [LlmUsage.user_id == user_id]
    if type_filter:
        cond.append(LlmUsage.type == type_filter)
    total = int((await db.scalar(select(func.count(LlmUsage.id)).where(*cond))) or 0)
    rows = (
        await db.execute(
            select(LlmUsage)
            .where(*cond)
            .order_by(LlmUsage.created_at.desc(), LlmUsage.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    items = [
        {
            "id": u.id,
            "session_id": u.session_id,
            "request_id": u.request_id,
            "provider": u.provider,
            "model": u.model,
            "type": u.type,
            "input_tokens": u.input_tokens,
            "cached_tokens": u.cached_tokens,
            "uncached_tokens": u.uncached_tokens,
            "output_tokens": u.output_tokens,
            "total_tokens": u.total_tokens,
            "estimated_cost": float(u.estimated_cost),
            "latency_ms": u.latency_ms,
            "status": u.status,
            "created_at": u.created_at,
        }
        for u in rows
    ]
    return {"total": total, "items": items}


async def get_admin_billing_overview(db: AsyncSession, range_key: str = "7d") -> dict:
    """管理员：全站用量汇总 + 按费用排行前 10 的用户。"""
    since = _range_start(range_key)
    cond = []
    if since is not None:
        cond.append(LlmUsage.created_at >= since)

    totals = (
        await db.execute(
            select(
                func.count(LlmUsage.id),
                func.count(func.distinct(LlmUsage.request_id)),
                func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.count(func.distinct(LlmUsage.user_id)),
            ).where(*cond)
        )
    ).one()
    by_type = _usage_buckets(
        (
            await db.execute(
                select(
                    LlmUsage.type,
                    func.count(LlmUsage.id),
                    func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                )
                .where(*cond)
                .group_by(LlmUsage.type)
                .order_by(func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc())
            )
        ).all()
    )
    by_model = _usage_buckets(
        (
            await db.execute(
                select(
                    LlmUsage.model,
                    func.count(LlmUsage.id),
                    func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
                )
                .where(*cond)
                .group_by(LlmUsage.model)
                .order_by(func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc())
            )
        ).all()
    )
    top_rows = (
        await db.execute(
            select(
                LlmUsage.user_id,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
            )
            .where(*cond)
            .group_by(LlmUsage.user_id)
            .order_by(func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc())
            .limit(10)
        )
    ).all()
    top_users = [
        {
            "user_id": r[0],
            "requests": int(r[1]),
            "tokens": int(r[2]),
            "cost": round(float(r[3]), 6),
            "username": None,
        }
        for r in top_rows
    ]
    user_ids = [t["user_id"] for t in top_users]
    if user_ids:
        uname_rows = (
            await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        ).all()
        uname_map = {rid: name for rid, name in uname_rows}
        for t in top_users:
            t["username"] = uname_map.get(t["user_id"])

    return {
        "range": range_key,
        "request_count": int(totals[1]),
        "total_requests": int(totals[0]),
        "total_cost": round(float(totals[2]), 6),
        "total_tokens": int(totals[3]),
        "active_users": int(totals[4]),
        "by_type": by_type,
        "by_model": by_model,
        "top_users": top_users,
    }


async def list_admin_user_usage(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    q: str = "",
) -> dict:
    """管理员：按用户聚合用量列表（可按用户名搜索，分页）。"""
    keyword = (q or "").strip()
    like = None
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"

    count_stmt = (
        select(func.count(func.distinct(LlmUsage.user_id)))
        .select_from(LlmUsage)
        .join(User, User.id == LlmUsage.user_id, isouter=True)
    )
    if like:
        count_stmt = count_stmt.where(User.username.like(like))
    total = int((await db.scalar(count_stmt)) or 0)

    stmt = (
        select(
            LlmUsage.user_id,
            User.username,
            func.count(LlmUsage.id),
            func.count(func.distinct(LlmUsage.request_id)),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(LlmUsage.input_tokens), 0),
            func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            func.coalesce(func.sum(LlmUsage.estimated_cost), 0),
            func.max(LlmUsage.created_at),
        )
        .join(User, User.id == LlmUsage.user_id, isouter=True)
        .group_by(LlmUsage.user_id, User.username)
        .order_by(
            func.coalesce(func.sum(LlmUsage.estimated_cost), 0).desc(),
            LlmUsage.user_id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if like:
        stmt = stmt.where(User.username.like(like))
    rows = (await db.execute(stmt)).all()
    items = [
        {
            "user_id": r[0],
            "username": r[1],
            "total_requests": int(r[2]),
            "request_count": int(r[3]),
            "total_tokens": int(r[4]),
            "input_tokens": int(r[5]),
            "output_tokens": int(r[6]),
            "total_cost": round(float(r[7]), 6),
            "last_used_at": r[8],
        }
        for r in rows
    ]
    return {"total": total, "items": items}
