# RepoFix 详细设计 01：Task Interface（模块 1 / 9）

> 版本：V1.0（已冻结）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)。
> 范围：用户如何发起任务、系统如何校验输入、初始化执行环境、处理取消、返回结果。只讨论设计，不进入代码实现。

---

## 一、模块定位与技术选型

### 职责（四件事，不参与模型决策）

| 职责 | 具体内容 |
| --- | --- |
| 任务接收 | 接收仓库路径、自然语言任务、验证命令、运行配置 |
| 输入校验 | 检查参数合法性，任务是否满足 MVP 约束 |
| 任务启动与控制 | 创建 Run、启动 Runtime、传递取消请求 |
| 结果展示 | 输出执行进度、终止原因、Patch 与报告位置 |

**不负责**：模型请求、工具分发、Context 构造、文件编辑、测试执行、直接修改任务状态。

### 为什么是 CLI + 独立 Task/Run API

| 候选 | 优势 | 当前不选的原因 |
| --- | --- | --- |
| **CLI** | 简单、方便自动化、适合开发者、容易复现任务 | **当前选择** |
| Web UI | 交互体验丰富 | 需额外实现前后端和远程会话 |
| IDE 插件 | 与开发过程集成紧密 | 依赖编辑器生态，增加通信与状态同步 |
| GitHub Bot | 可直接处理 Issue | 需要 Webhook、认证和远程执行流程 |

参考 Codex 将客户端入口与核心 Agent Runtime 解耦的思想，但**不复刻** App Server、Thread 协议和复杂客户端。关键取舍：

> CLI 是当前唯一用户入口，但**不能把 CLI 的输入解析和打印逻辑写进 Agent Runtime**——否则以后增加 IDE、API 或批量 Benchmark 时就要修改核心执行循环。

---

## 二、CLI 命令设计

MVP 只实现一个主命令（不做完整命令平台）：

```
repofix run \
  --repo ./my-project \
  --task "Fix the login validation bug"
```

### 参数契约

| 参数 | 必填 | 默认行为 | 说明 |
| --- | --- | --- | --- |
| `--repo` | 是 | 无 | 本地 Git 仓库路径 |
| `--task` | 二选一 | 无 | 直接传入自然语言任务 |
| `--task-file` | 二选一 | 无 | 从 UTF-8 文本文件读取任务 |
| `--base-ref` | 否 | `HEAD` | 修改起点的 Git Commit / Ref |
| `--model` | 否 | 配置或环境中的默认模型 | 指定模型 |
| `--test-command` | 否 | 自动尝试项目中已配置的 pytest 命令 | 任务内可见验证命令（由受控 Workspace 执行，不在宿主机执行） |
| `--output-dir` | 否 | RepoFix 运行数据目录 | 结果输出目录 |
| `--max-model-calls` | 否 | 30 | 模型调用预算 |
| `--max-tool-calls` | 否 | 100 | 工具调用预算 |
| `--timeout` | 否 | 1200 秒 | 单任务执行时限 |

`--task` 与 `--task-file` 必须且只能提供一个。首版不提供可配置的复杂权限模式、多模型路由或长期会话参数。

**`--test-command` 的协议路径（一致性评审明确）**：CLI 输入是字符串，但**仅在任务初始化（Preflight）阶段被解析并校验**为结构化 `argv` 数组与 `test_plan`；此后 Runtime / Tool System / Verifier 只消费结构化形式，不再重复解析 Shell 字符串（与 design/04、design/08 一致）。

**明确决定：一个 `repofix run` 只对应一次独立任务执行**——不支持同一 CLI 任务中连续提新需求，也不支持任务完成后继续聊天（与单任务 Benchmark 模式一致）。

---

## 三、TaskSpec / RunConfig / Run ID 的划分

避免两个对象承担相同职责：

- **TaskSpec**：描述**做什么**——`instruction`、`repo_path`、`base_commit`（由 `--base-ref` 解析为完整 Hash）、`test_command`。
- **RunConfig**：描述**怎么跑**——沿用全局契约默认值（30 / 100 / 1200 / 120 / 重试 2 / 网络禁用）。
- **run_id**：标识一次实际执行。

**为什么 TaskSpec 不放 `task_id`**：TaskSpec 描述任务内容，一次任务可能因不同模型或配置而重复执行。Benchmark 场景中同一 TaskSpec 可用不同模型执行多次，不会混淆任务定义与运行记录。

任务启动时固定 RunConfig，记录模型 ID、配置版本和执行预算；**模型不能在运行中自行提高预算、修改执行环境或改变用户任务**。

### MVP 审批裁剪（明确决策）

