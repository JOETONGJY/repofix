# RepoFix 实施计划（plan.md）

> **文档定位**：工程实施阶段的项目计划——从架构设计阶段（已冻结于技术方案探讨.md V1.9）转入按功能闭环分阶段开发。
> **上位文档**：[技术方案探讨.md](技术方案探讨.md)（架构 HLD）· [design/00–09](design/)（权威 LLD）· [specs/01–04](specs/)（实现契约）。
> **核心思路**：**Vertical Slice（纵向切片）+ 渐进集成**——不按九个模块逐个实现，而是按功能闭环分五个阶段（M1–M5），每个阶段都能运行、测试和验收。
> **项目目标回顾**：交付一个能"接收 Python 仓库 Bug 修复任务 → 自主检索和修改代码 → 执行测试 → 独立审查 → 输出 Patch、Trace 和评测结果"的 Coding Agent。

---

## 总原则

### 三条工程纪律（每阶段通用）

1. **每一阶段必须有可执行的完成证据**——不能因为代码文件已生成就标记模块完成；必须有对应测试、运行结果和已知限制。
2. **实现必须沿着现有架构演进**——不允许为了方便重新引入 LangGraph、MCP、通用 Multi-Agent 框架，或把模型、工具、状态、Workspace 写进一个巨型 Runner。
3. **每阶段结束做一次小型集成评审**——检查接口是否符合设计、行为是否符合预期、有无过度抽象、能否安全进入下一阶段。

### 分工

| 工作 | 负责方 |
| --- | --- |
| 架构边界、关键技术取舍 | 共同确定 |
| 具体 Python 实现 | GLM（编码执行） |
| 实现中发现的设计冲突 | GLM 提出，共同分析 |
| 代码评审与架构一致性检查 | GPT 协助检查 |
| 自动化测试与修复 | GLM 实施，依据测试结果复核 |
| 下一阶段交付范围 | 根据上一阶段实际结果决定 |

**控制的是接口、行为和验收结果，不是每个私有函数怎么写。** 例如：实现者可自行决定是否将 `ToolBatchScheduler` 做成独立类；但不能改变"工具失败后停止当前批次"的协议。

### 阶段推进规则

