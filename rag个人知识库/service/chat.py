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
import logging
from typing import List, Optional

import openai
from langchain_core.messages import HumanMessage

from rag个人知识库.agent.ai_assist import (
    ask,
    astream,
    append_thread_exchange,
    get_checkpointer,
)
from rag个人知识库.agent.intent import analyze
from rag个人知识库.config.db_config import async_session
from rag个人知识库.config.redis import cache_get, cache_index_sources, cache_key, cache_set
from rag个人知识库.crud.vector import select_visible_file_ids
from rag个人知识库.service.service import search_documents
from rag个人知识库.vector_store.milvus_store import ANSWER_CACHE_TTL

logger = logging.getLogger(__name__)

# analyze 用到的历史：最多取最近 N 轮（每轮 user+assistant 两条），单条截断长度
HISTORY_MAX_TURNS = 3
HISTORY_MAX_CHARS = 150
# 多问题编排时最多拆分的子问题数，防止一次请求触发过多检索/LLM 调用
MAX_QUESTIONS = 5


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
        logger.warning("[chat] 读取对话历史失败（不影响问答）：%s", e)
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
def _build_multi_user_prompt(
    questions: List[str],
    hits_by_question: List[List[dict]],
) -> str:
    """把多个子问题及其检索结果拼成一个汇总 user_prompt。

    格式：
      用户输入了多个问题，请按编号逐一回答：
      1. 问题1
      2. 问题2

      参考资料：
      [1] 对应问题1；来源：...
      ...
    """
    lines = [
        "用户输入了多个问题，请按编号逐一回答：",
        *[f"{i}. {q}" for i, q in enumerate(questions, start=1)],
        "",
        "参考资料：",
    ]
    ref_no = 1
    for q_idx, hits in enumerate(hits_by_question, start=1):
        if not hits:
            lines.append(f"[问题{q_idx}] 没有检索到相关资料")
            continue
        for hit in hits:
            source = hit.get("source") or "未知来源"
            content = (hit.get("content") or "").strip()
            lines.append(f"[{ref_no}] 对应问题{q_idx}；来源：{source}\n{content}")
            ref_no += 1
    return "\n\n".join(lines)


