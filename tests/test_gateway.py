"""LLM Gateway 测试：GW-01~07（specs/04）。全部使用假传输层——零 Token 消耗。"""
import json

import pytest

from repofix.contracts import AgentActor, ExecutionStatus, ModelRequest, ToolResult
from repofix.gateway import (
    DeepSeekAdapter,
    ModelPermanentError,
    ModelProtocolError,
    ModelTransientError,
    TransportError,
)
from repofix.gateway.transport import TransportResult


def make_request(**overrides) -> ModelRequest:
    defaults = dict(
        actor=AgentActor.MAIN,
        model_id="deepseek-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        request_id="req_test",
    )
    defaults.update(overrides)
    return ModelRequest(**defaults)


class FakeTransport:
    """按脚本返回预设 TransportResult 或抛 TransportError；记录全部载荷。"""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def post(self, path: str, payload: dict, timeout: int) -> TransportResult:
        self.calls.append({"path": path, "payload": payload, "timeout": timeout})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_adapter(transport, calls=None, retry_limit=2) -> DeepSeekAdapter:
    attempts = calls if calls is not None else []
    return DeepSeekAdapter(
        transport=transport, on_attempt=lambda: attempts.append(1),
        sleep=lambda _s: None, retry_limit=retry_limit,
    )


def chat_body(tool_calls=None, content="working", finish_reason="tool_calls", usage=None,
             reasoning=None, model="deepseek-flash", resp_id="resp_1") -> str:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning:
        message["reasoning_content"] = reasoning
    body = {"id": resp_id, "model": model, "choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


class TestGw01MultipleToolCalls:
    def test_gw01_ids_order_and_raw_arguments_preserved(self):
        """GW-01：连续多个 Tool Call —— ID、顺序、参数（含 raw_arguments）正确保留。"""
        body = chat_body(tool_calls=[
            {"id": "call_a", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "search_code", "arguments": "{\"pattern\": \"def fix\", \"limit\": 50}"}},
        ])
        transport = FakeTransport([TransportResult(200, body)])
        adapter = make_adapter(transport)
        response = adapter.complete(make_request())
        assert [c.call_id for c in response.tool_calls] == ["call_a", "call_b"]  # 顺序保留
        assert [c.tool_name for c in response.tool_calls] == ["read_file", "search_code"]
        assert response.tool_calls[0].raw_arguments == '{"path": "a.py"}'  # raw_arguments 保留
        assert response.tool_calls[0].arguments == {"path": "a.py"}  # 解析结果可用
        assert response.finish_reason == "tool_calls"
        assert response.response_id == "resp_1"

    def test_payload_carries_messages_tools_and_model(self):
        transport = FakeTransport([TransportResult(200, chat_body(tool_calls=[]))])
        adapter = make_adapter(transport)
        adapter.complete(make_request(tools=[{"type": "function", "function": {"name": "read_file"}}],
                                      max_output_tokens=512, timeout_seconds=99))
        payload = transport.calls[0]["payload"]
        assert payload["model"] == "deepseek-flash"
        assert payload["stream"] is False
        assert payload["max_tokens"] == 512
        assert payload["tools"][0]["function"]["name"] == "read_file"
        assert transport.calls[0]["timeout"] == 99


class TestGw02InvalidArgumentsJson:
    def test_gw02_invalid_json_keeps_call_for_invalid_argument(self):
        """GW-02：参数无效 JSON —— 不丢弃调用；保留 call_id 与 raw_arguments，arguments 置空。"""
        body = chat_body(tool_calls=[
            {"id": "call_x", "type": "function",
             "function": {"name": "read_file", "arguments": "{path: a.py,,"}},
        ])
        adapter = make_adapter(FakeTransport([TransportResult(200, body)]))
        response = adapter.complete(make_request())
        call = response.tool_calls[0]
        assert call.call_id == "call_x"  # call_id 保留（后续生成关联的 INVALID_ARGUMENT）
        assert call.raw_arguments == "{path: a.py,,"  # 原始输出保留供诊断
        assert call.arguments == {}  # 未解析成功：空参数 → Tool System 校验层拒绝


