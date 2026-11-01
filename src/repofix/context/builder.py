"""Context Engine（M3 完整版）：模型可见上下文构造（design/07、specs/02 Context Policy）。

核心概念：模型上下文是**构造的视图**，不是截断的日志。三层组装：
  System Rules + Task（固定保留）→ Working Memory（每次重新生成的视图）→ 完整模型—工具轮次。
裁剪以**完整轮次**为单位（CX-05）：要么保留整组配对消息，要么整体移除——
绝不出现只有 Result 没有对应 Call 的孤儿消息（CX-02）。

超预算（CX-04）：先截断冗长工具输出（双层限制：Dispatcher 32KiB 硬上限之外，
Context 层按 RunConfig.tool_output_truncate_chars 软预算，read_file 给予更高配额、
命令输出保留错误尾部），再整体移除较早轮次；紧急压缩一次后仍超限 →
ContextBudgetExceeded（Runtime 记 CONTEXT_BUDGET_EXCEEDED 并受控失败），无静默截断。

压缩只改变模型视图，不删除运行事实（规则 B）——SQLite 原始事件完整保留。
"""
import re

from repofix.context.tokens import TokenBudgetManager, estimate_static_overhead
from repofix.context.working_memory import render_working_memory
from repofix.contracts import ExecutionStatus, ModelResponse, ToolCall, ToolResult, WorkingState

# read_file 输出尾部的版本元数据（Workspace 契约）：Context 裁剪正文时必须保留（评审 P0-1）
_READ_METADATA_RE = re.compile(r"(\n\[lines \d+-\d+ of \d+\])?(\n\[sha256=[0-9a-fA-F]{64}\])\s*$")


class ContextPairingError(ValueError):
    """配对被破坏：assistant 的 tool_calls 与 tool 结果不一一对应。"""


class ContextBudgetExceeded(RuntimeError):
    """紧急压缩一次后仍超出上下文预算：受控失败，无静默截断（CX-04）。"""


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


def render_verification_feedback(report) -> str:
    """Final Verification 失败反馈（RT-07）：回流给模型继续修复。"""
    lines = ["Final Verification FAILED."]
    if report.failed_tests:
        lines.append("Failed tests: " + ", ".join(report.failed_tests))
    if report.summary:
        lines.append(report.summary)
    return "\n".join(lines)


def _trim_output(content: str, cap_chars: int, keep_tail: bool) -> str:
    """按字符软预算截断；keep_tail=True 保留尾部（命令输出错误通常在末尾）。"""
    if len(content) <= cap_chars:
        return content
    omitted = len(content) - cap_chars
    marker = f"\n[...context budget: {omitted} chars trimmed...]\n"
    if keep_tail:
        return marker + content[-cap_chars:]
    return content[:cap_chars] + marker


# Context 层软预算系数（specs/03 V0.3：按工具类型的重要性差异调整，不做无差别截断）
_SOFT_CAP_MULTIPLIERS = {
    "read_file": 4,  # 源码内容价值高：允许更高配额，保留开头
    "run_command": 1,  # Shell 输出冗长：严格软预算，保留错误尾部
    "search_code": 2,
    "list_files": 2,
}
_EMERGENCY_CAP_CHARS_FLOOR = 200


class _Turn:
    """一个完整模型—工具轮次：1 条 assistant 消息 + 对应的全部 tool 结果消息。

    配对按 call_id 映射建立（评审 P1-2）：渲染顺序跟随原始 Tool Call 顺序，
    不依赖结果列表的排列顺序——截断策略因此始终作用于正确的工具类型。
    """

    def __init__(self, response: ModelResponse, results: list[ToolResult]):
        self.response = response
        self.results_by_id: dict[str, ToolResult] = {result.call_id: result for result in results}

    def render(self, soft_chars: int, emergency: bool = False) -> list[dict]:
        response = self.response
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
                        else json_dumps(call.arguments),
                    },
                }
                for call in response.tool_calls
            ]
            entry.setdefault("content", None)
        messages = [entry]
        for call in response.tool_calls:
            result = self.results_by_id[call.call_id]  # record_results 已校验齐全
            text = render_tool_result(result)
            body, metadata = _split_read_metadata(text, call.tool_name)
            if emergency:
                cap = max(_EMERGENCY_CAP_CHARS_FLOOR, soft_chars // 4)
                text = _trim_output(body, cap, keep_tail=True) + metadata
            else:
                multiplier = _SOFT_CAP_MULTIPLIERS.get(call.tool_name, 2)
                keep_tail = call.tool_name in ("run_command", "search_code")
                text = _trim_output(body, soft_chars * multiplier, keep_tail=keep_tail) + metadata
            messages.append(
                {"role": "tool", "tool_call_id": result.call_id, "content": text}
            )
        return messages


def _split_read_metadata(text: str, tool_name: str) -> tuple[str, str]:
    """拆分 read_file 输出尾部的版本元数据（[lines …] / [sha256=…]）。

    元数据必须随裁剪后的正文一起回到模型可见内容（评审 P0-1）——否则模型
    看得到源码却拿不到 apply_patch 所需的 expected_sha256。其他工具无此元数据。
    """
    if tool_name != "read_file":
        return text, ""
    match = _READ_METADATA_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()], match.group(0)


