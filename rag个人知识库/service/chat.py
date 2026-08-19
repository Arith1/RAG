"""对话编排：意图识别 + 查询提炼 → 向量检索 → 拼接 user_prompt → Agent 生成回答。

流程（与产品定义一致）：
  1. analyze(content, history)：一次 LLM 结构化输出，同时得到 intent 与提炼后的 query；
     history 从对话记忆（Postgres checkpointer）读取最近几轮，用于把"它/那个/上面"
     等指代补全为可独立检索的 query
     - chat（闲聊）→ 只进入 LLM 对话，不检索
     - rag_ask / other → 进入问答环节
  2. search_documents(query)：对提炼后的 query 做向量检索
  3. 检索结果与 query 拼接成 user_prompt
  4. ask(user_prompt)：交给 Agent 生成回答
  5. 返回 answer + intent + query + sources（来源引用）
"""
import asyncio
from typing import List, Optional

import openai
from langchain_core.messages import HumanMessage

from rag个人知识库.agent.ai_assist import ask, astream, get_checkpointer
from rag个人知识库.agent.intent import analyze
from rag个人知识库.config.redis import cache_get, cache_key, cache_set
from rag个人知识库.service.service import search_documents
from rag个人知识库.vector_store.milvus_store import ANSWER_CACHE_TTL

# analyze 用到的历史：最多取最近 N 轮（每轮 user+assistant 两条），单条截断长度
HISTORY_MAX_TURNS = 3
HISTORY_MAX_CHARS = 150


def _msg_type(msg) -> Optional[str]:
    """兼容 dict 与 BaseMessage 两种 checkpoint 消息表示。"""
    if isinstance(msg, dict):
        return msg.get("type")
    return getattr(msg, "type", None)


def _msg_content(msg) -> str:
    """提取消息文本，兼容 str / 多模态 content 块列表。"""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        content = " ".join(parts)
    return str(content or "")


def load_recent_history(thread_id: str) -> Optional[str]:
    """从对话记忆（checkpointer）读取最近几轮对话，格式化为 analyze 用的历史文本。

    读的是与 agent 共用的同一份持久化记忆（Postgres / InMemory），不新增任何存储；
    无历史 / 读取失败时返回 None（analyze 退化为无上下文的原逻辑）。
    """
    try:
        tup = get_checkpointer().get_tuple({"configurable": {"thread_id": thread_id}})
    except Exception as e:
        print(f"[chat] 读取对话历史失败（不影响问答）：{e}")
        return None
    if tup is None:
        return None
    messages = tup.checkpoint.get("channel_values", {}).get("messages")
    if not messages:
        return None

    lines = []
    for msg in messages[-HISTORY_MAX_TURNS * 2:]:
        mtype = _msg_type(msg)
        if mtype == "human":
            role = "用户"
        elif mtype == "ai":
            role = "助手"
        else:
            continue  # 跳过 tool / system 等
        text = _msg_content(msg).strip()
        if not text:
            continue
        lines.append(f"{role}：{text[:HISTORY_MAX_CHARS]}")
    if not lines:
        return None
    return "\n".join(lines)


def _friendly_model_error(exc: Exception) -> str:
    """把常见的模型调用异常转换为用户可读的提示。"""
    if isinstance(exc, openai.APITimeoutError):
        return "模型响应超时，请稍后重试。"
    if isinstance(exc, openai.APIConnectionError):
        return "无法连接模型服务，请检查网络后重试。"
    if isinstance(exc, openai.AuthenticationError):
        return "模型 API Key 无效或没有访问权限。"
    if isinstance(exc, openai.RateLimitError):
        return "请求过于频繁，或 API 额度已用尽。"
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code in (402, 429):
            return "API 余额或调用额度可能不足，请检查账户。"
        if exc.status_code in (401, 403):
            return "模型 API Key 无效或没有访问权限。"
        return f"模型服务返回错误：{exc.status_code}"
    return "模型暂时不可用，请稍后重试。"


def _build_context(hits: List[dict]) -> str:
    """把检索命中结果拼成带编号的参考资料文本。"""
    blocks = []
    for index, hit in enumerate(hits, start=1):
        source = hit.get("source") or "未知来源"
        content = (hit.get("content") or "").strip()
        blocks.append(f"[{index}] 来源：{source}\n{content}")
    return "\n\n".join(blocks)


def _build_user_prompt(query: str, hits: List[dict]) -> str:
    """检索内容与 query 拼接成发给 Agent 的 user_prompt。"""
    context = _build_context(hits)
    return f"用户问题：{query}\n\n参考资料：\n{context}"


