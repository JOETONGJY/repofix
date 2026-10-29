# RepoFix 详细设计 00：全局契约与 Memory 设计

> 版本：V1.0（已冻结）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)（架构 HLD）。
> 地位：全部九个模块共同遵守的基础契约，**权威设计依据**；specs/ 实现规格已按本轮决策同步。
> 下一轮：[01-task-interface.md](01-task-interface.md) → 之后依次为 Agent Runtime 等其余模块。

---

## 一、Task-Scoped Memory：为什么没有单独的 Memory 模块

架构 HLD 已包含相关设计（分散在 Context Engine 与 State & Telemetry），本轮正式整合为 **Task-Scoped Memory（任务级记忆）** 设计。实现任务内 Memory，但不增加第十个模块，也不引入长期记忆系统。

### 三种 Memory

| 类型 | 保存什么 | MVP 是否需要 | 负责模块 |
| --- | --- | --- | --- |
| **Execution Memory** | 完整模型调用、工具调用、工具结果、事件 | 必须 | State & Telemetry（SQLite Event Store，不因上下文压缩而删除） |
| **Working Memory** | 任务目标与约束、已修改文件、最近失败原因、当前任务进度 | 必须 | Context Engine（由执行结果与工作区状态维护） |
| **Long-Term Memory** | 跨任务用户偏好、仓库经验、长期知识 | **不需要** | 后续扩展 |

Working Memory 的运行形态（可更新的进度提示，不是必须执行的固定计划）：

```
Task:               修复登录密码验证 Bug
Modified Files:     auth.py, tests/test_auth.py
Last Verification:  FAILED
Unresolved Issues:  空密码场景断言失败
Next:               检查密码验证分支
```

**为什么需要**：假设 Agent 执行了 25 轮工具调用，前面已修改 `auth.py`，第 20 轮测试发现新问题——如果只用最近几轮对话历史，模型可能忘记已修改的文件，甚至重复执行此前失败的操作。

**重要边界**：Working Memory **不是第二份事实数据库**。已修改文件从实际工作区或可信工具事件核实；测试结果以真实执行记录为准；模型生成的总结不能覆盖这些事实。第一版也不允许模型通过 `write_memory` 之类的工具写入"永久知识"。

### Memory 的 MVP 边界

| 设计问题 | MVP 决策 |
| --- | --- |
| 记忆作用范围 | 只在当前 Run 内生效 |
| 是否跨任务共享 | 不共享 |
| 是否需要向量数据库 / Embedding | 不需要 |
| 是否由模型决定保存什么 | 不由模型直接控制 |
| 已修改文件如何记忆 | 根据工作区实际状态和工具事件维护 |
| 错误与失败尝试如何记忆 | 来自结构化 ToolResult 和 VerificationReport |
| 历史过长怎么办 | 先裁剪冗长输出，保留关键状态和必要调用关系 |
| 是否需要 LLM 摘要 | MVP 不需要，作为后续可评测的 Context 策略 |
| 任务结束后怎么办 | 保留 Run 的 Trace 和结果，不自动传递给其他任务 |

### 两条不可违反的规则（直接用于 Context / Memory 验收测试）

- **规则 A：事实以事件和实际工作区为准。** 摘要称 `auth.py` 已修改但当前工作区无对应修改 → Context Engine 以实际文件状态为准，不继续信任摘要。
- **规则 B：压缩只能改变模型的上下文视图，不能删除运行事实。** 模型看不到 20 轮前的工具输出，SQLite 原始事件仍然存在。

这套设计结合了 Codex 的事件与执行生命周期思想、OpenHands 的 Context/Condenser 分层思想，是自主裁剪的方案——比简单添加向量数据库更贴合 Coding Agent 的实际问题。

---

## 二、六项全局设计原则（约束全部九个模块）

| 编号 | 全局原则 | 具体约束 |
| --- | --- | --- |
| G1 | 单任务隔离 | 一个 Run 对应一个独立的代码工作区 |
| G2 | 控制权集中 | Agent Runtime 唯一负责工具调度和任务生命周期 |
| G3 | 模型与执行分离 | 模型提出动作；Tool / Workspace 校验并执行 |
| G4 | 状态与上下文分离 | Event Store 保存事实，Context Engine 构造模型可见视图 |
| G5 | 安全优先 | 所有仓库代码操作经过受控 Workspace，不直接访问用户工作区 |
| G6 | 运行与验收分离 | Runtime 正常完成不代表任务修复成功 |

这些原则不规定模型如何推理，也不限制后续 Context 策略选择，但保证核心系统不会因某个模块的实现方式而失去一致性。

---

## 三、全局数据契约（八个核心对象 + 独立的验证 / 评测对象）

