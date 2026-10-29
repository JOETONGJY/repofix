# RepoFix 实现规格 01：数据契约（Contracts）

> 版本：V0.1（自 V1.3 架构文档提炼）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)。
> 目标：让开发者或 Coding LLM **不再猜测**数据结构的字段、枚举与语义。
> 规则：实现必须严格使用本文件定义的类型；变更契约必须同步更新架构文档 ADR 与 04 验收测试。

---

## 枚举定义

```python
from enum import Enum

class RunStatus(str, Enum):
    """运行生命周期。只表达执行过程，不表达业务正确性。"""
    CREATED = "created"
    PREPARING = "preparing"            # 创建仓库副本 + Preflight（design/00 新增）
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"  # 保留扩展状态：MVP 未启用（越权直接拒绝，见 specs/02）
    VERIFYING = "verifying"            # finish 已提交，Final Check 执行中
    REVIEWING = "reviewing"            # Final Check 通过后，Read-only Review Agent 审查候选 Patch（design/02）
    COMPLETED = "completed"            # 循环正常终止并产出 Patch；不代表修复成功
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"

class EvaluationOutcome(str, Enum):
    """业务正确性维度，由 Evaluation Harness 独立判定。"""
    NOT_EVALUATED = "not_evaluated"
    SUCCESS = "success"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"      # 环境失败 / 无法验证；不计入失败分母

class ExecutionStatus(str, Enum):
    """ToolResult.status —— 只描述「工具执行本身」，不描述业务结果。"""
    SUCCEEDED = "succeeded"   # 执行成功。exit_code=1（如 pytest 断言失败）仍是 SUCCEEDED，业务结果由 Verifier 判定
    FAILED = "failed"         # 工具执行失败（校验拒绝、超时、冲突、运行期错误）
    SKIPPED = "skipped"       # 批次中止后未执行；必须填 skipped_reason
    # 注：UNKNOWN 不作为已完成的 ToolResult 呈现给模型（design/03–04）。
    # 副作用结果未知由 tool_calls.side_effect_status = UNKNOWN 表达，走崩溃对账协议（specs/02）。

class ToolErrorCode(str, Enum):
    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_ARGUMENT = "invalid_argument"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"                 # 文件版本 / Patch 不匹配
    EXECUTION_ERROR = "execution_error"   # 运行期错误（含 retryable 的基础设施瞬态错误）
    CANCELLED = "cancelled"

class FailureLayer(str, Enum):
    """统计口径：故障归属层。模型 API 失败不计入工具失败统计。"""
    MODEL_API = "model_api"               # 429 / 认证失败 / 模型超时 / 协议解析异常
    TOOL_SYSTEM = "tool_system"           # 工具不存在 / 参数不合法 / 权限拒绝
    WORKSPACE = "workspace"               # 容器故障 / 命令超时 / 执行环境错误
    BUSINESS_OUTCOME = "business_outcome" # 断言失败 / 任务未修好

class VerificationAttribution(str, Enum):
    """Verifier 失败归因。不同归因有不同反馈策略（见 02 的 Final Check）。"""
    ASSERTION_FAILURE = "assertion_failure"   # 可反馈给模型继续修复
    ENVIRONMENT_ERROR = "environment_error"   # 继续修复无意义 → 终止
    EXECUTION_TIMEOUT = "execution_timeout"
    UNDETERMINABLE = "undeterminable"         # 无法验证 → Evaluation 记 INCONCLUSIVE

class SideEffectStatus(str, Enum):
    """tool_calls 表中「有副作用调用」的生命周期，用于崩溃对账。"""
    PENDING = "pending"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"

class EventType(str, Enum):
    """AgentEvent.type。事件只追加，不修改。命名对齐 design/00。"""
    RUN_STARTED = "run_started"
    PREFLIGHT_COMPLETED = "preflight_completed"
    RUN_FINISHED = "run_finished"              # payload.status = COMPLETED/FAILED/CANCELLED/BUDGET_EXCEEDED
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    MODEL_RETRY = "model_retry"                # 附 failure_layer=MODEL_API 与重试序号
    TOOL_STARTED = "tool_started"              # 副作用调用的 pre_state_hash 与本事件同事务写入
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"  # 保留扩展：MVP 未启用
    APPROVAL_RESOLVED = "approval_resolved"    # 保留扩展：MVP 未启用
    VERIFICATION_FINISHED = "verification_finished"
    REVIEW_STARTED = "review_started"          # design/02：Review Agent 开始审查
    REVIEW_FINISHED = "review_finished"        # 附 ReviewReport 摘要与 patch_hash
    CONTEXT_COMPACTED = "context_compacted"    # 附压缩前后 Token 估算
    NO_PROGRESS_WARNING = "no_progress_warning"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
```

