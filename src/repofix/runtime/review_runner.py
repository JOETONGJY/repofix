"""ReviewRunner：Read-only Review Agent 的固定审查循环（design/02、specs/02 Review Runner 语义）。

产品取舍：Reviewer 是质量增强机制，不是硬性安全门禁——
- 只读约束由工具注册表（Reviewer 视图无写工具/finish）+ Workspace read_only 双重保证；
- 有界执行：独立预算（max_reviewer_model_calls=3、max_reviewer_tool_calls=8；CONS-01：
  只读调用同时计入全局 max_tool_calls 计数器，无双重计数或遗漏）；
  预算在**执行前**原子检查并占用（评审 P0-1）——超配额的调用以 SKIPPED 记录并保留配对；
- submit_review 必须单独成批（评审 P0-3）：与其他调用混合 → 整批拒绝（同 Main 的 RT-06
  原则），配对完整、不采纳结论；只读调用走完整 Dispatcher 校验链（存在性→权限→Schema）；
- 最多一轮审查，输出结构化 ReviewReport；Reviewer 不可用/超预算 → INCONCLUSIVE，
  不把已通过确定性验证的任务判定为失败（RV-06）。
- Reviewer 模型预算按**真实 API 请求尝试**计数（含 Gateway 内部重试，评审 P1-3）：
  经 model_client.on_attempt 钩子绑定 reserve_model_attempt。
"""
import uuid

from repofix.context.builder import ContextBuilder
from repofix.contracts import (
    AgentActor,
    EventType,
    ExecutionStatus,
    ModelRequest,
    ModelResponse,
    ReviewFinding,
    ReviewReport,
    ReviewRequest,
    RunConfig,
    SideEffectStatus,
    ToolCall,
    ToolErrorCode,
    ToolResult,
)
from repofix.gateway.client import ModelClient
from repofix.runtime.loop_guard import BudgetTracker, BudgetViolation, Clock
from repofix.state.store import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry

REVIEW_SYSTEM_RULES = (
    "You are RepoFix Read-only Review Agent. You review a candidate patch against the user's "
    "task: check for logic errors, missed edge cases, and regression risks. You have read-only "
    "tools (list_files / search_code / read_file) to gather evidence. Never invent findings - "
    "every finding needs code-level evidence. When done, submit submit_review (alone in its batch) "
    "with status APPROVED (no unresolved findings), FINDINGS (max 5, each with evidence), or "
    "INCONCLUSIVE (cannot review reliably). Do not comment on code style. "
    "BUDGET: you have at most 3 model turns. Gather only the evidence you actually need, "
    "and make sure your final turn submits submit_review - a review that never submits is "
    "recorded as INCONCLUSIVE and wastes the review."
)

_STATUS_VALUES = {"APPROVED", "FINDINGS", "INCONCLUSIVE"}


class ReviewBudgetExceeded(RuntimeError):
    """Reviewer 独立预算耗尽：审查结束（INCONCLUSIVE），不影响主任务结论。"""


