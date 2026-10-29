"""Tool Dispatcher：单次调用的校验与执行（design/04）。

职责严格区分：Tool System 负责单次调用；Runtime（Scheduler）负责批次整体调度。
校验顺序（specs/02）：存在性 → Schema → 权限校验 → 预算（Scheduler 层）→ 执行。
Dispatcher 不直接调用 LLM 重新规划。

控制动作（finish / submit_review）由 Scheduler / Runner 在进入 Dispatcher 前拦截，
不进 ToolExecutor——这里保留防御性断言。
"""
import time
from typing import Protocol

from pydantic import ValidationError

from repofix.contracts import (
    AgentActor,
    ExecutionStatus,
    ToolCall,
    ToolErrorCode,
    ToolResult,
)
from repofix.tools.registry import ToolRegistry, ToolSideEffect

# 单次返回给模型的内容初始上限（specs/03：超限 truncated=true，保留开头 / 结尾 / 错误摘要）
DEFAULT_CONTENT_LIMIT_CHARS = 32 * 1024


class ToolExecutor(Protocol):
    """Workspace 执行能力抽象。M1 测试用 Fake；M2 替换为 DockerWorkspace 实现。"""

    def execute(self, tool_name: str, arguments: dict) -> ToolResult: ...


def truncate_content(content: str, limit: int) -> tuple[str, bool]:
    """超限截断：保留开头与结尾，中间以明确标记代替。"""
    if len(content) <= limit:
        return content, False
    head = content[: limit * 2 // 3]
    tail = content[-(limit // 3):] if limit >= 3 else ""
    marker = "\n...[truncated]...\n"
    return head + marker + tail, True


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        content_limit: int = DEFAULT_CONTENT_LIMIT_CHARS,
    ):
        self._registry = registry
        self._executor = executor
        self._content_limit = content_limit

    def validate(self, call: ToolCall, actor: AgentActor) -> ToolResult | None:
        """返回错误 ToolResult；None 表示通过校验（可进入意图持久化与执行）。"""
        spec = self._registry.get(call.tool_name)
        if spec is None:
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.TOOL_NOT_FOUND,
                error_message=(
                    f"unknown tool: {call.tool_name}; available tools: {', '.join(self._registry.names())}"
                ),
            )
        try:
            spec.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.INVALID_ARGUMENT,
                error_message=f"invalid arguments for {call.tool_name}: {exc.error_count()} error(s)",
            )
        if actor not in spec.allowed_actors:
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.PERMISSION_DENIED,
                error_message=f"actor {actor.value} is not allowed to call {call.tool_name}",
            )
        return None

    def execute(self, call: ToolCall) -> ToolResult:
        """执行单个已通过 validate() 校验的调用。副作用意图必须已由 Scheduler 持久化。"""
        spec = self._registry.get(call.tool_name)
        if spec is None or spec.side_effect is ToolSideEffect.CONTROL:
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.EXECUTION_ERROR,
                error_message="control actions are intercepted by Runtime, not dispatched",
            )
        started = time.perf_counter()
        try:
            result = self._executor.execute(tool_name=call.tool_name, arguments=call.arguments)
        except Exception as exc:  # noqa: BLE001 —— 执行器异常统一收敛为结构化 ToolResult
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.EXECUTION_ERROR,
                error_message=f"executor raised: {type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        result.call_id = call.call_id  # 执行器不感知 call_id，由 Dispatcher 统一绑定
        content, truncated = truncate_content(result.content, self._content_limit)
        result.content = content
        result.truncated = truncated or result.truncated
        result.duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
        return result