**MVP 不实现人机审批，越权操作直接拒绝。** 理由：首版既不允许修改原仓库，也不支持自动 Commit / Push、宿主机命令执行和沙箱权限升级——没有审批需要服务的真实业务操作。因此：

- 全局状态机的 `WAITING_APPROVAL` **保留为未来扩展状态**，不进入第一版活动状态集合。
- `approval_mode` 暂不实现，CLI 不做形同虚设的审批界面。
- 若未来引入仓库写回、网络访问或外部服务操作，再设计审批对象与授权范围。

---

## 四、任务启动流程

```
CLI 参数解析
→ 输入与 Git 仓库校验
→ 解析 Base Commit + 创建 RunConfig
→ 创建 Run ID 与运行记录
→ 调用 Agent Runtime
→ Runtime 调用 Workspace Manager 执行 Preflight
→ 返回 RunResult 并展示
```

职责边界：**Task Interface 检查输入是否合法；Workspace Manager 检查并准备仓库的实际执行条件**（`--repo` 是否为空是 CLI 的责任；隔离 Clone、依赖检测、基线测试由 Workspace / Preflight 负责）。这样 CLI 不会变成包含 Git、Docker 和测试逻辑的庞大模块。

---

## 五、仓库边界：未提交修改怎么办（明确决策）

**MVP 仅处理指定 Commit 的快照，默认拒绝存在未提交修改的工作区**：

| 情况 | 处理 |
| --- | --- |
| 干净的 Git 仓库 | 允许执行 |
| 存在未提交的已跟踪文件修改 | **拒绝执行** |
| 存在未跟踪且未忽略的文件 | **拒绝执行** |
| 被 Git 忽略的构建文件 | 不影响执行 |
| `base-ref` 不存在 | 拒绝执行 |
| 当前路径不是 Git 仓库 | 拒绝执行 |
| 包含暂不支持的 Submodule / LFS 依赖 | Preflight 明确报告不支持或环境不可用 |

**为什么拒绝而不是忽略**：首版用独立仓库副本执行；若用户有未提交修改而我们只按 Commit 创建副本，Agent 看到的代码与用户正在编辑的代码可能不一致。与其默默忽略产生误解，不如明确拒绝。**代价**是牺牲部分本地开发便利性，**换来**可复现的任务基线和清晰的行为语义；后续可支持显式工作区快照。

同时规定：Task Interface 不允许自动 Commit、Stash 或清理用户工作区。

---

## 六、任务取消

第一版只支持 `Ctrl+C` 主动取消：

| 场景 | 预期行为 |
| --- | --- |
| 等待 LLM 响应 | 向 Runtime 发送取消请求，停止等待与后续调用 |
| 执行普通工具 | 发出取消信号；能中断的尽快中断 |
| 运行 Shell | Workspace 终止容器内相关执行进程 |
| 已修改部分文件 | 不直接丢弃，保留任务仓库副本及已有修改以供诊断 |
| 取消时事件写入中 | 尽力完成状态记录；不承诺进程被强杀后仍能正常写入 |

**取消控制权属于 Runtime，CLI 只负责传递取消请求**。进程被强制终止时，下次检查旧任务应能识别异常中断，不能假装任务正常 CANCELLED。第一版不支持 `repofix resume`——事件与工作区用于审查诊断，不代表完整断点恢复。

---

## 七、输出协议：不能只输出一句"修复成功"

### RunResult 契约（每次任务结束都产生，尽可能保留诊断信息）

| 字段 | 含义 |
| --- | --- |
| `run_id` | 本次执行 ID |
| `runtime_status` | `COMPLETED` / `FAILED` / `CANCELLED` / `BUDGET_EXCEEDED` |
| `termination_reason` | 具体终止原因 |
| `verification_status` | `PASSED` / `FAILED` / `INCONCLUSIVE` / `NOT_RUN` |
| `modified_files` | 已修改文件列表 |
| `patch_path` / `trace_path` / `test_report_path` | 产物路径（可为空） |
| `model_calls` / `tool_calls` / `token_usage` / `duration_ms` | 用量统计 |

**重要区分**：`RunResult.verification_status` 只表达任务内可见验证；独立 Benchmark 的 `EvaluationOutcome` 不是 Task Interface 的实时任务结果——不能把隐藏测试结果当成 CLI 正常操作的必需输出。

### 输出目录（不写入用户原始 Git 仓库）

```
repofix-data/
└── runs/
    └── <run_id>/
        ├── result.json
        ├── changes.patch
        ├── trajectory.jsonl
        └── test-report.json
```

`repofix-data` 由配置解析到用户数据目录，支持 `--output-dir` 覆盖。**Patch 导出必须包含实际修改的已跟踪文件和新增文件**（不能只执行普通 `git diff` 而忽略未跟踪新文件）；临时测试缓存、忽略文件和敏感内容不写入 Patch。

