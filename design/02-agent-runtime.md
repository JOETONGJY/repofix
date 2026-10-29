# RepoFix 详细设计 02：Agent Runtime（修订版：双 Agent 架构定稿）

> 版本：V1.0（已冻结；ADR-03 正式修订）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)。
> 修订说明：ADR-03 由"单 Agent"修订为"**轻量双 Agent：Main Coding Agent + Read-only Review Agent**"。不引入通用 Multi-Agent 框架；两个 Agent、一个 Runtime、一个工作区。单 Agent Baseline 保留为对照配置。
> 按约定，验收测试只保留关键场景；主要篇幅用于设计思想、职责、接口、执行规则和异常边界。

---

## 一、整体架构：双 Agent，但不做复杂多 Agent 编排

```
Agent Runtime（统一控制状态、预算、调度和任务完成）
   │
   ├── Main Coding Agent
   │      Search → Read → Edit → Test → Finish
   │      读权限 · 写权限（唯一写入者） · 测试执行
   │
   ├── Deterministic Verifier
   │      检查 Patch、运行规定测试（不是 Agent）
   │
   └── Read-only Review Agent
          独立分析 Diff · 检查潜在回归 · 输出审查意见
          只读 · Runtime 处理审查意见，必要时交回 Main Agent 修正
```

### 选型思想

借鉴 Codex 的独立子任务和上下文隔离思想，但**不复制**它完整的多 Agent 委派协议。Codex 官方对多 Agent 的指导：独立任务适合委派，高度依赖前序结果的工作更适合留在主 Agent 中。

因此不是把 Search、Coding、Review 机械分成三个 Agent，而是**只把独立代码审查拆出来**。三项有针对性的工程判断：

1. 搜索与修改高度耦合，放在同一个 Agent 内部。
2. 审查需要相对独立的上下文，值得引入第二个 Agent。
3. 工作区只能有一个代码修改者，避免双 Agent 写入冲突。

### 两个 Agent 的职责边界

| 能力 | Main Agent | Review Agent |
| --- | --- | --- |
| 理解需求和定位代码 | 负责 | 仅为审查而理解 |
| 搜索和读取文件 | 允许 | 允许 |
| 修改代码 | **唯一写入者** | 禁止 |
| 执行 Shell / pytest | 允许，受沙箱限制 | 禁止 |
| 申请 Finish | 允许 | 禁止 |
| 提交结构化审查意见 | 不需要 | 负责 |
| 决定任务最终结束 | 无权直接决定 | 无权直接决定 |

**最终完成权属于 Runtime。**

## 一A、Main Agent 核心执行协议（一致性评审补回）

双 Agent 之外，Main Loop 的原定执行协议在本模块必须可直接查到（完整语义见 specs/02-runtime-semantics.md）：

**Turn 定义与无效响应**：一个 Turn = 一次成功的模型调用 + 该响应全部 ToolCall 的批次处理 + Observation 回流。响应无 ToolCall 且无 finish → 协议错误：携带错误信息重发一次；再次为空 → 终止 Run（fail_layer = MODEL_API）。**模型仅输出文本不结束任务**。

**0 / 1 / N 个 Tool Call**：按模型返回顺序逐个执行，不做依赖推断、不并行；每个调用（含 SKIPPED）按 call_id 与结果一一配对返回，不得构造不完整的历史消息。

**批次失败规则**：只读调用失败 → 后续只读可继续、其后写入类一律 SKIPPED；写入类失败 → 本批次剩余全部 SKIPPED（skipped_reason 必填，如 `batch_aborted_after_write_failure`）。

**finish 单独成批（V1.10 修订：整批拒绝）**：与其他调用混合返回 → **拒绝整个批次**，不执行任何调用（含只读、无任何副作用）——finish 返回 INVALID_ARGUMENT（提示单独提交），其余调用 SKIPPED（`batch_rejected_finish_mixed`），配对完整；模型可在下一轮重新提交。理由：`apply_patch + finish` 混合时"执行修改只拒 finish"会留下已修改却未验证的状态，整批拒绝语义确定、不猜测模型意图。

