"""多问题编排 prompt 纯逻辑单元测试。"""
from rag个人知识库.service.chat import _build_multi_user_prompt


class TestBuildMultiUserPrompt:
    def test_contains_questions_and_sources(self):
        prompt = _build_multi_user_prompt(
            ["LangChain 是什么", "LangGraph 是什么"],
            [
                [{"source": "uploads/1/a.txt", "content": "LangChain 是..."}],
                [{"source": "uploads/2/b.txt", "content": "LangGraph 是..."}],
            ],
        )
        assert "1. LangChain 是什么" in prompt
        assert "2. LangGraph 是什么" in prompt
        assert "uploads/1/a.txt" in prompt
        assert "uploads/2/b.txt" in prompt

    def test_empty_hits_note(self):
        prompt = _build_multi_user_prompt(
            ["没有资料的问题"],
            [[]],
        )
        assert "没有检索到相关资料" in prompt