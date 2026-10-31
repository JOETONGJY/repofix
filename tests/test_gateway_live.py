"""Gateway 真实协议兼容测试（integration 标记 + DEEPSEEK_API_KEY 门控）。

仅在显式运行（pytest -m integration）且配置了 Key 时执行；
单次消耗控制在百级 tokens（max_output_tokens=60，design/03 协议兼容测试）。
"""
import os

import pytest

from repofix.contracts import AgentActor, ModelRequest
from repofix.gateway import DeepSeekAdapter, ModelProtocolError, ModelTransientError

pytestmark = pytest.mark.integration

# 双重门控（花费纪律）：integration 标记之外，还必须显式设置 REPOFIX_LIVE_PROTOCOL_TEST=1
# 才会真实调用 API（每次约百级 tokens）。默认的全量回归永远不花钱。
if not (os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("REPOFIX_LIVE_PROTOCOL_TEST") == "1"):
    pytest.skip(
        allow_module_level=True,
        reason="live protocol test requires DEEPSEEK_API_KEY + REPOFIX_LIVE_PROTOCOL_TEST=1",
    )


def test_live_deepseek_tool_calling_round_trip():
    """真实端到端：一次带工具的最小请求返回可解析的 tool_calls（或优雅的协议错误）。"""
    adapter = DeepSeekAdapter(api_key=os.environ["DEEPSEEK_API_KEY"], retry_limit=1)
    request = ModelRequest(
        actor=AgentActor.MAIN,
        model_id="deepseek-flash",
        messages=[{"role": "user", "content": "Read the file src/auth.py using the tool."}],
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a text file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }],
        max_output_tokens=60,
        timeout_seconds=60,
        request_id="req_live_smoke",
    )
    try:
        response = adapter.complete(request)
    except (ModelProtocolError, ModelTransientError) as exc:
        pytest.fail(f"live protocol compatibility failed: {exc}")
    assert response.decision_id
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls, "expected the model to call read_file"
    call = response.tool_calls[0]
    assert call.tool_name == "read_file"
    assert "path" in call.arguments
    if response.usage:
        assert response.usage.get("total_tokens", 0) < 500  # 成本护栏