### TaskSpec：用户要求 Agent 做什么

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `instruction` | String | 用户自然语言需求 |
| `repo_path` | Path | 本地 Git 仓库路径 |
| `base_commit` | String | 执行任务的 Git 基线 |
| `test_command` | Optional String | 用户指定的可见验证命令 |

约束：MVP 只允许单仓库、单任务；`base_commit` 默认为启动时解析的 HEAD；脏工作区默认不包含未提交修改（design/01 收紧为直接拒绝）；任务不允许覆盖用户原仓库；任务描述与 GitHub Issue 格式解耦。TaskSpec 描述业务需求，不包含容器 ID、模型请求参数或内部工具状态。

### RunConfig：系统允许 Agent 做到什么程度

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `model_id` | 配置默认模型 | 模型标识 |
| `max_model_calls` | 30 | **Main Agent 初始阶段**的模型调用上限 |
| `max_tool_calls` | 100 | **跨角色全局**工具调用上限（Runtime 统一计数器，Reviewer 只读调用同样消耗） |
| `review_enabled` | True | 是否启用 Review Agent；False = 单 Agent Baseline（不暴露为首版 CLI 参数，由评测运行配置控制） |
| `max_reviewer_model_calls` | 3 | Reviewer 模型调用上限 |
| `max_reviewer_tool_calls` | 8 | Reviewer 只读工具调用上限 |
| `review_fix_model_calls` | 3 | 审查反馈后 Main 的修正模型调用上限 |
| `max_duration_seconds` | 1200 | 单任务最长执行时间（全局共享，含 Review 阶段） |
| `command_timeout_seconds` | 120 | 单条命令最长执行时间 |
| `model_retry_limit` | 2 | 单次模型请求最多额外重试次数 |
| `workspace_network` | `disabled` | 默认禁用执行环境网络 |

**预算关系（一致性评审明确）**：启用 Reviewer 时总模型调用上限 = 36（30 + 3 + 3），关闭时 = 30；工具调用无角色追加额度——全局 100 由统一计数器执行，角色上限在其内部再约束，避免双重计数或遗漏。

这些是初始工程默认值，不是实验验证的最优参数。Token 用量必须记录；启用严格 Token / 金额上限需定义可靠的计数与估算方式，不能只依赖模型返回的最终 Usage。**RunConfig 在任务开始后固定，模型不能自行修改执行预算或权限。**

### RunState：任务处于什么状态

```
CREATED → PREPARING → RUNNING → VERIFYING → REVIEWING → COMPLETED
                          │  ↑          │（验证失败反馈回 RUNNING） │
                          │  └──────────┘            │ APPROVED → COMPLETED
                          │                           └→ FINDINGS → RUNNING
                          │                               （修正后再次 VERIFYING，不二次 Review）
异常终态：FAILED / CANCELLED / BUDGET_EXCEEDED
保留扩展：WAITING_APPROVAL（MVP 未启用，见 design/01 审批裁剪）
```

| 状态 | 含义 |
| --- | --- |
| `CREATED` | 任务已接收 |
| `PREPARING` | 创建工作区、检查环境 |
| `RUNNING` | Agent 正在调用模型或工具 |
| `VERIFYING` | 正在进行 Finish 前的 Final Verification |
| `REVIEWING` | Final Verification 通过后，Read-only Review Agent 审查候选 Patch（design/02；最多一轮，第二次 VERIFYING 不再进入 Review） |
| `COMPLETED` | Agent 正常结束并接受 Finish |
| `FAILED` / `CANCELLED` / `BUDGET_EXCEEDED` | 异常终态 |
| `WAITING_APPROVAL` | 保留扩展状态（MVP 不进入活动状态集合） |

控制规则：**只有 Agent Runtime 可以发起任务生命周期转换**。Tool System 只能返回结果；Verifier 只能返回验证报告；两者都不能擅自把 RunState 设成 COMPLETED。运行状态与修复结果分开：`RuntimeStatus = COMPLETED` ≠ `EvaluationOutcome = SUCCESS`。

### ModelResponse：模型能输出什么

**单一权威定义在 design/03（LLM Gateway）**，本节仅引用其字段：`response_id`（供应商响应 ID）、`message`（标准化 assistant 消息，含文本输出）、`tool_calls`（本轮工具调用数组）、`finish_reason`（结束原因）、`usage`（Token 用量）、`provider_metadata`（协议续接受控元数据）、`decision_id`（决策关联 ID，见 design/06）。其他模块不得各自定义消息结构。

规定：**文本不是任务完成信号**——模型仅输出"任务已修复"时 Runtime 不结束，按异常响应策略有限纠正；模型必须通过受支持的 `finish` 动作申请正常完成。一轮响应允许 0 / 1 / N 个 Tool Call；多个调用的**调度权属于 Runtime，而不是 Gateway**。

