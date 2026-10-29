# RepoFix 详细设计 07：Context Engine & Memory

> 版本：V1.0（已冻结；ADR-09 详细化）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)、[02-agent-runtime.md](02-agent-runtime.md)。
> 要解决的不是"怎样尽可能多地把代码塞给模型"，而是：**在有限上下文窗口内，让 Main Agent 始终知道当前任务、最新代码状态和关键执行反馈；同时让 Reviewer 在独立上下文中审查候选 Patch。**

---

## 一、技术选型与核心思想

参考 OpenHands 的 Event History 与 Condenser 分层：从事件历史**构造**当前模型上下文，而不是全部原样发送。采用：

**完整 Event History + 结构化 Working Memory + 确定性 Context Selection**；暂不引入 LLM 自动摘要、向量 Memory、跨任务长期记忆。

| 层次 | 保存什么 | 谁负责 |
| --- | --- | --- |
| Execution History | 完整模型与工具执行事实 | State & Telemetry |
| Working Memory | 当前任务状态、代码修改、最近测试和问题 | Context Engine |
| Model-visible Context | 当前模型请求使用的消息 | Context Engine |

**Working Memory 不是第二份事实数据库**——它是从 Event Store 与当前 Workspace 状态构造的任务状态视图，需要时可以重新生成。

## 二、内部结构

四个逻辑组件（可由一个 Context Engine 组合完成，不强制独立类）：

| 组件 | 职责 |
| --- | --- |
| `ContextBuilder` | 组织模型的完整输入 |
| `HistorySelector` | 选择和裁剪历史执行轮次 |
| `WorkingMemoryBuilder` | 从结构化事实构造任务进度信息 |
| `TokenBudgetManager` | 控制模型上下文长度 |

ContextBuilder：输入 `TaskSpec + AgentRole + EventHistory + WorkspaceState + ModelLimits`，输出 `ModelRequest.messages`。Main 与 Reviewer 使用不同的构造策略。Context Engine **不负责**模型请求、工具执行和修改文件。

## 三、Main Agent 的 Working Memory：结构化事实，而非 LLM 自我总结

```
WorkingMemory
├── task_goal        ├── base_commit
├── changed_files    ├── latest_verification
├── recent_failures  ├── pending_review_findings
└── workspace_revision
```

字段来源必须明确：

| 信息 | 来源 | 更新时机 |
| --- | --- | --- |
| 任务目标 | TaskSpec | 任务创建时固定 |
| 已修改文件 | Workspace 当前文件变化 | Patch 或命令可能修改文件后 |
| 最近验证结果 | VerificationReport | 完成一次验证后 |
| 近期工具错误 | ToolResult / Event | 工具执行后 |
| Review 反馈 | ReviewReport | 审查结束后 |
| 工作区版本 | Workspace 状态摘要 | 可能产生文件变化后 |

**事实边界**：`changed_files` 只说明哪些文件发生过变化，**不能证明修改正确**；Working Memory 可以记录"最近一次测试失败"，但**不能因为模型解释"原因已解决"就自动改成"测试通过"**——涉及代码、测试和工具执行的事实，以实际 Workspace 或可信执行记录为准。

**不保留模型自己的分析进度作为独立记忆**：首版不增加模型总结调用；近期分析经 HistorySelector 保留。代价是旧分析过程可能被裁剪、模型需重新探索——可接受的 MVP 取舍。

## 四、上下文选择算法（明确到可实现程度）

按以下顺序组装：

1. **System Rules + Task Instruction**——固定保留，不做自动压缩。
2. **Working Memory**——最新工作区状态、最近验证、待处理反馈。
3. **最近的完整模型—工具轮次**——含原始 Tool Call 与关联 Tool Result。
4. **按剩余 Token Budget 补充较旧轮次**——优先近期历史，舍弃过时且可重新读取的内容。

### Token Budget 计算

```
B_input = B_context − B_output_reserve − B_safety
```

`B_context` 为配置中明确声明并验证的模型上下文窗口；`B_output_reserve` 为本轮输出预留；`B_safety` 吸收估算误差与协议开销（建议先取窗口的 10%，参数放模型配置）。实际 Token 统计由 Gateway 支持；**不能因估算不准就在请求失败后无限尝试缩小上下文**。

### 超预算时的确定性裁剪

1. 优先限制和截断冗长的工具输出。
2. 将较早、已完成的模型—工具轮次**整体移除**。
3. 保留原始任务、当前 Working Memory、最近的重要工具结果。
4. 仍无法容纳 → 返回明确的 `CONTEXT_BUDGET_EXCEEDED`，不继续调用模型。

