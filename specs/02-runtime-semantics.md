# RepoFix 实现规格 02：Runtime 语义（Runtime Semantics）

> 版本：V0.1（自 V1.3 架构文档提炼）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)。
> 依赖：[01-contracts.md](01-contracts.md) 的全部枚举与模型。工具定义见 [03-tools-and-workspace.md](03-tools-and-workspace.md)。

---

## Turn（一轮）的精确定义

一个 **Turn** = 一次成功的模型调用 + 该响应返回的全部 ToolCall 的批次处理 + 观察回流。

**空响应处理**：模型响应中既无 ToolCall 也无 `finish` → 视为协议错误：向模型返回结构化错误并重新请求一次（计 `MODEL_RETRY`）；再次为空 → 终止 Run，`fail_layer = MODEL_API`。

## RunStatus 状态转换表

| 当前状态 | 事件 | 下一状态 | 备注 |
| --- | --- | --- | --- |
| CREATED | 启动条件满足 | PREPARING | 创建仓库副本、启动容器、Preflight（Runtime 调用 Workspace Manager） |
| PREPARING | Preflight 通过 | RUNNING | Preflight 失败 → FAILED（fail_layer=WORKSPACE） |
| RUNNING | 命中非白名单 / 越权操作 | RUNNING | **MVP 无审批**：直接返回 PERMISSION_DENIED 回流模型（见「越权处理」；WAITING_APPROVAL 为保留扩展状态，本轮不启用） |
| RUNNING | 收到有效 finish | VERIFYING | 执行 Final Check（见 Finish 协议） |
| VERIFYING | Final Check 通过且 review_enabled | REVIEWING | 启动 Review Agent（触发条件见「Review Runner 语义」） |
| VERIFYING | Final Check 通过且 review_enabled=False | COMPLETED | 单 Agent Baseline 路径 |
| REVIEWING | ReviewReport.status = APPROVED | COMPLETED | 记录审查结果；不等于 Benchmark 成功 |
| REVIEWING | ReviewReport.status = FINDINGS | RUNNING | Findings 反馈回流（修正预算 ≤3 次模型调用）；修正后再次 VERIFYING，**不再触发 Review** |
| REVIEWING | Reviewer 不可用 / INCONCLUSIVE | COMPLETED | 保留确定性验证结果，报告标记审查未完成 |
| VERIFYING | Final Check 通过 | COMPLETED | 产出 Patch + Trace |
| VERIFYING | Final Check 断言失败 | RUNNING | 失败反馈回流，预算内继续 |
| VERIFYING | 无法验证（环境/超时） | COMPLETED | 附 `UNDETERMINABLE` 报告；Evaluation 记 INCONCLUSIVE |
| RUNNING | 不可恢复错误 | FAILED | fail_layer 按四层归属填写 |
| RUNNING | 用户取消 | CANCELLED | 终止进程树（见 03） |
| RUNNING | 任一预算触顶 | BUDGET_EXCEEDED | |

状态转换必须由 `StateMachine` 代码驱动；非法转换抛异常并写事件。

## Gateway 响应接纳规则（design/03）

1. **完整响应接纳之后才能执行工具**——输出截断、结构损坏或未完成时，不允许根据部分内容提前执行。
2. Tool 参数 JSON 无效但 call_id / tool_name 合法 → 保留 Call ID，生成 INVALID_ARGUMENT ToolResult 交 Tool System 语义处理。
3. **call_id 丢失或重复 → 整个响应批次不可接纳**；不得自行生成 ID 假装来自模型。
4. 模型历史必须按供应商原生工具消息协议重建：不得丢失工具调用 ID、角色关系和供应商要求的续接字段（如 reasoning 字段）；Event Store 保留协议续接所需信息，但不得把 API Key 等敏感凭据写入 Trace。
5. 模型重试的实际 API 请求次数计入总预算；Gateway 不能自行无限退避或绕过 Runtime 预算（单次请求额外重试上限 = config.model_retry_limit，默认 2）。