class TestGw03CallIdIntegrity:
    def test_missing_call_id_rejects_whole_batch(self):
        body = chat_body(tool_calls=[
            {"id": "call_ok", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"type": "function", "function": {"name": "list_files", "arguments": "{}"}},  # 丢失 id
        ])
        adapter = make_adapter(FakeTransport([TransportResult(200, body)]))
        with pytest.raises(ModelProtocolError) as excinfo:
            adapter.complete(make_request())
        assert "missing call_id" in str(excinfo.value)

    def test_duplicate_call_id_rejects_whole_batch(self):
        body = chat_body(tool_calls=[
            {"id": "call_dup", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "call_dup", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
        ])
        adapter = make_adapter(FakeTransport([TransportResult(200, body)]))
        with pytest.raises(ModelProtocolError) as excinfo:
            adapter.complete(make_request())
        assert "duplicate call_id" in str(excinfo.value)


class TestGw04IncompleteResponse:
    def test_truncated_output_rejected_for_protocol_correction(self):
        """GW-04：达到输出长度限制 → 不接纳部分内容，进入有限协议纠正。"""
        body = chat_body(tool_calls=[], content="partial...", finish_reason="length")
        adapter = make_adapter(FakeTransport([TransportResult(200, body)]))
        with pytest.raises(ModelProtocolError) as excinfo:
            adapter.complete(make_request())
        assert "truncated" in str(excinfo.value)

    def test_empty_choices_rejected(self):
        adapter = make_adapter(FakeTransport([TransportResult(200, json.dumps({"choices": []}))]))
        with pytest.raises(ModelProtocolError):
            adapter.complete(make_request())


class TestGw05ProviderMetadata:
    def test_reasoning_content_preserved_for_next_request(self):
        """GW-05：供应商续接字段（reasoning_content）保存到 provider_metadata，
        Context 下一轮请求原样带回，不被丢弃。"""
        from repofix.context.builder import ContextBuilder

        body = chat_body(tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ], reasoning="let me think about auth.py")
        adapter = make_adapter(FakeTransport([TransportResult(200, body)]))
        response = adapter.complete(make_request())
        assert response.provider_metadata["reasoning_content"] == "let me think about auth.py"

        builder = ContextBuilder("rules")
        builder.start_task("task")
        builder.record_response(response)
        builder.record_results(
            [ToolResult(call_id="call_1", status=ExecutionStatus.SUCCEEDED, content="data")],
            response.tool_calls,
        )
        next_messages = builder.next_messages()
        assistant = [m for m in next_messages if m.get("role") == "assistant"][0]
        assert assistant["reasoning_content"] == "let me think about auth.py"  # 下一轮请求中保留


class TestGw06RetryAndBudget:
    def test_retryable_5xx_then_success_with_attempts_counted(self):
        """GW-06：5xx 退避重试成功；每次真实尝试计入预算。"""
        script = [
            TransportResult(503, "server busy"),
            TransportResult(429, "rate limited"),
            TransportResult(200, chat_body(tool_calls=[])),
        ]
        transport = FakeTransport(script)
        attempts: list[int] = []
        adapter = DeepSeekAdapter(transport=transport, on_attempt=lambda: attempts.append(len(attempts) + 1),
                                  sleep=lambda _s: None, retry_limit=2)
        response = adapter.complete(make_request())
        assert response.finish_reason == "tool_calls"
        assert len(attempts) == 3  # 每次真实 API 尝试都计数
        assert len(transport.calls) == 3

    def test_retry_after_header_respected(self):
        waited: list[float] = []
        transport = FakeTransport([
            TransportResult(429, "slow down", headers={"Retry-After": "7"}),
            TransportResult(200, chat_body(tool_calls=[])),
        ])
        adapter = DeepSeekAdapter(transport=transport, on_attempt=lambda: None,
                                  sleep=waited.append, retry_limit=2)
        adapter.complete(make_request())
        assert waited == [7.0]  # 尊重 Retry-After

    def test_retry_limit_exhausted_raises_transient(self):
        transport = FakeTransport([TransportResult(503, "down")] * 3)
        adapter = make_adapter(transport, retry_limit=2)
        with pytest.raises(ModelTransientError):
            adapter.complete(make_request())
        assert len(transport.calls) == 3  # 1 次初始 + 2 次重试

    def test_auth_failure_is_permanent_no_retry(self):
        transport = FakeTransport([TransportResult(401, '{"error": "bad key"}')])
        adapter = make_adapter(transport)
        with pytest.raises(ModelPermanentError):
            adapter.complete(make_request())
        assert len(transport.calls) == 1  # 不重试

    def test_transport_error_retried_then_transient(self):
        transport = FakeTransport([TransportError("conn reset")] * 3)
        adapter = make_adapter(transport, retry_limit=2)
        with pytest.raises(ModelTransientError):
            adapter.complete(make_request())
        assert len(transport.calls) == 3