async def chat(
    content: str,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
    thread_id: str = "default",
    user_id: Optional[int] = None,
    retrieve_own_private: bool = True,
    retrieve_own_public: bool = True,
    retrieve_kb_public: bool = True,
    retrieve_owner_ids: Optional[List[int]] = None,
    file_ids: Optional[List[int]] = None,
    load_history: bool = True,
) -> dict:
    """对话统一入口：意图识别 → （闲聊直答 | 问答环节）。

    user_id: 检索可见性过滤（仅返回当前用户可见文档），传 None 不过滤（CLI 场景）。
    检索范围（会话首问锁定）：retrieve_own_private/own_public/kb_public/retrieve_owner_ids，
    透传给每个 search_documents 调用。
    """
    # 第一步：意图识别 + 查询提炼（结合最近对话补全指代；读取失败则退化为无历史）
    # 新会话（load_history=False）没有 checkpoint，跳过历史读取省一次 Postgres 往返
    history = await asyncio.to_thread(load_recent_history, thread_id) if load_history else None
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
            logger.exception("[chat] 闲聊模型调用失败")
            return {
                "answer": _friendly_model_error(exc),
                "intent": analysis.intent,
                "query": None,
                "sources": [],
                "hits": [],
                "error": "model_unavailable",
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
        answer = "当前仅支持知识库问答和闲聊，暂不支持文档上传、修改、删除等操作。"
        await asyncio.to_thread(append_thread_exchange, thread_id, content, answer)
        return {
            "answer": answer,
            "intent": analysis.intent,
            "query": None,
            "sources": [],
            "hits": [],
        }

    # 可见文件 id 每个请求只查一次：单问题/多问题并行检索复用，避免重复查 MySQL
    if user_id is not None and file_ids is None:
        async with async_session() as db:
            file_ids = await select_visible_file_ids(
                db,
                user_id,
                retrieve_own_private=retrieve_own_private,
                retrieve_own_public=retrieve_own_public,
                retrieve_kb_public=retrieve_kb_public,
                retrieve_owner_ids=retrieve_owner_ids,
            )

    # 问答环节：先把用户输入规范成可检索子问题列表
    questions = [q.strip() for q in (analysis.questions or []) if q.strip()]
    if not questions:
        questions = [analysis.query or content]
    if len(questions) > MAX_QUESTIONS:
        logger.warning("[chat] 子问题数量超过上限 %d，截断处理", MAX_QUESTIONS)
        questions = questions[:MAX_QUESTIONS]
    query = questions[0] if questions else (analysis.query or content)

    if len(questions) == 1:
        single_q = questions[0]
        hits = await search_documents(
            single_q, k=k, source=source, expr=expr, user_id=user_id, file_ids=file_ids,
            retrieve_own_private=retrieve_own_private,
            retrieve_own_public=retrieve_own_public,
            retrieve_kb_public=retrieve_kb_public,
            retrieve_owner_ids=retrieve_owner_ids,
        )
        all_hits = hits
        hits_by_question = [hits]
        user_prompt = _build_user_prompt(single_q, hits)
        sources = [
            {
                "index": index,
                "source": hit.get("source"),
                "score": hit.get("score"),
                "content": hit.get("content"),
            }
            for index, hit in enumerate(all_hits, start=1)
        ]
    else:
        # 多问题并行检索，减少整体响应时间
        hits_by_question = list(await asyncio.gather(
            *(
                search_documents(
                    q, k=k, source=source, expr=expr, user_id=user_id, file_ids=file_ids,
                    retrieve_own_private=retrieve_own_private,
                    retrieve_own_public=retrieve_own_public,
                    retrieve_kb_public=retrieve_kb_public,
                    retrieve_owner_ids=retrieve_owner_ids,
                )
                for q in questions
            )
        ))
        all_hits = [hit for q_hits in hits_by_question for hit in q_hits]
        user_prompt = _build_multi_user_prompt(questions, hits_by_question)
        sources = []
        index = 1
        for qi, q_hits in enumerate(hits_by_question, start=1):
            for hit in q_hits:
                sources.append({
                    "index": index,
                    "question": questions[qi - 1],
                    "source": hit.get("source"),
                    "score": hit.get("score"),
                    "content": hit.get("content"),
                })
                index += 1

    if not all_hits:
        answer = "知识库中未找到相关资料，请换个问法，或确认文档已入库。"
        await asyncio.to_thread(append_thread_exchange, thread_id, content, answer)
        return {
            "answer": answer,
            "intent": analysis.intent,
            "query": query,
            "sources": [],
            "hits": [],
        }

    # 回答缓存：同一 user_prompt（query + 相同参考资料）→ 复用 LLM 回答（TTL 1 小时）。
    # 缓存值改为 {answer, source_list}，便于按文档 source 精准失效。
    # 兼容旧缓存：如果缓存还是纯字符串，直接作为 answer 使用。
    ans_key = cache_key("ans", user_prompt)
    cached_answer = await cache_get(ans_key)
    if cached_answer is not None:
        answer = cached_answer.get("answer") if isinstance(cached_answer, dict) else cached_answer
        # 缓存命中未经过 LLM，手动补写对话记忆，保证会话历史完整
        await asyncio.to_thread(append_thread_exchange, thread_id, content, answer)
    else:
        try:
            answer = await asyncio.to_thread(
                ask,
                [HumanMessage(content=user_prompt)],
                thread_id,
            )
            await cache_set(
                ans_key,
                {
                    "answer": answer,
                    "source_list": [hit.get("source") for hit in all_hits],
                },
                ANSWER_CACHE_TTL,
            )
            await cache_index_sources(ans_key, [hit.get("source") for hit in all_hits])
        except Exception as exc:
            logger.exception("[chat] 问答模型调用失败")
            return {
                "answer": _friendly_model_error(exc),
                "intent": analysis.intent,
                "query": query,
                "sources": sources,
                "hits": all_hits,
                "error": "model_unavailable",
            }

    return {
        "answer": answer,
        "intent": analysis.intent,
        "query": query,
        "sources": sources,
        "hits": all_hits,
    }


async def chat_stream(
    content: str,
    thread_id: str = "default",
    session_id: Optional[str] = None,
    k: int = 3,
    source: Optional[str] = None,
    expr: Optional[str] = None,
    user_id: Optional[int] = None,
    retrieve_own_private: bool = True,
    retrieve_own_public: bool = True,
    retrieve_kb_public: bool = True,
    retrieve_owner_ids: Optional[List[int]] = None,
    file_ids: Optional[List[int]] = None,
    load_history: bool = True,
):
    """流式问答：analyze + 检索为前置步骤（普通 await），LLM 生成段逐 token 产出。

    产出事件（JSON dict，供 SSE 帧封装）：
      {"type": "meta",   "session_id", "intent", "query", "sources": [...]}  检索完成，即将开始生成
      {"type": "token",  "text": "..."}                        生成中的增量片段
      {"type": "done",   "answer": "完整回答"}                 生成结束
      {"type": "answer", "intent", "query", "answer", "sources"} 闲聊/other/无资料：一次性完整结果
      {"type": "error",  "message"}                            生成异常

    session_id 由 API 层生成并回传（meta 事件带上），前端据此维持多轮会话；
    与 chat() 共用 analyze/检索/缓存逻辑，仅 LLM 生成段改为流式。
    """
    # 新会话（load_history=False）没有 checkpoint，跳过历史读取省一次 Postgres 往返
    history = await asyncio.to_thread(load_recent_history, thread_id) if load_history else None
    analysis = await asyncio.to_thread(analyze, content, history)

    # 闲聊：不走检索，直接完整回答
    if analysis.intent == "chat":
        try:
            answer = await asyncio.to_thread(
                ask, [HumanMessage(content=content)], thread_id
            )
        except Exception as exc:
            # 与 chat() 的闲聊分支保持一致：模型调用异常转成 error 事件，
            # 前端可展示友好提示，而不是收到裸 500
            yield {"type": "error", "session_id": session_id,
                   "message": _friendly_model_error(exc)}
            return
        yield {"type": "answer", "session_id": session_id, "intent": "chat", "query": None,
               "answer": answer, "sources": []}
        return

    if analysis.intent == "other":
        answer = "当前仅支持知识库问答和闲聊，暂不支持文档上传、修改、删除等操作。"
        await asyncio.to_thread(append_thread_exchange, thread_id, content, answer)
        yield {"type": "answer", "session_id": session_id, "intent": "other", "query": None,
               "answer": answer,
               "sources": []}
        return

    # 可见文件 id 每个请求只查一次：单问题/多问题并行检索复用，避免重复查 MySQL
    if user_id is not None and file_ids is None:
        async with async_session() as db:
            file_ids = await select_visible_file_ids(
                db,
                user_id,
                retrieve_own_private=retrieve_own_private,
                retrieve_own_public=retrieve_own_public,
                retrieve_kb_public=retrieve_kb_public,
                retrieve_owner_ids=retrieve_owner_ids,
            )

    questions = [q.strip() for q in (analysis.questions or []) if q.strip()]
    if not questions:
        questions = [analysis.query or content]
    if len(questions) > MAX_QUESTIONS:
        logger.warning("[chat_stream] 子问题数量超过上限 %d，截断处理", MAX_QUESTIONS)
        questions = questions[:MAX_QUESTIONS]
    query = questions[0] if questions else (analysis.query or content)

    try:
        if len(questions) == 1:
            single_q = questions[0]
            hits = await search_documents(
                single_q, k=k, source=source, expr=expr, user_id=user_id, file_ids=file_ids,
                retrieve_own_private=retrieve_own_private,
                retrieve_own_public=retrieve_own_public,
                retrieve_kb_public=retrieve_kb_public,
                retrieve_owner_ids=retrieve_owner_ids,
            )
            all_hits = hits
            hits_by_question = [hits]
            user_prompt = _build_user_prompt(single_q, hits)
            sources = [
                {"index": index, "source": hit.get("source"),
                 "score": hit.get("score"), "content": hit.get("content")}
                for index, hit in enumerate(all_hits, start=1)
            ]
        else:
            # 多问题并行检索，减少整体响应时间
            hits_by_question = list(await asyncio.gather(
                *(
                    search_documents(
                        q, k=k, source=source, expr=expr, user_id=user_id, file_ids=file_ids,
                        retrieve_own_private=retrieve_own_private,
                        retrieve_own_public=retrieve_own_public,
                        retrieve_kb_public=retrieve_kb_public,
                        retrieve_owner_ids=retrieve_owner_ids,
                    )
                    for q in questions
                )
            ))
            all_hits = [hit for q_hits in hits_by_question for hit in q_hits]
            user_prompt = _build_multi_user_prompt(questions, hits_by_question)
            sources = []
            index = 1
            for qi, q_hits in enumerate(hits_by_question, start=1):
                for hit in q_hits:
                    sources.append({
                        "index": index,
                        "question": questions[qi - 1],
                        "source": hit.get("source"),
                        "score": hit.get("score"),
                        "content": hit.get("content"),
                    })
                    index += 1
    except Exception:
        # 检索失败（如 Milvus 不可用）：发固定提示，详细异常只写服务端日志。
        logger.exception("[chat] 流式检索失败")
        yield {
            "type": "error",
            "session_id": session_id,
            "message": "检索服务暂时不可用，请稍后重试",
        }
        return

    # meta 事件回传 session_id：前端据此维持多轮会话（服务端生成的 id 必须返回）
    yield {"type": "meta", "session_id": session_id, "intent": analysis.intent,
           "query": query, "questions": questions, "sources": sources}

    if not all_hits:
        answer = "知识库中未找到相关资料，请换个问法，或确认文档已入库。"
        await asyncio.to_thread(append_thread_exchange, thread_id, content, answer)
        yield {"type": "answer", "intent": analysis.intent, "query": query,
               "answer": answer,
               "sources": []}
        return

    # 回答缓存命中：直接回放完整答案（仍走 token 事件，前端体验一致）
    # 缓存值为 {answer, source_list}；兼容旧纯字符串缓存。
    ans_key = cache_key("ans", user_prompt)
    cached = await cache_get(ans_key)
    if cached is not None:
        cached_answer = cached.get("answer") if isinstance(cached, dict) else cached
        # 缓存命中未经过 LLM，手动补写对话记忆，保证会话历史完整
        await asyncio.to_thread(append_thread_exchange, thread_id, content, cached_answer)
        yield {"type": "token", "text": cached_answer}
        yield {"type": "done", "answer": cached_answer}
        return

    parts: List[str] = []
    try:
        async for token in astream([HumanMessage(content=user_prompt)], thread_id):
            parts.append(token)
            yield {"type": "token", "text": token}
    except Exception as exc:
        logger.exception("[chat] 流式模型调用失败")
        yield {"type": "error", "message": _friendly_model_error(exc)}
        return
    answer = "".join(parts)
    await cache_set(
        ans_key,
        {
            "answer": answer,
            "source_list": [hit.get("source") for hit in all_hits],
        },
        ANSWER_CACHE_TTL,
    )
    await cache_index_sources(ans_key, [hit.get("source") for hit in all_hits])
    yield {"type": "done", "answer": answer}
