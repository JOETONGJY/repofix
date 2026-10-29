# RepoFix 详细设计 06：State & Telemetry

> 版本：V1.0（已冻结；ADR-11 / ADR-16 详细化）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)。
> 模块定位：保存发生过什么（Execution Memory）以及当前运行状态——解决**可追踪、可诊断、可统计**三个实际需求。

---

## 一、设计目标

能回答四个问题：Agent 为什么修改了这个文件？某个工具究竟执行过没有？Main 和 Reviewer 分别消耗了多少资源？程序中断后哪些操作已完成、哪些结果无法确认？

选型（沿用）：**SQLite 为运行记录的单一权威存储，JSONL 仅作为可导出的 Trace**。

**重要边界：SQLite 是运行状态与事件记录的权威来源，不是文件内容的权威来源——真实代码状态以 Workspace 为准。**

## 二、最小数据模型：三张核心表

| 表 | 主要字段 | 职责 |
| --- | --- | --- |
| `runs` | run_id、任务配置、Base Commit、运行状态、终止原因、时间 | 任务元数据与生命周期 |
| `events` | run_id、seq、**actor**、event_type、payload、时间 | 完整结构化执行轨迹 |
| `tool_calls` | run_id、**decision_id**、call_id、工具名、参数、执行状态、结果 | 工具调用状态与异常对账 |

Token 用量、模型请求耗时、Reviewer 成本**保存在模型调用事件中，统计时聚合产生**；不单独建 Metrics 数据库、Checkpoint 表或 Memory 表；查询性能证明有必要时再增加汇总表。

### 为什么同时需要 events 和 tool_calls

`events` 记录历史（MODEL_REQUESTED / MODEL_RESPONDED / TOOL_STARTED / TOOL_FINISHED / VERIFICATION_FINISHED / REVIEW_FINISHED / RUN_FINISHED…）；`tool_calls` 回答更具体的问题——**"对于某个调用，当前确认的执行状态到底是什么？"** 异常中断时无需重扫全部 Event 就能找到未完成调用。

这不是两套权威数据：同一个 SQLite 库，**关联更新必须使用事务保持一致**。

## 三、Event Schema 与双 Agent 区分

统一 `AgentEvent`：run_id / seq（Run 内单调递增）/ **actor（MAIN / REVIEWER / SYSTEM）** / event_type / payload / created_at。

**`decision_id` 定位规则**：每次模型响应保存对应 `decision_id`，工具调用通过该 ID 关联；工具调用唯一定位为 **`(run_id, decision_id, call_id)`**——不假设不同模型响应中的 call_id 永不重复。

Main 与 Reviewer 使用相同 Event Store，各自有明确 actor 标识和模型调用记录——足以支持双 Agent 的成本、延迟和失败归因，**不需要消息队列或 AgentBus**。

## 四、关键写入时机：先记录意图，再产生副作用

以 `apply_patch` 为例：

```
Runtime 接受 Tool Call
→ SQLite: 记录调用意图
→ SQLite: 标记 STARTED
→ Workspace: 执行文件修改
→ SQLite: 保存结果
→ 更新 Tool 状态及完成事件
```

关键规则：

1. **执行意图未成功持久化，不允许执行有副作用的工具。**
2. 工具执行成功后，状态更新和完成事件在**同一数据库事务**内提交。
3. Tool 调用结果持久化失败后，Runtime **不再继续新的写入操作**。
4. 无法确认副作用时保留 `UNKNOWN`，不自动重试。

持久化状态：`PENDING → STARTED → SUCCEEDED / FAILED / UNKNOWN`；未实际执行的调用进入 `SKIPPED`。与模块 4 ToolResult 的区别：**UNKNOWN 是持久化执行状态，不代表已获得可信的工具完成结果**。

### 程序崩溃后怎么办

MVP 不支持自动 Resume，采用**有限状态核对**：

- 下次访问该 Run 时识别未终结的 Tool Call，无法确认结果的标记 `UNKNOWN`。
- 检查工作区是否仍存在，保留可获取的文件状态和 Diff。
- 未正常终结的 Run 标记 `FAILED`（termination_reason = `PROCESS_INTERRUPTED`），说明诊断结果。
- **不自动重放工具调用**。对有前后 Hash 的 `apply_patch` 尽可能判断是否已生效；对 Shell 命令不承诺完整判断副作用。

> 事件回放不是工具重放——这也是首版不实现完整 Durable Execution 的原因。

## 五、Workspace 与 SQLite 的一致性

**真实工程限制：SQLite 事务无法与普通文件修改构成跨资源原子事务。** 目标不是实现"数据库与文件操作同时成功或同时失败"，而是：**执行前留下意图，执行后留下结果，异常时能识别不确定性。**

正常完成任务前，Runtime 必须确认（否则不得静默宣称正常完成）：

1. 不存在尚未处理完的工具调用。
2. 最终工作区状态可读取。
3. 最终 Patch 可以生成。
4. 最后一次验证针对的是**当前代码状态**，而不是更早版本。

## 六、Trace 与隐私边界

JSONL 从 SQLite 导出，用于查看轨迹、分析工具失败率、比较模型。但不能无条件保存所有原始内容：

- API Key、认证头、环境凭据**不进入事件**。
- 工具输出限制大小，过长内容存为受限运行产物，经路径 + Hash 引用。
- Trace 含源代码和错误栈，按**敏感运行数据**处理。
- 工作区源代码和 SQLite 数据**不直接挂载给不可信执行容器**。

## 七、不实现与关键验证

不做 Kafka、Redis、分布式 Event Bus、OpenTelemetry 平台、跨进程恢复引擎、自动状态快照系统。

**关键验证**：工具执行前意图已持久化；异常中断后不自动重复副作用操作；Main 与 Reviewer 的轨迹和成本可分别统计。