class TestGw07Usage:
    def test_usage_normalized_when_present(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        adapter = make_adapter(FakeTransport([TransportResult(200, chat_body(usage=usage))]))
        response = adapter.complete(make_request())
        assert response.usage["total_tokens"] == 15

    def test_gw07_missing_usage_marked_unknown_not_fabricated(self):
        """GW-07：Usage 缺失 → 标记未知（None），不虚构 Token 和费用。"""
        adapter = make_adapter(FakeTransport([TransportResult(200, chat_body(usage=None))]))
        response = adapter.complete(make_request())
        assert response.usage is None


class TestRunnerGatewayIntegration:
    def test_protocol_error_gets_finite_correction_then_recovers(self, registry, config):
        """Gateway 协议错误 → MODEL_RETRY 一次 → 恢复正常（RT-01/13 同策略）。"""
        from repofix.contracts import (
            EventType,
            PatchStatus,
            RunStatus,
            TestStatus,
            VerificationOutcome,
            VerificationReport,
        )
        from repofix.gateway import ModelProtocolError
        from repofix.runtime.runner import AgentRunner
        from repofix.state import SQLiteEventStore
        from repofix.tools.dispatcher import ToolDispatcher
        from tests.conftest import FakeClock, FakeToolExecutor, FakeVerifier, ScriptedModelClient

        responses = [
            ModelProtocolError("response truncated"),
            model_response("d2", [make_tool_call("f1", "finish", summary="done")]),
        ]
        store = SQLiteEventStore(":memory:")
        report = VerificationReport(
            candidate_revision="m1", patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED,
        )
        runner = AgentRunner(
            run_id="run_gw", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(responses), registry=registry,
            dispatcher=ToolDispatcher(registry, FakeToolExecutor()), store=store,
            verifier=FakeVerifier([report]), clock=FakeClock(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        types = [e.type for e in store.get_events("run_gw")]
        assert types.count(EventType.MODEL_RETRY) == 1

    def test_attempt_counting_not_duplicated_for_gateway_clients(self, registry, config):
        """counts_own_attempts 标记：Runner 不预计数，避免与 Gateway 钩子双重统计。"""
        from repofix.contracts import RunStatus
        from repofix.gateway import ModelTransientError
        from repofix.runtime.runner import AgentRunner
        from repofix.state import SQLiteEventStore
        from repofix.tools.dispatcher import ToolDispatcher
        from tests.conftest import FakeClock, FakeToolExecutor, FakeVerifier, ScriptedModelClient

        class MarkedScriptedClient(ScriptedModelClient):
            counts_own_attempts = True
            counted = 0

            def complete(self, request):
                self.counted += 1
                raise ModelTransientError("down")

        client = MarkedScriptedClient([])
        store = SQLiteEventStore(":memory:")
        runner = AgentRunner(
            run_id="run_cnt", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=client, registry=registry,
            dispatcher=ToolDispatcher(registry, FakeToolExecutor()), store=store,
            verifier=FakeVerifier([]), clock=FakeClock(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.FAILED
        assert result.model_calls == 0  # Runner 未预计数（标记生效）


from repofix.contracts import TaskSpec  # noqa: E402 —— 供 Runner 集成测试使用
from tests.test_runner_e2e import TASK_ARGS, make_tool_call, model_response  # noqa: E402