**LoopGuard**：硬性预算（模型 / 工具调用次数、Token、费用、墙钟）触顶 → BUDGET_EXCEEDED；无进展检测（同工具 + 规范化参数哈希 + 目标文件未变化）连续 3 次 → 注入一次 NO_PROGRESS_WARNING，累计 5 次 → 终止。

**模型重试**：429 / 5xx / 超时 / 连接错误指数退避，上限 model_retry_limit（默认 2）；重试的实际 API 请求计入总预算，Gateway 不得自行无限退避或绕过 Runtime 预算。

---

## 二、Reviewer 的实现方式：真 Agent，不是单次 Review Pass

| 实现 | 能力 | 是否采用 |
| --- | --- | --- |
| LLM Review Pass | 输入 Diff，直接输出审查意见 | 不采用作为最终形式 |
| **Read-only Review Agent** | 阅读 Diff，自主决定是否读取 / 搜索相关代码，再提交审查结果 | **采用** |
| 通用 Subagent Framework | 动态创建多个 Agent、复杂通信与任务分派 | 不采用 |

Review Agent 与主 Agent **复用同一个 `ModelClient` 和 Tool System**，但各自拥有独立的消息历史与工具权限。不需要新建进程，更不需要 Agent Bus。

---

## 三、触发规则：固定审查阶段

不让主 Agent 自由决定是否调用 Reviewer——审查行为必须**可复现、可评测**（有些任务调用、有些不调用就无法对比）。

### 运行流程

```
Main Agent
   ├── Search / Read / Edit / Test
   └── finish
         ↓
    Final Verification
         ├── 测试失败 ────────────→ Main Agent 继续修复
         ├── 环境不可用 ──────────→ 按环境异常规则终止
         └── 通过 / 无可用测试
               ↓
          Review Agent
               ├── 无实质问题 ────→ COMPLETED
               └── 发现问题
                     ↓
               Main Agent 修正或解释
                     ↓
               Final Verification
                     ↓
               COMPLETED / FAILED
```

### 触发规则表

| 条件 | 行为 |
| --- | --- |
| 主 Agent 尚未提交 Finish | 不运行 Reviewer |
| Patch 无效或为空 | 不运行 Reviewer，退回 Main Agent |
| 可见测试存在断言失败 | 先由 Main Agent 修复 |
| 可见验证通过 | **启动 Reviewer** |
| 无可用测试，但 Patch 合法 | 启动 Reviewer，最终验证状态仍为 `INCONCLUSIVE` |
| 测试环境发生不可恢复错误 | 不启动 Reviewer，按环境错误处理 |

**Reviewer 默认只启动一次。** Reviewer 提出问题后，主 Agent 只有一次**受限的反馈修正阶段**；修正后重新运行确定性 Verifier，但**不再二次启动 Reviewer**。因此最终报告必须注明审查对应的 Patch 版本（`patch_hash`）——若随后发生代码修改，不能声称 Reviewer 已审查最终版本。这是有意识的轻量化取舍。

---

## 四、输入、工具与输出契约

### 4.1 输入：`ReviewRequest`

| 字段 | 含义 |
| --- | --- |
| `run_id` | 当前任务 ID |
| `instruction` | 用户原始任务要求 |
| `base_commit` | 原始代码版本 |
| `candidate_diff` | 主 Agent 生成的候选 Patch |
| `verification_summary` | 最近的可见验证结果 |
| `workspace_ref` | 当前候选代码工作区引用 |
| `patch_hash` | 审查时的 Diff 指纹 |

**Reviewer 不接收 Main Agent 的完整推理和对话历史**——让它根据需求、实际代码和测试证据做相对独立的判断，而不是重复主 Agent 的解释。

### 4.2 Reviewer 可使用的工具

| 工具 | 允许 | 理由 |
| --- | --- | --- |
| `list_files` / `search_code` / `read_file` | 是 | 了解代码组织、查找调用者、检查实现 |
| `apply_patch` | 否 | 不允许 Reviewer 修改代码 |
| `run_command` | 否 | 避免引入执行副作用 |
| `finish` | 否 | Reviewer 无权结束主任务 |
| `submit_review` | 是 | 提交结构化审查结果（Reviewer 专用控制动作，不是 Workspace 工具） |

