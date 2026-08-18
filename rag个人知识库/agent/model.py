"""DeepSeek 模型创建与进程内复用。"""
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(override=True)

# 模型名可通过环境变量 DEEPSEEK_MODEL 覆盖
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
DEEPSEEK_MAX_RETRIES = int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))

_chat_model = None


def get_chat_model():
    """加载 DeepSeek 对话模型，进程内复用单例。"""
    global _chat_model
    if _chat_model is None:
        _chat_model = init_chat_model(
            model=DEEPSEEK_MODEL,
            model_provider="deepseek",
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            extra_body={"thinking": {"type": "disabled"}},
            timeout=DEEPSEEK_TIMEOUT,
            max_retries=DEEPSEEK_MAX_RETRIES,
        )
    return _chat_model
