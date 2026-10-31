"""公共数据契约（specs/01-contracts.md 权威定义）。

实现必须严格使用本文件定义的类型；变更契约必须同步更新 specs/01 与 specs/04。
"""
from typing import Literal

from pydantic import BaseModel, Field

from repofix.contracts.enums import (
    AgentActor,
    EvaluationOutcome,
    EventType,
    ExecutionStatus,
    PatchStatus,
    RunStatus,
    SideEffectStatus,
    TestStatus,
    ToolErrorCode,
    VerificationAttribution,
    VerificationOutcome,
    VerificationStatus,
)

__all__ = [
    "AgentEvent",
    "EvalTask",
    "EvaluationResult",
    "ModelRequest",
    "ModelResponse",
    "ReviewFinding",
    "ReviewReport",
    "ReviewRequest",
    "RunConfig",
    "RunResult",
    "SideEffectStatus",
    "TaskSpec",
    "ToolCall",
    "ToolResult",
    "VerificationReport",
    "VerificationRequest",
    "WorkingState",
]


class TaskSpec(BaseModel):
    """描述「做什么」（design/00、01）。同一 TaskSpec 可用不同 RunConfig 重复执行，run_id 区分。"""

    instruction: str
    repo_path: str
    base_commit: str
    test_command: str | None = None


class RunConfig(BaseModel):
    """描述「怎么跑」。任务启动后固定，模型不得修改。默认值为初始工程值，待实验校准。"""

    model_id: str
    prompt_version: str = "v0"
    # —— 预算（任一触顶即 BUDGET_EXCEEDED）——
    max_model_calls: int = Field(default=30, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_duration_seconds: int = Field(default=1200, ge=1)
    max_total_tokens: int | None = None
    cost_limit_usd: float | None = None
    # —— 执行环境 ——
    command_timeout_seconds: int = Field(default=120, ge=1)
    workspace_network: str = "disabled"
    # —— 模型调用与守卫（阈值唯一权威：本文件默认值；Runtime 不得硬编码另一套数字）——
    model_retry_limit: int = Field(default=2, ge=0)
    no_progress_repeat_threshold: int = Field(default=3, ge=1)
    no_progress_terminate_threshold: int = Field(default=5, ge=1)
    # Context 层工具输出软预算（字符）；Dispatcher 另有 32 KiB UTF-8 字节硬上限（specs/03 V0.3）
    tool_output_truncate_chars: int = Field(default=2000, ge=1)
    # —— Context Engine（design/07：B_input = B_context − B_output_reserve − B_safety；None = 不裁剪）——
    context_window_tokens: int | None = None
    output_reserve_tokens: int = Field(default=4096, ge=0)
    # —— Review Agent（design/02；review_enabled=False 退回单 Agent Baseline）——
    review_enabled: bool = True
    max_reviewer_model_calls: int = Field(default=3, ge=1)
    max_reviewer_tool_calls: int = Field(default=8, ge=1)
    review_fix_model_calls: int = Field(default=3, ge=1)
    # —— 保留扩展（MVP 未实现）：approval_mode 恒为 deny 语义：越权直接拒绝 ——
    output_dir: str = "./repofix-data"


class WorkingState(BaseModel):
    """任务内结构化进度（三层记忆之二）。

    更新规则：写入类 ToolCall SUCCEEDED 后由 Runtime 更新；
    禁止由模型直接改写；Compaction 后从 WorkingState + 最近事件重建摘要。
    """

    run_id: str
    modified_files: list[str] = []
    failed_tests: list[str] = []
    unresolved_errors: list[str] = []
    plan_summary: str = ""
    updated_at_step: int = 0


class ToolCall(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict
    raw_arguments: str | None = None


class ModelRequest(BaseModel):
    """Runtime → Gateway 的模型调用请求（design/03）。"""

    actor: AgentActor
    model_id: str
    messages: list[dict]
    tools: list[dict]
    max_output_tokens: int | None = None
    timeout_seconds: int = 120
    request_id: str


class ModelResponse(BaseModel):
    """Gateway → Runtime 的标准化响应。完整接纳后才允许执行工具。"""

    response_id: str | None = None
    decision_id: str
    message: dict | None = None
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: dict | None = None
    provider_metadata: dict = {}


class ToolResult(BaseModel):
    """统一 Observation。字段语义必须遵守，不得自行增删枚举值。"""

    call_id: str
    status: ExecutionStatus
    content: str = ""
    exit_code: int | None = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    skipped_reason: str | None = None
    retryable: bool = False
    duration_ms: int | None = None
    truncated: bool = False


class VerificationRequest(BaseModel):
    """Runtime → Verifier 的输入（design/08）。test_plan 任务初始化时确定，Main 不能在 finish 时改写。"""

    run_id: str
    workspace_ref: str
    base_commit: str
    candidate_revision: str
    test_plan: list[str]
    baseline_result: dict
    timeout_seconds: int = 300


class VerificationReport(BaseModel):
    """确定性验证结果（design/08）。candidate_revision 必须与实际验证的代码状态一致。"""

    candidate_revision: str
    patch_status: PatchStatus
    test_status: TestStatus
    outcome: VerificationOutcome
    failure_kind: VerificationAttribution | None = None
    failed_tests: list[str] = []
    summary: str = ""
    report_ref: str | None = None


class EvalTask(BaseModel):
    """评测任务定义（design/09）。visible 与 hidden 配置严格分离。"""

    task_id: str
    repo_source: str
    base_commit: str
    instruction: str
    environment_id: str
    visible_test_plan: list[str]
    hidden_test_plan: list[str]
    success_criteria: dict


class EvaluationResult(BaseModel):
    run_id: str
    outcome: EvaluationOutcome
    run_status: RunStatus
    reason: str = ""
    regression_detected: bool = False


class RunResult(BaseModel):
    """CLI 任务结果（design/01）。每次任务结束都产生；Benchmark 结果不是 CLI 必需输出。"""

    run_id: str
    runtime_status: RunStatus
    termination_reason: str
    verification_status: VerificationStatus
    modified_files: list[str] = []
    patch_path: str | None = None
    trace_path: str | None = None
    test_report_path: str | None = None
    model_calls: int
    tool_calls: int
    token_usage: dict
    duration_ms: int
    review_status: str | None = None
    reviewed_patch_hash: str | None = None


class ReviewFinding(BaseModel):
    """单条审查意见。Reviewer 不得臆造：必须有 evidence 依据。"""

    finding_id: str
    file_path: str
    line: int | None = None
    severity: Literal["high", "medium", "low"]
    reason: str
    evidence: str
    suggested_fix: str = ""
    resolved: bool | None = None


class ReviewRequest(BaseModel):
    """Runtime → Review Agent 的审查输入。不包含 Main Agent 的推理与消息历史。"""

    run_id: str
    instruction: str
    base_commit: str
    candidate_diff: str
    verification_summary: str
    workspace_ref: str
    patch_hash: str


class ReviewReport(BaseModel):
    """Review Agent 的结构化输出。status 只能由 submit_review 提交。"""

    status: Literal["APPROVED", "FINDINGS", "INCONCLUSIVE"]
    patch_hash: str
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=5)
    summary: str = ""


class AgentEvent(BaseModel):
    """追加式事件。seq 由 EventStore 分配，保证同 run 内单调递增。"""

    seq: int
    run_id: str
    step: int
    actor: AgentActor
    type: EventType
    ts: float
    payload: dict