**只读约束必须由运行时工具注册表和 Workspace 权限共同保证，不能仅写在 System Prompt 中。**

Reviewer 在 Main Agent 停止修改文件后运行，读取同一个候选工作区的受限只读视图；若审查过程中发现 Patch 版本变化（hash 不符），终止本轮审查并记录冲突。

### 4.3 输出：`ReviewReport`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `status` | Enum | `APPROVED` / `FINDINGS` / `INCONCLUSIVE` |
| `patch_hash` | String | 本次审查针对的 Patch |
| `findings` | List | 具体问题，最多 5 条 |
| `summary` | String | 总结性意见 |

每条 Finding：

```
finding_id / file_path / line
severity        # high / medium / low
reason          # 问题是什么
evidence        # 代码或调用关系依据
suggested_fix   # 建议如何处理
```

审查重点：逻辑错误、遗漏边界条件、兼容性与回归风险。**不检查代码风格**，不代替 Lint——模型 Review 适合发现需要语义判断的问题，机械规则由确定性检查处理（与 Codex 代码审查实践一致）。

---

## 五、审查反馈如何交给 Main Agent

**Reviewer 只能提出问题，不能直接决定主任务失败。**

### 5.1 APPROVED

Runtime 记录审查结果 → 接受原本的 Finish → 任务 COMPLETED。注意：**Reviewer 的认可不等于独立 Benchmark 成功**。

### 5.2 FINDINGS

1. Runtime 将结构化 Finding 作为审查反馈交给主 Agent。
2. 主 Agent 决定修改代码，或说明为什么某条意见不成立（解释内容写入 finish 的 `summary`——**不设独立 review_response 字段**，与 design/04 的 finish 契约保持一致）。
3. 主 Agent 重新提交 `finish`。
4. Runtime 再运行一次 Final Verification。
5. 验证满足结束条件 → 结束任务并保留审查记录。

**不要求主 Agent 接受 Reviewer 的全部意见**——Reviewer 可能误报，因此它没有代码修改权和最终裁决权。未解决的意见在结果报告中**明确保留**，不删除。

### 5.3 Reviewer 异常处理

| 异常 | 处理 |
| --- | --- |
| Reviewer 模型调用失败 | 在其独立预算内重试；最终失败 → `INCONCLUSIVE` |
| Reviewer 超出调用预算 | 结束审查，不进入无限循环 |
| Reviewer 输出格式错误 | 有限纠正，仍错误 → `INCONCLUSIVE` |
| Reviewer 请求写文件或运行 Shell | 返回权限拒绝 |
| Reviewer 无法提出有证据的问题 | 不得臆造 Finding |
| Reviewer 本身不可用 | 保留确定性验证结果，任务可结束，报告明确标记审查未完成 |

**产品取舍**：Reviewer 是**质量增强机制，不是硬性安全门禁**。权限与操作安全由 Workspace / Policy 保证，规定测试由 Verifier 保证。审查阶段不可恢复时，不会因为第二个模型不可用就把已通过确定性验证的 Patch 判定为代码错误。

---

## 六、Runtime 调整：不增加第十个模块

只在 Agent Runtime 中增加轻量 `ReviewRunner`，由同一主 Runtime 协调：

```
Agent Runtime
 ├── AgentRunner          # Main Coding Agent Loop
 ├── ReviewRunner         # Read-only Review Loop
 ├── RunStateController
 ├── LoopGuard
 └── ToolBatchScheduler
```

Main 与 Reviewer 复用 LLM Gateway 与 Tool Dispatcher，使用不同的 Prompt、工具集合和上下文。

### 6.1 状态机最小变化

原状态基础上增加 `REVIEWING`：

```
RUNNING
   ↓ finish
VERIFYING
   ├── 验证失败 → RUNNING
   └── 验证满足要求
         ↓
      REVIEWING
         ├── APPROVED → COMPLETED
         └── FINDINGS → RUNNING → VERIFYING → COMPLETED
```

**第二次进入 `VERIFYING` 时不再触发 Reviewer**。FAILED / CANCELLED / BUDGET_EXCEEDED 等异常终态不变。

### 6.2 Context / Memory 隔离

