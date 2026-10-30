"""Tool Dispatcher：单次调用的校验与执行（design/04 V1.1）。

职责严格区分：Tool System 负责单次调用；Runtime（Scheduler）负责批次整体调度。
校验顺序（specs/02 V0.2 / specs/03 V0.3）：存在性 → 权限校验 → Schema 校验 →
语义前置条件 → 预算（Scheduler 层）→ 执行。无权限调用不解析参数——拒绝结果
不受参数内容影响（Reviewer 请求 apply_patch 无论参数是否畸形一律 PERMISSION_DENIED）。
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

# 单次返回给模型的内容硬上限：32 KiB，按 UTF-8 字节计（specs/03 V0.3）；
# 截断标记计入上限，最终输出（含标记）不得越界。
DEFAULT_CONTENT_LIMIT_BYTES = 32 * 1024

_TRUNCATION_MARKER = "\n...[truncated]...\n"


class ToolExecutor(Protocol):
    """Workspace 执行能力抽象。M1 测试用 Fake；M2 替换为 DockerWorkspace 实现。

    call_id 由 Scheduler 传入：实现方据此保存受限大小的全量输出产物
    （specs/01：stdout / stderr 全文存 SQLite，不进模型上下文）。
    """

    def execute(self, tool_name: str, arguments: dict, call_id: str = "") -> ToolResult: ...


class FullOutputSource(Protocol):
    """可选能力：按 call_id 取回工具全量输出（stdout, stderr），供持久化。"""

    def pop_full_output(self, call_id: str) -> tuple[str, str] | None: ...


class ExecutorCrashError(RuntimeError):
    """执行器在执行过程中抛出异常——副作用是否已发生无法确认（specs/02 V0.2 规则 8）。

    不能仅凭异常认定没有副作用：调用方（Scheduler）必须将持久化状态记为
    UNKNOWN 并终止执行，不自动重试。
    """

    def __init__(self, tool_name: str, cause: Exception):
        self.tool_name = tool_name
        self.cause = cause
        super().__init__(f"executor raised during {tool_name}: {type(cause).__name__}: {cause}")


def truncate_content(content: str, limit_bytes: int = DEFAULT_CONTENT_LIMIT_BYTES) -> tuple[str, bool]:
    """按 UTF-8 字节截断：保留开头与结尾，中间以明确标记代替，总输出不越界。"""
    encoded = content.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return content, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    remaining = limit_bytes - len(marker)
    if remaining <= 0:
        # 上限连标记都放不下：返回空内容（仍如实标记截断）
        return "", True
    head_len = remaining * 2 // 3
    tail_len = remaining - head_len
    head = encoded[:head_len].decode("utf-8", errors="ignore")
    tail = encoded[-tail_len:].decode("utf-8", errors="ignore")
    return head + _TRUNCATION_MARKER + tail, True


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        content_limit_bytes: int = DEFAULT_CONTENT_LIMIT_BYTES,
    ):
        self._registry = registry
        self._executor = executor
        self._content_limit_bytes = content_limit_bytes

    def validate(self, call: ToolCall, actor: AgentActor) -> ToolResult | None:
        """返回错误 ToolResult；None 表示通过校验（可进入意图持久化与执行）。

        顺序（V1.1）：存在性 → 权限 → Schema。无权限调用不解析参数。
        """
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
        if actor not in spec.allowed_actors:
            return ToolResult(
                call_id=call.call_id,
                status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.PERMISSION_DENIED,
                error_message=f"actor {actor.value} is not allowed to call {call.tool_name}",
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
        return None

    def execute(self, call: ToolCall) -> ToolResult:
        """执行单个已通过 validate() 校验的调用。副作用意图必须已由 Scheduler 持久化。

        执行器抛出异常时 raising ExecutorCrashError（副作用未知），
        不将其降级为普通 FAILED 结果。
        """
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
            result = self._executor.execute(tool_name=call.tool_name, arguments=call.arguments, call_id=call.call_id)
        except Exception as exc:  # noqa: BLE001 —— 异常必须上抛为副作用未知，不能伪装成确定失败
            raise ExecutorCrashError(call.tool_name, exc) from exc
        result.call_id = call.call_id  # 执行器不感知 call_id，由 Dispatcher 统一绑定
        content, truncated = truncate_content(result.content, self._content_limit_bytes)
        result.content = content
        result.truncated = truncated or result.truncated
        result.duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
        return result

    def drain_full_output(self, call_id: str) -> tuple[str, str] | None:
        """委托给执行器的可选全量输出源（stdout / stderr 全文，供 SQLite 持久化）。"""
        source = getattr(self._executor, "pop_full_output", None)
        if callable(source):
            return source(call_id)
        return None