## Batch 调度规则（决策规则，逐条执行）

模型一次响应可能返回 0 / 1 / N 个 ToolCall。Runtime 拥有调度权，按以下规则处理：

1. 按模型返回的顺序**逐个**执行，不做依赖推断，不做并行。
2. 单个调用处理链：存在性 → Schema 校验 → 权限校验 → 预算 → 执行。
3. **只读调用**（list_files / search_code / read_file）失败：后续只读调用可继续；其后若有**写入类调用** → 一律 SKIPPED。
4. **写入类调用**（apply_patch / run_command）失败：本批次所有剩余调用 → SKIPPED（`skipped_reason="batch_aborted_after_write_failure"`）。
5. **`finish` 必须单独成批（V1.10 修订：整批拒绝）**：若与其他调用混合返回，Runtime **拒绝整个批次**——本批次不执行任何工具调用（含只读，无任何副作用）：finish 调用返回 `INVALID_ARGUMENT`（错误消息说明 finish 必须单独提交），其余调用全部返回 `SKIPPED`（`skipped_reason="batch_rejected_finish_mixed"`），配对完整。理由：`apply_patch + finish` 混合时，"执行修改只拒 finish"会留下已修改却未走验证的状态；整批拒绝语义确定，不猜测模型意图。模型可在下一轮重新提交。
6. 每个调用（含 SKIPPED）的结果必须按模型协议与 call_id **一一配对**返回，不得构造不完整的历史消息。
7. 批次结束：全部结果写入事件与 WorkingState 更新，然后进入下一 Turn。

## Finish 协议

- 收到**单独成批**的有效 `finish(summary, changed_files)` → 状态转 `VERIFYING`。
- **Final Check**（每次 finish 只执行一次，不无限重验）：
  1. 校验 `changed_files` 与 WorkingState.modified_files 一致（不符 → 断言失败反馈）。
  2. 运行可见测试（Verifier；基线对比见 03 的 Preflight）。
- 结果分支：
  - 通过且 `review_enabled=False` → `COMPLETED`（单 Agent Baseline 路径）。
  - 通过且 `review_enabled=True` → `REVIEWING`，交 Read-only Review Agent（触发条件与执行规则见下节）。
  - `ASSERTION_FAILURE` → 报告反馈给模型，回 RUNNING 继续修复（受预算约束）。
  - `ENVIRONMENT_ERROR` / `EXECUTION_TIMEOUT` / `UNDETERMINABLE` → 终止为 `COMPLETED` + 附报告（Evaluation 记 INCONCLUSIVE），**不启动 Review，不再重验**。
- 模型反复提交 finish 允许，但每次都消耗预算与 Turn 计数，LoopGuard 正常生效。

## 停止条件（LoopGuard）

| 条件 | 默认阈值 | 动作 |
| --- | --- | --- |
| 模型调用次数 | config.max_model_calls（默认 30） | BUDGET_EXCEEDED |
| 工具调用次数 | config.max_tool_calls（默认 100） | BUDGET_EXCEEDED |
| 总 Token / 费用 | config 可选上限（启用需可靠计数） | BUDGET_EXCEEDED |
| 墙钟时间 | config.max_duration_seconds（默认 1200） | BUDGET_EXCEEDED |
| 无进展检测 | 「同工具 + 规范化参数哈希相同 + 目标文件未变化」连续 repeat_threshold 次；**阈值唯一权威 = specs/01 RunConfig 默认值（repeat=3 / terminate=5，可配置），Runtime 不得硬编码另一套数字** | 达 repeat → 注入一次 `NO_PROGRESS_WARNING`；累计达 terminate → FAILED（fail_layer=TOOL_SYSTEM，reason=no_progress） |

规范化参数哈希：`sha256(tool_name + json.dumps(args, sort_keys=True))`。目标文件未变化 = 相关文件的 mtime/hash 与上次相同。

## 重试责任表

