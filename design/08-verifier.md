# RepoFix 详细设计 08：Verifier

> 版本：V1.0（已冻结；ADR-13 定稿）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)、[02-agent-runtime.md](02-agent-runtime.md)。
> 核心定位：**确定性验证组件，不是第三个 Agent**——不调用 LLM 判断代码正确性，不维护推理历史；依据真实工作区、Patch 和测试执行结果生成 `VerificationReport`。

---

## 一、与 Evaluation Harness 的严格区分

| | Verifier | Evaluation Harness |
| --- | --- | --- |
| 核心问题 | 当前 Patch 是否满足**可见**的交付条件？ | Agent 是否**真正解决**了评测任务？ |
| 所在位置 | Agent 执行流程内 | Agent 执行流程外 |
| 是否向 Agent 反馈 | 是 | 否 |
| 测试内容 | Agent 可见的规定测试 | 独立验收测试 |
| 主要目的 | 促进修复、控制 Finish | 衡量效果、比较方案 |

核心原则：**模型认为已修好、可见测试通过、独立评测成功，是三个不同的判断。**

## 二、探索性测试 vs 最终验证

Main Agent 可以用 `run_command` 主动跑 pytest 并据此改代码（普通 Tool Observation），但**不能决定自己已满足交付条件**。Main 提交 `finish` 后，由 Runtime 调用 Verifier 按**预先确定的验证规则**执行——Finish 语义因此保持稳定。

内部只保留两个组件：

| 组件 | 职责 |
| --- | --- |
| `PatchValidator` | 检查最终 Diff、修改范围和候选代码版本 |
| `TestRunner` | 经 Workspace 运行规定测试，记录结果及失败原因（复用 Workspace 命令执行能力，不另建 Shell Executor） |

`VerificationReport` 是数据结构，不做成独立业务组件。

## 三、触发时机（条件触发，不在每次 Tool Call 后强测）

| 触发点 | 处理 |
| --- | --- |
| Environment Preflight | 建立可见测试基线 |
| Main 调用 `run_command` | 普通工具结果，不属于 Final Verification |
| Main 提交 `finish` | **必须执行 Final Verification** |
| Final Verification 发现可修复失败 | 反馈 Main，允许继续修改 |
| Reviewer 提出问题且 Main 修改代码 | 修改后**再次** Final Verification |
| Reviewer 仅建议、Main 未修改 | 原验证仍有效，但审查问题必须记录 |

Reviewer 不直接调用 Verifier，也不能修改 VerificationReport。

## 四、契约

### VerificationRequest（输入）

| 字段 | 说明 |
| --- | --- |
| `run_id` / `workspace_ref` / `base_commit` | 当前执行 / 工作区 / 基线 |
| `candidate_revision` | 待验证的候选代码版本标识 |
| `test_plan` | 规定的测试命令及来源——**任务初始化时确定，Main 不能在 finish 时临时改写** |
| `baseline_result` | Preflight 中可用的测试基线 |
| `timeout_seconds` | 验证时间预算 |

### VerificationReport（输出）

| 字段 | 说明 |
| --- | --- |
| `candidate_revision` | 本次验证针对的代码版本——**由 Workspace 生成，含新建文件**，不能用模型声称的文件列表、也不能只依赖已跟踪文件 Diff |
| `patch_status` | `VALID` / `EMPTY` / `INVALID` |
| `test_status` | `PASSED` / `FAILED` / `NOT_AVAILABLE` / `ERROR` |
| `outcome` | `PASSED` / `FAILED` / `INCONCLUSIVE` / `ERROR` |
| `failure_kind` | 可选失败类别 |
| `summary` / `report_ref` | 简要结果 / 详细测试报告引用 |

## 五、Final Verification 流程与完成规则

```
Main 提交 finish → Runtime 进入 VERIFYING → 计算候选代码版本
→ PatchValidator
   ├─ Diff 为空/无效 → FAILED → 反馈 Main
   └─ Patch 合法 → 执行规定可见测试 → TestRunner
        ├─ PASSED     → REVIEWING
        ├─ FAILED     → RUNNING（反馈修复）
        └─ 不可验证   → 按原因处理
```

**"不可验证"必须区分清楚，否则编码模型会自行决定处理方式**：