## 核心模型（Pydantic）

```python
from pydantic import BaseModel, Field

class TaskSpec(BaseModel):
    """描述「做什么」（design/00、01）。同一 TaskSpec 可用不同 RunConfig 重复执行，run_id 区分。"""
    instruction: str                 # 自然语言任务（Issue 文本是其中一种来源）
    repo_path: str                   # 本地 Git 仓库路径
    base_commit: str                 # 已解析的完整 Commit Hash；MVP 只处理该快照
    test_command: str | None = None  # 可见验证命令；由受控 Workspace 执行，不在宿主机执行
    # 注：task_id 不放入 TaskSpec；脏工作区由 CLI 直接拒绝（design/01）

class RunConfig(BaseModel):
    """描述「怎么跑」。任务启动后固定，模型不得修改。默认值为初始工程值，待实验校准。"""
    model_id: str
    prompt_version: str = "v0"
    # —— 预算（任一触顶即 BUDGET_EXCEEDED）——
    max_model_calls: int = 30
    max_tool_calls: int = 100
    max_duration_seconds: int = 1200
    max_total_tokens: int | None = None    # 启用需可靠计数，不能只信模型返回的最终 Usage
    cost_limit_usd: float | None = None
    # —— 执行环境 ——
    command_timeout_seconds: int = 120
    workspace_network: str = "disabled"
    # —— 模型调用与守卫 ——
    model_retry_limit: int = 2             # 单次请求最多额外重试次数
    no_progress_repeat_threshold: int = 3
    no_progress_terminate_threshold: int = 5
    tool_output_truncate_chars: int = 2000 # Context 层工具输出软预算（字符）；Dispatcher 另有 32 KiB UTF-8 字节硬上限（specs/03 V0.3）
    # —— Review Agent（design/02；review_enabled=False 退回单 Agent Baseline）——
    review_enabled: bool = True
    max_reviewer_model_calls: int = 3
    max_reviewer_tool_calls: int = 8          # 只读工具调用
    review_fix_model_calls: int = 3           # 审查反馈后 Main 的修正模型调用上限
    # —— 保留扩展（MVP 未实现）——
    # approval_mode: "interactive" | "deny"   # MVP 恒为 deny 语义：越权直接拒绝
    output_dir: str = "./repofix-data"     # 运行数据根目录（CLI --output-dir 可覆盖）

class WorkingState(BaseModel):
    """任务内结构化进度（三层记忆之二，见架构文档 Context 一章）。"""
    run_id: str
    modified_files: list[str] = []
    failed_tests: list[str] = []      # 最近一次失败的测试名
    unresolved_errors: list[str] = []
    plan_summary: str = ""            # 当前进度摘要（确定性生成；V2 可由 Compactor 生成）
    updated_at_step: int = 0
    # 更新规则：写入类 ToolCall SUCCEEDED 后由 Runtime 更新；
    # 禁止由模型直接改写；Compaction 后从 WorkingState + 最近事件重建摘要。

class ToolCall(BaseModel):
    call_id: str                      # 模型协议返回的调用 ID，原样保留
    tool_name: str
    arguments: dict                   # 解析后的参数，用于 Schema 校验与执行
    raw_arguments: str | None = None  # 模型原始 JSON 字符串：用于诊断与准确记录模型输出（design/03）
    # JSON 解析失败但 call_id / tool_name 合法 → 仍生成关联该 ID 的 INVALID_ARGUMENT ToolResult；
    # call_id 丢失或重复 → 整个响应批次不可接纳，不得自行生成 ID。

class ModelRequest(BaseModel):
    """Runtime → Gateway 的模型调用请求（design/03）。"""
    actor: AgentActor                 # MAIN / REVIEWER —— 决定工具 Schema 视图
    model_id: str
    messages: list[dict]              # Context Engine 构造并通过 Provider 协议校验的消息
    tools: list[dict]                 # 由 actor 对应的 Tool Registry 生成；不能由模型决定
    max_output_tokens: int | None = None
    timeout_seconds: int = 120
    request_id: str                   # 本地生成的请求关联 ID

class ModelResponse(BaseModel):
    """Gateway → Runtime 的标准化响应。完整接纳后才允许执行工具。"""
    response_id: str | None = None    # 供应商响应 ID
    decision_id: str                  # 本地生成的决策关联 ID；工具调用经 (run_id, decision_id, call_id) 唯一定位（design/06）
    message: dict | None = None       # 标准化 assistant 消息
    tool_calls: list["ToolCall"] = []
    finish_reason: str | None = None
    usage: dict | None = None         # 缺失时标记未知，不虚构 Token 和费用
    provider_metadata: dict = {}      # 协议续接所需的受控元数据（不得含 API Key 等敏感凭据）

class ToolResult(BaseModel):
    """统一 Observation。字段语义必须遵守，不得自行增删枚举值。"""
    call_id: str
    status: ExecutionStatus           # 对齐 design/00 命名
    content: str                      # 模型可见结果；已按截断策略截断（单次上限约 32 KiB，保留尾部）
    exit_code: int | None = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    skipped_reason: str | None = None # status=SKIPPED 时必填，如 "batch_aborted_after_write_failure"
    retryable: bool = False           # 仅基础设施瞬态错误为 True；业务失败永远 False
    duration_ms: int | None = None
    truncated: bool = False           # content 是否经过截断（design/03–04 输出控制）
    # 以下信息存 SQLite，不进入模型上下文：
    # stdout / stderr 全文（分开保存）、full_output_ref、side_effect_status

class PatchStatus(str, Enum):
    VALID = "valid"
    EMPTY = "empty"
    INVALID = "invalid"


class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_AVAILABLE = "not_available"   # 没有配置且无法自动发现可用测试
    ERROR = "error"                   # 测试命令无法启动 / 超时状态不明


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"     # 无可用测试：可进 Review，但不得宣称验证通过
    ERROR = "error"                   # 环境 / 配置 / 验证期间代码变化 → 终止


class VerificationRequest(BaseModel):
    """Runtime → Verifier 的输入（design/08）。test_plan 任务初始化时确定，Main 不能在 finish 时改写。"""
    run_id: str
    workspace_ref: str
    base_commit: str
    candidate_revision: str           # 由 Workspace 生成，含新建文件
    test_plan: list[str]              # 规定的测试命令及来源
    baseline_result: dict             # Preflight 测试基线
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
    report_ref: str | None = None     # 详细测试报告引用


class EvalTask(BaseModel):
    """评测任务定义（design/09）。visible 与 hidden 配置严格分离。"""
    task_id: str
    repo_source: str
    base_commit: str                  # 含待修复 Bug 的版本
    instruction: str
    environment_id: str
    visible_test_plan: list[str]      # Agent 可见的验证计划
    hidden_test_plan: list[str]       # 仅评测器持有；文件不进 Agent 工作区
    success_criteria: dict            # 预定义成功要求

class EvaluationResult(BaseModel):
    run_id: str
    outcome: EvaluationOutcome
    run_status: RunStatus             # 记录评测时的运行状态，双维度分别统计
    reason: str = ""
    regression_detected: bool = False

class VerificationStatus(str, Enum):
    """RunResult.verification_status —— 只表达任务内可见验证，与 EvaluationOutcome 分离。"""
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"

class RunResult(BaseModel):
    """CLI 任务结果（design/01）。每次任务结束都产生；Benchmark 结果不是 CLI 必需输出。"""
    run_id: str
    runtime_status: RunStatus         # COMPLETED / FAILED / CANCELLED / BUDGET_EXCEEDED
    termination_reason: str
    verification_status: VerificationStatus
    modified_files: list[str] = []
    patch_path: str | None = None
    trace_path: str | None = None
    test_report_path: str | None = None
    model_calls: int                  # 模型 API 请求尝试次数（含失败与重试，specs/02 V0.2）
    tool_calls: int                   # 工具请求数（预算口径：含校验拒绝/权限拒绝/SKIPPED；控制动作不计入；实际执行次数另存事件，specs/02 V0.2）
    token_usage: dict
    duration_ms: int
    review_status: str | None = None  # APPROVED / FINDINGS / INCONCLUSIVE / NOT_RUN（design/02）
    reviewed_patch_hash: str | None = None  # 审查对应的 Patch 版本；其后发生修改不得声称已审查


class ReviewFinding(BaseModel):
    """单条审查意见。Reviewer 不得臆造：必须有 evidence 依据。"""
    finding_id: str
    file_path: str
    line: int | None = None
    severity: Literal["high", "medium", "low"]
    reason: str                       # 问题是什么
    evidence: str                     # 代码或调用关系依据
    suggested_fix: str = ""
    resolved: bool | None = None      # 修正阶段后由 Runtime 标注；未解决的意见在报告中保留


class ReviewRequest(BaseModel):
    """Runtime → Review Agent 的审查输入。不包含 Main Agent 的推理与消息历史。"""
    run_id: str
    instruction: str
    base_commit: str
    candidate_diff: str
    verification_summary: str
    workspace_ref: str                # 候选代码工作区引用（只读视图）
    patch_hash: str                   # 审查时的 Diff 指纹；审查中发现变化 → 终止并记录冲突


class ReviewReport(BaseModel):
    """Review Agent 的结构化输出。status 只能由 submit_review 提交。"""
    status: Literal["APPROVED", "FINDINGS", "INCONCLUSIVE"]
    patch_hash: str
    findings: list[ReviewFinding] = Field(max_length=5)
    summary: str = ""

class AgentActor(str, Enum):
    """事件归属：区分 Main Agent / Review Agent / 系统（design/02）。"""
    MAIN = "main"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class AgentEvent(BaseModel):
    """追加式事件。seq 由 EventStore 分配，保证同 run 内单调递增。"""
    seq: int
    run_id: str
    step: int                         # 第几个 Turn（模型调用轮次）
    actor: AgentActor                 # MAIN / REVIEWER / SYSTEM——区分两个 Agent 的调用、Token 与失败归因
    type: EventType
    ts: float                         # unix 时间戳
    payload: dict                     # 各事件类型的负载见 02 的事件顺序表
```