async def chat(
    content: str,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
    thread_id: str = "default",
) -> dict:
    """对话统一入口：意图识别 → （闲聊直答 | 问答环节）。"""
    # 第一步：意图识别 + 查询提炼（结合最近对话补全指代；读取失败则退化为无历史）
    history = await asyncio.to_thread(load_recent_history, thread_id)
    analysis = await asyncio.to_thread(analyze, content, history)

    # 闲聊：只进入 LLM 对话，不做检索
    if analysis.intent == "chat":
        try:
            answer = await asyncio.to_thread(
                ask,
                [HumanMessage(content=content)],
                thread_id,
            )
        except Exception as exc:
            return {
                "answer": _friendly_model_error(exc),
                "intent": analysis.intent,
                "query": None,
                "sources": [],
                "hits": [],
                "error": str(exc),
            }
        return {
            "answer": answer,
            "intent": analysis.intent,
            "query": None,
            "sources": [],
            "hits": [],
        }

    # 当前没有文档上传/修改/删除等管理功能，遇到 other 直接明确提示，不触发检索
    if analysis.intent == "other":
        return {
            "answer": "当前仅支持知识库问答和闲聊，暂不支持文档上传、修改、删除等操作。",
            "intent": analysis.intent,
            "query": None,
            "sources": [],
            "hits": [],
        }

    # 问答环节：用提炼后的 query 向量检索 → 拼接 user_prompt → Agent
    query = analysis.query or content
    hits = await search_documents(query, k=k, source=source, expr=expr)
    if not hits:
        return {
            "answer": "知识库中未找到相关资料，请换个问法，或确认文档已入库。",
            "intent": analysis.intent,
            "query": query,
            "sources": [],
            "hits": [],
        }

    user_prompt = _build_user_prompt(query, hits)
    # 回答缓存：同一 user_prompt（query + 相同参考资料）→ 复用 LLM 回答（TTL 1 小时）。
    # user_prompt 已包含完整上下文，跨会话同问同答是安全的；Redis 不可用则正常生成。
    ans_key = cache_key("ans", user_prompt)
    cached_answer = await cache_get(ans_key)
    if cached_answer is not None:
        answer = cached_answer
    else:
        try:
            answer = await asyncio.to_thread(
                ask,
                [HumanMessage(content=user_prompt)],
                thread_id,
            )
            await cache_set(ans_key, answer, ANSWER_CACHE_TTL)
        except Exception as exc:
            sources = [
                {
                    "index": index,
                    "source": hit.get("source"),
                    "score": hit.get("score"),
                    "content": hit.get("content"),
                }
                for index, hit in enumerate(hits, start=1)
            ]
            return {
                "answer": _friendly_model_error(exc),
                "intent": analysis.intent,
                "query": query,
                "sources": sources,
                "hits": hits,
                "error": str(exc),
            }

    sources = [
        {
            "index": index,
            "source": hit.get("source"),
            "score": hit.get("score"),
            "content": hit.get("content"),
        }
        for index, hit in enumerate(hits, start=1)
    ]
    return {
        "answer": answer,
        "intent": analysis.intent,
        "query": query,
        "sources": sources,
        "hits": hits,
    }


async def chat_stream(
    content: str,
    thread_id: str = "default",
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
):
    """流式问答：analyze + 检索为前置步骤（普通 await），LLM 生成段逐 token 产出。

    产出事件（JSON dict，供 SSE 帧封装）：
      {"type": "meta",   "intent", "query", "sources": [...]}  检索完成，即将开始生成
      {"type": "token",  "text": "..."}                        生成中的增量片段
      {"type": "done",   "answer": "完整回答"}                 生成结束
      {"type": "answer", "intent", "query", "answer", "sources"} 闲聊/other/无资料：一次性完整结果
      {"type": "error",  "message"}                            生成异常

    与 chat() 共用 analyze/检索/缓存逻辑，仅 LLM 生成段改为流式。
    """
    history = await asyncio.to_thread(load_recent_history, thread_id)
    analysis = await asyncio.to_thread(analyze, content, history)

    # 闲聊：不走检索，直接完整回答
    if analysis.intent == "chat":
        answer = await asyncio.to_thread(ask, [HumanMessage(content=content)], thread_id)
        yield {"type": "answer", "intent": "chat", "query": None,
               "answer": answer, "sources": []}
        return

    if analysis.intent == "other":
        yield {"type": "answer", "intent": "other", "query": None,
               "answer": "当前仅支持知识库问答和闲聊，暂不支持文档上传、修改、删除等操作。",
               "sources": []}
        return

    query = analysis.query or content
    try:
        hits = await search_documents(query, k=k, source=source, expr=expr)
    except Exception as exc:
        # 检索失败（如 Milvus 不可用）：发 error 事件而非直接断流，前端可提示重试
        yield {"type": "error", "message": f"检索服务异常：{exc}"}
        return
    sources = [
        {"index": index, "source": hit.get("source"),
         "score": hit.get("score"), "content": hit.get("content")}
        for index, hit in enumerate(hits, start=1)
    ]
    yield {"type": "meta", "intent": analysis.intent, "query": query, "sources": sources}

    if not hits:
        yield {"type": "answer", "intent": analysis.intent, "query": query,
               "answer": "知识库中未找到相关资料，请换个问法，或确认文档已入库。",
               "sources": []}
        return

    user_prompt = _build_user_prompt(query, hits)
    # 回答缓存命中：直接回放完整答案（仍走 token 事件，前端体验一致）
    ans_key = cache_key("ans", user_prompt)
    cached = await cache_get(ans_key)
    if cached is not None:
        yield {"type": "token", "text": cached}
        yield {"type": "done", "answer": cached}
        return

    parts: List[str] = []
    try:
        async for token in astream([HumanMessage(content=user_prompt)], thread_id):
            parts.append(token)
            yield {"type": "token", "text": token}
    except Exception as exc:
        yield {"type": "error", "message": _friendly_model_error(exc)}
        return
    answer = "".join(parts)
    await cache_set(ans_key, answer, ANSWER_CACHE_TTL)
    yield {"type": "done", "answer": answer}
