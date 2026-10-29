"""ToolBatchScheduler：0/1/N Tool Call 的顺序调度协议（specs/02 Batch 调度规则）。

Runtime 拥有调度权。按模型返回顺序逐个执行，不做依赖推断、不并行；
每个调用（含 SKIPPED）按 call_id 与结果一一配对返回，不得构造不完整的历史消息。

核心规则：
- finish 必须单独成批（V1.10：混合 → 整批拒绝，无任何执行与副作用）；
- 只读失败：后续只读可继续，其后写入类一律 SKIPPED（batch_aborted_after_read_failure）；
- 写入类失败：本批次剩余全部 SKIPPED（batch_aborted_after_write_failure）；
- 意图先行：执行前先持久化执行意图（start_tool_call），意图未成功持久化不执行
  （StoreWriteError 直接上抛，由 Runner 终止且不再继续新的写入操作）。

持久化状态约定：实际执行过的调用 STARTED→SUCCEEDED/FAILED；
未实际执行的调用（校验拒绝、批次中止、预算跳过、整批拒绝）直接记 SKIPPED，
其 ToolResult（含 FAILED/INVALID_ARGUMENT）保存在 result_json 供回流与统计。
"""
from dataclasses import dataclass, field

from repofix.contracts import (
    AgentActor,
    ExecutionStatus,
    SideEffectStatus,
    ToolCall,
    ToolErrorCode,
    ToolResult,
)
from repofix.runtime.loop_guard import BudgetTracker
from repofix.state.store import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, ToolSideEffect

FINISH_TOOL = "finish"
SUBMIT_REVIEW_TOOL = "submit_review"


@dataclass
class BatchOutcome:
    """一个批次的结果：results 顺序与输入 ToolCall 一一对应。"""

    results: list[ToolResult] = field(default_factory=list)
    control_tool: str | None = None  # 单独成批的控制动作名（finish），由 Runner 处理
    control_call: ToolCall | None = None
    budget_exhausted: bool = False  # 批次中途工具预算耗尽（剩余调用 SKIPPED）


class ToolBatchScheduler:
    def __init__(
        self,
        run_id: str,
        dispatcher: ToolDispatcher,
        registry: ToolRegistry,
        store: SQLiteEventStore,
        budget: BudgetTracker,
    ):
        self._run_id = run_id
        self._dispatcher = dispatcher
        self._registry = registry
        self._store = store
        self._budget = budget

    def execute_batch(
        self,
        calls: list[ToolCall],
        actor: AgentActor,
        step: int,
        decision_id: str,
        ts: float,
    ) -> BatchOutcome:
        if not calls:
            return BatchOutcome()

        control_calls = [c for c in calls if self._side_effect_of(c.tool_name) is ToolSideEffect.CONTROL]
        if control_calls:
            if len(calls) > 1:
                return self._reject_mixed_control_batch(calls, step, decision_id, ts)
            return BatchOutcome(control_tool=control_calls[0].tool_name, control_call=control_calls[0])

        outcome = BatchOutcome()
        read_failure_seen = False
        abort_reason: str | None = None

        for call in calls:
            side = self._side_effect_of(call.tool_name)
            if abort_reason is not None:
                outcome.results.append(self._record_skipped(call, step, decision_id, abort_reason, ts))
                continue
            if outcome.budget_exhausted or self._budget.tool_calls_remaining() <= 0:
                if not outcome.budget_exhausted:
                    outcome.budget_exhausted = True
                outcome.results.append(self._record_skipped(call, step, decision_id, "budget_exhausted", ts))
                continue
            if read_failure_seen and side in (ToolSideEffect.WRITE, ToolSideEffect.PROCESS):
                outcome.results.append(
                    self._record_skipped(call, step, decision_id, "batch_aborted_after_read_failure", ts)
                )
                continue

            validation_error = self._dispatcher.validate(call, actor)
            if validation_error is not None:
                # 校验拒绝：未实际执行，错误结果回流模型（持久化 SKIPPED + result_json）
                outcome.results.append(
                    self._record_skipped_with_result(call, step, decision_id, validation_error, ts)
                )
                continue

            result = self._execute_one(call, step, decision_id, ts)
            outcome.results.append(result)

            if result.status is ExecutionStatus.FAILED:
                if side in (ToolSideEffect.WRITE, ToolSideEffect.PROCESS):
                    abort_reason = "batch_aborted_after_write_failure"
                else:
                    read_failure_seen = True
        return outcome

    # ---- 内部 ----

    def _execute_one(self, call: ToolCall, step: int, decision_id: str, ts: float) -> ToolResult:
        # 意图先行：执行意图未成功持久化，不允许执行（StoreWriteError 上抛 → Runner 终止）
        self._store.start_tool_call(
            run_id=self._run_id,
            decision_id=decision_id,
            call_id=call.call_id,
            step=step,
            tool_name=call.tool_name,
            arguments=call.arguments,
            ts=ts,
        )
        self._budget.count_tool_call()
        result = self._dispatcher.execute(call)
        side_effect_status = (
            SideEffectStatus.SUCCEEDED
            if result.status is ExecutionStatus.SUCCEEDED
            else SideEffectStatus.FAILED
        )
        self._store.finish_tool_call(
            run_id=self._run_id,
            decision_id=decision_id,
            call_id=call.call_id,
            step=step,
            side_effect_status=side_effect_status,
            result=result,
            ts=ts,
        )
        return result

    def _record_skipped(
        self, call: ToolCall, step: int, decision_id: str, reason: str, ts: float
    ) -> ToolResult:
        result = ToolResult(
            call_id=call.call_id,
            status=ExecutionStatus.SKIPPED,
            skipped_reason=reason,
        )
        return self._record_skipped_with_result(call, step, decision_id, result, ts)

    def _record_skipped_with_result(
        self, call: ToolCall, step: int, decision_id: str, result: ToolResult, ts: float
    ) -> ToolResult:
        self._store.record_skipped_call(
            run_id=self._run_id,
            decision_id=decision_id,
            call_id=call.call_id,
            step=step,
            tool_name=call.tool_name,
            arguments=call.arguments,
            result=result,
            ts=ts,
        )
        return result

    def _reject_mixed_control_batch(
        self, calls: list[ToolCall], step: int, decision_id: str, ts: float
    ) -> BatchOutcome:
        """RT-06（V1.10）：finish 与其他调用混合 → 整批拒绝，无任何执行与副作用。

        finish 返回 INVALID_ARGUMENT（提示单独提交），其余调用 SKIPPED
        （batch_rejected_finish_mixed），配对完整；模型可在下一轮重新提交。
        """
        results: list[ToolResult] = []
        for call in calls:
            if self._side_effect_of(call.tool_name) is ToolSideEffect.CONTROL:
                result = ToolResult(
                    call_id=call.call_id,
                    status=ExecutionStatus.FAILED,
                    error_code=ToolErrorCode.INVALID_ARGUMENT,
                    error_message=f"{call.tool_name} must be submitted alone in its batch",
                )
            else:
                result = ToolResult(
                    call_id=call.call_id,
                    status=ExecutionStatus.SKIPPED,
                    skipped_reason="batch_rejected_finish_mixed",
                )
            results.append(self._record_skipped_with_result(call, step, decision_id, result, ts))
        return BatchOutcome(results=results)

    def _side_effect_of(self, tool_name: str) -> ToolSideEffect | None:
        spec = self._registry.get(tool_name)
        return spec.side_effect if spec else None