### ToolResult 字段语义问答（实现时的判定规则）

| 问题 | 判定 |
| --- | --- |
| Shell 执行成功但测试断言失败，`status` 填什么？ | `SUCCEEDED`，`exit_code=1`。业务失败由 Verifier 归因，不计入 Tool Failure 统计 |
| `content` 是完整 stdout 吗？ | 不是。是**截断后的模型可见输出**（单次上限约 32 KiB）；全文分开存 SQLite |
| 返回被截断时模型怎么知道？ | `truncated=true`；保留有价值的开头 / 结尾 / 错误摘要 |
| stdout / stderr 分开吗？ | 存储分开；模型可见输出为合并视图（stderr 优先保留错误尾部） |
| `SKIPPED` 需要原因吗？ | 必填 `skipped_reason` |
| 已执行但结果丢失怎么表示？ | 不生成伪装完成的 ToolResult；由 `tool_calls.side_effect_status = UNKNOWN` 表达，走崩溃对账（specs/02） |
| 谁把 ToolResult 转成模型 API 的工具消息？ | LLM Gateway 的 `ResponseNormalizer`，按所选模型供应商协议转换；必须保证 call_id 与结果一一对应 |

## SQLite 表结构（权威存储）

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    status TEXT NOT NULL,               -- RunStatus
    eval_outcome TEXT NOT NULL DEFAULT 'not_evaluated',
    termination_reason TEXT,            -- 含 PROCESS_INTERRUPTED（design/06）
    started_at REAL, ended_at REAL,
    model_id TEXT, prompt_version TEXT,
    total_tokens INTEGER DEFAULT 0,     -- 由模型调用事件聚合回填
    total_cost_usd REAL DEFAULT 0,
    fail_layer TEXT                     -- FailureLayer；终态为 FAILED 时必填
);