### ToolCall / ToolResult

| 对象 | 字段 | 含义 |
| --- | --- | --- |
| ToolCall | `call_id` / `tool_name` / `arguments` | 调用唯一标识 / 工具名 / 解析后参数 |
| ToolResult | `call_id` | 与 ToolCall 对应 |
| ToolResult | `status` | `SUCCEEDED` / `FAILED` / `SKIPPED`（**UNKNOWN 不在此列**——它是持久化执行状态 PersistedToolStatus = `PENDING/STARTED/SUCCEEDED/FAILED/SKIPPED/UNKNOWN` 的成员，表示无法确认执行结果，不能伪装为工具返回的 Observation；见 design/04、06） |
| ToolResult | `error_code` | 具体错误类别 |
| ToolResult | `content` | 模型可见的结果 |
| ToolResult | `exit_code` | 命令退出码，可为空 |
| ToolResult | `duration_ms` | 执行耗时 |

两条硬性约束：

1. **`SUCCEEDED` 表示工具执行完成，不代表业务成功**。`pytest` 退出码 1 是有效的工具观察，不是基础设施故障。
2. **每个模型发出的 ToolCall 都要有对应结果或明确终止记录**。批次中途失败 → 剩余调用标 `SKIPPED`；进程崩溃导致副作用未知 → 持久化状态标 `UNKNOWN`，不能假装执行成功。

### AgentEvent：完整执行历史

统一事件信封：`event_id` / `run_id` / `seq`（Run 内单调递增）/ `event_type` / `timestamp` / `payload`。

第一版核心事件：`RUN_STARTED`、`MODEL_REQUESTED`、`MODEL_RESPONDED`、`TOOL_STARTED`、`TOOL_FINISHED`、`APPROVAL_REQUESTED`、`APPROVAL_RESOLVED`、`VERIFICATION_FINISHED`、`RUN_FINISHED`。

事件持久化到 SQLite；JSONL 由数据库导出，**不同时维护第二份实时写入日志**。Event Store 是执行事实的权威记录；Working Memory 和当前 RunState 是其与真实工作区状态共同构成的运行视图。

---

## 四、跨模块控制权分配

"唯一负责"指最终决策与写入所有权，不代表模块间不能协作（如 Runtime 调用 Verifier 并根据结果决定是否继续修复，但不能让模型自己伪造验证结果）：

| 行为 | 唯一负责模块 |
| --- | --- |
| 接收用户任务 | Task Interface |
| 决定是否继续调用模型 | Agent Runtime |
| 决定执行哪个 Tool Call | Agent Runtime |
| 调用模型并解析模型响应 | LLM Gateway |
| 构造模型可见历史和 Working Memory | Context Engine |
| 校验工具名称和参数 | Tool System |
| 校验执行权限与操作审批（MVP：越权直接拒绝，见 design/01） | Workspace & Policy |
| 执行文件与命令操作 | Workspace（通过 Tool System 调用） |
| 保存事件与任务状态 | State & Telemetry |
| 生成测试验证结果 | Verifier |
| 判定 Benchmark 是否成功 | Evaluation Harness |

---

## 五、全局契约验收标准（实现完成后必须满足）

| 测试场景 | 预期行为 |
| --- | --- |
| 模型返回不存在的工具 | 返回结构化错误，不执行 |
| 模型返回三个工具，第二个写入失败 | 保留第一项结果，第二项失败，第三项标记跳过 |
| 模型只输出"完成了" | 不直接标记任务完成 |
| 模型主动调用 `finish` | Runtime 进入验证流程 |
| 已达预算 | 停止新增模型或工具调用，记录明确原因 |
| Context 历史过长 | 安全裁剪，保留任务目标与必要消息关系 |
| Agent 已修改文件，但旧消息被压缩 | Working Memory 仍反映真实修改 |
| CLI 任务中断 | 保存可获得的事件与工作区状态，不盲目重复副作用操作 |
| Agent 正常结束但独立测试失败 | Runtime 与 Evaluation 分别记录结果 |

---

## 六、本轮冻结范围

**已冻结**：单 Run 隔离、Runtime 控制权、核心数据对象、运行与验收分离、Execution / Working Memory 分层、SQLite 权威事件存储、无跨任务长期记忆。

**留给模块详细设计**：状态转换矩阵、多 Tool Call 精确调度、工具 Schema、审批策略（如未来启用）、事件 Payload、Memory 更新算法、Token Budget 分配与压缩细节。

> 全局契约明确了各模块共同遵守的接口方向和控制权，但不声称详细设计已完成——例如多工具调用中断后的具体协议，必须在 Agent Runtime 章节定稿，而不是交给编码模型临时决定。
