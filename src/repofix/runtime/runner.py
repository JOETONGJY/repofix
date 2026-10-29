"""AgentRunner：Main Agent 主循环（specs/02 Turn 循环伪代码的实现）。

Runtime 拥有状态转换与工具批次调度权；模型只提出动作；
finish 是 Runtime 控制动作，经 Final Verification 决定是否接受；
所有执行均受配置预算限制；文本不是任务完成信号。
"""
import uuid

from repofix.context.builder import ContextBuilder
from repofix.contracts import (
    AgentActor,
    EventType,
    ExecutionStatus,
    FailureLayer,
    ModelRequest,
    RunConfig,
    RunResult,
    RunStatus,
    SideEffectStatus,
    TaskSpec,
    ToolCall,
    ToolResult,
    VerificationOutcome,
    VerificationReport,
    VerificationRequest,
    VerificationStatus,
    WorkingState,
)
from repofix.gateway.client import ModelClient, ModelClientError
from repofix.runtime.loop_guard import (
    BudgetTracker,
    BudgetViolation,
    Clock,
    GuardSignalType,
    NoProgressDetector,
)
from repofix.runtime.scheduler import FINISH_TOOL, BatchOutcome, ToolBatchScheduler
from repofix.runtime.state_machine import StateMachine
from repofix.state.store import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry
from repofix.verifier.base import Verifier

DEFAULT_SYSTEM_RULES = (
    "You are RepoFix Main Coding Agent. Fix the reported bug in the task workspace: "
    "locate relevant code with search/list/read, modify files with apply_patch, "
    "and validate with run_command. Submit finish (alone in its batch) when the fix is complete."
)