def json_dumps(arguments: dict) -> str:
    import json

    return json.dumps(arguments, ensure_ascii=False, sort_keys=True)


class ContextBuilder:
    """维护模型可见消息视图。事实记录在 EventStore，这里只是视图（G4 分离）。"""

    def __init__(
        self,
        system_rules: str,
        *,
        context_window_tokens: int | None = None,
        output_reserve_tokens: int = 4096,
        tool_output_soft_chars: int = 2000,
        tools_payload: list[dict] | None = None,
    ):
        self._system_rules = system_rules
        self._window = context_window_tokens
        self._reserve = output_reserve_tokens
        self._soft_chars = tool_output_soft_chars
        self._budget = None
        if context_window_tokens is not None:
            # 工具 Schema 属于最终 ModelRequest 的固定开销，计入预算（评审 P0-2）
            static_overhead = estimate_static_overhead(tools_payload or [])
            self._budget = TokenBudgetManager(
                context_window_tokens, output_reserve_tokens, static_overhead_tokens=static_overhead
            )
        self._task: dict | None = None
        self._task_goal = ""
        self._base_commit = ""
        self._entries: list[tuple[str, object]] = []  # ("turn", _Turn) | ("note", dict)
        self._last_read_hashes: dict[str, str | None] = {}
        self._current_hashes: dict[str, str | None] = {}
        # 操作序号（评审 P1-1）：run_command 可能修改任意文件——
        # 命令之后未重读的文件一律视为过期，不再标记 confirmed current
        self._op_counter = 0
        self._last_read_op: dict[str, int] = {}
        self._last_command_op = 0

    def start_task(self, instruction: str, base_commit: str = "") -> None:
        self._task = {"role": "user", "content": instruction}
        self._task_goal = instruction
        self._base_commit = base_commit

    # ---- 记录接口 ----

    def record_response(self, response: ModelResponse) -> None:
        if response.tool_calls:
            # 先占位：轮次在 record_results 中补全配对
            self._entries.append(("pending_turn", response))
        else:
            entry: dict = {"role": "assistant"}
            if response.message and response.message.get("content"):
                entry["content"] = response.message["content"]
            reasoning = response.provider_metadata.get("reasoning_content")
            if reasoning:
                entry["reasoning_content"] = reasoning
            self._entries.append(("note", entry))

    def record_results(self, results: list[ToolResult], expected_calls: list[ToolCall]) -> None:
        expected_ids = [call.call_id for call in expected_calls]
        result_ids = [result.call_id for result in results]
        if sorted(expected_ids) != sorted(result_ids) or len(expected_ids) != len(result_ids):
            raise ContextPairingError(
                f"tool result pairing broken: expected {expected_ids}, got {result_ids}"
            )
        self._track_file_versions(expected_calls, results)
        if self._entries and self._entries[-1][0] == "pending_turn":
            response = self._entries.pop()[1]
            self._entries.append(("turn", _Turn(response, results)))
        else:
            raise ContextPairingError("tool results recorded without a preceding response")

    def record_protocol_error(self, detail: str) -> None:
        """协议错误纠正（RT-01/13）：携带错误信息重新请求一次。"""
        self._entries.append(
            ("note", {"role": "user", "content": f"Protocol error: {detail} Respond with tool calls or finish."})
        )

    def record_verification_feedback(self, report) -> None:
        self._entries.append(("note", {"role": "user", "content": render_verification_feedback(report)}))

    # ---- 视图构造 ----

    def next_messages(self, working_state: WorkingState | None = None) -> list[dict]:
        """构造本轮模型可见消息（每次调用重新生成视图；裁剪不改写历史记录）。"""
        head: list[dict] = [{"role": "system", "content": self._system_rules}]
        if self._task is not None:
            head.append(dict(self._task))
        if working_state is not None:
            head.append(
                {
                    "role": "user",
                    "content": render_working_memory(
                        task_goal=self._task_goal,
                        base_commit=self._base_commit,
                        modified_files=list(working_state.modified_files),
                        last_read_hashes=dict(self._last_read_hashes),
                        current_hashes=dict(self._current_hashes),
                        last_read_ops=dict(self._last_read_op),
                        last_command_op=self._last_command_op,
                        failed_tests=list(working_state.failed_tests),
                        unresolved_errors=list(working_state.unresolved_errors),
                    ),
                }
            )
        if self._budget is None:
            return head + self._render_all(emergency=False)
        return self._build_within_budget(head)

    # ---- 内部 ----

    def _render_all(self, emergency: bool) -> list[dict]:
        messages: list[dict] = []
        for kind, entry in self._entries:
            if kind == "turn":
                messages.extend(entry.render(self._soft_chars, emergency=emergency))
            elif kind == "pending_turn":
                raise ContextPairingError("pending turn without results during rendering")
            else:
                messages.append(dict(entry))
        return messages

    def _build_within_budget(self, head: list[dict]) -> list[dict]:
        normal = self._render_all(emergency=False)
        if self._budget.fits(head + normal):
            return head + normal
        # 紧急压缩（specs/02：仅一次）：全部工具输出压到紧急配额
        emergency = self._render_all(emergency=True)
        if self._budget.fits(head + emergency):
            return head + emergency
        # 仍超限：从最旧轮次开始整体移除（保留全部 note——关键事实不丢）
        turn_indices = [i for i, (kind, _) in enumerate(self._entries) if kind == "turn"]
        if turn_indices:
            for cut in range(len(turn_indices)):
                remaining = turn_indices[cut + 1:]
                messages = self._render_from(remaining, emergency=True)
                if self._budget.fits(head + messages):
                    return head + messages
        raise ContextBudgetExceeded(
            "context budget exceeded even after emergency compaction and "
            "dropping all older turns; refusing to silently truncate key messages"
        )

    def _render_from(self, turn_positions: list[int], emergency: bool) -> list[dict]:
        messages: list[dict] = []
        for index, (kind, entry) in enumerate(self._entries):
            if kind == "turn":
                if index in turn_positions:
                    messages.extend(entry.render(self._soft_chars, emergency=emergency))
            elif kind == "pending_turn":
                raise ContextPairingError("pending turn without results during rendering")
            else:
                messages.append(dict(entry))
        return messages

    def _track_file_versions(self, calls: list[ToolCall], results: list[ToolResult]) -> None:
        """文件版本追踪（CX-06 + 评审 P1-1）。

        每个成功工具调用占一个操作序号：read 记录读取哈希与序号；patch 更新当前哈希；
        **run_command 可能修改任意源文件**——命令之后未重读的文件一律不再标记
        confirmed current（保守失效，与"Workspace 是文件事实来源"一致）。
        """
        result_by_id = {result.call_id: result for result in results}
        for call in calls:
            result = result_by_id.get(call.call_id)
            if result is None or result.status is not ExecutionStatus.SUCCEEDED:
                continue
            self._op_counter += 1
            path = call.arguments.get("path") if isinstance(call.arguments, dict) else None
            if call.tool_name == "read_file" and path:
                marker = "[sha256="
                if marker in result.content:
                    self._last_read_hashes[path] = result.content.rsplit(marker, 1)[1].split("]")[0]
                    self._last_read_op[path] = self._op_counter
            elif call.tool_name == "apply_patch" and path:
                marker = "new sha256="
                if marker in result.content:
                    self._current_hashes[path] = result.content.rsplit(marker, 1)[1].split(")")[0].split("]")[0]
                else:
                    self._current_hashes.setdefault(path, None)
            elif call.tool_name == "run_command":
                # 保守失效：无法确定命令改了哪些文件，命令后所有已读认知降级
                self._last_command_op = self._op_counter