| 层 | 可重试 | 上限 | 不可重试 |
| --- | --- | --- | --- |
| Model Client（LLM Gateway） | 429 / 5xx / 超时 / 连接错误；指数退避，尊重 Retry-After | model_retry_limit（默认 **2**） | 认证失败（终止，MODEL_API）；协议解析异常（携带错误信息重发 **1** 次） |
| Tool 执行 | `EXECUTION_ERROR` 且 retryable=True（基础设施瞬态） | 1（重试前核对副作用状态） | INVALID_ARGUMENT、PERMISSION_DENIED、CONFLICT、TOOL_NOT_FOUND（结构化反馈给模型）；业务失败（绝不机械重试） |

## Final Verification 规则（design/08）

**探索性测试 ≠ 最终验证**：Main 可用 run_command 自主跑 pytest（普通 Observation），但不能决定满足交付条件；finish 后由 Runtime 调用 Verifier 按**预先确定的 test_plan** 执行（初始化时确定，不可在 finish 时改写）。

流程：计算候选代码版本（`candidate_revision` = `patch_hash` = `sha256(get_changes() 相对 base_commit 的 unified diff)`——同一函数贯穿 Verifier / Reviewer / RunResult，工作区变化即失效，见 design/08 一致性评审；由 Workspace 生成、含新建文件）→ PatchValidator（EMPTY / INVALID → FAILED 反馈）→ TestRunner 按规定测试执行 → 分支：

| 情况 | Outcome | Runtime 行为 |
| --- | --- | --- |
| 合法非空 Patch，规定测试通过 | PASSED | 进入 REVIEWING（review_enabled）或 COMPLETED |
| Patch 为空或格式无效 | FAILED | 反馈 Main 修正 |
| 有效测试断言失败 | FAILED | 反馈 Main 修正 |
| 没有配置且无法自动发现可用测试 | INCONCLUSIVE | 可进入 Review，**不能宣称验证通过** |
| 指定测试命令无法启动 | ERROR | 终止任务，记录环境/配置错误 |
| 测试超时且无法确定状态 | ERROR | 终止任务，不当作代码逻辑失败 |
| 验证期间候选代码发生变化 | ERROR | 旧验证失效，不能接受 Finish |

**基线按测试身份比较，不按失败数量**：基线通过+最终通过=可判定；基线失败+最终全通过=可判定；基线通过+最终失败=回归或修复失败；目标 Bug 测试基线失败+最终仍失败=**修复未完成**；基线无关失败仍在→保留不确定性；无法可靠识别测试差异→不定为成功。**不能把基线失败测试全部排除出验收**（有些正是目标 Bug 的测试）。

**Bug 修复模式必须产生有效代码变化**才能接受 Finish；纯解释型任务不在首版验收范围。验证结束后再次确认代码状态：测试运行修改了待交付源码 → 该验证不能用于接受 Finish。

**与 Reviewer 的协作**：APPROVED 可接受（≠ 隐藏验收成功）；FINDINGS → 受限修正 → **重新 Final Verification**；未修改仅回应 → 保留未解决 Finding；修正后标注"审查版本 ≠ 最终交付版本"（reviewed_patch_hash）。**Reviewer 是质量建议者，Verifier 是确定性事实提供者，Runtime 才是完成控制者。**

## 越权处理（V1.4 修订：MVP 无审批）

design/01 裁剪决策：MVP 不实现人机审批——首版既不允许修改原仓库，也不支持自动 Commit / Push、宿主机命令执行和沙箱权限升级，**没有审批需要服务的真实业务操作**。

- 命中非白名单 / 越权操作 → **直接返回 `PERMISSION_DENIED`**（不允许模型绕过），写 `TOOL_FINISHED` 事件，结果回流模型。
- `WAITING_APPROVAL` 状态与 `APPROVAL_REQUESTED / APPROVAL_RESOLVED` 事件保留为扩展位；未来引入仓库写回、网络访问或外部服务操作时，再设计审批对象与授权范围。

