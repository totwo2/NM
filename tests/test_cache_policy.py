"""
缓存策略测试
覆盖: 工具调用结果不可缓存、纯 LLM 生成可缓存
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 直接复用 web.server 的 _is_cacheable_result
from web.server import _is_cacheable_result  # noqa: E402


class TestCachePolicy:
    def test_pure_llm_cacheable(self):
        result = {
            "text": "人工智能是...",
            "tool_calls": [],
            "model": "agnes-2.5-flash",
        }
        assert _is_cacheable_result(result) is True

    def test_tool_call_not_cacheable(self):
        result = {
            "text": "请假申请已提交",
            "tool_calls": [{"name": "oa_start"}],
            "model": "agnes-2.5-flash",
        }
        assert _is_cacheable_result(result) is False

    def test_empty_text_not_cacheable(self):
        assert _is_cacheable_result({"text": "", "tool_calls": []}) is False

    def test_unknown_model_not_cacheable(self):
        result = {"text": "x", "tool_calls": [], "model": "unknown"}
        assert _is_cacheable_result(result) is False

    def test_no_tool_calls_key_cacheable(self):
        # AgentLoop 始终返回 tool_calls 键；缺失时按空列表处理（可缓存）
        assert _is_cacheable_result({"text": "x"}) is True