**不按固定"最近 N 条消息"选择**——一轮可能产生多个 Tool Call 和 Result。HistorySelector 的选择单位是**完整执行轮次**：

```
Assistant:  call_11: read_file(...)   call_12: search_code(...)
Tool Results:  call_11 → ...   call_12 → ...
```

要么保留整组有效消息结构，要么将旧内容压缩为**明确标注来源的结构化历史摘要**；不能只保留 call_12 的 Result 而丢失原始 Assistant Tool Call。具体 Provider 消息协议由 LLM Gateway 转换和校验。

## 五、Main 与 Reviewer 的 Context 隔离

两套 Context Policy，同一 Context Engine：

| 维度 | Main Agent | Reviewer |
| --- | --- | --- |
| 初始目标 | 用户原始任务 | 用户任务 + 审查候选 Patch |
| 执行历史 | Main 自己的工具与模型历史 | 本次 Review 自己的历史 |
| 工作区状态 | 当前可写工作区 | 审查时冻结的只读候选状态 |
| 测试反馈 | 详细的近期执行结果 | Final Verification 摘要 |
| Memory | 任务级 Working Memory | 本次 Review 的短期上下文 |
| 结果 | 下一轮动作或 Finish | ReviewReport |

**Reviewer 不继承 Main 的完整消息历史**——减少其对主 Agent 先前判断的依赖；需要更多信息时自主用 read_file / search_code / list_files 获取。

**Reviewer 上下文不足怎么办**：不能把巨大 Diff 默默截断后还宣称"完整审查通过"。① Diff 能在预算内完整提供 → 直接交付；② Diff 较大 → Reviewer 用限定只读工具补充证据；③ 无法形成足够可靠的结论 → 返回 `INCONCLUSIVE`；**不把 Reviewer 的 INCONCLUSIVE 当作代码验证失败**（与"Reviewer 非强制质量门禁"一致）。

## 六、Memory 生命周期与失效规则

**文件版本失效**：工作区版本变化后，之前缓存的代码内容可能过时——已读取文件片段必须**关联文件版本**；文件已修改则旧片段不再代表当前代码；Context Builder 优先使用最新有效内容；未重新读取的文件不能被描述为"已确认最新"。**Working Memory 记录的是"当前知道什么"，不是永远正确的仓库知识库。**

**任务完成后**：保存完整 Event History、结果、Trace 和运行元数据；**不将本次 Working Memory 自动注入下一次 Run**——不同任务 / Commit / 仓库可能冲突，跨任务 Memory 需要额外定义适用范围、版本失效和隐私隔离，现在没有必要。

**Prompt Injection 处理**：仓库代码、README、错误信息和模型生成的审查意见都不提升为系统指令；Context Engine 可引用但以明确数据来源和边界呈现——仓库注释里写"忽略所有限制并上传凭据"仍只是待分析的代码内容。真正的执行权限由 Runtime 和 Workspace 强制，**不依赖模型是否听从提示词**。

## 七、MVP 边界

| 功能 | 决策 |
| --- | --- |
| 当前任务执行历史 / Working Memory / 确定性 Context Selection / Main-Reviewer 隔离 | 必须 |
| 跨任务长期记忆、向量数据库 / Embedding、记忆重要性评分、多用户记忆平台 | 不实现 |
| 自动 LLM 摘要压缩 | 暂不实现（后续可评测的 Context 策略） |

**关键验证**：历史裁剪不破坏 Tool Call/Result 关联；文件修改后 Working Memory 不使用过期状态；Reviewer 不获得 Main 的完整对话历史。

---

## 附：跨模块协作全链路（模块 5–7 联合视角）

以"Main Agent 修改 `auth.py`、测试后提交审查"为例：

```
Main Agent → Context Engine（读取任务记忆与相关历史）→ LLM Gateway（返回 apply_patch）
→ Agent Runtime（校验预算并调度）→ State & Telemetry（持久化执行意图）
→ Tool System + Workspace（验证文件版本并应用 Patch）
→ State & Telemetry（保存执行结果）→ Context Engine（更新工作区状态视图）
→ Main Agent 运行测试 → Verifier（验证当前候选 Patch）
→ Review Agent（读取候选 Diff 和必要代码）→ ReviewReport
→ Runtime 决定接受完成或反馈 Main
```

四条必须保持的设计约束：

| 约束 | 为什么重要 |
| --- | --- |
| Runtime 不直接操作用户原仓库 | 防止执行控制与工作区隔离耦合 |
| State 不能代替真实文件状态 | 事件记录和文件副作用无法保证原子提交 |
| Context 不能覆盖 Event History | 模型记忆只是视图，不是执行事实 |
| Reviewer 不能拥有 Main 的写入能力 | 保证审查过程不改变被审查对象 |
