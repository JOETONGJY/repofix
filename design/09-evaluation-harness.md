# RepoFix 详细设计 09：Evaluation Harness

> 版本：V1.0（已冻结；ADR-14 定稿）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)、[08-verifier.md](08-verifier.md)。
> 核心问题：**在相同任务与执行条件下，这个 Coding Agent 实际能解决多少问题？Reviewer 是否真正改善了修复质量？付出了多少额外成本？**

---

## 一、设计定位

独立于 Agent Runtime 的评测程序：**不复用 Main Agent 的判断、不让 Reviewer 决定评分、不用另一个 LLM 作为主要成功判据**。

采用：**固定任务集 + 独立测试执行 + 确定性结果判定 + 指标统计**（延续 ADR-13/14，增加双 Agent 专项评测）。

最小内部组成（逻辑职责，非四个服务）：`DatasetLoader`（加载任务与验收配置）、`EvalRunner`（每任务启动独立 Run、收集候选 Patch）、`ResultJudge`（隔离环境中应用 Patch、执行隐藏测试、判定）、`MetricsAggregator`（成功率、成本、时延、失败类别汇总）。

## 二、评测任务定义：EvalTask

| 字段 | 含义 |
| --- | --- |
| `task_id` / `repo_source` / `base_commit` | 任务 ID / 固定仓库来源 / 含 Bug 版本 |
| `instruction` | 提供给 Agent 的任务描述 |
| `environment_id` | 固定测试环境 |
| `visible_test_plan` | Agent 可见的验证计划 |
| `hidden_test_plan` | **仅评测器可用**的独立验收计划 |
| `success_criteria` | 预定义成功要求 |

**两种配置严格分离**：Agent 可见的 TaskSpec 只含可见测试信息；隐藏测试配置只能由 Evaluation Harness 持有——不只 Prompt 不展示，**隐藏测试文件本身也不能放进 Agent 可搜索读取的工作区**。

**任务集**：20–30 个高质量 Python Bug 修复任务；先用少量任务验证评测器本身再扩展。区分两类任务：**可修复任务**（计入 Task Success Rate）与**不完整/不可完成任务**（评价 Agent 能否合理报告不确定性，单独统计——避免把"合理拒绝不可完成任务"误计为修复失败）。

## 三、隐藏验收的执行：独立干净工作区

```
EvalTask → Agent Run（生成候选 Patch / RunResult）→ Evaluation Harness
→ 创建新的评测工作区 → 从 Base Commit 初始化 → 应用候选 Patch
   ├─ Patch 无效 → FAILED
→ 运行 Hidden Tests + Regression Tests → ResultJudge
   ├─ SUCCESS / FAILED / INCONCLUSIVE
```

**为什么不在 Agent 的工作区跑隐藏测试**：Agent 工作区可能残留测试缓存、临时文件或其他非 Patch 修改。我们真正检验的是——**提交的 Patch 能否独立复现修复效果**。干净基线重新应用 Patch，还能发现典型问题：Agent 在自己工作区"修好了"，但导出的 Patch 缺新文件或依赖变更，别人无法复现。

隐藏测试是评测资产，不传给 Main 或 Reviewer；评测中执行的仓库代码不接触评测服务凭据和宿主机资源。

## 四、结果定义与统计口径

| 结果 | 判断条件 |
| --- | --- |
| `SUCCESS` | 正常交付有效 Patch；Patch 可独立应用；隐藏测试及回归检查通过 |
| `FAILED` | 无有效交付、Patch 无法应用、隐藏测试失败或明确回归 |
| `INCONCLUSIVE` | 评测环境或验收机制异常，无法可靠判断 |
| `NOT_EVALUATED` | 尚未运行独立验收（执行状态，不是成功结论） |

**Runtime COMPLETED 只是成功的必要条件，不是充分条件**；Runtime 预算耗尽没有交付 Patch 的任务**不能从成功率分母中消失**。

**主要指标口径**：

```
Task Success Rate = 独立验收成功的任务数 / 有效且已尝试的可修复任务数
```

模型运行失败、无 Patch、预算耗尽、评测暂时无法判定的有效任务都**不算成功、不静默移出分母**；单独报告 INCONCLUSIVE 数量及原因。只有评测前数据集校验确认任务本身无效（基线仓库不可重建、隐藏测试配置损坏）才排除，并记录理由——避免因困难任务或环境异常多而人为抬高成功率。

**隐藏测试的基线需要验证**（建集时检查）：原始 Bug 版本确实体现待修复问题；测试环境可正常执行；修复后的参考实现满足验收条件（或有可靠预验证依据）；无关的已有失败不被误判为回归。**不能要求"所有隐藏测试在原始基线都必须通过"**。

## 五、指标（首版三层）

| 层次 | MVP 核心指标 | 作用 |
| --- | --- | --- |
| 业务效果 | Task Success Rate、Regression Rate | 判断修复能力 |
| 执行效率 | Token、模型调用次数、Tool Calls、耗时、成本 | 判断资源效率 |
| 运行可靠性 | Invalid Tool Call Rate、Tool Infrastructure Failure Rate、Budget Exhaustion Rate | 判断 Harness 稳定性 |

严格区分：pytest 断言失败 = 业务反馈；工具参数不合法 = Invalid Tool Call；Docker 启动失败 = 执行环境故障；**Reviewer 提出错误意见 = 审查质量问题，不是 Tool Failure**。双 Agent 模式所有成本按 MAIN / REVIEWER 区分并报告总量。**目前没有任何真实成功率，不能预设提升百分比。**

## 六、核心实验：Reviewer 有没有价值

| 配置 | 执行结构 |
| --- | --- |
| A：Baseline | Main Coding Agent + Deterministic Verifier |
| B：双 Agent | Main + Verifier + Read-only Reviewer |

相同任务、模型、工作区、测试环境、主 Agent 初始预算。重点比较：Task Success Rate（隐藏验收）、Regression Rate、Cost / Token、Latency、**Reviewer Finding Precision**（报告的问题有多少确实成立）。

**实验公平性**：B 的总模型预算天然高于 A——B 更好可能只因推理预算更多。因此主实验反映端到端表现，**另设一个预算匹配对照组**（单 Agent 拥有接近双 Agent 的总调用 / 成本预算），才能回答"独立审查是否比单纯堆推理资源更有价值"。关键任务重复运行，记录样本量与波动。

## 七、复现要求与边界

每次评测记录：task_id、RepoFix 代码版本、模型 ID、Prompt 版本、工具 Schema 版本、RunConfig、测试环境版本、最终 Patch Hash、运行与评测结果。不同实验用**同一份固定任务清单**，不能中途换题不记录。Reviewer APPROVED 后 Main 又改代码的，通过 Patch Hash 识别：该认可不对应最终提交版本。

**不实现**：大规模分布式 Benchmark、在线评测服务、自动生成评测数据、LLM-as-Judge 主判据、为漂亮数字扩大任务规模。

**关键验收**：① 隐藏测试不进 Agent 可见工作区；② 相同 Patch 在相同评测环境结果可复现；③ Runtime 失败、隐藏测试失败、评测基础设施异常被正确区分并计入统计。
