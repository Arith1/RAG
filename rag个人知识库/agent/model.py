"""对话模型创建与进程内复用：DeepSeek 主模型 + DashScope 千问降级。

- Temperature 统一由 MODEL_TEMPERATURE 控制（默认 0.7），主/降级模型共用。
- 创建主模型后先发一次最小请求验证连通性；DeepSeek 不可用时自动降级为
  .env 配置的 DashScope 千问（QWEN_MODEL，默认 qwen3.7-flash），保证问答链路可用。
- 降级是进程级的一次性决定：重启进程后会重新尝试 DeepSeek 主模型。
- 两级都不可用且未配置千问时，首次调用抛出原始异常（由上层转为用户可读提示）。
"""
import logging
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# ── 主模型：DeepSeek ──
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
DEEPSEEK_MAX_RETRIES = int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))

# ── 降级模型：DashScope 千问（OpenAI 兼容端点，复用 .env 里的 DASHSCOPE_* 配置）──
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.7-flash")
QWEN_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "")
QWEN_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

# 采样温度：主模型与降级模型统一使用（默认 0.7）
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.7"))

_chat_model = None


def _create_deepseek():
    return init_chat_model(
        model=DEEPSEEK_MODEL,
        model_provider="deepseek",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        extra_body={"thinking": {"type": "disabled"}},
        temperature=MODEL_TEMPERATURE,
        timeout=DEEPSEEK_TIMEOUT,
        max_retries=DEEPSEEK_MAX_RETRIES,
    )


def _create_qwen():
    # DashScope 兼容模式：qwen3 系混合思考模型在非流式调用下必须显式关闭 thinking
    return init_chat_model(
        model=QWEN_MODEL,
        model_provider="openai",
        api_key=QWEN_API_KEY,
        base_url=QWEN_BASE_URL,
        extra_body={"enable_thinking": False},
        temperature=MODEL_TEMPERATURE,
        timeout=DEEPSEEK_TIMEOUT,
        max_retries=DEEPSEEK_MAX_RETRIES,
    )


def _ping(model) -> None:
    """最小连通性验证：真实请求但只生成 1 个 token，失败时抛异常。"""
    model.bind(max_tokens=1).invoke("ping")


def get_chat_model():
    """加载对话模型（进程内复用单例），创建后先做连通性自检。

    顺序：DeepSeek 主模型 → 连通失败自动降级 DashScope 千问；
    降级模型同样不可用则抛出异常，不静默返回坏模型。
    """
    global _chat_model
    if _chat_model is not None:
        return _chat_model

    try:
        primary = _create_deepseek()
        _ping(primary)
        _chat_model = primary
        logger.info(
            "[model] DeepSeek(%s) 连通性验证通过（temperature=%s）",
            DEEPSEEK_MODEL, MODEL_TEMPERATURE,
        )
        return _chat_model
    except Exception as exc:
        if not (QWEN_API_KEY and QWEN_BASE_URL):
            logger.error(
                "[model] DeepSeek(%s) 连通失败且未配置 DASHSCOPE_BASE_URL/DASHSCOPE_API_KEY，无法降级：%s",
                DEEPSEEK_MODEL, exc,
            )
            raise
        logger.warning(
            "[model] DeepSeek(%s) 连通失败（%s），降级为千问 %s",
            DEEPSEEK_MODEL, exc, QWEN_MODEL,
        )

    fallback = _create_qwen()
    _ping(fallback)
    _chat_model = fallback
    logger.info(
        "[model] 已降级为千问模型 %s（temperature=%s）",
        QWEN_MODEL, MODEL_TEMPERATURE,
    )
    return _chat_model
