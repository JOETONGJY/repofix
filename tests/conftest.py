"""测试辅助：ScriptedModelClient / FakeToolExecutor / FakeVerifier / 可控时钟。

两条 Fake 纪律（plan.md）：与真实组件同接口契约；仅放测试辅助代码，不建生产级 Mock 框架。
Fake 不生成 Trace / RunResult——这些必须由真实 Runtime 产生。
"""
import json

import pytest

from repofix.contracts import (
    ExecutionStatus,
    ModelRequest,
    ModelResponse,
    RunConfig,
    ToolCall,
    ToolResult,
    VerificationReport,
    VerificationRequest,
)
from repofix.tools.registry import ToolRegistry, register_default_tools


class FakeClock:
    """可控时钟：测试手动推进。"""

    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class ScriptedModelClient:
    """按脚本依次返回预设 ModelResponse；记录收到的请求供配对断言。"""

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("ScriptedModelClient script exhausted")
        return self._responses.pop(0)


class FakeToolExecutor:
    """与未来真实 Workspace 同接口。预设结果，不产生真实副作用。"""

    def __init__(self, results: dict[str, ToolResult] | None = None):
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []
        self.observers: list = []  # 执行瞬间回调（用于意图先行断言）

    def register(self, tool_name: str, result: ToolResult) -> None:
        self._results[tool_name] = result

    def execute(self, tool_name: str, arguments: dict) -> ToolResult:
        self.calls.append((tool_name, arguments))
        for observer in self.observers:
            observer(tool_name, arguments)
        template = self._results.get(tool_name)
        if template is None:
            # call_id 由 Dispatcher 统一绑定；Fake 用占位值构造
            return ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED, content=f"fake {tool_name} ok")
        return template.model_copy(update={"call_id": "pending"})

    def executed(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeVerifier:
    """按脚本返回 VerificationReport；记录收到的 VerificationRequest。"""

    def __init__(self, reports: list[VerificationReport]):
        self._reports = list(reports)
        self.requests: list[VerificationRequest] = []

    def verify(self, request: VerificationRequest) -> VerificationReport:
        self.requests.append(request)
        if not self._reports:
            raise AssertionError("FakeVerifier script exhausted")
        return self._reports.pop(0)


# ---- 构造辅助 ----


def make_tool_call(call_id: str, tool_name: str, **arguments) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, sort_keys=True),
    )


def model_response(
    decision_id: str,
    tool_calls: list[ToolCall] | None = None,
    text: str | None = None,
    usage: dict | None = None,
    finish_reason: str | None = None,
) -> ModelResponse:
    message = {"role": "assistant", "content": text} if text else None
    return ModelResponse(
        decision_id=decision_id,
        message=message,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=usage,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


@pytest.fixture
def config() -> RunConfig:
    return RunConfig(model_id="scripted-model", review_enabled=False)