class AgentRunner:
    """单 Run 的执行控制者。M1 仅实现 Main Agent 路径（review_enabled=False）。"""

    def __init__(
        self,
        *,
        run_id: str,
        task: TaskSpec,
        config: RunConfig,
        model_client: ModelClient,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        store: SQLiteEventStore,
        verifier: Verifier,
        clock: Clock,
        system_rules: str = DEFAULT_SYSTEM_RULES,
    ):
        if config.review_enabled:
            raise NotImplementedError(
                "Review Agent path arrives in M4; M1 runs with review_enabled=False only"
            )
        self._run_id = run_id
        self._task = task
        self._config = config
        self._model_client = model_client
        self._registry = registry
        self._store = store
        self._verifier = verifier
        self._clock = clock
        self._context = ContextBuilder(system_rules)
        self._budget = BudgetTracker(config, clock)
        self._scheduler = ToolBatchScheduler(
            run_id=run_id,
            dispatcher=dispatcher,
            registry=registry,
            store=store,
            budget=self._budget,
        )
        self._guard = NoProgressDetector(
            repeat_threshold=config.no_progress_repeat_threshold,
            terminate_threshold=config.no_progress_terminate_threshold,
        )
        self._state_machine = StateMachine()
        self._working_state = WorkingState(run_id=run_id)
        self._step = 0
        self._verification_status = VerificationStatus.NOT_RUN
        self._token_usage: dict = {"prompt": 0, "completion": 0, "total": 0}

    # ---- 主循环 ----

    def run(self) -> RunResult:
        self._store.create_run(
            run_id=self._run_id,
            repo=self._task.repo_path,
            base_commit=self._task.base_commit,
            model_id=self._config.model_id,
            prompt_version=self._config.prompt_version,
            ts=self._clock.now(),
        )
        self._append_event(EventType.RUN_STARTED, {"task": self._task.instruction[:200]})
        self._transition(RunStatus.PREPARING)
        # M2 起由 WorkspaceManager 执行真实 Preflight（Clone / 容器 / 依赖 / 基线测试）
        self._append_event(EventType.PREFLIGHT_COMPLETED, {"mode": "m1_no_workspace"})
        self._transition(RunStatus.RUNNING)
        self._context.start_task(self._task.instruction)

        protocol_retry_used = False
        while True:
            try:
                self._budget.check_model_budget()
                self._budget.check_tool_budget()
            except BudgetViolation as violation:
                return self._finish(
                    RunStatus.BUDGET_EXCEEDED, f"budget_exhausted:{violation.reason}"
                )

            response = self._request_model()
            if response is None:
                return self._finish(
                    RunStatus.FAILED,
                    "model_client_error",
                    fail_layer=FailureLayer.MODEL_API,
                )
            self._step += 1
            self._accumulate_usage(response.usage)
            self._append_event(
                EventType.MODEL_RESPONDED,
                {
                    "decision_id": response.decision_id,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                    "tool_call_count": len(response.tool_calls),
                },
            )

            # 协议错误：无 ToolCall 即无 finish（空响应 / 纯文本，RT-01/RT-13）
            if not response.tool_calls:
                if not protocol_retry_used:
                    protocol_retry_used = True
                    self._append_event(
                        EventType.MODEL_RETRY,
                        {"reason": "no_tool_calls_and_no_finish", "step": self._step},
                    )
                    self._context.record_response(response)
                    self._context.record_protocol_error(
                        "response contained no tool calls and no finish"
                    )
                    continue
                return self._finish(
                    RunStatus.FAILED,
                    "model_protocol_error:empty_response",
                    fail_layer=FailureLayer.MODEL_API,
                )
            protocol_retry_used = False

            outcome = self._scheduler.execute_batch(
                calls=response.tool_calls,
                actor=AgentActor.MAIN,
                step=self._step,
                decision_id=response.decision_id,
                ts=self._clock.now(),
            )
            self._context.record_response(response)

            if outcome.control_tool == FINISH_TOOL:
                self._record_control_action(outcome)
                finish_outcome = self._handle_finish()
                if finish_outcome == "accepted":
                    return self._finish(
                        RunStatus.COMPLETED,
                        "finish_accepted",
                        verification_status=self._verification_status,
                    )
                if finish_outcome == "error":
                    # VerificationOutcome.ERROR：不可恢复错误，不当作正常完成（specs/02 V0.2）
                    return self._finish(
                        RunStatus.FAILED,
                        "verification_error",
                        fail_layer=FailureLayer.WORKSPACE,
                        verification_status=self._verification_status,
                    )
                continue  # 验证失败反馈已回流，回 RUNNING 继续修复（RT-07）

            self._context.record_results(outcome.results, response.tool_calls)
            if outcome.side_effect_unknown:
                # 执行器异常且副作用未知：终止 Run，不自动重试（specs/02 V0.2 规则 8）
                return self._finish(
                    RunStatus.FAILED,
                    "tool_side_effect_unknown",
                    fail_layer=FailureLayer.WORKSPACE,
                )
            self._update_working_state(response.tool_calls, outcome.results)
            executed = [
                call
                for call, result in zip(response.tool_calls, outcome.results)
                if result.status is not ExecutionStatus.SKIPPED
            ]
            signal = self._guard.observe_batch(executed)
            if signal.type is GuardSignalType.TERMINATE:
                return self._finish(
                    RunStatus.FAILED,
                    "no_progress",
                    fail_layer=FailureLayer.TOOL_SYSTEM,
                )
            if signal.type is GuardSignalType.WARNING:
                self._append_event(
                    EventType.NO_PROGRESS_WARNING,
                    {"key": signal.key, "repeats": signal.repeats},
                )
            if outcome.budget_exhausted:
                return self._finish(
                    RunStatus.BUDGET_EXCEEDED, "budget_exhausted:max_tool_calls"
                )

    # ---- 模型请求 ----

    def _request_model(self):
        request_id = f"req_{self._step + 1}_{uuid.uuid4().hex[:8]}"
        request = ModelRequest(
            actor=AgentActor.MAIN,
            model_id=self._config.model_id,
            messages=self._context.next_messages(),
            tools=self._registry.view_for(AgentActor.MAIN),
            request_id=request_id,
        )
        self._append_event(
            EventType.MODEL_REQUESTED,
            {"request_id": request_id, "message_count": len(request.messages)},
        )
        # 每次真实 API 请求尝试都在发出前计入模型预算（含失败与重试，specs/02 V0.2）；
        # M3 Gateway 的内部重试通过同一计数钩子上报。
        self._budget.count_model_call()
        try:
            return self._model_client.complete(request)
        except ModelClientError:
            return None

    # ---- finish / Final Verification ----

    def _handle_finish(self) -> str:
        """finish 提交 → VERIFYING → Final Verification。

        返回 "accepted"（Run 可正常结束）/ "error"（验证环境或组件错误，Run 终止为
        FAILED）/ "feedback"（断言失败，反馈回流继续修复）。
        """
        self._transition(RunStatus.VERIFYING)
        report = self._run_verification()
        self._append_event(
            EventType.VERIFICATION_FINISHED, report.model_dump(mode="json")
        )
        if report.outcome is VerificationOutcome.PASSED:
            self._verification_status = VerificationStatus.PASSED
            return "accepted"
        if report.outcome is VerificationOutcome.INCONCLUSIVE:
            # 无可用测试 ≠ 验证失败：不能宣称验证通过，允许正常完成（VF-05）
            self._verification_status = VerificationStatus.INCONCLUSIVE
            return "accepted"
        if report.outcome is VerificationOutcome.ERROR:
            # 不可恢复错误：终止任务，不重验，不当作正常完成（design/08 V1.1）
            self._verification_status = VerificationStatus.INCONCLUSIVE
            return "error"
        # 断言失败 → 反馈回流继续修复（RT-07）
        self._verification_status = VerificationStatus.FAILED
        self._transition(RunStatus.RUNNING)
        self._context.record_verification_feedback(report)
        self._working_state.failed_tests = list(report.failed_tests)
        self._working_state.updated_at_step = self._step
        return "feedback"

    def _run_verification(self) -> VerificationReport:
        request = VerificationRequest(
            run_id=self._run_id,
            workspace_ref="m1-no-workspace",  # M2/M3 替换为真实 WorkspaceHandle
            base_commit=self._task.base_commit,
            candidate_revision="m1-no-workspace",  # M3 接 patch_hash 统一生成函数
            test_plan=[self._task.test_command] if self._task.test_command else [],
            baseline_result={},
            timeout_seconds=self._config.command_timeout_seconds,
        )
        try:
            return self._verifier.verify(request)
        except Exception as exc:  # noqa: BLE001 —— 验证组件故障按"无法验证"处理，不伪装 PASSED
            return VerificationReport(
                candidate_revision=request.candidate_revision,
                patch_status="invalid",
                test_status="error",
                outcome=VerificationOutcome.ERROR,
                summary=f"verifier failure: {type(exc).__name__}: {exc}",
            )

    def _record_control_action(self, outcome: BatchOutcome) -> None:
        """finish 控制动作写入 Trace（不进 ToolExecutor）。"""
        call = outcome.control_call
        assert call is not None
        result = ToolResult(
            call_id=call.call_id,
            status=ExecutionStatus.SUCCEEDED,
            content="finish submitted; final verification executed",
        )
        self._store.start_tool_call(
            run_id=self._run_id,
            decision_id="runtime-control",
            call_id=call.call_id,
            step=self._step,
            tool_name=call.tool_name,
            arguments=call.arguments,
            ts=self._clock.now(),
        )
        self._store.finish_tool_call(
            run_id=self._run_id,
            decision_id="runtime-control",
            call_id=call.call_id,
            step=self._step,
            side_effect_status=SideEffectStatus.SUCCEEDED,
            result=result,
            ts=self._clock.now(),
        )
        self._context.record_results([result], [call])

    # ---- 状态与收尾 ----

    def _update_working_state(self, calls: list[ToolCall], results: list[ToolResult]) -> None:
        """写入类调用 SUCCEEDED 后由 Runtime 更新（禁止模型直接改写）。"""
        result_by_id = {result.call_id: result for result in results}
        for call in calls:
            result = result_by_id.get(call.call_id)
            if (
                call.tool_name == "apply_patch"
                and result is not None
                and result.status is ExecutionStatus.SUCCEEDED
            ):
                path = call.arguments.get("path")
                if path and path not in self._working_state.modified_files:
                    self._working_state.modified_files.append(path)
        self._working_state.updated_at_step = self._step

    def _finish(
        self,
        status: RunStatus,
        termination_reason: str,
        fail_layer: FailureLayer | None = None,
        verification_status: VerificationStatus | None = None,
    ):
        self._transition(status)
        self._store.set_run_status(
            self._run_id,
            status,
            ts=self._clock.now(),
            termination_reason=termination_reason,
            fail_layer=fail_layer.value if fail_layer else None,
        )
        self._append_event(
            EventType.RUN_FINISHED,
            {"status": status.value, "termination_reason": termination_reason},
        )
        self._store.set_run_token_usage(self._run_id, self._token_usage.get("total", 0))
        return RunResult(
            run_id=self._run_id,
            runtime_status=status,
            termination_reason=termination_reason,
            verification_status=verification_status or self._verification_status,
            modified_files=list(self._working_state.modified_files),
            model_calls=self._budget.model_calls,
            tool_calls=self._budget.tool_calls,
            token_usage=dict(self._token_usage),
            duration_ms=int(self._budget.elapsed_seconds() * 1000),
        )

    def _transition(self, target: RunStatus) -> None:
        self._state_machine.transition(target)
        self._store.set_run_status(self._run_id, target, ts=self._clock.now())

    def _append_event(self, event_type: EventType, payload: dict) -> None:
        model_events = (EventType.MODEL_REQUESTED, EventType.MODEL_RESPONDED, EventType.MODEL_RETRY)
        actor = AgentActor.MAIN if event_type in model_events else AgentActor.SYSTEM
        self._store.append_event(
            run_id=self._run_id,
            step=self._step,
            actor=actor,
            event_type=event_type,
            payload=payload,
            ts=self._clock.now(),
        )

    def _accumulate_usage(self, usage: dict | None) -> None:
        if not usage:
            return  # Usage 缺失标记未知，不虚构 Token 和费用
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                short = key.removesuffix("_tokens")
                self._token_usage[short] = self._token_usage.get(short, 0) + value
        self._budget.record_usage(usage)  # Token 预算检查依据（缺失时预算侧不计）
