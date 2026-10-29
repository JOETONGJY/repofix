"""公共契约枚举（specs/01-contracts.md 权威定义，实现不得自行增删枚举值）。"""
from enum import Enum


class RunStatus(str, Enum):
    """运行生命周期。只表达执行过程，不表达业务正确性。"""

    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"  # 保留扩展状态：MVP 未启用（越权直接拒绝）
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class EvaluationOutcome(str, Enum):
    """业务正确性维度，由 Evaluation Harness 独立判定。"""

    NOT_EVALUATED = "not_evaluated"
    SUCCESS = "success"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(str, Enum):
    """ToolResult.status —— 只描述「工具执行本身」，不描述业务结果。

    UNKNOWN 不作为已完成的 ToolResult 呈现给模型；
    副作用结果未知由 tool_calls.side_effect_status = UNKNOWN 表达，走崩溃对账协议。
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolErrorCode(str, Enum):
    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_ARGUMENT = "invalid_argument"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"


class FailureLayer(str, Enum):
    """统计口径：故障归属层。模型 API 失败不计入工具失败统计。"""

    MODEL_API = "model_api"
    TOOL_SYSTEM = "tool_system"
    WORKSPACE = "workspace"
    BUSINESS_OUTCOME = "business_outcome"


class VerificationAttribution(str, Enum):
    """Verifier 失败归因。不同归因有不同反馈策略。"""

    ASSERTION_FAILURE = "assertion_failure"
    ENVIRONMENT_ERROR = "environment_error"
    EXECUTION_TIMEOUT = "execution_timeout"
    UNDETERMINABLE = "undeterminable"


class SideEffectStatus(str, Enum):
    """tool_calls 表中「有副作用调用」的生命周期，用于崩溃对账。"""

    PENDING = "pending"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    """AgentEvent.type。事件只追加，不修改。"""

    RUN_STARTED = "run_started"
    PREFLIGHT_COMPLETED = "preflight_completed"
    RUN_FINISHED = "run_finished"
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    MODEL_RETRY = "model_retry"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"  # 保留扩展：MVP 未启用
    APPROVAL_RESOLVED = "approval_resolved"  # 保留扩展：MVP 未启用
    VERIFICATION_FINISHED = "verification_finished"
    REVIEW_STARTED = "review_started"
    REVIEW_FINISHED = "review_finished"
    CONTEXT_COMPACTED = "context_compacted"
    NO_PROGRESS_WARNING = "no_progress_warning"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"


class AgentActor(str, Enum):
    """事件归属：区分 Main Agent / Review Agent / 系统。"""

    MAIN = "main"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class PatchStatus(str, Enum):
    VALID = "valid"
    EMPTY = "empty"
    INVALID = "invalid"


class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_AVAILABLE = "not_available"
    ERROR = "error"


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class VerificationStatus(str, Enum):
    """RunResult.verification_status —— 只表达任务内可见验证，与 EvaluationOutcome 分离。"""

    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"
