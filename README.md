# RepoFix

**Repository-Aware Coding Agent Harness** —— 参考 Codex 的执行控制架构与 OpenHands 的模块化设计，使用 Python 自研的面向真实代码仓库的 Coding Agent。首版聚焦 **Python 仓库 Bug 修复**：输入一条自然语言任务描述，系统在隔离的仓库副本中自主完成代码检索、修改、测试与审查，最终交付可独立应用的 Patch、完整执行 Trace 与验证报告。

> 核心研究问题不是"LLM 会不会写代码"，而是：**在真实仓库、有限上下文、有限预算和受控执行环境中，如何让 LLM 可靠地完成多步骤代码修改，并让执行过程可验证、可追踪、可分析？**
>
> 因此本项目的重点不是训练模型或封装 SDK，而是 **Agent Harness 工程**：执行循环、工具协议、上下文管理、状态持久化、安全边界、质量验证与效果评测。

---

## 目录

- [项目定位](#项目定位)
- [架构总览](#架构总览)
- [执行流程](#执行流程)
- [模块划分](#模块划分)
- [关键设计决策](#关键设计决策)
- [三层质量保障的区分](#三层质量保障的区分)
- [评测与对照实验](#评测与对照实验)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [路线图与当前进度](#路线图与当前进度)
- [MVP 明确不做什么](#mvp-明确不做什么)
- [文档体系](#文档体系)
- [常见设计追问](#常见设计追问)

---

## 项目定位

| 维度 | 选择 | 说明 |
| --- | --- | --- |
| 工作对象 | 单个 Git 仓库（首版限定 Python） | Repository-aware：理解整个仓库而非单个文件 |
| 交互形式 | CLI，一次性任务模式 | `repofix run --repo ./project --task "Fix the login validation bug"` |
| 任务模式 | 一次输入、自动执行到结束 | 便于控制变量与复现，不做通用聊天式编程助手 |
| 交付物 | Patch + Trace + Test Report | 默认不自动 Commit / Push，用户仓库保持只读 |
| 首个场景 | Issue-to-Patch（Bug 修复） | 标准化评测场景，但 Harness 不与 Issue 格式绑定 |

**这个项目是什么、不是什么：**

- 不是重新训练一个编程模型，也不是给某个模型简单套一层工具调用；
- 不是基于 Codex SDK 或 OpenHands SDK 的二次封装——两者是架构参考，核心执行语义由本项目自行实现；
- 是一个把"模型决策"与"受控执行"分离的 Harness：模型负责不确定性的代码理解与决策，Harness 负责确定性的执行控制与状态管理，测试与独立评测负责判断结果。

架构参考来源：**Codex（主）** 提供 Agent Loop、执行生命周期、工具调度、Sandbox 与状态事件设计思想；**OpenHands（辅）** 提供 Action / Observation / Executor、Workspace 抽象、Event History 与 Condenser 等模块化思想。两者均只借鉴设计并按单任务场景裁剪，不复制其 App Server、多轮会话协议或完整 SDK。

---

## 架构总览

**一个 Runtime、两个 Agent、一个确定性验证器、一个工作区。** 不引入通用多 Agent 框架。

| 角色 | 职责 | 权限 |
| --- | --- | --- |
| **Main Coding Agent** | 理解任务、搜索代码、修改文件、运行测试、提交 Finish | 读 + 唯一写入权 + 受限命令执行 |
| **Read-only Review Agent** | 独立阅读候选 Diff、检查回归风险、提交有证据的审查意见 | 只读工具 + 结构化 `submit_review` |
| **Deterministic Verifier** | 检查 Patch、执行规定测试、给出确定性结论（不是 Agent，不调用 LLM） | 由 Runtime 调用 |
| **Agent Runtime** | 统一调度、预算控制、状态转换、完成判定 | 唯一拥有控制权 |

**为什么只把 Review 拆成第二个 Agent：** 搜索与修改高度耦合（反复"搜索 → 阅读 → 修改 → 测试 → 再搜索"），留在同一个 Agent 内避免昂贵的上下文传递；审查则需要相对独立的上下文——Reviewer 不接收 Main 的推理历史，基于任务描述、实际代码与测试证据独立判断。工作区只允许一个写入者，避免并发修改冲突。

**Reviewer 是真 Agent 而非单次 Review Pass：** 它拥有独立的"模型—只读工具"循环，能自主决定是否查看调用方、搜索相似实现、阅读测试，再通过 `submit_review` 提交最多 5 条带证据的 Finding。它没有修改权、没有 Finish 权、没有最终裁决权——权限由工具注册表与只读 Workspace 双重保证，不依赖 Prompt 约束。

审查是**有界的一次性阶段**：Main 提交有效候选 Patch 并通过确定性验证后触发一次 Review；若有 Finding，Main 只获得一次受限修正（≤3 次模型调用），修正后重新验证但不再二次审查。Reviewer 是尽力而为的质量增强机制，不是硬性安全门禁——Reviewer 不可用时，已通过的确定性验证仍然有效，结果标记"审查未完成"。

双 Agent 是否真的优于单 Agent **不由设计预先断言**：系统保留 `review_enabled=False` 的单 Agent 基线，通过对照实验用数据回答（见[评测与对照实验](#评测与对照实验)）。

---

## 执行流程

```
CLI 接收任务 + 仓库 + 运行配置
        │
        ▼
  输入校验（脏工作区直接拒绝；仅接受干净 base_commit 快照）
        │
        ▼
  Workspace Preflight
  （独立 Clone → 受限容器 → 依赖检测 → 基线测试）
        │
        ▼
┌──── Main Agent Loop（受预算与循环守卫约束）────┐
│   Context 构造 → LLM 调用 → Tool Calls        │
│        → 校验（存在性→权限→Schema→语义→预算）  │
│        → Docker Workspace 执行 → 结果回流      │
└──────────────┬─────────────────────────────┘
               │ 提交 finish（必须单独成批）
               ▼
     Deterministic Verifier（Final Verification）
        │
   ┌────┴─────────────┐
   ▼                  ▼
 验证失败          通过 / 无可用测试（INCONCLUSIVE）
   │                  │
   ▼                  ▼
 反馈 Main 修正    Read-only Reviewer（review_enabled 时）
                      │
             ┌────────┴────────┐
             ▼                 ▼
          APPROVED          FINDINGS → Main 限次修正 → 再次验证
             │                 │
             └────────┬────────┘
                      ▼
            RunResult + Patch + Trace
                      │
                      ▼
        Independent Evaluation（隐藏测试独立验收）
```

**Agent Loop 的核心执行规则——模型只提出动作，Runtime 决定能否执行、按什么顺序执行、何时终止：**

- 一次模型响应可包含 0 / 1 / N 个 Tool Call，按返回顺序逐个执行，不并行、不推断依赖；
- 纯文本响应不是完成信号：进行一次有限协议纠正，再次无效则终止；
- 工具失败按副作用类别传播：写入类失败中止整个批次，后续调用标记 `SKIPPED`（带原因），已成功结果保留，所有调用与结果按 Call ID 一一配对；
- `finish` 与普通工具混合返回时**整批拒绝**（不执行任何调用、无任何副作用），避免产生"已修改但未验证"的状态；
- 硬性预算（模型调用 / 工具请求 / Token / 墙钟）触顶即终止；无进展检测（相同动作 + 参数哈希重复且目标文件未变化）先警告后终止；
- 每次模型 API 请求尝试（含失败重试）与每个工具请求（含被拒 / 跳过）都计入预算，非法调用无法绕过。

---

## 模块划分

八个逻辑核心模块 + 一个独立评测模块（单进程部署，非微服务）：

| 模块 | 职责 | 技术选择 |
| --- | --- | --- |
| Task Interface | 任务输入、校验、结果展示 | Typer CLI、TaskSpec、RunConfig |
| Agent Runtime | Agent Loop、状态机、工具调度、预算、审查调度 | Python 自研、LoopGuard |
| LLM Gateway | 模型协议适配、Tool Calling、用量记录 | OpenAI 兼容 Chat Completions + ProviderAdapter |
| Tool System | 工具注册、参数校验、权限检查、结构化结果 | Pydantic、ToolRegistry、ToolDispatcher |
| Workspace & Policy | 独立仓库副本、文件操作、命令执行、安全隔离 | Git Clone + Docker + PermissionPolicy |
| State & Telemetry | 运行状态、事件历史、执行追踪 | SQLite（权威存储）+ JSONL 导出 |
| Context Engine & Memory | 模型上下文构造、任务级记忆、Token 控制 | 确定性 ContextBuilder / WorkingMemory |
| Verifier | Final Patch 检查与可见测试执行 | PatchValidator + TestRunner |
| Evaluation Harness | 隐藏验收、指标统计、对照实验 | 固定任务集 + ResultJudge + MetricsAggregator |

**最重要的模块边界：** Runtime 是唯一任务生命周期控制者；Gateway 只负责模型协议，不执行工具；Tool System 只负责单次调用，批次调度归 Runtime；Workspace 是代码文件状态的权威来源，SQLite 是执行事件与运行状态的权威来源；Context 是从事实记录构造的模型可见视图（不是第二份数据库）；Verifier 提供确定性事实；Evaluation 独立于 Agent 执行链路。

---

## 关键设计决策

每一项选型都遵循同一原则：**选择能解决当前关键问题的最小技术集合，而不是功能最多的框架**，并明确记录牺牲了什么。

**自研 Harness，而不是套用框架。** 直接使用 OpenHands / PydanticAI 会把核心执行逻辑封装在框架内部，本项目恰恰希望深入实现并验证这些机制（工具批次语义、失败处理、预算、上下文管理、可追踪性）；LangGraph 擅长的图式工作流、持久 Checkpoint 与人工中断恢复在单任务动态工具循环中并非刚需。复用成熟基础设施（Pydantic、Git、ripgrep、Docker、pytest、SQLite），自研全部执行语义。若未来出现跨任务持久编排需求，再重新评估。

**原生 Tool Calling + 多层校验，而不是从文本解析动作。** 原生协议提供了明确的 Call ID、工具名与参数结构，但模型仍可能输出无效 JSON、重复 Call ID 或不存在的工具——因此完整接纳响应后才执行；参数保留原始 JSON 与解析结果双份；校验顺序为存在性 → 权限 → Schema → 语义前置 → 预算 → 执行，无权限的调用不解析参数（拒绝结果不受参数内容影响）。工具集合固定为 `list_files / search_code / read_file / apply_patch / run_command` 五个工作区工具加 `finish / submit_review` 两个 Runtime 控制动作；命令使用 argv 数组与显式白名单（pytest / unittest / compileall / ruff），不做任意 Shell。不引入 MCP——固定工具集合已覆盖首版需求。

**单文件 Patch + SHA-256 版本校验。** 每次 `apply_patch` 只处理一个文件的 Unified Diff，修改必须携带读取时获得的文件 Hash，冲突即拒绝、应用即原子。多文件任务通过多次调用完成。这个取舍牺牲部分编辑效率，换来简单、可解释的冲突与失败语义（避免多文件部分应用、跨文件事务与复杂冲突恢复）。

**独立仓库副本 + 受限 Docker，而不是 Worktree 或原仓库直跑。** Worktree 与原仓库共享 Git 元数据，不能当作强隔离机制。独立 Clone（`--no-local`，不共享对象存储）后挂载进非 root、禁网、限资源的容器（不暴露 Docker Socket，不注入模型密钥）。诚实的边界声明：Docker 不能绝对安全地执行任意恶意代码；容器禁网也不代表数据不出本机——模型请求仍会把必要的代码片段发送给外部 LLM API。

**SQLite 权威存储 + 意图先行写入。** 单机单 Run 顺序调度不需要消息队列。关键规则是**先持久化执行意图，再产生副作用**：副作用工具执行前写入 `PENDING → STARTED`（含文件前置 Hash 与期望结果），完成后结果与事件同事务提交。SQLite 事务无法与文件修改构成跨资源原子事务，因此异常中断后通过**有限状态核对**处理：能与预期一致的补记成功，仍为原始状态的判定未生效，无法解释的标记 `UNKNOWN` 并终止——不自动重放工具调用。这是务实的"可诊断、保守失败"策略，不是完整的 Durable Execution。

**任务级三层记忆，不引入向量数据库。** Execution Memory（SQLite 完整事件）、Working Memory（任务目标、已修改文件、最近验证结果——以实际工作区与执行记录为准，不信任模型自述）、Model-visible Context（按预算构造的视图）。上下文裁剪以完整的"模型—工具轮次"为单位，不破坏 Call/Result 配对；首版使用确定性选择而非 LLM 摘要或 Embedding，先建立可复现、可解释的基线。

**预算与循环守卫。** 默认 Main 30 次模型请求、全局 100 次工具请求、任务 20 分钟、单命令 120 秒（均为初始工程值，非实验最优）。无进展检测在重复相同动作且目标文件未变化时先注入警告、后终止。

**模型接入：OpenAI 兼容协议 + 自定义 Gateway。** 首版验证一条端到端路径（DeepSeek Chat Completions Tool Calling）。模型由 ProviderAdapter 适配，"OpenAI 兼容"不等于任意模型可直接替换——特殊推理字段与消息续接必须经过协议兼容测试。模型能力与 Harness 实现分离，后续可用相同评测比较不同模型。

---

## 三层质量保障的区分

整个系统最有价值的设计之一——三个机制回答三个不同层次的问题，互不可替代：

| 机制 | 核心问题 | 判断方式 |
| --- | --- | --- |
| **Reviewer** | 这个 Patch 是否存在潜在逻辑问题？ | LLM 语义分析，可能有误报 |
| **Verifier** | Patch 是否满足规定的可见测试与交付条件？ | 确定性工具与测试执行 |
| **Evaluation** | Agent 是否真正解决了独立评测任务？ | 隐藏测试 + 回归验收 |

由此严格区分三个状态：**Runtime COMPLETED ≠ Verification PASSED ≠ Evaluation SUCCESS。** 模型宣称完成、可见测试通过、独立验收成功是三件不同的事。例如：没有可用测试时 Runtime 可以正常结束，但验证状态只能是 `INCONCLUSIVE`，不能假装通过；基线对比按测试身份（而非失败数量）进行——目标 Bug 的测试基线失败、修复后仍失败 = 修复未完成；评测器在干净的 Base Commit 上重新应用最终 Patch 再跑隐藏测试，检验的是 **Patch 能否独立复现修复效果**，同时避免 Agent 工作区残留影响结果。

---

## 评测与对照实验

项目不以"Agent 能跑起来"为成功标准。计划建立 **20–30 个可复现的 Python Bug 修复任务**（可见测试与隐藏验收严格分离；含少量不可完成任务单独统计"合理报告不确定性"的能力），指标分三层：

- **业务效果**：独立验收成功率、回归率；
- **执行效率**：Token / 成本 / 时延 / 调用次数；
- **运行可靠性**：无效工具调用率、基础设施故障率、预算耗尽率——pytest 断言失败是业务反馈，不计入基础设施故障。

核心对照实验回答"独立审查是否有价值"：

| 组 | 配置 |
| --- | --- |
| A | Main Agent + Verifier（单 Agent 基线） |
| B | Main Agent + Verifier + Reviewer（双 Agent） |
| C | 预算匹配的单 Agent（获得接近 B 的总调用/成本预算） |

设置 C 组的原因：B 的总预算天然高于 A，B 更好可能只因推理资源更多。只有预算匹配对照才能回答——**独立审查的价值是否超过单纯增加推理预算**。即使实验结论是 Reviewer 收益小于成本，也是有效的工程结论（据此回退单 Agent 基线），而非项目失败。目前所有指标均为定义，实际数值以实验数据为准。

---

## 快速开始

环境要求：Python 3.11+、Git；运行集成测试与全链路演练需要 Docker Desktop（Linux 优先，Windows 建议 WSL2 后端）。

```bash
# 1. 克隆并安装（src 布局，editable 安装）
git clone https://github.com/JOETONGJY/repofix.git
cd repofix
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. 单元测试（无需 Docker / 无外部依赖）
pytest -m "not integration"

# 3. 构建预构建执行环境镜像（集成测试与演练需要）
docker build -f docker/repofix-env.Dockerfile -t repofix-env:latest docker/

# 4. Docker 集成测试（容器隔离约束、真实 ripgrep、超时终止、Preflight）
pytest -m integration

# 5. 全链路演练：Scripted 模型驱动真实 Runtime + 真实工具链
#    search → read → patch → test → finish，产出可独立 git apply 的 Patch 与 Trace
PYTHONPATH=. python scripts/export_m2_full_drill.py
```

演练产物输出在 `repofix-data/runs/<run_id>/`：`changes.patch`（可独立 `git apply`）、`trajectory.jsonl`（结构化执行轨迹）、`result.json`（运行结果）。

---

## 项目结构

```
repofix/
├── 技术方案探讨.md        # 架构 HLD：16 项 ADR、54 个问题的设计回答（冻结于 V1.10）
├── design/                # 权威模块详细设计（00–09，全部冻结）
├── specs/                 # 实现契约与验收测试编号（01–04）
├── plan.md                # 实施计划：五阶段路线、任务单、进度记录
├── docker/
│   └── repofix-env.Dockerfile   # 预构建执行环境（受限容器镜像）
├── scripts/
│   ├── export_m1_sample_trace.py      # M1 协议闭环样例 Trace
│   └── export_m2_full_drill.py        # M2 真实工具链全链路演练
├── src/repofix/
│   ├── contracts/         # 公共契约：枚举 + Pydantic 模型（specs/01 权威定义）
│   ├── runtime/           # AgentRunner 主循环、状态机、批次调度、LoopGuard
│   ├── tools/             # 静态工具注册表（按角色生成视图）+ 单次调用分发器
│   ├── workspace/         # 仓库副本管理、Docker CLI 封装、受限工作区、Preflight
│   ├── state/             # SQLite 三表存储（意图先行、崩溃对账、Trace 导出）
│   ├── context/           # 模型可见消息构造（配对完整性强制）
│   ├── gateway/           # 模型接入协议（M3 接入真实 Provider）
│   └── verifier/          # 确定性验证接口（M3 接入真实实现）
└── tests/                 # 单元测试 + Docker 集成测试（integration 标记）
```

---

## 路线图与当前进度

按纵向切片推进：每个阶段都能运行、测试与验收，先验证协议正确性，再逐层替换为真实能力。

| 阶段 | 目标 | 状态 |
| --- | --- | --- |
| **M1** | 真实 Contracts / Runtime / 调度 / SQLite + Scripted 外部能力 | ✅ 完成（含集成评审修订：权限优先校验、双层输出截断、验证错误语义、请求预算计数；100 项测试） |
| **M2** | 真实 Git 副本 + Docker 沙箱 + 六工具 + Preflight + Patch 导出 | ✅ 完成（143 项测试含 11 项 Docker 集成；容器约束实测；全链路演练通过；Patch 独立应用验证通过） |
| **M3** | DeepSeek 接入 + Context Engine + Task CLI + 真实 Verifier，第一次真实修复 | 进行中 |
| **M4** | Read-only Review Agent 与反馈闭环 | 未开始 |
| **M5** | 隐藏验收、指标统计与对照实验 | 未开始 |

诚实的进度声明：项目已有完整的设计文档体系与经过测试的 Harness 实现（Runtime 协议、工具系统、隔离工作区均可运行验证），但**尚未产生真实模型修复成功率、Reviewer 提升幅度等实测数据**——这些在 M3–M5 完成后由实验给出，不预先声称。

---

## MVP 明确不做什么

裁剪不是实现能力不足，而是让每项功能都有明确的业务价值与验证标准：

| 不支持 | 原因 |
| --- | --- |
| Web UI / IDE 插件 / 远程服务 | CLI 已足够验证核心执行能力 |
| 多语言 / 多仓库任务 | 先解决 Python 单仓库 Bug 修复 |
| Search Agent / Planner / 多级子 Agent | 没有足够的角色独立性收益 |
| 多 Agent 并发写入 | 冲突与一致性问题大于收益 |
| 任意 Shell / 运行时自动装依赖 | 执行边界过宽；依赖走预构建镜像 |
| MCP 动态工具生态 | 固定工具集合已覆盖首版需求 |
| Embedding / 向量库 / 跨任务长期记忆 | 暂无跨任务检索需求 |
| LLM 自动上下文摘要 | 先建立确定性上下文基线，后续作为可评测策略 |
| 自动 Commit / Push / PR | 保持用户仓库只读，Patch 供人工检查 |
| 自动 Resume / 分布式恢复 | 首版保守终止并保存可诊断状态 |
| 人机审批流 | 首版无权限提升类操作，越权直接拒绝 |

---

## 文档体系

项目采用三层文档驱动开发，全部随代码版本化：

| 层 | 文档 | 内容 |
| --- | --- | --- |
| 架构 HLD | [技术方案探讨.md](技术方案探讨.md) | 16 项 ADR 决策与理由、54 个工程问题的分类回答、架构收敛过程 |
| 模块 LLD | [design/00–09](design/) | 各模块权威详细设计（全局契约、Runtime、Gateway、工具、工作区、状态、上下文、验证器、评测） |
| 实现契约 | [specs/01–04](specs/) | 数据契约、Runtime 语义、工具与工作区规格、验收测试矩阵 |
| 实施计划 | [plan.md](plan.md) | 五阶段任务单、验收标准、集成评审清单与逐阶段进度记录 |

---

## 常见设计追问

**与普通 ReAct Agent 的区别？** ReAct 描述的是"推理—行动—观察"的基本模式；本项目在此基础上明确了工具批次语义、角色权限、预算控制、结构化事件、失败分类、状态转换、验证与独立评测——把一个研究性循环工程化为可测试的 Harness。

**为什么不让模型自己决定什么时候结束？** 自然语言声明不等于任务完成。只有单独提交的 `finish` 控制动作能触发最终验证，由 Runtime 根据真实 Patch 与验证结果决定是否接受。

**为什么不让 Reviewer 直接修改代码？** 独立审查的价值来自不同上下文与明确职责：Reviewer 只发现问题，Main 负责修改——避免写入冲突，也使 Reviewer 的贡献可以被归因度量。

**为什么不用向量数据库检索仓库？** 首版的核心困难是可靠定位与修改代码，不一定是语义检索规模。先用 ripgrep + 文件树 + 按需读取建立可复现基线，后续通过成功率与检索成本数据决定是否引入更高级的检索策略。

**如何区分是模型的问题还是 Harness 的问题？** 依靠结构化 Trace 与四层故障归属：模型 API 失败、工具系统拒绝、工作区执行错误、业务结果失败分别记录。pytest 退出码 1 是模型可利用的业务反馈，不算基础设施故障。

**Prompt Injection 怎么防？** 仓库代码与工具输出一律视为低信任数据，其中的文本不能改变系统权限；真正的权限由 Runtime、工具注册表与 Workspace 强制执行，不依赖模型是否听从提示词；模型密钥不进入容器。

**最大的技术风险？** 模型在复杂仓库上的定位与修复能力、上下文裁剪丢失关键信息、工作区副作用一致性、Reviewer 成本收益不达标。四类风险均有对应的 Trace、测试或评测指标覆盖，而不是只看最终成功率。

---

## License

暂未设置开源许可证（个人研究项目）。文档与代码内容可直接阅读参考；如需转载或引用，请注明来源。