class ReviewRunner:
    """一轮有界审查。由 AgentRunner 在 REVIEWING 阶段调用。"""

    def __init__(
        self,
        *,
        run_id: str,
        config: RunConfig,
        model_client: ModelClient,
        registry: ToolRegistry,  # 完整注册表：Reviewer 视图按 actor 过滤生成
        workspace_executor,  # read_only=True 的执行器（经 Dispatcher 校验链调用）
        store: SQLiteEventStore,
        clock: Clock,
        global_tool_budget: BudgetTracker,  # CONS-01：Reviewer 只读调用计入全局计数器
    ):
        self._run_id = run_id
        self._config = config
        self._model_client = model_client
        self._registry = registry
        self._executor = workspace_executor
        self._store = store
        self._clock = clock
        self._global_budget = global_tool_budget
        self._step = 0
        self._submitted: ReviewReport | None = None

    # ---- 主入口 ----

    def review(self, request: ReviewRequest) -> ReviewReport:
        """执行一轮审查。任何不可恢复异常都收敛为 INCONCLUSIVE（RV-06 故障隔离）。"""
        self._append_event(EventType.REVIEW_STARTED, {
            "patch_hash": request.patch_hash,
            "candidate_chars": len(request.candidate_diff),
        })
        try:
            report = self._bounded_review(request)
        except (ReviewBudgetExceeded, BudgetViolation):
            report = self._inconclusive("reviewer budget exhausted; review ended early")
        except Exception as exc:  # noqa: BLE001 —— Reviewer 任何故障不阻断主任务（RV-06）
            report = self._inconclusive(f"reviewer unavailable: {type(exc).__name__}: {exc}")
        self._append_event(EventType.REVIEW_FINISHED, {
            "status": report.status,
            "patch_hash": report.patch_hash,
            "findings": len(report.findings),
            "summary": report.summary[:200],
        })
        return report

    # ---- 内部 ----

    def _bounded_review(self, request: ReviewRequest) -> ReviewReport:
        budget = BudgetTracker(
            RunConfig(
                model_id=self._config.model_id,
                max_model_calls=self._config.max_reviewer_model_calls,
                max_tool_calls=self._config.max_reviewer_tool_calls,
                max_duration_seconds=self._config.max_duration_seconds,
            ),
            self._clock,
        )
        # 评审 P1-3：真实 API 请求尝试（含 Gateway 内部重试）经 on_attempt 计入独立预算。
        # 共享 Main Adapter 时不可占用其钩子——CLI 为 Reviewer 装配独立 Adapter 实例；
        # 脚本客户端无重试行为，钩子等价于 Runtime 请求计数。
        if hasattr(self._model_client, "on_attempt"):
            self._model_client.on_attempt = budget.reserve_model_attempt
        dispatcher = ToolDispatcher(
            self._registry, self._executor,
            content_limit_bytes=32 * 1024,
        )
        context = ContextBuilder(
            REVIEW_SYSTEM_RULES,
            context_window_tokens=self._config.context_window_tokens,
            output_reserve_tokens=self._config.output_reserve_tokens,
            tool_output_soft_chars=self._config.tool_output_truncate_chars,
            tools_payload=self._registry.view_for(AgentActor.REVIEWER),
        )
        context.start_task(_review_task_text(request), base_commit=request.base_commit)

        for _ in range(self._config.max_reviewer_model_calls + 1):
            # 工具预算只在只读分发时逐调用检查（评审复核边界 2）：
            # 工具配额用尽后，模型仍可在剩余预算内单独提交 submit_review 结论
            response = self._model_client.complete(self._model_request(context, budget))
            self._step += 1
            self._append_event(
                EventType.MODEL_RESPONDED,
                {"actor": AgentActor.REVIEWER.value, "decision_id": response.decision_id,
                 "tool_call_count": len(response.tool_calls)},
            )

            if not response.tool_calls:
                # 纯文本：有限协议纠正（无 submit_review 不构成审查结论）
                context.record_response(response)
                context.record_protocol_error("respond with read-only tools or submit_review")
                continue

            # 评审 P0-3：submit_review 必须单独成批——混合返回整批拒绝（同 Main RT-06 原则）
            if len(response.tool_calls) > 1 and any(
                c.tool_name == "submit_review" for c in response.tool_calls
            ):
                context.record_response(response)
                results = [self._reject_mixed_submit(call) for call in response.tool_calls]
                context.record_results(results, response.tool_calls)
                context.record_protocol_error(
                    "submit_review must be submitted alone in its batch; resubmit it alone"
                )
                continue

            context.record_response(response)
            results = [self._dispatch(call, budget, dispatcher) for call in response.tool_calls]
            context.record_results(results, response.tool_calls)
            report = self._extract_report(response)
            if report is not None:
                report.patch_hash = request.patch_hash  # 审查版本绑定（RV-07，Runtime 另有事后复核）
                return report
        return self._inconclusive("reviewer produced no review within its model budget")

    def _model_request(self, context: ContextBuilder, budget: BudgetTracker) -> ModelRequest:
        if not hasattr(self._model_client, "on_attempt"):
            # 无重试钩子的客户端（脚本）：仍按 Runtime 请求计数占用预算
            budget.reserve_model_attempt()
        # 有钩子的客户端（真实 Adapter）：每次真实 API 尝试（含重试）由钩子原子占用
        return ModelRequest(
            actor=AgentActor.REVIEWER,
            model_id=self._config.model_id,
            messages=context.next_messages(),
            tools=self._registry.view_for(AgentActor.REVIEWER),  # 只读视图 + submit_review
            max_output_tokens=self._config.output_reserve_tokens,
            request_id=f"review_{self._step + 1}_{uuid.uuid4().hex[:8]}",
        )

    def _dispatch(self, call: ToolCall, budget: BudgetTracker, dispatcher: ToolDispatcher) -> ToolResult:
        """Reviewer 调用分发：submit_review 由 Runtime 拦截（控制动作——不进 Dispatcher、
        不计入工具预算，specs/02 V0.2 计数口径）；只读调用预算执行前原子占用（本地 + 全局），
        走完整 Dispatcher 校验链（存在性→权限→Schema→执行）。超配额 SKIPPED 并保留配对（P0-1）。"""
        if call.tool_name == "submit_review":
            result = self._submit_review_result(call)
            result.call_id = call.call_id
            status = (SideEffectStatus.FAILED if result.status is ExecutionStatus.FAILED
                      else SideEffectStatus.SUCCEEDED)
            self._persist(call, result, status)
            return result

        if budget.tool_calls_remaining() <= 0 or self._global_budget.tool_calls_remaining() <= 0:
            return self._record_skipped(call, budget, "reviewer tool budget exhausted")
        budget.count_tool_call()  # 本地配额：执行前占用
        self._global_budget.count_tool_call()  # CONS-01：全局计数器一次占用

        validation_error = dispatcher.validate(call, AgentActor.REVIEWER)
        if validation_error is not None:
            result = validation_error
        else:
            result = dispatcher.execute(call)
        result.call_id = call.call_id  # 执行器不感知 call_id，统一绑定（配对完整性）
        side_effect = (SideEffectStatus.SUCCEEDED if result.status is ExecutionStatus.SUCCEEDED
                       else SideEffectStatus.FAILED)
        self._persist(call, result, side_effect)
        return result

    def _reject_mixed_submit(self, call: ToolCall) -> ToolResult:
        """混合批次整批拒绝：submit_review 得 INVALID_ARGUMENT，其余调用 SKIPPED。"""
        if call.tool_name == "submit_review":
            result = ToolResult(
                call_id=call.call_id, status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.INVALID_ARGUMENT,
                error_message="submit_review must be submitted alone in its batch",
            )
            side_effect = SideEffectStatus.FAILED
        else:
            result = ToolResult(
                call_id=call.call_id, status=ExecutionStatus.SKIPPED,
                skipped_reason="batch_rejected_submit_review_mixed",
            )
            side_effect = SideEffectStatus.SKIPPED
        self._persist(call, result, side_effect)
        return result

    def _record_skipped(self, call: ToolCall, budget: BudgetTracker, reason: str) -> ToolResult:
        result = ToolResult(call_id=call.call_id, status=ExecutionStatus.SKIPPED,
                            skipped_reason=reason)
        self._persist(call, result, SideEffectStatus.SKIPPED)
        return result

    def _submit_review_result(self, call: ToolCall) -> ToolResult:
        report = _parse_review(call.arguments)
        if isinstance(report, str):
            return ToolResult(
                call_id=call.call_id, status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.INVALID_ARGUMENT,
                error_message=report,
            )
        self._submitted = report
        return ToolResult(call_id=call.call_id, status=ExecutionStatus.SUCCEEDED,
                          content=f"review submitted: {report.status}")

    def _extract_report(self, response: ModelResponse) -> ReviewReport | None:
        report = self._submitted
        self._submitted = None
        _ = response
        return report

    def _persist(self, call: ToolCall, result: ToolResult, side_effect: SideEffectStatus) -> None:
        self._store.start_tool_call(
            run_id=self._run_id, decision_id=f"reviewer-{self._step}",
            call_id=call.call_id, step=self._step, tool_name=call.tool_name,
            arguments=call.arguments, ts=self._clock.now(),
        )
        self._store.finish_tool_call(
            run_id=self._run_id, decision_id=f"reviewer-{self._step}",
            call_id=call.call_id, step=self._step, side_effect_status=side_effect,
            result=result, ts=self._clock.now(),
        )

    def _inconclusive(self, summary: str) -> ReviewReport:
        return ReviewReport(status="INCONCLUSIVE", patch_hash="", findings=[], summary=summary)

    def _append_event(self, event_type: EventType, payload: dict) -> None:
        self._store.append_event(
            run_id=self._run_id, step=self._step, actor=AgentActor.REVIEWER,
            event_type=event_type, payload=payload, ts=self._clock.now(),
        )