## 副作用调用与崩溃恢复

生命周期：`PENDING → STARTED → SUCCEEDED / FAILED / SKIPPED / UNKNOWN`（定义见 01）。

**写入时机（design/06，意图先行）**：① 执行意图未成功持久化，**不允许执行有副作用的工具**（先写意图 + STARTED，再执行）；② 工具成功后状态更新与完成事件**同一事务**提交；③ 结果持久化失败后，Runtime 不再继续新的写入操作；④ 无法确认副作用保留 UNKNOWN，不自动重试。

- `apply_patch` 执行前必须写入 `pre_state_hash` 与 `expected_result`（事务内与 STARTED 同批提交）。
- 恢复流程（Run 重启时对 STARTED 且无完成记录的调用）：
  1. 核对实际文件 hash == expected_result → 补记 `SUCCEEDED`，不重试。
  2. 实际 hash == pre_state_hash → 判定未生效；按错误类型决定是否重试。
  3. 其他状态 → 补记 `UNKNOWN`，**停止自动恢复**（Run 终止，标记需人工确认）。
- 任意 Shell 命令：无法用文件 hash 完整核对副作用 → 状态未知时**终止**，不承诺精确一次执行。
- **崩溃后的有限状态核对（MVP 无自动 Resume，design/06）**：识别未终结的 Tool Call → 无法确认的标 UNKNOWN；检查工作区保留可获取的文件状态与 Diff；未正常终结的 Run 标记 FAILED（termination_reason = `PROCESS_INTERRUPTED`）；**不自动重放工具调用**。
- **Run 正常完成前的四项检查（design/06，不满足不得静默宣称完成）**：① 无未处理完的工具调用；② 最终工作区状态可读取；③ 最终 Patch 可生成；④ 最后一次验证针对当前代码状态而非更早版本。

## Context Policy（接口语义；压缩算法本身可实验替换）

- 每轮构造模型消息前，估算输入 Token；超过可用预算 → 触发压缩（规则见架构文档 Context 一章：不按固定轮数）。
- 压缩顺序：先移除过时的大型工具输出，再压缩旧观察；**不可丢**：任务约束、最近有效文件状态、未解决错误、工具消息配对。
- 紧急压缩一次后仍超限 → 写 `CONTEXT_BUDGET_EXCEEDED` 事件，Run FAILED（fail_layer=TOOL_SYSTEM，reason=context_budget_exceeded）。**不静默截断关键消息。**
- 发送给模型的消息必须满足模型 API 协议：Tool Call 与 Tool Result 严格配对，不得拆散。

## 事件顺序（单 Turn 标准序列）

```
MODEL_REQUESTED → MODEL_RESPONDED
→ TOOL_STARTED → TOOL_FINISHED × N        # 副作用调用的 pre_state_hash 与 TOOL_STARTED 同事务写入
→ [CONTEXT_COMPACTED] / [NO_PROGRESS_WARNING]
→ （finish 时）VERIFICATION_FINISHED
→ （review_enabled 时）REVIEW_STARTED → [REVIEWER: MODEL/TOOL 事件，actor=REVIEWER] → REVIEW_FINISHED
→ RUN_FINISHED（payload.status = COMPLETED / FAILED / CANCELLED / BUDGET_EXCEEDED）
```

## Turn 循环伪代码

```python
while True:
    check_budget(config)                             # 触顶 → BUDGET_EXCEEDED
    messages = context_builder.build(working_state)  # 可能触发压缩
    response = gateway.complete(messages)            # 含重试；空响应 / 纯文本响应按协议错误处理
    if response.finish and response.is_alone:
        run.status = VERIFYING
        report = verifier.final_check(working_state)
        ...  # 按 Finish 协议分支
    results = dispatcher.execute_batch(response.tool_calls)  # 按调度规则
    event_store.append_all(results)
    working_state.update_from(results)
    loop_guard.observe(results)                      # 无进展检测
```
