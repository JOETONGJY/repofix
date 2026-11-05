"""ReviewRunner：Read-only Review Agent 的固定审查循环（design/02、specs/02 Review Runner 语义）。

产品取舍：Reviewer 是质量增强机制，不是硬性安全门禁——
- 只读约束由工具注册表（Reviewer 视图无写工具/finish）+ Workspace read_only 双重保证；
- 有界执行：独立预算（max_reviewer_model_calls=3、max_reviewer_tool_calls=8，CONS-01：
  工具调用同时计入全局 max_tool_calls 计数器，无双重计数或遗漏）；
- 最多一轮审查，输出结构化 ReviewReport；Reviewer 不可用/超预算 → INCONCLUSIVE，
  不把已通过确定性验证的任务判定为失败（RV-06）。
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
from repofix.tools.registry import ToolRegistry

REVIEW_SYSTEM_RULES = (
    "You are RepoFix Read-only Review Agent. You review a candidate patch against the user's "
    "task: check for logic errors, missed edge cases, and regression risks. You have read-only "
    "tools (list_files / search_code / read_file) to gather evidence. Never invent findings - "
    "every finding needs code-level evidence. When done, submit submit_review (alone in its batch) "
    "with status APPROVED (no unresolved findings), FINDINGS (max 5, each with evidence), or "
    "INCONCLUSIVE (cannot review reliably). Do not comment on code style."
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
        workspace_executor,  # read_only=True 的执行器（dispatch 直接调用）
        store: SQLiteEventStore,
        clock: Clock,
        global_tool_budget: BudgetTracker,  # CONS-01：Reviewer 工具调用计入全局计数器
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
        context = ContextBuilder(
            REVIEW_SYSTEM_RULES,
            context_window_tokens=self._config.context_window_tokens,
            output_reserve_tokens=self._config.output_reserve_tokens,
            tool_output_soft_chars=self._config.tool_output_truncate_chars,
            tools_payload=self._registry.view_for(AgentActor.REVIEWER),
        )
        context.start_task(_review_task_text(request), base_commit=request.base_commit)

        for _ in range(self._config.max_reviewer_model_calls + 1):
            try:
                budget.check_tool_budget()
            except BudgetViolation:
                raise ReviewBudgetExceeded("reviewer tool budget exhausted") from None
            try:
                response = self._model_client.complete(self._model_request(context, budget))
            except BudgetViolation:
                raise ReviewBudgetExceeded("reviewer model budget exhausted") from None
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

            # 先分发（submit_review 在此被 Runtime 拦截并登记结论），再提取报告
            context.record_response(response)
            results = [self._dispatch(call) for call in response.tool_calls]
            context.record_results(results, response.tool_calls)
            report = self._extract_report(response)
            if report is not None:
                report.patch_hash = request.patch_hash  # 审查版本绑定（RV-07）
                return report
        return self._inconclusive("reviewer produced no review within its model budget")

    def _model_request(self, context: ContextBuilder, budget: BudgetTracker) -> ModelRequest:
        budget.reserve_model_attempt()  # Reviewer 预算原子占用（每次真实尝试）
        return ModelRequest(
            actor=AgentActor.REVIEWER,
            model_id=self._config.model_id,
            messages=context.next_messages(),
            tools=self._registry.view_for(AgentActor.REVIEWER),  # 只读视图 + submit_review
            max_output_tokens=self._config.output_reserve_tokens,
            request_id=f"review_{self._step + 1}_{uuid.uuid4().hex[:8]}",
        )

    def _dispatch(self, call: ToolCall) -> ToolResult:
        """Reviewer 工具调用分发：submit_review 由 Runtime 拦截（控制动作）；
        只读工具经 read_only 执行器；越权在注册表层已被视图排除。"""
        if call.tool_name == "submit_review":
            # 控制动作：不计入工具预算（specs/02 V0.2 计数口径）
            result = self._submit_review_result(call)
            result.call_id = call.call_id
            self._persist(call, result)
            return result
        result = self._executor.execute(
            tool_name=call.tool_name, arguments=call.arguments, call_id=call.call_id
        )
        result.call_id = call.call_id  # 执行器不感知 call_id，统一绑定（配对完整性）
        self._persist(call, result)
        self._global_budget.count_tool_call()  # CONS-01：只读调用计入全局计数器
        return result

    def _submit_review_result(self, call: ToolCall) -> ToolResult:
        report = _parse_review(call.arguments)
        if isinstance(report, str):
            return ToolResult(
                call_id=call.call_id, status=ExecutionStatus.FAILED,
                error_code=ToolErrorCode.INVALID_ARGUMENT,
                error_message=report,
            )
        self._submitted: ReviewReport = report
        return ToolResult(call_id=call.call_id, status=ExecutionStatus.SUCCEEDED,
                          content=f"review submitted: {report.status}")

    def _extract_report(self, response: ModelResponse) -> ReviewReport | None:
        report = getattr(self, "_submitted", None)
        if report is not None:
            del self._submitted
            return report
        # 有限协议纠正的兜底：纯文本响应不构成审查结论
        _ = response
        return None

    def _persist(self, call: ToolCall, result: ToolResult) -> None:
        status = (SideEffectStatus.SUCCEEDED if result.status is ExecutionStatus.SUCCEEDED
                  else SideEffectStatus.FAILED)
        self._store.start_tool_call(
            run_id=self._run_id, decision_id=f"reviewer-{self._step}",
            call_id=call.call_id, step=self._step, tool_name=call.tool_name,
            arguments=call.arguments, ts=self._clock.now(),
        )
        self._store.finish_tool_call(
            run_id=self._run_id, decision_id=f"reviewer-{self._step}",
            call_id=call.call_id, step=self._step, side_effect_status=status,
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