| 内容 | Main Agent | Review Agent |
| --- | --- | --- |
| 用户任务 | 共享 | 共享 |
| 实际代码与 Diff | 可访问 | 只读访问 |
| 模型消息历史 | Main 独立 | Review 独立 |
| Working Memory | Main 维护 | 本次 Review 的短期历史 |
| SQLite Event Store | 同一 Run，分别记录身份 | 同一 Run，分别记录身份 |

每条 Agent 事件增加 `actor = MAIN | REVIEWER | SYSTEM`——足以区分两个 Agent 的模型调用、工具行为、Token 消耗和失败归因。**不需要两个独立数据库，也不需要跨 Agent 共享长期 Memory。**

### 6.3 资源预算（初始值，非性能最优结论）

| 参数 | 默认限制 |
| --- | --- |
| Main Agent 模型调用 | 最多 30 次 |
| Reviewer 模型调用 | 最多 3 次 |
| Reviewer 只读工具调用 | 最多 8 次 |
| Reviewer 执行次数 | 1 轮 |
| Review 反馈后的 Main 修正模型调用 | 最多 3 次 |
| 总任务时长 | 仍为 20 分钟 |

开启 Reviewer 后最坏情况模型调用上限 36 次（30 + 3 + 3）；不启用时沿用单 Agent 预算。Main 与 Reviewer 的工具调用**分别统计**，总执行时间共享同一任务预算。

**全局与角色预算的关系（一致性评审明确）**：`max_model_calls = 30` 指 Main Agent 初始阶段上限，**不是全局上限**；全局上限随 review_enabled 为 36 或 30。`max_tool_calls = 100` 是**跨角色全局计数**（Runtime 统一计数器，Reviewer 只读调用同样消耗），Reviewer 角色上限（≤8）在其内部再约束——一个计数器 + 角色上限即可，不需要预算管理框架，也不遗漏 Reviewer 调用。`review_enabled` 属于 RunConfig，不暴露为首版 CLI 参数，由 Evaluation Harness 的运行配置控制。

---

## 七、关键验收测试（只保留影响架构正确性的场景）

| 测试 | 核心要求 |
| --- | --- |
| Review 触发 | 只有有效候选 Patch 完成规定的 Final Check 后，才启动 Reviewer |
| 权限隔离 | Reviewer 无法修改代码或调用 Shell |
| 上下文隔离 | Reviewer 不依赖 Main Agent 的完整消息历史 |
| 反馈闭环 | Reviewer 提出问题后，Main Agent 能在预算内修正并重新验证 |
| 有界执行 | Reviewer 不能无限调用工具，也不会反复触发审查 |
| 故障隔离 | Reviewer 不可用时，不把已通过验证的代码误判为失败 |

---

## 八、ADR-03 修订后的最终设计

| 决策项 | 最终设计 |
| --- | --- |
| Agent 架构 | Main Coding Agent + Read-only Review Agent |
| 调度方式 | 单 Runtime、固定审查阶段、顺序执行 |
| Main Agent | 唯一拥有代码修改权 |
| Reviewer | 独立上下文、有限只读工具调用、结构化审查反馈 |
| Verifier | 确定性测试及 Patch 检查，不是 Agent |
| Agent 间通信 | 只通过结构化 `ReviewRequest` / `ReviewReport` |
| 重试与修正 | 最多一轮审查、一次受限的反馈修正阶段 |
| **不实现** | Search Agent、通用 AgentBus、动态子 Agent 创建、并行写入、多级 Agent 树 |
| 核心收益 | 兼顾代码自主修复和相对独立的质量审查 |
| 主要代价 | 额外模型成本、审查误报、完成时间增加 |

**单 Agent Baseline 保留为对照**：通过 `review_enabled` 配置关闭 Review 阶段，其他核心机制不变——这样才能通过实验比较 Reviewer 是否真正改善成功率，而不是预先声称它一定有帮助。

**下一模块**：模块 3 LLM Gateway——一个统一 Model Gateway 服务两个 Agent（模型接入、Main/Reviewer 独立上下文、Tool Calling 协议、响应解析、模型重试、Usage 统计），而不是为两个 Agent 建两套模型基础设施。
