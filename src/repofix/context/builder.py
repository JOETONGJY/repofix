"""Context Engine（M1 最小版）：消息构造，保证下一轮请求包含正确的 Tool Call/Result 配对。

完整 Context Engine（Token Budget、轮次裁剪、Working Memory、文件版本失效）在 M3 实现；
本模块先固化配对完整性约束（specs/02 Context Policy：Tool Call 与 Tool Result 严格配对，
不得拆散）——这是 M1 端到端闭环「配对可见性」验证的基础。
"""
import json

from repofix.contracts import (
    ExecutionStatus,
    ModelResponse,
    ToolCall,
    ToolResult,
    VerificationReport,
)


class ContextPairingError(ValueError):
    """配对被破坏：assistant 的 tool_calls 与 tool 结果不一一对应。"""


def render_tool_result(result: ToolResult) -> str:
    """ToolResult → 模型可见文本。SKIPPED 必须带原因；FAILED 带错误码与消息。"""
    if result.status is ExecutionStatus.SKIPPED:
        return f"[skipped] reason={result.skipped_reason}"
    if result.status is ExecutionStatus.FAILED:
        parts = [f"[error] code={result.error_code.value if result.error_code else 'unknown'}"]
        if result.error_message:
            parts.append(result.error_message)
        return " ".join(parts)
    if result.exit_code is not None:
        return f"{result.content}\n[exit_code={result.exit_code}]"
    return result.content


def render_verification_feedback(report: VerificationReport) -> str:
    """Final Verification 失败反馈（RT-07）：回流给模型继续修复。"""
    lines = ["Final Verification FAILED."]
    if report.failed_tests:
        lines.append("Failed tests: " + ", ".join(report.failed_tests))
    if report.summary:
        lines.append(report.summary)
    return "\n".join(lines)


class ContextBuilder:
    """维护模型可见消息历史（视图），不是执行事实记录（事实在 EventStore）。"""

    def __init__(self, system_rules: str):
        self._messages: list[dict] = [{"role": "system", "content": system_rules}]

    def start_task(self, instruction: str) -> None:
        self._messages.append({"role": "user", "content": instruction})

    def next_messages(self) -> list[dict]:
        """当前完整消息历史（副本）。M3 在此处接入 Token Budget 与轮次裁剪。"""
        return [dict(message) for message in self._messages]

    def record_response(self, response: ModelResponse) -> None:
        """记录 assistant 消息：文本 + 本轮全部 tool_calls（按模型协议结构保留）。

        供应商续接字段（如 DeepSeek thinking mode 的 reasoning_content）必须原样
        保留在 assistant 消息中，下一轮请求按原生协议带回（GW-05，design/03 硬约束）。
        """
        entry: dict = {"role": "assistant"}
        if response.message and response.message.get("content"):
            entry["content"] = response.message["content"]
        reasoning = response.provider_metadata.get("reasoning_content")
        if reasoning:
            entry["reasoning_content"] = reasoning
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.tool_name,
                        "arguments": call.raw_arguments
                        if call.raw_arguments is not None
                        else json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                    },
                }
                for call in response.tool_calls
            ]
            entry.setdefault("content", None)
        self._messages.append(entry)

    def record_results(self, results: list[ToolResult], expected_calls: list[ToolCall]) -> None:
        """记录工具结果：与最近一次 assistant 的 tool_calls 严格一一配对。"""
        expected_ids = [call.call_id for call in expected_calls]
        result_ids = [result.call_id for result in results]
        if sorted(expected_ids) != sorted(result_ids) or len(expected_ids) != len(result_ids):
            raise ContextPairingError(
                f"tool result pairing broken: expected {expected_ids}, got {result_ids}"
            )
        for result in results:
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": render_tool_result(result),
                }
            )

    def record_protocol_error(self, detail: str) -> None:
        """协议错误纠正（RT-01/13）：携带错误信息重新请求一次。"""
        self._messages.append(
            {
                "role": "user",
                "content": f"Protocol error: {detail} Respond with tool calls or finish.",
            }
        )

    def record_verification_feedback(self, report: VerificationReport) -> None:
        self._messages.append(
            {"role": "user", "content": render_verification_feedback(report)}
        )