### CLI 展示规范（格式示例，非实际运行数据）

```
RepoFix Agent
──────────────────────────────
Run ID:       run_001
Repository:   my-project
Status:       COMPLETED
Verification: PASSED

Modified Files: 2
Model Calls:    12
Tool Calls:     26

Patch:          .../changes.patch
Trace:          .../trajectory.jsonl
Test Report:    .../test-report.json
```

失败任务必须展示明确失败原因，不让用户从原始模型对话中自己找。

---

## 八、CLI 退出码

避免把正常终止和业务成功混为一谈：

| 退出码 | 含义 |
| --- | --- |
| `0` | Runtime 正常完成，且规定的可见验证通过 |
| `1` | Runtime 正常完成，但没有足够的可见验证，结果未充分确认 |
| `2` | 输入参数、仓库或配置校验失败 |
| `3` | Runtime / Workspace 不可恢复错误，或任务未能完成规定验证 |
| `4` | 时间、模型调用或工具调用预算耗尽 |
| `130` | 用户主动中断 |

**退出码 0 也不代表隐藏 Benchmark 验收一定成功**——仅说明本次 CLI 任务满足定义好的本地验证要求，避免自动化脚本误判。

---

## 九、模块异常与边界处理

| 异常 | 处理方式 | 是否启动 LLM |
| --- | --- | --- |
| 缺少必填 CLI 参数 / 任务文本为空 / 仓库路径不存在 / 工作区不干净 / Base Commit 无效 / 模型配置或凭据缺失 | 立即拒绝 | 否 |
| Docker 无法启动 | Preflight 失败 | 否 |
| Python 依赖或测试环境不可用 | 记录 Environment Failure，按预检策略失败或标记不可验证 | 通常否 |
| 模型服务调用失败 | Runtime 根据 LLM 错误策略处理 | 已开始执行 |
| 用户取消 | 触发 Runtime 取消与清理逻辑 | 不再新增调用 |
| Patch 导出失败 | 标记交付失败，保留已有运行记录 | 不再新增调用 |

测试环境不可用时，**不默认让模型修改业务代码来"修复环境"**；若明确允许安装缺失依赖，属于另一个权限与环境管理设计问题，不进首版。

---

## 十、模块 1 验收测试

| 测试 ID | 场景 | 预期行为 |
| --- | --- | --- |
| TI-01 | 合法仓库与任务 | 创建 TaskSpec、RunConfig，启动 Runtime |
| TI-02 | 同时提供 `--task` 和 `--task-file` | 参数错误 |
| TI-03 | 空任务 | 拒绝执行 |
| TI-04 | 无效 Git 路径 | 拒绝执行 |
| TI-05 | 工作区有未提交修改 | 不执行，原仓库保持不变 |
| TI-06 | Base Commit 不存在 | 不执行 |
| TI-07 | 模型配置缺失 | 不执行 |
| TI-08 | `Ctrl+C` | 请求取消，不新增任务动作 |
| TI-09 | 正常结束但没有测试 | 结果标记未充分验证，不能声称修复成功 |
| TI-10 | 执行完成后导出 Patch | 包含应交付的修改和新文件 |
| TI-11 | 失败或预算耗尽 | 返回明确终止原因和诊断路径 |
| TI-12 | 同一 TaskSpec 执行两次 | 不同 Run ID、相互隔离的工作区和记录 |

---

## 十一、设计总结与待衔接契约

**本轮确定**：一次性 CLI 唯一入口；TaskSpec / RunConfig / Run ID 分离；单仓库单任务单 Run；仅针对已提交快照执行；不改原仓库、不自动 Commit / Push；默认输出 Patch、Trace、Result 与可用 Test Report；Ctrl+C 取消、无 Resume；**MVP 无审批交互，越权直接拒绝**。

**由后续模块补全**：Agent Runtime（精确状态转换、Finish、预算）；LLM Gateway（模型协议、失败重试）；Tool System（工具 Schema、错误码）；Workspace（副本与 Docker 生命周期）；Verifier（验证失败与无法验证的判定）；State（SQLite Schema、事件写入时机、结果导出）。

**轻量化判断**：本模块实现可以只有一个 CLI 入口、两个配置对象、一份运行结果对象加少量参数检查与输出逻辑——关键不是代码量，而是与 Runtime / Workspace / Verifier 的**职责边界**。既参考了 Codex 的接口分层思想，也体现自有裁剪：保留真实任务执行所需的输入、输出和控制机制，舍弃复杂会话、Web UI、GitHub 集成和暂时用不到的审批系统。