def _review_task_text(request: ReviewRequest) -> str:
    diff = request.candidate_diff
    if len(diff) > 20000:
        diff = diff[:20000] + "\n[...diff truncated for review context...]"
    return (
        f"Task given to the Main Agent:\n{request.instruction}\n\n"
        f"Verification summary:\n{request.verification_summary}\n\n"
        f"Candidate diff (patch_hash={request.patch_hash[:16]}):\n{diff}\n\n"
        f"Review this patch. Gather evidence with read-only tools if needed, "
        f"then submit submit_review alone in its batch."
    )


def _parse_review(arguments: dict) -> ReviewReport | str:
    """校验 submit_review 参数 → ReviewReport；失败返回错误描述。"""
    status = arguments.get("status")
    if status not in _STATUS_VALUES:
        return "submit_review.status must be APPROVED / FINDINGS / INCONCLUSIVE"
    findings = []
    for raw in arguments.get("findings") or []:
        if not isinstance(raw, dict) or not raw.get("evidence"):
            return "every finding requires evidence"
        findings.append(ReviewFinding(
            finding_id=raw.get("finding_id", f"f{len(findings) + 1}"),
            file_path=raw.get("file_path", ""),
            line=raw.get("line"),
            severity=raw.get("severity", "medium") if raw.get("severity") in ("high", "medium", "low") else "medium",
            reason=raw.get("reason", ""),
            evidence=raw.get("evidence", ""),
            suggested_fix=raw.get("suggested_fix", ""),
        ))
    if len(findings) > 5:
        return "at most 5 findings are allowed"
    if status == "APPROVED" and findings:
        return "APPROVED must not carry unresolved findings"
    if status == "FINDINGS" and not findings:
        return "FINDINGS requires at least one finding with evidence"
    return ReviewReport(
        status=status, patch_hash="", findings=findings,
        summary=arguments.get("summary", ""),
    )