CREATE TABLE events (
    seq INTEGER,                        -- 同 run 内单调递增
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    actor TEXT NOT NULL,                -- AgentActor：MAIN / REVIEWER / SYSTEM（design/02、06）
    type TEXT NOT NULL,
    ts REAL NOT NULL,
    payload_json TEXT NOT NULL,         -- 模型调用事件内含 Token 用量，统计时聚合
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE tool_calls (
    call_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,          -- 关联的模型响应决策 ID
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    side_effect_status TEXT NOT NULL,   -- SideEffectStatus
    pre_state_hash TEXT,                -- 写入类调用：执行前文件内容 sha256
    expected_result TEXT,               -- 写入类调用：期望结果描述（如 post-patch hash）
    result_json TEXT,                   -- ToolResult 序列化
    stdout TEXT, stderr TEXT,           -- 全文，不进模型上下文
    started_at REAL, completed_at REAL,
    PRIMARY KEY (run_id, decision_id, call_id)  -- 不假设跨响应 call_id 不重复（design/06）
);
```

设计约定（design/06）：**只维护三张核心表**——Token 用量、模型耗时、Reviewer 成本保存在模型调用事件 payload 中，统计时聚合；不单独建 usage / metrics / checkpoint / memory 表，查询性能证明必要时再加汇总表。**SQLite 是运行状态与事件的权威来源，不是文件内容的权威来源**——真实代码状态以 Workspace 为准。

写入规则：事件与 tool_call 状态更新在**同一个 SQLite 事务**内提交；trajectory.jsonl 由 Trace Exporter 从这些表导出，**不参与状态判定**。

## 契约测试要求

- 每个模型的 schema 校验测试（合法/非法用例）必须存在并通过。
- 枚举值不得在实现中自行扩展；新增枚举 = 契约变更，需更新本文件与 04 测试。
