"""意图规则层单元测试：问候 / 元问题 / 闲聊 / 正常问题分流。"""
import pytest

from rag个人知识库.agent.intent import Intent, analyze, classify_by_rules, _normalize_rule_text


class TestIntentRules:
    @pytest.mark.parametrize("text", [
        "你好", "您好呀", "谢谢！", "晚上好", "Hello", "在吗", "好的", "再见",
    ])
    def test_greeting_is_chat(self, text):
        assert classify_by_rules(text) == "chat"

    @pytest.mark.parametrize("text", [
        "我刚才问了什么", "你记得我之前说过什么吗", "回顾一下我们聊过什么", "之前问过的问题",
    ])
    def test_meta_conversation_is_chat(self, text):
        # 元问题必须走 agent 对话（记忆在 agent 侧），不能被判成 other 短路
        assert classify_by_rules(text) == "chat"

    @pytest.mark.parametrize("text", [
        "今天天气怎么样", "你是谁", "你会做什么", "你是什么模型",
    ])
    def test_small_talk_is_chat(self, text):
        assert classify_by_rules(text) == "chat"

    @pytest.mark.parametrize("text", [
        "LangChain 和 LangGraph 有什么区别？",
        "帮我上传一份文档",
        "什么是向量检索",
    ])
    def test_normal_questions_not_rule_hit(self, text):
        assert classify_by_rules(text) is None  # 交给 LLM

    def test_intent_model_supports_questions(self):
        intent = Intent(intent="rag_ask", query="LangChain 是什么", questions=["LangChain 是什么", "LangGraph 是什么"], confidence=0.9, reason="test")
        assert intent.questions == ["LangChain 是什么", "LangGraph 是什么"]

    def test_rule_chat_questions_empty(self):
        result = analyze("你好")
        assert result.intent == "chat"
        assert result.query is None
        assert result.questions == []

    def test_normalize_fullwidth_punct(self):
        assert _normalize_rule_text("你好！") == "你好!"
        assert _normalize_rule_text(" 谢谢  ") == "谢谢"