| 情况 | Outcome | Runtime 行为 |
| --- | --- | --- |
| 合法非空 Patch，规定测试通过 | `PASSED` | 进入 Reviewer |
| Patch 为空或格式无效 | `FAILED` | 反馈 Main 修正 |
| 有效测试断言失败 | `FAILED` | 反馈 Main 修正 |
| 没有配置且无法自动发现可用测试 | `INCONCLUSIVE` | 允许进入 Reviewer，但**不能宣称验证通过** |
| 明确指定的测试命令无法启动 | `ERROR` | 终止任务，记录环境/配置错误 |
| 测试超时且无法确定状态 | `ERROR` | 终止任务，不当作代码逻辑失败 |
| 验证期间候选代码发生变化 | `ERROR` | 旧验证失效，不能接受 Finish |

**没有测试不代表测试失败，也不代表测试通过**——这是保留 `INCONCLUSIVE` 的原因。

**Bug 修复模式必须产生实际的、有效的代码变化才能接受 Finish**；纯解释型或无需修改的任务不属于首版验收范围。

### 候选版本标识的统一生成（一致性评审明确）

`candidate_revision`（Verifier）与 `patch_hash`（Reviewer / RunResult）使用**同一生成函数**：

```
candidate_revision = patch_hash = sha256( get_changes() 相对 base_commit 的 unified diff )
```

生成时机：Final Verification 开始时、Review 启动时、RunResult 导出时各自计算一次；任一环节工作区发生变化 → 重算不一致 → 旧验证 / 旧审查**失效**。这样 Verifier、Reviewer 与最终交付三者始终对应同一份实际代码，失效规则只有一条。（测试命令协议同此路径：CLI `--test-command` 字符串仅在任务初始化时解析校验为结构化 argv + test_plan，Runtime / Verifier 不再重复解析 Shell 字符串。）

## 六、基线比较：按测试身份，不按失败数量

只看"最终失败数量没有增加"是错的——基线 3 个失败、修改后仍 3 个失败，可能是原来 3 个通过、新增 3 个失败。

| 基线情况 | 最终情况 | 处理原则 |
| --- | --- | --- |
| 基线通过 | 最终通过 | 可判定通过 |
| 基线失败 | 最终全部通过 | 可判定通过 |
| 基线通过 | 最终出现失败 | 修复失败或产生回归 |
| 目标 Bug 测试基线失败 | 最终仍失败 | **修复未完成** |
| 基线已有无关失败 | 最终仍有同样无关失败 | 不能直接视为成功，按可靠测试覆盖标记不确定性 |
| 无法可靠识别测试差异 | 最终仍有失败 | 不能只比较失败数量就判定成功 |

首版用固定测试命令 + pytest 结构化报告识别测试身份；无法可靠识别就保留不确定性，**不用 LLM 推测"这些失败应该没关系"**。

必须避免的错误：**不能把所有基线失败的测试排除出最终验收**——某些测试恰好就是验证目标 Bug 是否修复的。

**验证是否针对最新 Patch**：Verifier 开始时记录候选版本，结束后再次确认相关代码状态；测试运行本身修改了待交付源码 → 该验证不能用于接受 Finish（测试缓存 / 临时输出可按明确规则排除，真实源码变化不能静默忽略）。避免"Reviewer 审的是 A、Verifier 测的是 A、最终交付的是被后续命令改过的 B"。

## 七、与 Reviewer 的协作（沿用固定审查阶段）

- `APPROVED`：Runtime 可接受 Finish，**不代表隐藏验收一定成功**。
- `FINDINGS`：Main 一次受限修正；**修改后必须重新 Final Verification**。
- Main 不修改只回应：保留未解决 Finding，不假装已修复。
- `INCONCLUSIVE`：可完成任务，但报告必须注明未完成独立审查。
- 审查后发生修改：结果中标记 **"Reviewer 审查版本" vs "最终交付版本"** 是否一致，旧 APPROVED 不得用于新版本。

> **Reviewer 是质量建议者；Verifier 是确定性事实提供者；Runtime 才是完成控制者。**

## 八、MVP 限制与关键验收

不实现：自动生成测试、LLM Judge、复杂覆盖率分析、自动修复测试环境、跨语言测试插件。

**关键验收三项**：① finish 但可见测试失败 → 必须返回修复循环；② 无可用测试时不能错误产生 PASSED；③ 验证后代码版本变化 → 旧报告不能用于接受最终 Patch。

模块选型一句话：**确定性 Final Verification + 版本绑定的验证报告，不增加第三个智能体。**