- 每阶段的验收标准绑定 specs/04 的测试编号，测试全绿才进入下一阶段。
- 实现中发现设计歧义 → 列入交付报告的「设计歧义」节 → 确认后**同步修订 design/ 与 specs/**，再继续编码（不允许代码与文档长期分叉）。
- 未经评审不得跳阶段；M3 与 M4 之间必须有一次真实任务的运行记录。

---

## 五阶段路线总览

| 阶段 | 名称 | 核心交付 | 外部依赖 |
| --- | --- | --- | --- |
| **M1** | Contracts + 最小 Agent Loop（**真实 Harness 核心 + Scripted 外部适配**） | 项目骨架、公共契约、核心状态机、Tool Call 调度、SQLite 基础记录；**仅外部能力使用 Fake/Scripted**（ModelClient / ToolExecutor / Verifier），Harness 核心全部真实 | 无（不耗 Token、不启动 Docker） |
| **M2** | 真实 Workspace + Tool System | Git Clone、Docker Sandbox、六个工作区工具、四层校验、Preflight、Patch 导出 | Docker（无 LLM） |
| **M3** | 真实 Coding Agent 闭环 | LLM Gateway、Context Engine、Task CLI、Verifier——**第一次真实修复 Python Bug**（单 Agent Baseline） | LLM API + Docker |
| **M4** | Read-only Review Agent | 独立审查上下文、结构化 Findings、一次有界反馈修正——升级为方案 C（双 Agent） | 同 M3 |
| **M5** | Evaluation Harness + 工程收尾 | 独立验收任务集、成功率统计、Trace 分析、单/双 Agent 对照实验（含预算匹配组） | 同 M3 + 评测资产 |

依赖关系：

```
M1（协议正确性）→ M2（真实工具）→ M3（真实闭环）→ M4（双 Agent）→ M5（评测与收尾）
                     └──────────── M2 完成后可与 M3 的 Gateway 开发并行准备 ────────────┘
```

**关键顺序决策——M3 先于 M4**：虽然已选定双 Agent，但必须先跑通 Main Agent 再引入 Reviewer——否则出 Bug 时无法判断是主循环、工具系统还是 Agent 协作机制的问题。

### 技术选型决策（已确认，M1 启动前冻结）

| 项目 | 决策 |
| --- | --- |
| **模型 Provider** | **DeepSeek**（正式确定）：Base URL `https://api.deepseek.com`；OpenAI 兼容 Chat Completions；初始模型 **`deepseek-flash`**，对照可选 `deepseek-v4-pro`（2026 新命名；旧 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07 退役，不作默认值）；API Key 从 `DEEPSEEK_API_KEY` 环境变量读取，**不写入源码 / 日志 / 仓库**。首次协议冒烟用 Non-thinking Mode；后续评估 Thinking Mode 并正确保留 `reasoning_content`（否则带工具的下一轮请求可能报错）。费用不预设固定月度预算——M3 记录真实调用 / Token / 费用后再校准 |
| 开发环境 | 推荐 WSL2 Linux + Docker Desktop WSL 集成；**M2 前确认实际环境**（是否已装 WSL2/Docker——待确认项）。M1 不依赖 Docker |
| 测试仓库 | M3 初期用小型**合成 Python Bug 仓库**（可控可复现）；之后加入固定 Commit 的真实开源仓库 Bug 体现 Repository-Aware 能力；不用公司私有代码 |
| Git / GitHub | M1 开始 `git init`，按阶段本地提交；GitHub **私有仓库**；项目成熟并检查无密钥 / 敏感 Trace 后再考虑公开 |
| 工具链 | Python 3.11+（本机 3.12.7）、uv（未装则以 venv+pip 起步，pyproject 保持 uv 兼容）、pytest（+按需 pytest-cov）、ruff、mypy（不阻塞主线） |
| CLI | Typer |
| SQLite | 标准库 `sqlite3` + 轻量 DAO，不用 ORM |
| Docker 操作 | subprocess 调用 Docker CLI（参数数组）；容器生命周期 M2 定 |
| 包结构 | 包名 `repofix`；`src/repofix/` + `tests/`；Prompt 作为包资源版本化（`src/repofix/prompts/`） |

> M1 / M2 不调用 DeepSeek API；Provider 决策只影响 Gateway 接口设计与 M3 的 ProviderAdapter 实现。

---

## M1：Contracts + 最小 Agent Loop

**目标**：证明 RepoFix 的 Agent Loop 协议正确，而不是先证明模型能写代码。

### 真实 / Fake 边界（M1 定稿：Fake 外部能力，不 Fake 核心行为）

**核心 Harness 组件从 M1 就是真实实现；只有尚未接入的外部能力使用 Fake / Scripted。** 否则 M1 只是模拟了一套系统，M2/M3 又要重新实现另一套，之前的验证价值大幅降低。

| 组件 | M1 实现 | 理由 |
| --- | --- | --- |
| 公共 Contracts / Pydantic | **真实** | 后续所有模块复用 |
| Agent Runtime / Agent Loop | **真实** | M1 的核心交付 |
| Tool Batch Scheduler | **真实** | 验证真实调度协议 |
| LoopGuard / 状态机 | **真实** | 避免后续重写预算逻辑 |
| SQLite State & Telemetry | **真实** | 验证真正的事件持久化 |
| Tool Registry / Dispatcher | **真实最小版** | 参数校验、权限判断、分发流程真实 |
| Context Engine | **最小消息构造**（保证下一轮请求包含正确的 Tool Call/Result 关联） | 完整 Context 与 Memory 在 M3 |
| LLM Gateway | Fake ModelClient（Scripted） | 尚未接入外部 API |
| Tool Executor / Workspace | Fake / Scripted Executor | 不启动 Docker、不碰真实仓库 |
| Verifier | Fake Verifier（预设通过 / 失败 / 无法验证） | M3 接真实 PatchValidator + TestRunner |
| Reviewer | 不实现（M4） | 公共契约保留 actor、REVIEWING 等冻结枚举，不建空壳 |
| Evaluation Harness | 不实现（M5） | 同上 |

**两条 Fake 纪律**：① Fake 实现与未来真实组件**使用相同接口契约**（M2/M3 通过替换 Adapter 接入真实能力，而不是重写 Harness）；② Fake 仅放在测试辅助代码中，不为它建复杂的生产级 Mock 框架——简单测试函数够用就不加抽象层。

**接口注入方式**（Runtime 只依赖抽象能力）：

| 接口 | M1 使用 | 后续替换 |
| --- | --- | --- |
| `ModelClient` | `ScriptedModelClient` | M3 真实 ProviderAdapter |
| `ToolExecutor` | `FakeToolExecutor` | M2 DockerWorkspace |
| `Verifier` | `FakeVerifier` | M3 PatchValidator + TestRunner |
| `EventStore` | 真实 SQLite 实现 | 无须替换 |
| `Clock`（可选） | 测试可控时间 | 生产单调时钟 |

**M1 只实现 Main Agent 运行路径**：`review_enabled = False`；合法 Finish 经 Fake Verifier 接受后直接完成（REVIEWING 转移在 M4 才有运行逻辑）。

### M1 验证场景示例

```
ScriptedModelClient
 ├── Response 1: read_file("auth.py")
 ├── Response 2: apply_patch(...) + run_command("pytest")
 └── Response 3: finish("修复完成")

FakeToolExecutor: read_file → SUCCEEDED；apply_patch → SUCCEEDED；run_command → SUCCEEDED, exit_code=0
FakeVerifier: VerificationReport → PASSED

真实 Runtime 执行全流程 → 真实 SQLite 记录事件
```

测试检查的是**真实 Runtime 的行为**：三阶段执行顺序、Call ID 与 ToolResult 关联、状态转换、SQLite 可查询、失败 / 预算耗尽的正确终止。**Fake 不得提前生成"看起来正确"的 Trace 或 RunResult——这些必须由真实 Runtime 产生。** 同时验证**下一轮模型请求中包含正确的 Tool Call/Result 配对**（否则模型—工具闭环只验证了一半）。

### M1 / M2 职责边界

| | M1 | M2 |
| --- | --- | --- |
| Tool Registry | 真实基础结构 | 添加具体工具定义 |
| 参数校验 | 通用 Pydantic 校验链路 | 完善每个工具的语义约束 |
| Tool Dispatcher | 真实调度入口 | 接入真实 Executor |
| Workspace / Tool Execution | 不实现真实文件访问；Fake 预设结果 | Git Clone + Docker；真正读、搜、改、运行 |
| State & Telemetry | 真实 SQLite | 继续复用并记录真实工具结果 |
| Agent Runtime | 真实 | 只做必要集成修改 |

M2 结束时得到的**不是一套新系统，而是 M1 Harness 获得真实执行能力**。

### M1 实施蓝图（规划稿，待确认后启动编码）

#### 环境现状（本机已核实）

| 项 | 状态 |
| --- | --- |
| Python | 3.12.7 可用（满足 3.11+） |
| uv | **未安装**——以 `python -m venv .venv` + pip 起步；pyproject.toml 按 uv 兼容格式写，之后可无缝迁移 |
| git | 2.34 可用；M1 第一步 `git init` |
| Docker / WSL2 | **待确认**（GPT 提出的问题尚未回答：是否已装 WSL2 + Docker Desktop？只影响 M2 准备步骤，不阻塞 M1） |
| M1 依赖 | 仅 `pydantic` + `pytest`（dev）；不引 CLI / Docker / HTTP 库 |

#### 代码结构规划

```
repofix/（项目根 = 当前目录）
├── pyproject.toml              # src 布局；uv 兼容；依赖 pydantic，dev 依赖 pytest/ruff
├── .gitignore                  # .venv/、repofix-data/、__pycache__、*.sqlite 等
├── src/repofix/
│   ├── contracts/              # 【真实】specs/01 的完整落地
│   │   ├── enums.py            #   RunStatus / ExecutionStatus / SideEffectStatus / ToolErrorCode /
│   │   │                       #   EventType / AgentActor / FailureLayer / Verification 相关枚举
│   │   └── models.py           #   TaskSpec / RunConfig / ModelRequest / ModelResponse /
│   │                           #   ToolCall / ToolResult / AgentEvent / WorkingState /
│   │                           #   VerificationRequest / VerificationReport / RunResult
│   ├── gateway/
│   │   └── client.py           # 【真实接口 + 测试 Scripted】ModelClient Protocol；
│   │                           #   ScriptedModelClient 放 tests/（同接口契约）
│   ├── tools/
│   │   ├── registry.py         # 【真实】ToolRegistry：名称 → Schema + 副作用类别 + 角色可见性
│   │   └── dispatcher.py       # 【真实】四层校验中前三层（权限 / Schema / 语义占位），
│   │                           #   ToolExecutor 以 Protocol 注入（M2 替换为 DockerWorkspace）
│   ├── runtime/
│   │   ├── state_machine.py    # 【真实】RunStatus 转换表；非法转换抛异常并写事件
│   │   ├── loop_guard.py       # 【真实】预算检查 + 无进展检测（阈值读 RunConfig，不硬编码）
│   │   ├── scheduler.py        # 【真实】0/1/N 顺序调度、失败 SKIPPED、finish 整批拒绝
│   │   └── runner.py           # 【真实】AgentRunner 主循环（Turn 编排、VERIFYING、预算、终止）
│   ├── state/
│   │   └── store.py            # 【真实】sqlite3 三表 DAO：意图先行写入、崩溃对账
│   │                           #   （reconcile：STARTED 无完成记录 → UNKNOWN / PROCESS_INTERRUPTED）、
│   │                           #   trajectory.jsonl 导出
│   ├── context/
│   │   └── builder.py          # 【最小】消息构造：system + task + 轮次配对追加；
│   │                           #   保证下一轮请求包含完整 Tool Call/Result 配对
│   └── verifier/
│       └── base.py             # 【真实接口 + 测试 Fake】Verifier Protocol；FakeVerifier 放 tests/
└── tests/
    ├── conftest.py             # ScriptedModelClient / FakeToolExecutor / FakeVerifier / 内存时钟
    ├── test_contracts.py       # 契约 schema 校验（合法/非法用例）
    ├── test_state_machine.py   # RT-09
    ├── test_scheduler.py       # RT-03/04/05/06
    ├── test_loop_guard.py      # RT-10/11
    ├── test_store.py           # ST 系列：意图先行、对账、Trace 导出
    └── test_runner_e2e.py      # RT-01/02/07/08/12/13 + 配对可见性 + 端到端闭环
```

#### 关键设计要点（编码时的约束）

1. **依赖方向**：runtime → tools/state/context/gateway/verifier 的 **Protocol 接口**；contracts 被所有模块引用；无循环依赖。
2. **finish 是控制动作**：在 scheduler/runner 拦截，不进 ToolExecutor；混合批次整批拒绝（specs/02 V1.10）。
3. **意图先行**：write/process 类调用先写 tool_calls（PENDING + STARTED 同事务）再执行；结果与完成事件同事务。
4. **预算计数**：模型调用在 runner 计数（含未来重试的 API 次数接口）；工具调用全局计数器（跨 actor）；墙钟用注入 Clock。
5. **LoopGuard 阈值**：只从 RunConfig 读取；规范化参数哈希 = `sha256(tool_name + json.dumps(args, sort_keys=True))`；"目标文件未变化"以可注入谓词表达（M1 默认视为未变化，M2 接真实文件状态）。
6. **Fake 纪律**：三个 Fake 全部放 tests/，与真实组件同接口；ScriptedModelClient 记录收到的 messages 供配对断言；**Fake 不生成 Trace/RunResult**。
7. **review_enabled=False**：M1 不写 ReviewRunner；契约保留 actor / REVIEWING 枚举。

#### 实施步骤（每步可独立验证）

| 步骤 | 内容 | 验证 |
| --- | --- | --- |
| 0 | git init + .gitignore + pyproject + venv + 依赖安装 + ruff 配置 | `pip install -e .[dev]` 成功 |
| 1 | contracts（enums + models） | test_contracts 通过 |
| 2 | state/store（三表 DAO + reconcile + 导出） | test_store 通过 |
| 3 | runtime/state_machine | test_state_machine 通过 |
| 4 | tools（registry + dispatcher，executor Protocol） | 单测 + Fake executor 冒烟 |
| 5 | runtime/scheduler | test_scheduler 通过 |
| 6 | runtime/loop_guard | test_loop_guard 通过 |
| 7 | context/builder + gateway/verifier Protocol | 配对单测 |
| 8 | runtime/runner 主循环 + conftest 三 Fake | **test_runner_e2e 全绿 = M1 验收达成** |
| 9 | 全量回归 + ruff + git commit（M1 里程碑提交） | 交付报告 |

#### M1 交付物清单（编码完成后的输出）

代码变更 + 全部测试运行结果 + 一条 Scripted 端到端 Trace 样例（SQLite 导出的 trajectory.jsonl）+ 模块完成情况与设计文档对应表 + 未实现列表 + 发现的设计歧义。

### 任务单（可直接交给编码执行）

#### 任务定位

你是 RepoFix Agent 项目的实现工程师。本阶段开始工程实现，但必须严格遵循项目已有的架构设计，不得自行扩展产品范围。**本次仅实现 M1：Contracts + 最小 Agent Loop。**

#### 设计依据

先阅读：`design/00-global-contracts.md`、`design/01-task-interface.md`、`design/02-agent-runtime.md`、`design/03-llm-gateway.md`、`design/04-tool-system.md`、`design/06-state-telemetry.md`，以及 `specs/` 中与上述模块相关的实现契约。如文档存在实质冲突，先明确列出，不得自行选择不同的外部接口或执行语义。以最新已确认的一致性修订为准；不要根据旧版文档恢复已取消的功能。

#### 实现范围

1. 建立简洁的 Python 项目结构和测试环境。
2. 实现统一的公共数据契约与枚举（Pydantic；项目采用类型注解）。
3. 实现 Main Agent 的最小执行循环（真实 Runtime：Loop、Scheduler、状态机、LoopGuard）。
4. 实现 0/1/N Tool Call 的顺序调度协议。
5. 实现核心状态转换、预算检查、有限协议纠正。
6. 实现基础 SQLite Run / Event / Tool Call 持久化（三表模型，意图先行写入）。
7. 编写测试专用 ScriptedModelClient、FakeToolExecutor、FakeVerifier（与真实组件同接口契约，仅放测试辅助代码）。
8. 实现最小消息构造（保证下一轮模型请求包含正确的 Tool Call/Result 配对）。
9. 编写关键架构行为的自动化测试。

#### 必须遵守的规则

- Agent Runtime 拥有状态转换和工具批次调度权。
- Gateway 不执行工具；Tool Dispatcher 不自主调用模型。
- 一个模型响应包含多个 Tool Call 时，按顺序执行。
- 批次中出现失败，后续调用必须标记为 SKIPPED。
- 工具执行前先记录执行意图。
- 不自动重试可能产生副作用的工具。
- Finish 是 Runtime 控制动作，不是普通 Workspace 操作。
- `UNKNOWN` 是持久化执行状态（tool_calls.side_effect_status），不是普通 ToolResult 的成功/失败状态。
- 不得让模型自然语言声明直接改变 RunStatus。
- 所有执行均受配置预算限制。

#### 本阶段不实现

真实 LLM API、Git Clone / Docker Sandbox、真实代码编辑和 Shell 执行、Context 压缩、Read-only Review Agent、真实 pytest Final Verification、Evaluation Harness、MCP / LangGraph / 多 Agent 通信框架、Web UI / IDE / 分布式执行。上述只保留必须使用的**接口边界**，不写无实际用途的空壳框架；Fake 组件仅用于测试，不伪装成生产实现。

#### 验收要求（对应 specs/04 测试编号）

| 验收场景 | 必须实现的行为 | 测试编号 |
| --- | --- | --- |
| 模型返回 0 / 1 / N 个 Tool Call | 顺序执行，Call ID 与结果一一对应 | RT-01/02/03 |
| 第二个工具失败（写入类） | 后续工具标记 SKIPPED，不执行；成功结果保留 | RT-04、TL 系列（Fake 层） |
| 只读失败后跟写入调用 | 写入调用 SKIPPED | RT-05 |
| 模型返回纯文本 | 不直接结束，执行有限协议纠正（一次重发，再空则 FAILED） | RT-13 |
| Finish 与普通工具混合 | **整批拒绝**：不执行任何调用（含只读、无任何副作用）；finish 得 INVALID_ARGUMENT（提示单独提交），其余调用 SKIPPED（skipped_reason=batch_rejected_finish_mixed），配对完整；下一轮单独 finish 可被接受 | RT-06 |
| Finish 单独调用 | 进入 VERIFYING，由模拟 Verifier 返回结果并按规则转移 | RT-07/08、RT-09（状态转换表） |
| 预算耗尽（次数 / 时间参数化） | 正确终止 BUDGET_EXCEEDED，不再调用模型 | RT-10 |
| 无进展检测 | 注入警告与终止阈值以 **specs/01 RunConfig 默认值为唯一权威**（repeat=3 / terminate=5，可配置）；Runtime 不得硬编码另一套数字 | RT-11 |
| 越权工具（Fake 层） | 直接 PERMISSION_DENIED 回流 | RT-12 |
| 执行事件 | 关键状态和工具意图可从 SQLite 查询；崩溃注入对账（Fake 层） | ST-01/02/04 |

所有测试必须可以在**不访问外部模型 API、不启动 Docker** 的情况下运行。

#### 交付要求

完成后提交：实际代码变更；测试及运行结果；当前模块完成情况；与设计文档的对应关系；尚未实现的功能列表；发现的设计歧义或潜在问题。不要求实现所有九个模块。**优先保证协议正确、结构简洁、测试可重复，而不是代码量或抽象层数量。**

### M1 退出条件

- 上表全部测试通过（CI 可重复执行）。
- Fake 环境下完成一次确定性 Agent 执行闭环：正常终止、产出结构化事件、可从 SQLite 导出 trajectory.jsonl。
- 集成评审通过（接口与 design/specs 一致、无空壳抽象）。

---

## M2：真实 Workspace + Tool System

**目标**：让工具真实作用于代码——真实 Git 仓库副本、Docker 沙箱、文件检索 / 编辑 / 命令执行全链路可用，但**仍不接真实 LLM**（用脚本化 ModelClient 按预设序列驱动真实工具，复用 M1 的 Fake 骨架换成 Scripted 驱动）。

### 交付项

1. `WorkspaceManager`：独立 Clone（不共享对象存储）→ checkout base_commit → 受限 Docker 容器（非 root、无网络、资源限制、仅挂载任务副本）。
2. `DockerWorkspace`：实现 Workspace 八操作（initialize / read_file / search_code / list_files / apply_patch / run_command / get_changes / close）；apply_patch 原子替换 + sha256 版本校验；run_command argv 白名单 + 进程树终止。
3. 六个工作区工具的真实实现（四层校验完整）：ripgrep 检索（不经 Shell 拼接）、带行号读取并返回 Hash、单文件 Patch、命令执行。
4. `PermissionPolicy` 按 actor 生成工具视图（Reviewer 视图占位即可，M4 才有 Reviewer）。
5. Environment Preflight：仓库校验、Clone、容器启动、依赖检测（预构建镜像）、测试命令检查（`--test-command` 字符串在此解析为 argv + test_plan）、基线测试（区分断言失败与环境阻塞）。
6. Patch 导出：`get_changes` 相对 base_commit 的完整 Diff（含未跟踪新文件、排除缓存/忽略/敏感内容）；`changes.patch` 可独立 `git apply`。
7. 脏工作区拒绝逻辑（CLI 校验层）。

### 验收标准（绑定 specs/04）

| 场景 | 测试编号 |
| --- | --- |
| 隔离性：任务前后用户原仓库完全不变 | WS-01/02 |
| 脏工作区拒绝、base_commit 校验 | WS-03、TI-05/06 |
| 容器约束实际生效（非 root / 无网络 / 资源限制 / 无凭据） | WS-04 |
| Preflight 各步失败处理 + 基线记录（断言失败 ≠ 预检失败） | WS-05/06 |
| Patch 导出含 run_command 产生的变化 | WS-07 |
| 工具参数校验 / 路径逃逸 / symlink / Hash 冲突 / 新建覆盖 | TL-01～07、TL-10 |
| 命令白名单外拒绝、超时进程树终止 | TL-08/09 |
| 无匹配搜索 = SUCCEEDED 空结果；输出截断 truncated | TL-11/12/13 |

### 本阶段不做

真实 LLM、Context Engine、Reviewer、Verifier 的 pytest 集成（TestRunner 接口保留）、Evaluation。**验收场景**：用 Scripted ModelClient 在真实仓库副本上完整走一遍 search → read → patch → test 序列，产出合法 changes.patch 和 Trace。

### 退出条件

上表测试全绿（需要 Docker 环境的测试标记为 integration，与单元测试分离）；一个真实小型 Python 仓库上完成脚本驱动的完整工具链演练；集成评审通过。

---

## M3：真实 Coding Agent 闭环

**目标**：接入真实模型，第一次**真正修复 Python Bug**——形成可运行的单 Agent Baseline（review_enabled=False 路径）。

### 交付项

1. `LLM Gateway`：OpenAI 兼容 Chat Completions ProviderAdapter（首个验证端点：DeepSeek 或 GLM 兼容接口，model_id / base_url 由运行配置指定）；ModelResponse 标准化（raw_arguments 保留、provider_metadata 续接字段保存还原）；重试与预算挂钩；**协议兼容测试**（换模型必须过）。
2. `Context Engine`（首版）：四步组装顺序（System Rules + Task → Working Memory → 最近完整轮次 → 预算补充）；Token 公式 `B_input = B_context − B_output_reserve − B_safety`；轮次为单位裁剪、配对完整性；CONTEXT_BUDGET_EXCEEDED 受控失败；文件版本失效规则。
3. `Task Interface` CLI：`repofix run` 完整参数、退出码 0/1/2/3/4/130、输出目录 repofix-data/runs/<run_id>/、RunResult 展示。
4. `Verifier`：PatchValidator + TestRunner（复用 Workspace 执行）；完成规则七种情况；基线按测试身份比较；candidate_revision 版本绑定。
5. LoopGuard / 预算 / 崩溃对账接真实组件。

### 验收标准

| 场景 | 测试编号 |
| --- | --- |
| Gateway：多 Tool Call 保留、无效 JSON、call_id 冲突拒批、续接字段不丢、重试计预算、Usage 缺失不虚构 | GW-01～07 |
| Context：截断保关键错误、配对有效、压缩后关键事实在、预算超限受控失败、轮次单位、文件版本失效 | CX-01～06 |
| Verifier：失败归因四类、finish 后测试失败回修复循环、无测试 INCONCLUSIVE、验证后代码变化旧报告失效、test_plan 不可改写、无 Diff 拒绝 | VF-01、VF-04～08 |
| CLI：退出码矩阵、输出产物（TI-09～12、CLI-01/02） | TI/CLI 系列 |
| **端到端里程碑**：在 2–3 个自选的真实 Python Bug 修复任务上，Agent 自主完成定位 → 修改 → 测试 → finish → 产出合法 Patch 与 Trace | 人工验收 + Trace 审查 |

### 退出条件

上述测试全绿；至少 2 个真实 Bug 修复任务的完整成功 Trace（允许失败案例并存并归因）；单 Agent Baseline 可复现运行；集成评审通过——**这是整个项目第一次端到端可演示**。

---

## M4：Read-only Review Agent

**目标**：从单 Agent Baseline 升级为正式选定的方案 C（双 Agent）。

### 交付项

1. `ReviewRunner`：固定审查阶段（Final Verification 通过后触发）；REVIEWING 状态与转移；最多一轮审查 + 一次受限修正（≤3 次模型调用），第二次 VERIFYING 不再触发 Review。
2. Reviewer 上下文：独立消息历史（不含 Main 推理记录）；ReviewRequest 契约；只读工作区视图（同一候选副本）。
3. 工具视图按 actor 过滤：Reviewer 注册表不含 apply_patch / run_command / finish；`submit_review` 控制动作（≤5 条 Finding、APPROVED 不带未解决 Finding）。
4. patch_hash 版本边界：审查前后核对；修正后标注"已审查旧版本"。
5. Reviewer 独立预算（3 模型 / 8 工具）接入全局计数器（CONS-01）。
6. 异常处理：Reviewer 不可用不阻断已验证任务；INCONCLUSIVE 不算代码验证失败。

### 验收标准（绑定 specs/04 RV 系列 + CONS）

| 场景 | 测试编号 |
| --- | --- |
| 触发条件（通过才启动、断言失败不启动、无测试可启动） | RV-01 |
| 权限隔离（写 / Shell / finish 全拒绝） | RV-02 |
| 上下文隔离（不依赖 Main 历史） | RV-03 |
| 反馈闭环（FINDINGS → 修正 → 重新验证、不二次审查） | RV-04 |
| 有界执行（预算截断、单轮审查） | RV-05 |
| 故障隔离（Reviewer 不可用不影响已验证任务） | RV-06 |
| Patch 版本一致性（reviewed_patch_hash） | RV-07、CONS-03 |
| 双 Agent 预算无双重计数 / 无遗漏 | CONS-01 |

### 退出条件

RV + CONS 测试全绿；M3 的 2–3 个真实任务在双 Agent 模式下重跑通过；一次 FINDINGS → 修正 → 复验的完整 Trace 作为演示样本；集成评审通过。

---

## M5：Evaluation Harness + 工程收尾

**目标**：用数据回答两个问题——**Agent 实际能解决多少问题？Reviewer 是否真正创造了价值？** 形成可展示、可复现、有数据支撑的工程项目。

### 交付项

1. 评测任务集：20–30 个高质量 Python Bug 修复任务（EvalTask 契约；visible / hidden 严格分离，隐藏测试文件不进 Agent 工作区；含少量不可完成任务单独统计）。先以 3–5 个任务验证评测器本身，再扩展规模。
2. `ResultJudge`：干净工作区独立验收（Base Commit 重初始化 → 应用 Patch → 隐藏测试 + 回归检查）。
3. `MetricsAggregator`：三层指标（业务效果 / 执行效率 / 运行可靠性）；统计口径 `成功率 = 独立验收成功数 / 有效且已尝试的可修复任务数`；INCONCLUSIVE 单独报告；成本按 MAIN / REVIEWER 区分。
4. **Experiment G**：A（单 Agent Baseline）vs B（双 Agent）vs **预算匹配对照组**（单 Agent 获得接近 B 的总预算）——同一固定任务清单、关键任务重复运行、报告样本量与波动、Reviewer Finding Precision。
5. 工程收尾：README（快速上手 + 架构说明）、失败案例分析文档（Trace 驱动）、复现说明（固定任务清单 + 版本记录：RepoFix 版本 / 模型 ID / Prompt 版本 / 环境版本 / Patch Hash）。

### 验收标准（绑定 specs/04 EV 系列）

| 场景 | 测试编号 |
| --- | --- |
| 隐藏测试隔离 | EV-01 |
| 独立复现（相同 Patch 相同环境结果一致） | EV-02 |
| 统计口径区分（Runtime 失败 / 隐藏测试失败 / 评测设施异常；预算耗尽留分母） | EV-03、VF-02 |
| INCONCLUSIVE 单独报告 | EV-04 |
| 双 Agent 对照实验可执行 | EV-05 |

### 退出条件

任务集达 20–30 个且建集校验通过；Experiment G 三组对照完成并产出报告（无论结论方向——Reviewer 无价值也是有效结论，对应回退 Baseline 的决策依据）；失败案例分析 ≥5 例归因到模块层；项目可被第三方按 README 复现。

---

## 阶段 ↔ 测试矩阵总览

| specs/04 用例组 | M1 | M2 | M3 | M4 | M5 |
| --- | :-: | :-: | :-: | :-: | :-: |
| RT（Runtime 协议） | ✅ | | 回归 | 回归 | 回归 |
| TL（工具，Fake→真实） | Fake 层 | ✅ 真实 | 回归 | 回归 | |
| WS（Workspace 隔离） | | ✅ | 回归 | 回归 | |
| ST（状态 / 恢复） | ✅ Fake | 补真实 | 回归 | 回归 | |
| GW（Gateway） | | | ✅ | 回归 | 回归 |
| CX（Context） | | | ✅ | 回归 | 回归 |
| VF（Verifier） | Fake 层 | | ✅ | 回归 | 回归 |
| RV（Review Agent） | | | | ✅ | 回归 |
| EV（Evaluation） | | | | | ✅ |
| TI / CLI | 参数层 | ✅ | ✅ 完整 | 回归 | 回归 |
| CONS（跨模块一致性） | 部分 | ✅ | ✅ | ✅ | ✅ |

> 每阶段完成后运行**全量回归**，防止后续阶段破坏已验证协议。

---

## 里程碑集成评审清单（每阶段结束时执行）

1. **接口一致性**：公共契约是否与 specs/01 一致？有无私自增删枚举 / 字段？
2. **行为一致性**：执行语义是否与 specs/02、03 一致（调度、Finish、越权、预算）？
3. **测试证据**：验收测试是否全绿？是否可重复（无外部依赖泄漏到单元测试）？
4. **抽象克制**：有无空壳框架、无实际用途的层、过度设计？
5. **文档同步**：实现中发现并确认的歧义是否已回写 design/ 与 specs/？
6. **进入下一阶段的准备**：依赖是否就绪（Docker / API Key / 任务仓库）？

---

## 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 模型 Tool Calling 质量不达标（无效 JSON、丢 call_id 频发） | M3 反复重试浪费预算 | GW 协议纠正已在设计内；若仍严重，评估换首验 Provider（协议兼容测试保证可替换） |
| Docker 在 Windows 环境行为差异 | M2 隔离测试不稳定 | 优先 Linux 环境；Windows 经验证的 Linux Docker 使用，已知限制记录（design/05 既定） |
| 基线测试过慢拖垮 Preflight / Verifier | M3 时延超预算 | 只跑代表性子集；未建完整基线必须记录（既定规则） |
| Reviewer 误报率高 | M4 修正阶段空转 | Reviewer Finding Precision 在 M5 度量；无价值则回退 Baseline（有效结论，非失败） |
| 评测任务集质量差（环境不可重建 / 验收不区分好坏 Patch） | M5 数据无意义 | 建集校验四项先行；先 3–5 个任务验证评测器再扩规模 |
| 阶段间文档与代码分叉 | 后续阶段基于错误契约开发 | 工程纪律第 5 条：歧义确认后立即回写 design/specs，评审清单逐阶段检查 |
| 范围蔓延（实现者顺手加功能） | 偏离 MVP、拖延里程碑 | 每阶段"不做"清单明确；交付报告含"未实现列表"作为正面交付物 |

---

## 与现有文档体系的关系

| 文档 | 阶段角色 |
| --- | --- |
| 技术方案探讨.md | 架构依据（冻结）；每阶段集成评审的对照基准 |
| design/00–09 | 模块设计权威；实现歧义的回写目标 |
| specs/01–04 | 实现契约与验收测试来源（任务单直接引用编号） |
| **plan.md（本文件）** | 阶段计划、任务单、验收进度追踪 |
| 后续 `07-architecture-decisions.md` 等 | M5 收尾时从上述文档体系化拆出（面向展示） |

**进度记录约定**：每阶段完成后，在下方追加一行完成记录（日期 / 阶段 / 测试结果 / 遗留项）。

### 进度记录

| 日期 | 阶段 | 结果 | 遗留项 |
| --- | --- | --- | --- |
| 2026-10-29 | M1 规划 | 完成：选型决策冻结（DeepSeek flash / Typer / sqlite3 DAO / src 布局…）、真实-Fake 边界、RT-06 整批拒绝同步、实施蓝图（结构/步骤/测试映射） | ① WSL2 / Docker Desktop 是否已装（影响 M2 准备）；② uv 未装，以 venv+pip 起步；③ 编码未启动——蓝图经确认后执行 |
| 2026-10-29 | M1 实现 | 完成：蓝图获用户确认后当日实现——真实 Harness 核心全部落地（contracts / state store / state machine / tools / scheduler / loop_guard / context / runner），三 Fake 全部在 tests/；**89 个测试全绿**（RT-01~13、ST-01~04、TL Fake 层语义、契约 schema）；ruff 通过；样例 trajectory.jsonl 已生成（repofix-data/runs/run_m1_sample_001/）；仓库推送 GitHub JOETONGJY/repofix（**用户改定为 public**，覆盖原私有仓库决策） | ① 集成评审待执行；② 两处设计歧义待共同确认：dispatcher 校验顺序（specs/02「存在性→Schema→权限」vs design/04 流程图「权限先行」，按 specs/02 实现）与输出截断常量（RunConfig.tool_output_truncate_chars=2000 vs specs/03 单次上限 32KiB，按 32KiB 实现，M2 落地具体工具时定稿）；③ WSL2/Docker 状态待用户确认（M2 前置）；④ 全部安装内容置于 E 盘（venv / pip 缓存） |
| 2026-10-30 | M1 修订（集成评审） | 完成：GPT 评审「有条件通过」的修订任务全部落实并回归——① 校验顺序改为**权限优先**（存在性→权限→Schema→语义→预算→执行；design/04 V1.1、specs/02 V0.2、specs/03 V0.3 同步）；② 输出截断**双层限制**（Dispatcher 32 KiB **UTF-8 字节**硬上限含标记不越界 + Context 层 2000 字符软预算，specs/03 V0.3 / design/04 V1.1）；③ `VerificationOutcome.ERROR` 为不可恢复错误 → **Run FAILED**（fail_layer=WORKSPACE），状态机增加 VERIFYING→FAILED（design/08 V1.1、specs/02 V0.2、RT-08 修订）；④ Scheduler 校验/权限拒绝同样触发**批次失败传播**（按副作用类别，RT-14）；⑤ 执行器异常 → `side_effect_status=UNKNOWN` + Run 终止不重试（ST-05，specs/02 V0.2 规则 8）；⑥ 工具预算计数对象 = **工具请求**（含拒绝/跳过，控制动作不计入），实际执行另计；⑦ 模型预算按**每次真实 API 请求尝试**发出前计数（GW-06）；⑧ specs/02 finish 旧契约残留清理（`finish(summary)`，Final Check 第 1 步改为 PatchValidator）。**100 个测试全绿**（新增 RT-14/TL-14/ST-05/字节截断/计数口径用例），ruff 通过 | ① M1 正式关闭待用户确认本轮修订；② WSL2/Docker 环境核实（M2 前置，可与修订并行）；③ M2 真实 Workspace 写入须在本次 Runtime 语义修复回归通过后集成（评审要求） |
